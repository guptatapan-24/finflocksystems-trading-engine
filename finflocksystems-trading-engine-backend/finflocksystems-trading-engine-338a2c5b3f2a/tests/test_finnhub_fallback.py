import json
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

import pytest
import responses
from django.utils import timezone

from apps.accounts.models import BrokerAccount
from apps.audit.models import OutboxEvent
from apps.broker_gateway.sync import process_snapshot
from apps.instruments.models import BrokerContract, Instrument, InstrumentProviderMapping
from apps.market_data.aggregation import FiveSecondTradeAggregator
from apps.market_data.fallback import (
    begin_primary_probe, classify_ibkr_error, failover_subscription, mark_subscription_unusable,
    monitor_subscriptions, publish_provider_event,
)
from apps.market_data.mapping import fallback_eligibility, verify_finnhub_mapping
from apps.market_data.providers.base import ProviderCandle, ProviderError, ProviderErrorCode, ProviderQuote
from apps.market_data.providers.finnhub import FinnhubClient, FinnhubError
from apps.market_data.realtime import FinnhubRealtimeWorker, FinnhubWebSocketTransport, reconnect_delay
from apps.market_streams.models import InstrumentMarketState, MarketDataProviderTransition, MarketDataSubscription
from apps.portfolios.models import TradingPortfolio
from apps.strategies.framework import create_instance
from apps.strategies.models import StrategyWarmupReadiness
from tests.managed_gateway import bind_gateway_mode


pytestmark = pytest.mark.django_db


def canonical(symbol="AAPL", *, asset_class="STK", conid=265598, primary_exchange="NASDAQ"):
    instrument = Instrument.objects.create(
        symbol=symbol, asset_class=asset_class, exchange="SMART", primary_exchange=primary_exchange, currency="USD",
    )
    contract = BrokerContract.objects.create(
        instrument=instrument, conid=conid, primary_exchange=primary_exchange, local_symbol=symbol,
        qualified_at=timezone.now(),
    )
    return instrument, contract


def verified_mapping(instrument, symbol=None):
    return InstrumentProviderMapping.objects.create(
        instrument=instrument, provider="FINNHUB", provider_symbol=symbol or instrument.symbol,
        exchange_mic="XNAS", provider_exchange="NASDAQ NMS - GLOBAL MARKET", currency="USD",
        status="VERIFIED", verification_method="AUTOMATIC", verified_at=timezone.now(),
    )


def subscription(instrument, conid, timeframe="1m", **kwargs):
    return MarketDataSubscription.objects.create(
        instrument=instrument, conid=conid, timeframe=timeframe, consumer_count=1,
        required_history_bars=kwargs.pop("required_history_bars", 2), state=kwargs.pop("state", "ACTIVE"), **kwargs,
    )


class MappingClient:
    def __init__(self, candidates=None, profile=None):
        self.candidates = candidates or [{"provider_symbol": "AAPL", "display_symbol": "AAPL",
            "description": "Apple", "type": "Common Stock", "currency": "USD", "mic": "XNAS",
            "figi": "BBG000B9XRY4", "isin": "US0378331005", "raw": {"symbol": "AAPL"}}]
        self.profile_value = profile or {"provider_symbol": "AAPL", "currency": "USD",
            "provider_exchange": "NASDAQ NMS - GLOBAL MARKET", "country": "US", "isin": "US0378331005",
            "figi": "", "raw": {"ticker": "AAPL"}}

    def search_symbols(self, query):
        return self.candidates

    def profile(self, symbol):
        return self.profile_value


def test_mapping_requires_exact_currency_and_exchange_evidence_and_deduplicates_identity():
    instrument, _ = canonical()
    mapping = verify_finnhub_mapping(instrument, client=MappingClient())
    assert mapping.status == "VERIFIED" and mapping.provider_symbol == "AAPL" and mapping.exchange_mic == "XNAS"

    mapping.status = "PENDING"
    mapping.save(update_fields=["status"])
    duplicate = MappingClient(candidates=MappingClient().candidates * 2)
    mapping = verify_finnhub_mapping(instrument, client=duplicate)
    assert mapping.status == "VERIFIED" and mapping.provider_symbol == "AAPL"

    mismatch = MappingClient(profile={**MappingClient().profile_value, "currency": "EUR"})
    mapping = verify_finnhub_mapping(instrument, client=mismatch)
    assert mapping.status == "UNSUPPORTED" and "currenc" in mapping.last_error.lower()


def test_mapping_recognizes_full_nyse_provider_exchange_name_without_mic():
    instrument,_=canonical("JNJ",conid=8719,primary_exchange="NYSE")
    client=MappingClient(
        candidates=[{"provider_symbol":"JNJ","display_symbol":"JNJ","description":"Johnson & Johnson",
                     "type":"Common Stock","currency":"","mic":"","figi":"","isin":""}],
        profile={"provider_symbol":"JNJ","currency":"USD",
                 "provider_exchange":"NEW YORK STOCK EXCHANGE, INC.","country":"US"},
    )
    mapping=verify_finnhub_mapping(instrument,client=client)
    assert mapping.status=="VERIFIED" and mapping.provider_exchange=="NEW YORK STOCK EXCHANGE, INC."


def test_mapping_recognizes_nse_mic_for_indian_stock():
    instrument,_=canonical("KISSHT",conid=54321,primary_exchange="NSE")
    instrument.currency="INR";instrument.save(update_fields=["currency"])
    client=MappingClient(
        candidates=[{"provider_symbol":"KISSHT","description":"Kissht","type":"Common Stock",
                 "currency":"INR","mic":"XNSE","figi":"","isin":""}],
        profile={"provider_symbol":"KISSHT","currency":"INR","provider_exchange":"NSE","country":"IN"})
    mapping=verify_finnhub_mapping(instrument,client=client)
    assert mapping.status=="VERIFIED" and mapping.exchange_mic=="XNSE"


def test_non_stock_and_unverified_contracts_fail_closed(settings):
    settings.MARKET_DATA_FALLBACK_ENABLED = True
    settings.FINNHUB_LIVE_FALLBACK_ENABLED = True
    instrument, contract = canonical("EURUSD", asset_class="CASH", conid=42)
    item = subscription(instrument, contract.conid)
    eligible, reason, mapping = fallback_eligibility(item)
    assert eligible is False and reason == ProviderErrorCode.UNSUPPORTED_INSTRUMENT and mapping is None


@pytest.mark.parametrize("timeframe,resolution,seconds", [
    ("1m", "1", 60), ("5m", "5", 300), ("15m", "15", 900), ("1h", "60", 3600), ("1d", "D", 86400),
])
@responses.activate
def test_finnhub_historical_normalization_for_supported_timeframes(settings, timeframe, resolution, seconds):
    settings.FINNHUB_API_KEY = "test-key"
    responses.get(settings.FINNHUB_BASE_URL + "/stock/candle", json={
        "s": "ok", "t": [1_700_000_000], "o": [10], "h": [12], "l": [9], "c": [11], "v": [100],
    })
    rows = FinnhubClient().historical_candles(
        "AAPL", timeframe, datetime(2023, 11, 1, tzinfo=dt_timezone.utc),
        datetime(2023, 11, 2, tzinfo=dt_timezone.utc),
    )
    assert len(rows) == 1 and (rows[0].window_end - rows[0].window_start).total_seconds() == seconds
    assert rows[0].open == 10 and rows[0].close == 11 and rows[0].volume == 100
    assert f"resolution={resolution}" in responses.calls[0].request.url
    assert "test-key" not in responses.calls[0].request.url


@responses.activate
def test_finnhub_intraday_history_paginates_month_limits_and_deduplicates(settings):
    settings.FINNHUB_API_KEY = "test-key"
    payload={"s":"ok","t":[1_700_000_000],"o":[10],"h":[12],"l":[9],"c":[11],"v":[100]}
    responses.get(settings.FINNHUB_BASE_URL+"/stock/candle",json=payload)
    responses.get(settings.FINNHUB_BASE_URL+"/stock/candle",json=payload)
    rows=FinnhubClient().historical_candles(
        "AAPL","1m",datetime(2023,1,1,tzinfo=dt_timezone.utc),datetime(2023,2,15,tzinfo=dt_timezone.utc))
    assert len(responses.calls)==2 and len(rows)==1


@responses.activate
def test_finnhub_quote_is_normalized_with_provider_timestamp(settings):
    settings.FINNHUB_API_KEY = "test-key"
    responses.get(settings.FINNHUB_BASE_URL + "/quote", json={"c": 123.45, "o": 120, "h": 124, "l": 119,
                                                                        "pc": 121, "t": 1_700_000_000})
    quote = FinnhubClient().quote("AAPL")
    assert quote.price == Decimal("123.45") and quote.event_time.tzinfo is not None


@responses.activate
def test_malformed_finnhub_candles_raise_normalized_provider_error(settings):
    settings.FINNHUB_API_KEY="test-key"
    responses.get(settings.FINNHUB_BASE_URL+"/stock/candle",json={
        "s":"ok","t":[1_700_000_000],"o":[10],"h":[9],"l":[8],"c":[11],"v":[100]})
    with pytest.raises(FinnhubError) as raised:
        FinnhubClient().historical_candles("AAPL","1m",1_699_999_000,1_700_001_000)
    assert raised.value.code=="FINNHUB_INVALID_RESPONSE"


class ProviderStub:
    def __init__(self, candles=None, quote=None, error=None):
        self.candles = candles or []
        self.quote_value = quote
        self.error = error

    def capabilities(self, asset_class, timeframe):
        return asset_class == "STK" and timeframe in FinnhubClient.RESOLUTIONS

    def historical_candles(self, symbol, timeframe, start, end):
        if self.error:
            raise self.error
        return self.candles

    def quote(self, symbol):
        if self.error:
            raise self.error
        if self.quote_value is None:
            raise ProviderError("no quote", code=ProviderErrorCode.FINNHUB_NO_DATA, provider="FINNHUB")
        return self.quote_value


def two_candles(now=None):
    now = (now or timezone.now()).replace(second=0, microsecond=0)
    return [ProviderCandle(now - timedelta(minutes=2), now - timedelta(minutes=1), Decimal("10"), Decimal("12"),
                           Decimal("9"), Decimal("11"), Decimal("100")),
            ProviderCandle(now - timedelta(minutes=1), now, Decimal("11"), Decimal("13"), Decimal("10"),
                           Decimal("12"), Decimal("150"))]


def enable_fallback(settings):
    settings.MARKET_DATA_FALLBACK_ENABLED = True
    settings.FINNHUB_HISTORICAL_FALLBACK_ENABLED = True
    settings.FINNHUB_LIVE_FALLBACK_ENABLED = True
    settings.FINNHUB_AUTO_FAILBACK_ENABLED = True


def test_historical_failover_uses_canonical_outbox_and_reference_price_provenance(settings):
    enable_fallback(settings)
    instrument, contract = canonical()
    mapping = verified_mapping(instrument)
    item = subscription(instrument, contract.conid, required_history_bars=2)
    quote = ProviderQuote(Decimal("12.5"), timezone.now())
    result = failover_subscription(item.pk, ProviderErrorCode.IBKR_ENTITLEMENT, historical=True,
                                   client=ProviderStub(two_candles(), quote))
    result.refresh_from_db()
    assert result.active_provider == "FINNHUB" and result.fallback_state == "FALLBACK" and result.state == "DEGRADED"
    events = list(OutboxEvent.objects.filter(topic="market.raw.v1").order_by("created_at"))
    assert len(events) == 2
    assert all(event.payload["instrument_id"] == instrument.pk and event.payload["conid"] == contract.conid for event in events)
    assert all(event.payload["provider_symbol"] == mapping.provider_symbol for event in events)
    assert all(event.payload["provider_generation"] == str(result.provider_generation) for event in events)
    state = InstrumentMarketState.objects.get(instrument=instrument)
    assert state.reference_price == Decimal("12.5") and state.reference_price_provider == "FINNHUB"
    assert state.reference_price_source == "finnhub_quote"
    assert list(MarketDataProviderTransition.objects.values_list("new_provider", flat=True)) == ["NONE", "FINNHUB"]


def bar_payload(item, generation, start, *, provider="IBKR", source="ibkr_live", close="10"):
    end = start + timedelta(seconds=5)
    return {"source_event_id": f"{provider}:{item.pk}:{start.isoformat()}",
        "subscription_key": f"{item.instrument_id}:{item.timeframe}", "instrument_id": item.instrument_id,
        "conid": item.conid, "symbol": item.instrument.symbol, "exchange": item.instrument.exchange,
        "currency": item.instrument.currency, "event_kind": "BAR", "timeframe": "5s",
        "event_time": start.isoformat(), "window_start": start.isoformat(), "window_end": end.isoformat(),
        "open": close, "high": close, "low": close, "close": close, "volume": "5", "is_final": True,
        "source": source, "provider": provider, "provider_generation": str(generation)}


def test_generation_gating_and_canonical_window_idempotency(settings):
    enable_fallback(settings)
    instrument, contract = canonical()
    item = subscription(instrument, contract.conid)
    current = item.provider_generation
    start = timezone.now().replace(microsecond=0, second=0)
    assert publish_provider_event(bar_payload(item, uuid.uuid4(), start))["reason"] == "GENERATION_MISMATCH"
    assert publish_provider_event({k: v for k, v in bar_payload(item, current, start).items()
                                   if k != "provider_generation"})["reason"] == "GENERATION_MISSING"
    assert publish_provider_event(bar_payload(item, current, start))["created"] is True

    fallback_generation = uuid.uuid4()
    MarketDataSubscription.objects.filter(pk=item.pk).update(active_provider="FINNHUB",
        provider_generation=fallback_generation, fallback_state="FALLBACK")
    item.refresh_from_db()
    duplicate = publish_provider_event(bar_payload(item, fallback_generation, start,
        provider="FINNHUB", source="finnhub_live", close="99"))
    assert duplicate["accepted"] is True and duplicate["created"] is False
    assert OutboxEvent.objects.filter(topic="market.raw.v1").count() == 1
    assert OutboxEvent.objects.get(topic="market.raw.v1").payload["close"] == "10"


def test_ibkr_delayed_metadata_is_repaired_at_backend_ingress(settings):
    enable_fallback(settings)
    instrument, contract = canonical()
    item = subscription(instrument, contract.conid)
    event_time = timezone.now().replace(microsecond=0, second=0) - timedelta(minutes=15)
    received_at = timezone.now().replace(microsecond=0)
    payload = bar_payload(
        item,
        item.provider_generation,
        event_time,
        source="ibkr_delayed",
    )
    payload["market_data_type"] = "LIVE"
    payload["received_at"] = event_time.isoformat()

    result = publish_provider_event(payload, received_at=received_at)

    assert result["accepted"] is True
    stored = OutboxEvent.objects.get(topic="market.raw.v1").payload
    assert stored["source"] == "ibkr_delayed"
    assert stored["market_data_type"] == "DELAYED"
    assert stored["event_time"] == event_time.isoformat()
    assert stored["received_at"] == received_at.isoformat()


def test_confirmed_failback_promotes_probe_epoch_and_rejects_delayed_finnhub(settings):
    enable_fallback(settings)
    settings.PRIMARY_RECOVERY_CONFIRMATION_EVENTS = 3
    instrument, contract = canonical()
    fallback_generation, probe_generation = uuid.uuid4(), uuid.uuid4()
    start = timezone.now().replace(microsecond=0, second=(timezone.now().second // 5) * 5)
    item = subscription(instrument, contract.conid, active_provider="FINNHUB", fallback_state="RECOVERING",
        provider_generation=fallback_generation, primary_probe_generation=probe_generation,
        primary_probe_started_at=timezone.now(), last_published_window_end=start)
    for index in range(2):
        result = publish_provider_event(bar_payload(item, probe_generation, start + timedelta(seconds=5 * index)))
        assert result == {"accepted": False, "reason": "PRIMARY_PROBE"}
    result = publish_provider_event(bar_payload(item, probe_generation, start + timedelta(seconds=10)))
    assert result["accepted"] is True
    item.refresh_from_db()
    assert item.active_provider == "IBKR" and item.provider_generation == probe_generation
    delayed = publish_provider_event(bar_payload(item, fallback_generation, start + timedelta(seconds=15),
        provider="FINNHUB", source="finnhub_live"))
    assert delayed["reason"] == "PROVIDER_MISMATCH"
    assert MarketDataProviderTransition.objects.filter(new_provider="IBKR", reason="IBKR_RECOVERED").exists()


def test_broker_error_triggers_historical_fallback_instead_of_blocking(settings, monkeypatch):
    enable_fallback(settings)
    instrument, contract = canonical()
    verified_mapping(instrument)
    item = subscription(instrument, contract.conid, required_history_bars=2)
    provider = ProviderStub(two_candles(), ProviderQuote(Decimal("12"), timezone.now()))
    monkeypatch.setattr("apps.market_data.fallback.FinnhubClient", lambda: provider)
    process_snapshot({"event_type": "command.failed", "payload": {"command_type": "SUBSCRIBE_MARKET_DATA",
        "payload": {"subscription_key": f"{instrument.pk}:1m",
            "provider_generation": str(item.provider_generation)},
        "error": "IBKR error 354: Requested market data is not subscribed"}})
    item.refresh_from_db()
    assert item.active_provider == "FINNHUB" and item.state == "DEGRADED"
    assert OutboxEvent.objects.filter(topic="market.raw.v1").count() == 2


def test_finnhub_failure_marks_both_providers_unusable(settings):
    enable_fallback(settings)
    instrument, contract = canonical()
    verified_mapping(instrument)
    item = subscription(instrument, contract.conid, required_history_bars=0)
    error = ProviderError("down", code=ProviderErrorCode.FINNHUB_UNAVAILABLE, provider="FINNHUB")
    result = failover_subscription(item.pk, ProviderErrorCode.IBKR_DISCONNECTED, client=ProviderStub(error=error))
    assert result.active_provider == "NONE" and result.fallback_state == "FAILED" and result.state == "ERROR"


def test_provider_recovery_restores_audited_active_strategy_state(settings):
    enable_fallback(settings)
    instrument, contract = canonical()
    item = subscription(instrument, contract.conid)
    account = BrokerAccount.objects.create(account_id="DU-RECOVERY")
    portfolio = TradingPortfolio.objects.create(name="Provider recovery", account=account)
    bind_gateway_mode(portfolio)
    item.gateway_session = portfolio.gateway_session
    item.save(update_fields=["gateway_session", "updated_at"])
    instance, _ = create_instance(
        name="Audited active recovery",
        definition_key="FIXED_WEIGHT_REBALANCE",
        portfolio=portfolio,
        instrument_id=instrument.pk,
        timeframe="1m",
        parameters={"direction": "LONG"},
        target_configuration={"target_weight": "0.01"},
        qualify=False,
    )
    version = instance.versions.get(version=instance.version)
    now = timezone.now()
    instance.enabled = True
    instance.state = "LONG"
    instance.warmup_completed_at = now
    instance.first_evaluation_completed_at = now
    instance.execution_active_at = now
    instance.save()
    StrategyWarmupReadiness.objects.create(
        strategy_instance=instance,
        strategy_version=version,
        provider="IBKR",
        provider_generation=str(item.provider_generation),
        requirement_hashes=[],
        requirement_snapshot_hash="r" * 64,
        bar_ids=["warmup-bar"],
        bar_timestamps=[],
        evidence_hash="e" * 64,
        is_current=True,
    )

    error = ProviderError(
        "down",
        code=ProviderErrorCode.FINNHUB_UNAVAILABLE,
        provider="FINNHUB",
    )
    result = failover_subscription(
        item.pk,
        ProviderErrorCode.IBKR_DISCONNECTED,
        client=ProviderStub(error=error),
    )
    instance.refresh_from_db()
    assert result.state == "ERROR"
    assert instance.state == "BLOCKED"
    assert instance.state_data["market_data_resume_state"] == "LONG"

    recovered_generation = uuid.uuid4()
    MarketDataSubscription.objects.filter(pk=item.pk).update(
        active_provider="IBKR",
        provider_generation=recovered_generation,
        fallback_state="PRIMARY",
        state="ACTIVE",
    )
    item.refresh_from_db()
    start = timezone.now().replace(microsecond=0, second=0)
    recovered_payload=bar_payload(item,recovered_generation,start)
    recovered_payload["subscription_key"]=(
        f"{item.gateway_session_id}:{item.instrument_id}:{item.timeframe}"
    )
    accepted = publish_provider_event(recovered_payload)
    instance.refresh_from_db()
    assert accepted["accepted"] is True
    assert instance.state == "LONG"
    assert instance.block_reason == ""
    assert "market_data_resume_state" not in instance.state_data


def test_trade_aggregation_deduplicates_orders_and_rejects_late_ticks():
    aggregator = FiveSecondTradeAggregator(allowed_lateness_seconds=2)
    start = datetime(2026, 7, 15, 0, 0, tzinfo=dt_timezone.utc)
    later = {"s": "AAPL", "t": int((start + timedelta(seconds=3)).timestamp() * 1000), "p": 12, "v": 2}
    earlier = {"s": "AAPL", "t": int((start + timedelta(seconds=1)).timestamp() * 1000), "p": 10, "v": 3}
    assert aggregator.add(later, now=start + timedelta(seconds=4)) == "ACCEPTED"
    assert aggregator.add(earlier, now=start + timedelta(seconds=4)) == "ACCEPTED"
    assert aggregator.add(dict(earlier), now=start + timedelta(seconds=4)) == "DUPLICATE"
    bars = aggregator.flush_ready(start + timedelta(seconds=7))
    assert len(bars) == 1 and (bars[0].open, bars[0].close, bars[0].high, bars[0].low) == (10, 12, 12, 10)
    assert bars[0].volume == 5 and bars[0].trade_count == 2
    assert aggregator.add({**earlier, "p": 11}, now=start + timedelta(seconds=8)) == "LATE"


def test_worker_fans_aggregated_bar_into_epoch_gated_outbox(settings):
    enable_fallback(settings)
    instrument, contract = canonical()
    verified_mapping(instrument)
    item = subscription(instrument, contract.conid, active_provider="FINNHUB", fallback_state="FALLBACK")
    worker = FinnhubRealtimeWorker(transport=object())
    from apps.market_data.realtime import desired_finnhub_subscriptions
    worker.desired = desired_finnhub_subscriptions()
    trade_time = timezone.now() - timedelta(seconds=10)
    worker._handle_message(json.dumps({"type": "trade", "data": [{"s": "AAPL",
        "t": int(trade_time.timestamp() * 1000), "p": 100, "v": 7}]}))
    worker._publish_ready()
    event = OutboxEvent.objects.get(topic="market.raw.v1")
    assert event.payload["provider"] == "FINNHUB" and event.payload["timeframe"] == "5s"
    assert event.payload["instrument_id"] == instrument.pk and event.payload["conid"] == contract.conid
    item.refresh_from_db()
    assert item.last_fallback_event_at is not None


def test_error_classification_and_reconnect_backoff_are_bounded():
    assert classify_ibkr_error("354", "not subscribed") == ProviderErrorCode.IBKR_ENTITLEMENT
    assert classify_ibkr_error("162", "Historical data pacing violation") == ProviderErrorCode.IBKR_PACING
    assert classify_ibkr_error(None, "request timed out") == ProviderErrorCode.IBKR_TIMEOUT
    assert reconnect_delay(10, 30, jitter_value=1) == 30


def test_websocket_connection_error_never_exposes_api_key(settings, monkeypatch):
    import websocket
    settings.FINNHUB_API_KEY="super-secret-websocket-key"

    def fail(uri, **kwargs):
        raise websocket.WebSocketException(f"failed to connect to {uri}")

    monkeypatch.setattr(websocket,"create_connection",fail)
    with pytest.raises(ProviderError) as raised:
        FinnhubWebSocketTransport().connect()
    assert "super-secret-websocket-key" not in str(raised.value)


def test_websocket_graceful_close_is_reconnectable():
    transport=FinnhubWebSocketTransport()
    transport.connection=type("ClosedSocket",(),{"settimeout":lambda self,value:None,"recv":lambda self:""})()
    with pytest.raises(ProviderError) as raised:
        transport.receive()
    assert raised.value.retryable is True and raised.value.code==ProviderErrorCode.FINNHUB_UNAVAILABLE


class GatewayProbeStub:
    def __init__(self):
        self.cancelled = []
        self.subscribed = []

    def cancel_market_data(self, payload, key):
        self.cancelled.append((payload, key))
        return {"command_id": 1}

    def subscribe_market_data(self, payload, key):
        self.subscribed.append((payload, key))
        return {"command_id": 2}


def test_stale_primary_fails_over_then_queues_cautious_generation_probe(settings):
    enable_fallback(settings)
    settings.IBKR_MARKET_DATA_FAILOVER_GRACE_SECONDS = 15
    instrument, contract = canonical()
    verified_mapping(instrument)
    now = timezone.now()
    item = subscription(instrument, contract.conid, required_history_bars=0,
        last_primary_event_at=now - timedelta(seconds=16), requested_at=now - timedelta(seconds=20))
    provider = ProviderStub(quote=ProviderQuote(Decimal("101"), now))
    assert monitor_subscriptions(now, provider_client=provider) == 1
    item.refresh_from_db()
    assert item.active_provider == "FINNHUB" and item.failed_over_at is not None

    gateway = GatewayProbeStub()
    monitor_subscriptions(now + timedelta(seconds=1), provider_client=provider, gateway=gateway)
    item.refresh_from_db()
    assert len(gateway.cancelled) == 1 and len(gateway.subscribed) == 1
    probe_payload = gateway.subscribed[0][0]
    assert probe_payload["probe"] is True and probe_payload["historical_bars"] == 0
    assert probe_payload["provider_generation"] == str(item.primary_probe_generation)
    assert probe_payload["gateway_subscription_key"].startswith(f"{instrument.pk}:1m:probe:")


def test_ambiguous_mapping_prevents_fallback_without_calling_provider(settings):
    enable_fallback(settings)
    instrument, contract = canonical()
    InstrumentProviderMapping.objects.create(instrument=instrument, provider="FINNHUB", provider_symbol="AAPL",
        status="AMBIGUOUS", last_error="multiple matches")
    item = subscription(instrument, contract.conid)

    class MustNotCall:
        def quote(self, symbol):
            raise AssertionError("provider must not be called for ambiguous mapping")

    result = failover_subscription(item.pk, ProviderErrorCode.IBKR_ENTITLEMENT, client=MustNotCall())
    assert result.active_provider == "NONE" and result.fallback_reason == ProviderErrorCode.FINNHUB_MAPPING_INVALID


def test_strategies_continue_on_finnhub_and_block_only_after_both_providers_fail(settings):
    from apps.accounts.models import BrokerAccount
    from apps.portfolios.models import TradingPortfolio
    from apps.strategies.framework import create_instance
    from tests.managed_gateway import bind_gateway_mode
    from tests.strategy_activation import activate_strategy

    enable_fallback(settings)
    account = BrokerAccount.objects.create(account_id="DU-FALLBACK")
    portfolio = TradingPortfolio.objects.create(account=account, name="Fallback")
    bind_gateway_mode(portfolio)
    instrument, contract = canonical()
    verified_mapping(instrument)
    instance, _ = create_instance(name="Fallback strategy", definition_key="FIXED_WEIGHT_REBALANCE",
        portfolio=portfolio, instrument_id=instrument.pk, timeframe="1m", parameters={"direction": "LONG"},
        target_configuration={"target_weight": "0.1"}, qualify=False)
    instance,_,item=activate_strategy(instance,ready=False)
    result = failover_subscription(item.pk, ProviderErrorCode.IBKR_DISCONNECTED,
        client=ProviderStub(quote=ProviderQuote(Decimal("100"), timezone.now())))
    instance.refresh_from_db()
    assert result.active_provider == "FINNHUB" and instance.state == "WARMING_UP"
    mark_subscription_unusable(item.pk, ProviderErrorCode.FINNHUB_UNAVAILABLE)
    instance.refresh_from_db()
    assert instance.state == "BLOCKED" and instance.block_reason == ProviderErrorCode.FINNHUB_UNAVAILABLE


def test_position_api_uses_finnhub_reference_without_overwriting_broker_quantity_or_cost(settings, client):
    from apps.accounts.models import BrokerAccount
    from apps.portfolios.models import PortfolioPosition, TradingPortfolio

    account = BrokerAccount.objects.create(account_id="DU-PRICE")
    portfolio = TradingPortfolio.objects.create(account=account, name="Price provenance")
    instrument, _ = canonical()
    position = PortfolioPosition.objects.create(portfolio=portfolio, instrument=instrument,
        quantity=Decimal("7"), average_cost=Decimal("90"), market_price=0)
    InstrumentMarketState.objects.create(instrument=instrument, status="FRESH", reference_price=Decimal("101"),
        latest_event_at=timezone.now(), stale_after_seconds=300, reference_price_provider="FINNHUB",
        reference_price_source="finnhub_quote", provider_generation=uuid.uuid4())
    row = client.get(f"/api/v1/positions/?portfolio={portfolio.pk}").json()["data"][0]
    position.refresh_from_db()
    assert row["market_price"] == "101.00000000" and row["market_price_provider"] == "FINNHUB"
    assert row["market_price_source"] == "finnhub_quote"
    assert position.quantity == 7 and position.average_cost == 90 and position.market_price == 0
