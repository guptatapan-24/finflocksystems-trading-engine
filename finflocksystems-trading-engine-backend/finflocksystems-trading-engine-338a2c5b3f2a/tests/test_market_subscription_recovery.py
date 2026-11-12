"""Recovery of a market-data subscription the Gateway is not delivering.

The Gateway answers a subscribe for a key it already holds with "ACTIVE,
reused" and sends nothing new: no warm-up history, no switch to the new
provider generation. After an IBKR reconnect its held entries are dead
streams. These tests pin the backend-side recovery.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.broker_gateway.client import GatewayClient, GatewayError
from apps.broker_gateway.sync import process_snapshot
from apps.market_streams.models import MarketDataConsumerLease, MarketDataSubscription
from apps.market_streams.subscriptions import reconcile_market_subscription, restore_market_subscriptions
from tests.test_manual_quote_leases import _case

pytestmark=pytest.mark.django_db


def _gateway(monkeypatch, generation="generation-1", cancel_error=None):
    commands=[]
    monkeypatch.setattr(GatewayClient,"health",lambda self:{"connected":True,"connection_generation":generation})

    def subscribe(self,payload,key):
        commands.append(("subscribe",payload,key));return {"command_id":len(commands)}

    def cancel(self,payload,key):
        commands.append(("cancel",payload,key))
        if cancel_error:raise cancel_error
        return {"command_id":len(commands)}

    monkeypatch.setattr(GatewayClient,"subscribe_market_data",subscribe)
    monkeypatch.setattr(GatewayClient,"cancel_market_data",cancel)
    return commands


def _demand(session, instrument):
    MarketDataConsumerLease.objects.create(
        gateway_session=session,instrument=instrument,timeframe="1m",consumer_type="MANUAL",
        lease_key="ticket",expires_at=timezone.now()+timedelta(minutes=10))


def test_every_subscribe_is_preceded_by_a_cancel_of_the_same_key(settings, monkeypatch):
    _,session,instrument=_case(settings,"PREFLIGHT")
    _demand(session,instrument)
    commands=_gateway(monkeypatch)

    subscription=reconcile_market_subscription(instrument,"1m",gateway_session=session)

    assert [kind for kind,_,_ in commands]==["cancel","subscribe"]
    cancel_payload,subscribe_payload=commands[0][1],commands[1][1]
    assert cancel_payload=={"subscription_key":subscribe_payload["subscription_key"],"resubscribe":True}
    assert subscription.state=="SUBSCRIBING" and subscription.gateway_command_id==2


def test_a_failed_preflight_cancel_does_not_block_the_subscribe(settings, monkeypatch):
    _,session,instrument=_case(settings,"CANCELFAIL")
    _demand(session,instrument)
    commands=_gateway(monkeypatch,cancel_error=GatewayError("gateway busy"))

    subscription=reconcile_market_subscription(instrument,"1m",gateway_session=session)

    assert [kind for kind,_,_ in commands]==["cancel","subscribe"]
    assert subscription.state=="SUBSCRIBING" and subscription.active_provider=="IBKR"


def test_a_subscription_stalled_in_subscribing_is_requested_again(settings, monkeypatch):
    _,session,instrument=_case(settings,"STALLED")
    _demand(session,instrument)
    commands=_gateway(monkeypatch)
    first=reconcile_market_subscription(instrument,"1m",gateway_session=session)
    commands.clear()

    # Within the stall window: still waiting, nothing is re-sent.
    assert restore_market_subscriptions(gateway_session=session)==0
    assert commands==[]

    MarketDataSubscription.objects.filter(pk=first.pk).update(
        requested_at=timezone.now()-timedelta(seconds=settings.MARKET_SUBSCRIPTION_STALL_SECONDS+1))
    assert restore_market_subscriptions(gateway_session=session)==1

    again=MarketDataSubscription.objects.get(pk=first.pk)
    assert [kind for kind,_,_ in commands]==["cancel","subscribe"]
    assert again.state=="SUBSCRIBING" and again.provider_generation!=first.provider_generation


def test_a_subscription_that_received_events_is_not_treated_as_stalled(settings, monkeypatch):
    _,session,instrument=_case(settings,"FLOWING")
    _demand(session,instrument)
    commands=_gateway(monkeypatch)
    first=reconcile_market_subscription(instrument,"1m",gateway_session=session)
    commands.clear()
    requested=timezone.now()-timedelta(seconds=settings.MARKET_SUBSCRIPTION_STALL_SECONDS+1)
    MarketDataSubscription.objects.filter(pk=first.pk).update(
        requested_at=requested,last_event_at=requested+timedelta(seconds=5))

    assert restore_market_subscriptions(gateway_session=session)==0
    assert commands==[]


def test_an_errored_subscription_is_retried_no_faster_than_the_retry_interval(settings, monkeypatch):
    _,session,instrument=_case(settings,"ERRORED")
    _demand(session,instrument)
    commands=_gateway(monkeypatch)
    first=reconcile_market_subscription(instrument,"1m",gateway_session=session)
    MarketDataSubscription.objects.filter(pk=first.pk).update(state="ERROR",active_provider="NONE")
    commands.clear()

    assert restore_market_subscriptions(gateway_session=session)==0
    assert commands==[]

    MarketDataSubscription.objects.filter(pk=first.pk).update(
        requested_at=timezone.now()-timedelta(seconds=settings.MARKET_SUBSCRIPTION_RETRY_SECONDS+1))
    restore_market_subscriptions(gateway_session=session)
    assert [kind for kind,_,_ in commands]==["cancel","subscribe"]
    assert MarketDataSubscription.objects.get(pk=first.pk).state=="SUBSCRIBING"


def test_a_reconnect_resubscribes_even_inside_the_retry_interval(settings, monkeypatch):
    _,session,instrument=_case(settings,"RECONNECT")
    _demand(session,instrument)
    commands=_gateway(monkeypatch,"generation-1")
    first=reconcile_market_subscription(instrument,"1m",gateway_session=session)
    MarketDataSubscription.objects.filter(pk=first.pk).update(state="ERROR",active_provider="NONE")
    commands=_gateway(monkeypatch,"generation-2")

    assert restore_market_subscriptions(gateway_session=session)==1
    assert [kind for kind,_,_ in commands]==["cancel","subscribe"]


def test_a_failed_preflight_cancel_command_does_not_fail_the_subscription(settings, monkeypatch):
    import apps.market_data.fallback as fallback

    _,session,instrument=_case(settings,"CANCELCMD")
    _demand(session,instrument)
    _gateway(monkeypatch)
    subscription=reconcile_market_subscription(instrument,"1m",gateway_session=session)
    failed=[]
    monkeypatch.setattr(fallback,"handle_ibkr_failure",lambda sub,*args,**kwargs:failed.append(sub.pk))

    process_snapshot({"id":1,"event_type":"command.failed","payload":{
        "command_id":7,"command_type":"CANCEL_MARKET_DATA","error":"cancelRealTimeBars failed",
        "payload":{"subscription_key":f"{session.pk}:{instrument.pk}:1m","resubscribe":True}}},session)

    assert failed==[]
    current=MarketDataSubscription.objects.get(pk=subscription.pk)
    assert current.state=="SUBSCRIBING" and current.active_provider=="IBKR"
