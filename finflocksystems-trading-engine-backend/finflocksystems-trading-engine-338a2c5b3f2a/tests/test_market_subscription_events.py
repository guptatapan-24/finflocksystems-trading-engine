import pytest
import responses
import uuid
from datetime import timedelta
from django.utils import timezone
from apps.audit.models import OutboxEvent
from apps.broker_gateway.sync import process_snapshot
from apps.instruments.models import BrokerContract,Instrument
from apps.market_streams.models import MarketDataSubscription
from apps.event_bus.models import StreamHealthMetric
from apps.market_streams.tasks import check_warmup_timeouts
from apps.accounts.models import BrokerAccount
from apps.portfolios.models import TradingPortfolio
from apps.strategies.framework import create_instance
from tests.managed_gateway import bind_gateway_mode
from tests.strategy_activation import activate_strategy

pytestmark=pytest.mark.django_db


def test_gateway_raw_market_event_enters_transactional_outbox():
    instrument=Instrument.objects.create(symbol="RAW",exchange="SMART",currency="USD")
    BrokerContract.objects.create(instrument=instrument,conid=777)
    subscription=MarketDataSubscription.objects.create(
        instrument=instrument,conid=777,timeframe="1m",consumer_count=1,
        state="SUBSCRIBING",active_provider="IBKR",
    )
    payload={"source_event_id":"777:1m:2026-07-13T00:00:00+00:00","subscription_key":f"{instrument.pk}:1m",
        "instrument_id":instrument.pk,"conid":777,"symbol":"RAW","timeframe":"1m","event_time":"2026-07-13T00:01:00+00:00",
        "provider":"IBKR","provider_generation":str(subscription.provider_generation),
        "processing_mode":"LIVE","source":"ibkr_live"}
    process_snapshot({"event_type":"market.raw","payload":payload});process_snapshot({"event_type":"market.raw","payload":payload})
    subscription.refresh_from_db()
    assert subscription.state=="ACTIVE" and subscription.last_event_at is not None
    assert OutboxEvent.objects.filter(topic="market.raw.v1").count()==1


def test_legacy_ibkr_bootstrap_bar_is_published_as_warmup():
    instrument=Instrument.objects.create(symbol="BOOT",exchange="SMART",currency="USD")
    BrokerContract.objects.create(instrument=instrument,conid=778)
    subscription=MarketDataSubscription.objects.create(
        instrument=instrument,conid=778,timeframe="1m",consumer_count=1,
        state="SUBSCRIBING",active_provider="IBKR",
    )
    payload={
        "source_event_id":"778:1m:2026-07-13T00:00:00+00:00",
        "subscription_key":f"{instrument.pk}:1m","instrument_id":instrument.pk,
        "conid":778,"symbol":"BOOT","timeframe":"1m",
        "event_time":"2026-07-13T00:00:00+00:00",
        "window_start":"2026-07-13T00:00:00+00:00",
        "window_end":"2026-07-13T00:01:00+00:00","event_kind":"BAR",
        "open":"100","high":"101","low":"99","close":"100.5","volume":"10",
        "is_final":True,"provider":"IBKR","source":"ibkr_historical",
        "provider_generation":str(subscription.provider_generation),
    }

    process_snapshot({"event_type":"market.raw","payload":payload})

    event=OutboxEvent.objects.get(topic="market.raw.v1")
    assert event.payload["processing_mode"]=="WARMUP"


def test_explicit_live_ibkr_event_remains_live():
    instrument=Instrument.objects.create(symbol="LIVE",exchange="SMART",currency="USD")
    BrokerContract.objects.create(instrument=instrument,conid=779)
    subscription=MarketDataSubscription.objects.create(
        instrument=instrument,conid=779,timeframe="1m",consumer_count=1,
        state="SUBSCRIBING",active_provider="IBKR",
    )
    payload={
        "source_event_id":"779:live","subscription_key":f"{instrument.pk}:1m",
        "instrument_id":instrument.pk,"conid":779,"symbol":"LIVE","timeframe":"5s",
        "event_time":"2026-07-13T00:00:05+00:00","window_start":"2026-07-13T00:00:00+00:00",
        "window_end":"2026-07-13T00:00:05+00:00","event_kind":"BAR",
        "open":"100","high":"101","low":"99","close":"100.5","volume":"10",
        "is_final":True,"provider":"IBKR","source":"ibkr_live","processing_mode":"LIVE",
        "provider_generation":str(subscription.provider_generation),
    }

    process_snapshot({"event_type":"market.raw","payload":payload})

    event=OutboxEvent.objects.get(topic="market.raw.v1")
    assert event.payload["processing_mode"]=="LIVE"


def test_new_provider_generation_republishes_same_bootstrap_window():
    instrument=Instrument.objects.create(symbol="EPOCH",exchange="SMART",currency="USD")
    BrokerContract.objects.create(instrument=instrument,conid=780)
    subscription=MarketDataSubscription.objects.create(
        instrument=instrument,conid=780,timeframe="1m",consumer_count=1,
        state="SUBSCRIBING",active_provider="IBKR",
    )
    payload={
        "source_event_id":"780:bootstrap","subscription_key":f"{instrument.pk}:1m",
        "instrument_id":instrument.pk,"conid":780,"symbol":"EPOCH","timeframe":"1m",
        "event_time":"2026-07-13T00:00:00+00:00","window_start":"2026-07-13T00:00:00+00:00",
        "window_end":"2026-07-13T00:01:00+00:00","event_kind":"BAR",
        "open":"100","high":"101","low":"99","close":"100.5","volume":"10",
        "is_final":True,"provider":"IBKR","source":"ibkr_historical",
        "provider_generation":str(subscription.provider_generation),
    }
    process_snapshot({"event_type":"market.raw","payload":payload})
    subscription.provider_generation=uuid.uuid4()
    subscription.save(update_fields=["provider_generation","updated_at"])
    payload={**payload,"provider_generation":str(subscription.provider_generation)}

    process_snapshot({"event_type":"market.raw","payload":payload})

    assert OutboxEvent.objects.filter(topic="market.raw.v1").count()==2


def test_async_ibkr_market_error_blocks_strategy_with_exact_reason():
    account=BrokerAccount.objects.create(account_id="DU-PERMISSION");portfolio=TradingPortfolio.objects.create(name="Permission",account=account)
    bind_gateway_mode(portfolio)
    instrument=Instrument.objects.create(symbol="PERM",exchange="SMART",currency="USD");BrokerContract.objects.create(instrument=instrument,conid=780)
    instance,_=create_instance(name="Permission failure",definition_key="FIXED_WEIGHT_REBALANCE",portfolio=portfolio,
        instrument_id=instrument.pk,timeframe="1m",parameters={"direction":"LONG"},target_configuration={"target_weight":"0.01"},qualify=False)
    instance,_,subscription=activate_strategy(instance,ready=False)
    process_snapshot({"event_type":"market.error","payload":{"subscription_key":f"{instrument.pk}:1m",
        "provider_generation":str(subscription.provider_generation),
        "error_code":"354","error_message":"Requested market data is not subscribed"}},gateway_session=portfolio.gateway_session)
    instance.refresh_from_db();subscription.refresh_from_db()
    assert subscription.state=="ERROR" and subscription.last_error=="IBKR error 354: Requested market data is not subscribed"
    assert instance.state=="BLOCKED" and instance.block_reason==subscription.last_error
    process_snapshot({"event_type":"command.subscribe_market_data.completed","payload":{
        "subscription_key":f"{instrument.pk}:1m",
        "provider_generation":str(subscription.provider_generation),
    }},gateway_session=portfolio.gateway_session)
    subscription.refresh_from_db()
    assert subscription.state=="ACTIVE" and subscription.last_error=="IBKR error 354: Requested market data is not subscribed"


def test_stalled_warmup_becomes_visibly_blocked(settings):
    settings.WARMUP_TIMEOUT_SECONDS=30
    account=BrokerAccount.objects.create(account_id="DU-WARMUP");portfolio=TradingPortfolio.objects.create(name="Warmup",account=account)
    bind_gateway_mode(portfolio)
    instrument=Instrument.objects.create(symbol="STALL",exchange="SMART",currency="USD");BrokerContract.objects.create(instrument=instrument,conid=778)
    instance,_=create_instance(name="Stalled",definition_key="FIXED_WEIGHT_REBALANCE",portfolio=portfolio,instrument_id=instrument.pk,
        timeframe="1m",parameters={"direction":"LONG"},target_configuration={"target_weight":"0.01"},qualify=False)
    instance,_,subscription=activate_strategy(instance,ready=False);old=timezone.now()-timedelta(minutes=2)
    instance.warmup_started_at=old;instance.warmup_last_progress_at=old;instance.save(update_fields=["warmup_started_at","warmup_last_progress_at"])
    subscription.state="ERROR";subscription.last_error="IBKR error 354: Not subscribed"
    subscription.save(update_fields=["state","last_error","updated_at"])
    assert check_warmup_timeouts()==1
    instance.refresh_from_db();assert instance.state=="BLOCKED" and "IBKR error 354" in instance.block_reason


@responses.activate
def test_stream_health_is_not_green_when_consumer_heartbeat_is_stale(client,settings):
    settings.KAFKA_ENABLED=True;settings.MARKET_CONSUMER_HEARTBEAT_STALE_SECONDS=30
    responses.get(settings.FLINK_REST_URL+"/jobs/overview",json={"jobs":[{"id":"normalizer","state":"RUNNING"}]})
    StreamHealthMetric.objects.create(component="kafka",metric="connectivity",status="HEALTHY",value={"topics":8})
    StreamHealthMetric.objects.create(component="gateway",metric="connectivity",status="HEALTHY",
        value={"connected":True,"reconciled":True})
    heartbeat=StreamHealthMetric.objects.create(component="backend-market-consumer",metric="heartbeat",status="HEALTHY",value={})
    StreamHealthMetric.objects.create(component="backend-market-consumer",metric="topic_lag",status="HEALTHY",value={"total":0})
    StreamHealthMetric.objects.filter(pk=heartbeat.pk).update(observed_at=timezone.now()-timedelta(minutes=2))
    data=client.get("/api/v1/streaming/health/").json()["data"]
    assert data["consumer"]["status"]=="STALE" and data["data_path_status"]=="DEGRADED"
    assert "Backend market consumer heartbeat is not healthy" in data["data_path_reasons"]


def test_strategy_api_exposes_each_persisted_stream_stage(client):
    account=BrokerAccount.objects.create(account_id="DU-HEALTH");portfolio=TradingPortfolio.objects.create(name="Health",account=account)
    bind_gateway_mode(portfolio)
    instrument=Instrument.objects.create(symbol="PATH",exchange="SMART",currency="USD");BrokerContract.objects.create(instrument=instrument,conid=779)
    instance,_=create_instance(name="Path health",definition_key="FIXED_WEIGHT_REBALANCE",portfolio=portfolio,
        instrument_id=instrument.pk,timeframe="1m",parameters={"direction":"LONG"},target_configuration={"target_weight":"0.01"},qualify=False)
    instance,_,subscription=activate_strategy(instance)
    subscription.last_event_at=timezone.now()
    subscription.save(update_fields=["last_event_at","updated_at"])
    now=timezone.now()
    from apps.market_streams.models import MarketBar
    from apps.strategies.framework import evaluate_instance
    MarketBar.objects.create(instrument=instrument,bar_id="path-final",interval="1m",window_start=now-timedelta(minutes=1),
        window_end=now,open=1,high=1,low=1,close=1,volume=1,is_final=True,produced_at=now)
    evaluate_instance(instance,bar={"bar_id":"path-final","close":"1","is_final":True},indicators={},event_id="path-final:1")
    stream=client.get(f"/api/v1/strategy-instances/{instance.pk}/").json()["data"]["streaming"]
    assert stream["subscription_state"]=="ACTIVE" and stream["conid"]==779
    assert stream["last_raw_event"] and stream["last_canonical_event"] and stream["last_final_bar"]
    assert stream["last_strategy_run"] and stream["status"]=="HEALTHY"
