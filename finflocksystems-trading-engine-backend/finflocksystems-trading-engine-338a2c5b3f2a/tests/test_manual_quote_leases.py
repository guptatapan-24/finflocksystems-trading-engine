import json
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import BrokerAccount
from apps.broker_gateway.client import GatewayClient
from apps.instruments.models import BrokerContract,Instrument
from apps.market_streams.models import (
    InstrumentMarketState,MarketDataConsumerLease,MarketDataSubscription,
)
from apps.market_streams.subscriptions import restore_market_subscriptions,subscription_demand
from apps.portfolios.models import TradingPortfolio
from apps.strategies.models import StrategyInstance
from tests.managed_gateway import bind_gateway_mode


pytestmark=pytest.mark.django_db


def _case(settings,symbol="LEASE"):
    account=BrokerAccount.objects.create(account_id=f"DU-{symbol}")
    portfolio=TradingPortfolio.objects.create(name=symbol,account=account)
    session=bind_gateway_mode(portfolio)
    instrument=Instrument.objects.create(symbol=symbol)
    BrokerContract.objects.create(instrument=instrument,conid=900000+instrument.pk)
    return portfolio,session,instrument


class _Calls(list):
    pass


def _gateway(monkeypatch,generation="generation-1"):
    monkeypatch.setattr(GatewayClient,"health",lambda self:{
        "connected":True,"connection_generation":generation})
    calls=_Calls()
    monkeypatch.setattr(GatewayClient,"subscribe_market_data",
        lambda self,payload,key:calls.append((payload,key)) or {"command_id":len(calls)})
    # The cancel queued ahead of every subscribe (it clears a stale Gateway
    # entry) is recorded apart, so `calls` stays the demand-driven commands.
    calls.preflight_cancels=[]
    def cancel(self,payload,key):
        if payload.get("resubscribe"):
            calls.preflight_cancels.append((payload,key));return {"command_id":0}
        calls.append((payload,key));return {"command_id":len(calls)}
    monkeypatch.setattr(GatewayClient,"cancel_market_data",cancel)
    return calls


def _quote(client,portfolio,instrument,key="ticket-1"):
    return client.post("/api/v1/orders/manual-quote/",json.dumps({
        "portfolio_id":portfolio.pk,"instrument_id":instrument.pk,"lease_key":key,
    }),content_type="application/json")


def test_manual_quote_demand_activates_without_a_strategy(client,settings,monkeypatch):
    portfolio,session,instrument=_case(settings)
    calls=_gateway(monkeypatch)
    result=_quote(client,portfolio,instrument)
    subscription=MarketDataSubscription.objects.get(
        gateway_session=session,instrument=instrument,timeframe="1m")
    assert result.status_code==202
    assert subscription.consumer_count==1 and subscription.state=="SUBSCRIBING"
    assert len(calls)==1
    assert [payload["subscription_key"] for payload,_ in calls.preflight_cancels]==[calls[0][0]["subscription_key"]]
    data=result.json()["data"]
    assert data["subscription_id"]==subscription.pk
    assert data["subscription_state"]=="SUBSCRIBING"
    assert data["gateway_command_id"]==1
    assert data["subscription_error"]==""


def test_strategy_and_manual_demand_are_added(settings,monkeypatch):
    _,session,instrument=_case(settings,"COMBINED")
    lease=MarketDataConsumerLease.objects.create(
        gateway_session=session,instrument=instrument,timeframe="1m",consumer_type="MANUAL",
        lease_key="ticket",expires_at=timezone.now()+timedelta(minutes=2))
    strategy=StrategyInstance(instrument=instrument,timeframe="1m",enabled=True)
    monkeypatch.setattr("apps.market_streams.subscriptions._requirements",lambda *args:([strategy],7))
    instances,history,lease_count=subscription_demand(instrument,"1m",session)
    assert len(instances)==1 and history==7 and lease_count==1
    assert lease.expires_at>timezone.now()


def test_expired_manual_lease_stops_counting(settings):
    _,session,instrument=_case(settings,"EXPIRED")
    MarketDataConsumerLease.objects.create(
        gateway_session=session,instrument=instrument,timeframe="1m",consumer_type="MANUAL",
        lease_key="ticket",expires_at=timezone.now()-timedelta(seconds=1))
    assert subscription_demand(instrument,"1m",session)[2]==0


def test_restore_preserves_active_manual_demand(settings,monkeypatch):
    _,session,instrument=_case(settings,"RESTORE")
    MarketDataConsumerLease.objects.create(
        gateway_session=session,instrument=instrument,timeframe="1m",consumer_type="MANUAL",
        lease_key="ticket",expires_at=timezone.now()+timedelta(minutes=2))
    calls=_gateway(monkeypatch,"reconnected")
    assert restore_market_subscriptions(gateway_session=session)==0
    subscription=MarketDataSubscription.objects.get(gateway_session=session,instrument=instrument)
    assert subscription.consumer_count==1 and len(calls)==1


def test_repeated_quote_refresh_is_idempotent(client,settings,monkeypatch):
    settings.MANUAL_MARKET_DATA_LEASE_SECONDS=120
    portfolio,_,instrument=_case(settings,"REFRESH")
    _gateway(monkeypatch)
    first=_quote(client,portfolio,instrument)
    lease=MarketDataConsumerLease.objects.get();first_expiry=lease.expires_at
    MarketDataConsumerLease.objects.filter(pk=lease.pk).update(
        expires_at=timezone.now()+timedelta(seconds=10))
    second=_quote(client,portfolio,instrument)
    lease.refresh_from_db()
    assert first.status_code==second.status_code==202
    assert MarketDataConsumerLease.objects.count()==1 and lease.expires_at>first_expiry-timedelta(seconds=1)


def test_manual_quote_release_removes_demand(client,settings,monkeypatch):
    portfolio,session,instrument=_case(settings,"RELEASE")
    _gateway(monkeypatch);_quote(client,portfolio,instrument)
    result=client.delete("/api/v1/orders/manual-quote/",json.dumps({
        "portfolio_id":portfolio.pk,"lease_key":"ticket-1"}),content_type="application/json")
    assert result.status_code==200 and result.json()["data"]["released"] is True
    assert not MarketDataConsumerLease.objects.exists()
    assert MarketDataSubscription.objects.get(gateway_session=session,instrument=instrument).consumer_count==0


def test_gateway_sessions_have_isolated_manual_demand(settings):
    portfolio_a,session_a,instrument=_case(settings,"ISOLATED")
    account=BrokerAccount.objects.create(account_id="DU-ISOLATED-B")
    portfolio_b=TradingPortfolio.objects.create(name="isolated-b",account=account)
    session_b=bind_gateway_mode(portfolio_b)
    MarketDataConsumerLease.objects.create(
        gateway_session=session_a,instrument=instrument,timeframe="1m",consumer_type="MANUAL",
        lease_key="same-ticket",expires_at=timezone.now()+timedelta(minutes=2))
    assert subscription_demand(instrument,"1m",session_a)[2]==1
    assert subscription_demand(instrument,"1m",session_b)[2]==0


def test_quote_status_exposes_fresh_canonical_price(client,settings,monkeypatch):
    portfolio,_,instrument=_case(settings,"STATUS")
    _gateway(monkeypatch)
    InstrumentMarketState.objects.create(
        instrument=instrument,status="FRESH",reference_price="296.10",latest_event_at=timezone.now(),
        stale_after_seconds=300,reference_price_provider="IBKR",reference_price_source="ibkr_live")
    result=_quote(client,portfolio,instrument)
    data=result.json()["data"]
    assert result.status_code==200 and data["execution_usable"] is True
    assert data["reference_price"]=="296.10000000" and data["display_status"]=="READY"


def test_recent_ibkr_historical_price_is_not_execution_usable(client,settings,monkeypatch):
    portfolio,_,instrument=_case(settings,"HISTORICAL")
    _gateway(monkeypatch)
    InstrumentMarketState.objects.create(
        instrument=instrument,status="FRESH",reference_price="296.10",latest_event_at=timezone.now(),
        stale_after_seconds=300,reference_price_provider="IBKR",reference_price_source="ibkr_historical")

    result=_quote(client,portfolio,instrument)

    data=result.json()["data"]
    assert result.status_code==202 and data["execution_usable"] is False
    assert data["display_status"]=="WAITING_FOR_LIVE_MARKET_PRICE"
