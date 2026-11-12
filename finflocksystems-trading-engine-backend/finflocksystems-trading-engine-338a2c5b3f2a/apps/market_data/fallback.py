import hashlib
import json
import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.audit.models import OutboxEvent
from apps.broker_gateway.client import GatewayClient, GatewayError
from apps.event_bus.models import StreamHealthMetric
from apps.market_streams.models import (
    InstrumentMarketState, MarketDataProviderTransition, MarketDataSubscription,
)
from apps.strategies.models import StrategyInstance

from .mapping import fallback_eligibility
from .providers.base import ProviderError, ProviderErrorCode
from .providers.finnhub import FinnhubClient

logger = logging.getLogger(__name__)

FALLBACK_IBKR_ERRORS = {
    ProviderErrorCode.IBKR_DISCONNECTED, ProviderErrorCode.IBKR_TIMEOUT, ProviderErrorCode.IBKR_ENTITLEMENT,
    ProviderErrorCode.IBKR_NO_DATA, ProviderErrorCode.IBKR_PACING, ProviderErrorCode.IBKR_TEMPORARY,
}


def classify_ibkr_error(error_code=None, message=""):
    code = str(error_code or "").strip()
    text = str(message or "").lower()
    if code in {"1100", "2110"} or any(value in text for value in ("connection lost", "not connected", "disconnected")):
        return ProviderErrorCode.IBKR_DISCONNECTED
    if code in {"354", "10167", "10168"} or any(value in text for value in ("not subscribed", "market data permission", "entitlement")):
        return ProviderErrorCode.IBKR_ENTITLEMENT
    if code in {"420", "100"} or "pacing" in text:
        return ProviderErrorCode.IBKR_PACING
    if "timed out" in text or "timeout" in text:
        return ProviderErrorCode.IBKR_TIMEOUT
    if "no bars" in text or "no data" in text or "returned no" in text:
        return ProviderErrorCode.IBKR_NO_DATA
    if code in {"200", "321"} or any(value in text for value in ("ambiguous contract", "invalid contract")):
        return ProviderErrorCode.UNSUPPORTED_INSTRUMENT
    return ProviderErrorCode.IBKR_TEMPORARY


def _transition(subscription, previous_provider, new_provider, reason, previous_generation, generation, metadata=None):
    item=MarketDataProviderTransition.objects.create(
        subscription=subscription, instrument=subscription.instrument, timeframe=subscription.timeframe,
        previous_provider=previous_provider, new_provider=new_provider, reason=str(reason),
        previous_generation=previous_generation, generation=generation, metadata=metadata or {},
    )
    logger.info("Market-data provider transition",extra={"instrument_id":subscription.instrument_id,
        "timeframe":subscription.timeframe,"previous_provider":previous_provider,"new_provider":new_provider,
        "reason":str(reason),"provider_generation":str(generation)})
    return item


def _metric_increment(name, *, provider, reason=""):
    with transaction.atomic():
        metric, _ = StreamHealthMetric.objects.select_for_update().get_or_create(
            component="market-data-provider", metric=name,
            defaults={"status": "HEALTHY", "value": {"total": 0, "providers": {}, "reasons": {}}},
        )
        value = dict(metric.value or {})
        providers = dict(value.get("providers") or {})
        reasons = dict(value.get("reasons") or {})
        providers[provider] = int(providers.get(provider, 0)) + 1
        if reason:
            reasons[str(reason)] = int(reasons.get(str(reason), 0)) + 1
        value.update({"total": int(value.get("total", 0)) + 1, "providers": providers, "reasons": reasons})
        metric.status = "HEALTHY"
        metric.value = value
        metric.save(update_fields=["status", "value", "observed_at"])


def _block_strategies(subscription, reason):
    message = str(reason)[:255]
    instances=StrategyInstance.objects.filter(
        enabled=True,portfolio__gateway_session=subscription.gateway_session,
        instrument_id=subscription.instrument_id,timeframe=subscription.timeframe,
    )
    updated=0
    for instance in instances:
        state_data=dict(instance.state_data or {})
        if instance.state!="BLOCKED" and not state_data.get("market_data_resume_state"):
            state_data["market_data_resume_state"]=instance.state
            state_data["market_data_blocked_at"]=timezone.now().isoformat()
        instance.state="BLOCKED"
        instance.block_reason=message
        instance.state_data=state_data
        instance.save(update_fields=["state","block_reason","state_data","updated_at"])
        updated+=1
        construction_run_id=instance.target_configuration.get("construction_run_id")
        if construction_run_id:
            from apps.portfolio_construction.services import record_strategy_activation_result
            record_strategy_activation_result(construction_run_id,instance.pk)
    return updated


def _unblock_strategies(subscription):
    from apps.strategies.models import StrategyWarmupReadiness

    instances=StrategyInstance.objects.filter(
        enabled=True,portfolio__gateway_session=subscription.gateway_session,
        instrument_id=subscription.instrument_id,timeframe=subscription.timeframe,
        state="BLOCKED",
    ).filter(
        Q(block_reason__startswith="Market data unavailable:") | Q(block_reason__startswith="IBKR error ")
        | Q(block_reason__startswith="FINNHUB_") | Q(block_reason__startswith="MARKET_DATA_"),
    )
    from apps.market_streams.services import refresh_strategy_warmup_state
    updated=0
    for instance in instances:
        state_data=dict(instance.state_data or {})
        resume_state=str(state_data.pop("market_data_resume_state","") or "")
        state_data.pop("market_data_blocked_at",None)
        audited_ready=bool(
            instance.warmup_completed_at
            and instance.first_evaluation_completed_at
            and StrategyWarmupReadiness.objects.filter(
                strategy_instance=instance,
                strategy_version__version=instance.version,
                is_current=True,
            ).exists()
        )
        active_states={
            "READY_WAITING_FOR_LIVE_BAR","FLAT","ENTRY_PENDING","PARTIALLY_LONG",
            "LONG","EXIT_PENDING","PARTIALLY_SHORT","SHORT",
        }
        instance.state=resume_state if audited_ready and resume_state in active_states else "WARMING_UP"
        instance.block_reason=""
        instance.state_data=state_data
        instance.warmup_last_progress_at=timezone.now()
        instance.save(update_fields=[
            "state","block_reason","state_data","warmup_last_progress_at","updated_at",
        ])
        if instance.state=="WARMING_UP":
            refresh_strategy_warmup_state(instance)
        updated+=1
    return updated


def _canonical_outbox_key(payload):
    if str(payload.get("event_kind") or "").upper() == "BAR":
        mode=str(payload.get("processing_mode") or "LIVE").upper()
        identity=json.dumps({
            "instrument_id":payload["instrument_id"],
            "timeframe":payload["timeframe"],
            "window_start":payload["window_start"],
            "provider":str(payload.get("provider") or "").upper() if mode=="WARMUP" else "",
            "provider_generation":str(payload.get("provider_generation") or "") if mode=="WARMUP" else "",
            "processing_mode":mode,
        },sort_keys=True,separators=(",",":"))
        return f"market-raw-window:{hashlib.sha256(identity.encode()).hexdigest()}"
    return f"market-raw-event:{payload['instrument_id']}:{payload['source_event_id']}"


def _create_outbox(payload):
    from apps.event_bus.identity import deterministic_event_id, raw_event_key
    stable_key=raw_event_key(payload)
    event, created = OutboxEvent.objects.get_or_create(
        idempotency_key=_canonical_outbox_key(payload),
        defaults={"topic": "market.raw.v1", "event_type": "market.raw", "aggregate_type": "instrument",
                  "aggregate_id": str(payload["instrument_id"]), "partition_key": str(payload["instrument_id"]),
                  "event_id":deterministic_event_id("market.raw",stable_key),"payload": payload},
    )
    return event, created


def _parse_subscription_key(payload):
    key = str(payload.get("subscription_key") or "")
    if ":" not in key:
        raise ValueError("Market event is missing a canonical subscription_key")
    parts=key.split(":")
    if len(parts)>=3:
        return parts[-3],int(parts[-2]),parts[-1]
    return None,int(parts[0]),parts[1]


def _canonical_subscription_key(subscription):
    prefix=f"{subscription.gateway_session_id}:" if subscription.gateway_session_id else ""
    return f"{prefix}{subscription.instrument_id}:{subscription.timeframe}"


def _event_time(value):
    result = parse_datetime(str(value or ""))
    if result is None:
        raise ValueError("Market event has an invalid timestamp")
    return result


def _provider_processing_mode(payload):
    """Preserve producer mode, but classify legacy provider history as warm-up."""
    from apps.event_bus.identity import processing_mode

    supplied = payload.get("processing_mode")
    if supplied not in (None, ""):
        return processing_mode(supplied)
    source = str(payload.get("source") or "").strip().lower()
    if source in {"ibkr_historical", "finnhub_historical", "historical"}:
        return "WARMUP"
    return processing_mode(None)


def _provider_market_data_type(payload):
    """Return trustworthy market-data latency metadata for a provider event.

    Older Gateway builds emitted ``source=ibkr_delayed`` without the matching
    ``market_data_type`` field (or with its legacy LIVE default).  The source is
    authoritative for that IBKR path; preserving the wrong default makes a
    PAPER-safe delayed quote look like stale live data.
    """
    source = str(payload.get("source") or "").strip().lower()
    if source == "ibkr_delayed":
        return "DELAYED"
    supplied = str(payload.get("market_data_type") or "LIVE").strip().upper()
    if supplied not in {"LIVE", "DELAYED"}:
        raise ValueError("Market event has an invalid market_data_type")
    return supplied


def publish_provider_event(payload, *, received_at=None):
    """Gate a provider event by canonical contract, active provider, and provider epoch before outbox publication."""
    received_at = received_at or timezone.now()
    session_id,instrument_id, subscription_timeframe = _parse_subscription_key(payload)
    provider = str(payload.get("provider") or ("FINNHUB" if str(payload.get("source", "")).startswith("finnhub") else "IBKR")).upper()
    StreamHealthMetric.objects.update_or_create(
        component="market-raw-producer",
        metric="heartbeat",
        defaults={
            "status": "HEALTHY",
            "value": {
                "provider": provider,
                "stage": "received",
                "source_event_id": str(payload.get("source_event_id") or ""),
            },
        },
    )
    _metric_increment("events_received",provider=provider)
    with transaction.atomic():
        subscription = MarketDataSubscription.objects.select_for_update(of=("self",)).select_related(
            "instrument__broker_contract",
        ).filter(gateway_session_id=session_id,instrument_id=instrument_id, timeframe=subscription_timeframe).first()
        if not subscription:
            _metric_increment("events_dropped",provider=provider,reason="SUBSCRIPTION_MISSING")
            return {"accepted":False,"reason":"SUBSCRIPTION_MISSING"}
        if int(payload.get("instrument_id") or 0) != subscription.instrument_id:
            _metric_increment("events_dropped",provider=provider,reason="INSTRUMENT_MISMATCH")
            return {"accepted":False,"reason":"INSTRUMENT_MISMATCH"}
        if int(payload.get("conid") or 0) != subscription.conid:
            _metric_increment("events_dropped",provider=provider,reason="CONID_MISMATCH")
            return {"accepted":False,"reason":"CONID_MISMATCH"}
        generation_value = payload.get("provider_generation")
        if not generation_value and settings.MARKET_DATA_FALLBACK_ENABLED:
            _metric_increment("events_dropped", provider=provider, reason="GENERATION_MISSING")
            return {"accepted": False, "reason": "GENERATION_MISSING"}
        try:generation = uuid.UUID(str(generation_value)) if generation_value else subscription.provider_generation
        except (ValueError,TypeError,AttributeError):
            _metric_increment("events_dropped",provider=provider,reason="GENERATION_INVALID")
            return {"accepted":False,"reason":"GENERATION_INVALID"}
        try:mode=_provider_processing_mode(payload)
        except ValueError:
            _metric_increment("events_dropped",provider=provider,reason="PROCESSING_MODE_INVALID")
            return {"accepted":False,"reason":"PROCESSING_MODE_INVALID"}
        try:market_data_type=_provider_market_data_type(payload)
        except ValueError:
            _metric_increment("events_dropped",provider=provider,reason="MARKET_DATA_TYPE_INVALID")
            return {"accepted":False,"reason":"MARKET_DATA_TYPE_INVALID"}
        payload = {**payload, "provider": provider, "provider_generation": str(generation),
            "processing_mode":mode,"market_data_type":market_data_type,
            # Freshness is measured at the trusted backend ingestion boundary.
            # The exchange event_time intentionally remains delayed.
            "received_at":received_at.isoformat()}
        parsed_window_start=parsed_window_end=None
        if payload.get("event_kind", "").upper()=="BAR":
            try:
                parsed_window_start=_event_time(payload.get("window_start"))
                parsed_window_end=_event_time(payload.get("window_end"))
            except ValueError:
                _metric_increment("events_dropped",provider=provider,reason="TIMESTAMP_INVALID")
                return {"accepted":False,"reason":"TIMESTAMP_INVALID"}

        probe_accepted = False
        if provider == "IBKR" and subscription.active_provider in {"FINNHUB", "NONE"} and subscription.primary_probe_generation:
            if generation == subscription.primary_probe_generation and payload.get("source") == "ibkr_live":
                try:event_at = _event_time(payload.get("event_time"))
                except ValueError:
                    _metric_increment("events_dropped",provider=provider,reason="TIMESTAMP_INVALID")
                    return {"accepted":False,"reason":"TIMESTAMP_INVALID"}
                freshness = max(settings.IBKR_MARKET_DATA_FAILOVER_GRACE_SECONDS, 15)
                if abs((received_at - event_at).total_seconds()) <= freshness:
                    subscription.primary_probe_event_count += 1
                    subscription.last_primary_event_at = received_at
                    if subscription.primary_probe_event_count >= settings.PRIMARY_RECOVERY_CONFIRMATION_EVENTS:
                        window_start=parsed_window_start
                        clean_boundary = int(window_start.timestamp()) % 5 == 0
                        no_overlap = (subscription.last_published_window_end is None
                                      or window_start >= subscription.last_published_window_end)
                        if clean_boundary and no_overlap:
                            previous_generation = subscription.provider_generation
                            previous_provider = subscription.active_provider
                            subscription.active_provider = "IBKR"
                            subscription.provider_generation = generation
                            subscription.fallback_state = "PRIMARY"
                            subscription.fallback_reason = ""
                            subscription.state = "ACTIVE"
                            subscription.recovered_at = received_at
                            subscription.primary_probe_generation = None
                            subscription.primary_probe_started_at = None
                            subscription.primary_probe_event_count = 0
                            subscription.last_error = ""
                            _transition(subscription, previous_provider, "IBKR", "IBKR_RECOVERED",
                                        previous_generation, generation,
                                        {"confirmation_events": settings.PRIMARY_RECOVERY_CONFIRMATION_EVENTS})
                            probe_accepted = True
                    subscription.save()
                if not probe_accepted:
                    _metric_increment("events_dropped", provider=provider, reason="PRIMARY_PROBE")
                    return {"accepted": False, "reason": "PRIMARY_PROBE"}

        if provider != subscription.active_provider:
            _metric_increment("events_dropped", provider=provider, reason="PROVIDER_MISMATCH")
            return {"accepted": False, "reason": "PROVIDER_MISMATCH"}
        if generation != subscription.provider_generation:
            _metric_increment("events_dropped", provider=provider, reason="GENERATION_MISMATCH")
            return {"accepted": False, "reason": "GENERATION_MISMATCH"}

        _, created = _create_outbox(payload)
        subscription.last_event_at = received_at
        if provider == "IBKR":
            subscription.last_primary_event_at = received_at
            subscription.state = "ACTIVE"
        else:
            subscription.last_fallback_event_at = received_at
            subscription.state = "DEGRADED"
        if parsed_window_end is not None:
            if subscription.last_published_window_end is None or parsed_window_end > subscription.last_published_window_end:
                subscription.last_published_window_end = parsed_window_end
        subscription.last_error = ""
        subscription.save()
    _metric_increment("events_published" if created else "duplicate_events", provider=provider)
    StreamHealthMetric.objects.update_or_create(
        component="market-raw-producer",
        metric="heartbeat",
        defaults={
            "status": "HEALTHY",
            "value": {
                "provider": provider,
                "stage": "published" if created else "duplicate",
                "source_event_id": str(payload.get("source_event_id") or ""),
            },
        },
    )
    _unblock_strategies(subscription)
    return {"accepted": True, "created": created, "provider": provider}


def _historical_range(timeframe, count):
    seconds = {"1m":60,"5m":300,"15m":900,"1h":3600,"1d":86400}[timeframe]
    end = timezone.now()
    calendar_multiplier = 3 if timeframe == "1d" else 2
    start = end - timedelta(seconds=max(count, 1) * seconds * calendar_multiplier + 2 * 86400)
    return start, end


def _historical_payload(subscription, mapping, generation, candle, reason):
    stable = hashlib.sha256(
        f"FINNHUB:{mapping.provider_symbol}:{subscription.timeframe}:{candle.window_start.isoformat()}".encode()
    ).hexdigest()
    return {
        "source_event_id": stable, "subscription_key": _canonical_subscription_key(subscription),
        "instrument_id": subscription.instrument_id, "conid": subscription.conid,
        "symbol": subscription.instrument.symbol, "exchange": subscription.instrument.exchange,
        "currency": subscription.instrument.currency, "event_kind": "BAR", "timeframe": subscription.timeframe,
        "event_time": candle.window_start.isoformat(), "window_start": candle.window_start.isoformat(),
        "window_end": candle.window_end.isoformat(), "open": str(candle.open), "high": str(candle.high),
        "low": str(candle.low), "close": str(candle.close), "volume": str(candle.volume), "is_final": True,
        "source": "finnhub_historical", "provider": "FINNHUB", "provider_symbol": mapping.provider_symbol,
        "provider_generation": str(generation), "fallback_reason": str(reason),
        "processing_mode": "WARMUP",
    }


def _reference_state(subscription, mapping, generation, quote=None, candle=None):
    if quote:
        price, observed_at, source = quote.price, quote.event_time, "finnhub_quote"
    elif candle:
        price, observed_at, source = candle.close, candle.window_end, "finnhub_historical"
    else:
        return None
    source_key = f"{source}:{mapping.provider_symbol}:{observed_at.isoformat()}"
    source_uuid = uuid.uuid5(uuid.NAMESPACE_URL, source_key)
    state, _ = InstrumentMarketState.objects.update_or_create(
        instrument=subscription.instrument,
        defaults={"status": "FRESH", "reference_price": price, "latest_event_at": observed_at,
                  "watermark_at": timezone.now(), "stale_after_seconds": settings.MARKET_PRICE_STALE_SECONDS,
                  "source_event_id": source_uuid, "reference_price_provider": "FINNHUB",
                  "reference_price_source": source, "provider_generation": generation},
    )
    return state


def mark_subscription_unusable(subscription_id, reason, *, generation=None, diagnostic=None):
    with transaction.atomic():
        subscription = MarketDataSubscription.objects.select_for_update().get(pk=subscription_id)
        previous_provider = subscription.active_provider
        previous_generation = subscription.provider_generation
        generation = generation or uuid.uuid4()
        subscription.active_provider = "NONE"
        subscription.provider_generation = generation
        subscription.fallback_state = "FAILED"
        subscription.fallback_reason = str(reason)
        subscription.state = "ERROR"
        subscription.last_error = str(diagnostic or reason)[:2000]
        subscription.save()
        _transition(subscription, previous_provider, "NONE", reason, previous_generation, generation)
    _block_strategies(subscription, diagnostic or reason)
    _metric_increment("transitions", provider="NONE", reason=reason)
    return subscription


def failover_subscription(subscription_id, reason, *, historical=False, client=None, diagnostic=None):
    with transaction.atomic():
        subscription = MarketDataSubscription.objects.select_for_update(of=("self",)).select_related(
            "instrument__broker_contract",
        ).get(pk=subscription_id)
        eligible, ineligible_reason, mapping = fallback_eligibility(subscription, historical=historical)
        if not eligible:
            failure = ineligible_reason
        else:
            previous_provider = subscription.active_provider
            previous_generation = subscription.provider_generation
            generation = uuid.uuid4()
            subscription.active_provider = "NONE"
            subscription.provider_generation = generation
            subscription.fallback_state = "FAILING_OVER"
            subscription.fallback_reason = str(reason)
            subscription.state = "DEGRADED"
            subscription.last_error = str(reason)[:2000]
            subscription.save()
            _transition(subscription, previous_provider, "NONE", reason, previous_generation, generation,
                        {"phase": "FAILING_OVER"})
            failure = None
    if failure:
        return mark_subscription_unusable(subscription_id, failure, diagnostic=diagnostic)

    active_client = client or FinnhubClient()
    candles = []
    quote = None
    try:
        if historical and subscription.required_history_bars:
            if not active_client.capabilities(subscription.instrument.asset_class, subscription.timeframe):
                raise ProviderError("Finnhub does not support this contract/timeframe",
                                    code=ProviderErrorCode.UNSUPPORTED_INSTRUMENT, provider="FINNHUB", status_code=400)
            start, end = _historical_range(subscription.timeframe, subscription.required_history_bars)
            candles = active_client.historical_candles(mapping.provider_symbol, subscription.timeframe, start, end)
            candles = sorted(candles, key=lambda row: row.window_start)[-subscription.required_history_bars:]
            if not candles:
                raise ProviderError("Finnhub returned no historical bars", code=ProviderErrorCode.FINNHUB_NO_DATA,
                                    provider="FINNHUB")
        try:
            quote = active_client.quote(mapping.provider_symbol)
        except ProviderError as exc:
            if not candles:
                raise
            logger.info("Finnhub quote unavailable; using latest fallback candle", extra={
                "provider": "FINNHUB", "instrument_id": subscription.instrument_id, "error_code": exc.code,
            })
    except ProviderError as exc:
        return mark_subscription_unusable(subscription_id, exc.code, generation=generation)

    payloads = [_historical_payload(subscription, mapping, generation, candle, reason) for candle in candles]
    with transaction.atomic():
        subscription = MarketDataSubscription.objects.select_for_update().get(pk=subscription_id)
        if subscription.provider_generation != generation or subscription.fallback_state != "FAILING_OVER":
            return subscription
        for payload in payloads:
            _create_outbox(payload)
        if payloads:
            subscription.last_published_window_end = max(candle.window_end for candle in candles)
            subscription.last_event_at = timezone.now()
            subscription.last_fallback_event_at = subscription.last_event_at
        subscription.active_provider = "FINNHUB"
        subscription.fallback_state = "FALLBACK"
        subscription.state = "DEGRADED"
        subscription.failed_over_at = timezone.now()
        subscription.last_error = ""
        subscription.save()
        baseline=subscription.last_primary_event_at or subscription.requested_at
        failover_seconds=max(0,(subscription.failed_over_at-baseline).total_seconds()) if baseline else None
        _transition(subscription, "NONE", "FINNHUB", reason, generation, generation,
                    {"historical_bars":len(payloads),"provider_symbol":mapping.provider_symbol,
                     "time_to_failover_seconds":failover_seconds})
        _reference_state(subscription, mapping, generation, quote=quote, candle=candles[-1] if candles else None)
    _unblock_strategies(subscription)
    _metric_increment("transitions", provider="FINNHUB", reason=reason)
    return subscription


def handle_ibkr_failure(subscription, error_code=None, message="", *, historical=False, client=None):
    category = classify_ibkr_error(error_code, message)
    diagnostic = (f"IBKR error {error_code}: {message}" if error_code not in (None, "") else str(message or category))
    if category not in FALLBACK_IBKR_ERRORS:
        return mark_subscription_unusable(subscription.pk, category, diagnostic=diagnostic)
    return failover_subscription(subscription.pk, category, historical=historical, client=client, diagnostic=diagnostic)


def begin_primary_probe(subscription_id, *, gateway=None):
    if not settings.FINNHUB_AUTO_FAILBACK_ENABLED:
        return None
    now = timezone.now()
    with transaction.atomic():
        subscription = MarketDataSubscription.objects.select_for_update(of=("self",)).select_related(
            "instrument__broker_contract",
        ).get(pk=subscription_id)
        if subscription.active_provider not in {"FINNHUB", "NONE"} or not subscription.consumer_count:
            return None
        if (subscription.primary_probe_started_at
                and subscription.primary_probe_started_at > now - timedelta(seconds=settings.PRIMARY_PROBE_RETRY_SECONDS)):
            return None
        generation = uuid.uuid4()
        subscription.primary_probe_generation = generation
        subscription.primary_probe_started_at = now
        subscription.primary_probe_event_count = 0
        subscription.fallback_state = "RECOVERING"
        subscription.save()
    client = gateway or GatewayClient(subscription.gateway_session,require_commands=True)
    canonical_key = _canonical_subscription_key(subscription)
    runtime_key = f"{canonical_key}:probe:{generation}"
    payload = {
        "subscription_key": canonical_key, "gateway_subscription_key": runtime_key,
        "instrument_id": subscription.instrument_id, "conid": subscription.conid,
        "symbol": subscription.instrument.symbol, "asset_class": subscription.instrument.asset_class,
        "exchange": subscription.instrument.exchange, "currency": subscription.instrument.currency,
        "timeframe": subscription.timeframe, "historical_bars": 0, "provider": "IBKR",
        "provider_generation": str(generation), "probe": True,
    }
    try:
        client.cancel_market_data({"subscription_key": canonical_key}, f"market-probe-cancel:{subscription.pk}:{generation}")
        queued = client.subscribe_market_data(payload, f"market-probe:{subscription.pk}:{generation}")
        MarketDataSubscription.objects.filter(pk=subscription.pk, primary_probe_generation=generation).update(
            gateway_command_id=queued.get("command_id"), last_error="",
        )
        return queued
    except GatewayError as exc:
        with transaction.atomic():
            current=MarketDataSubscription.objects.select_for_update().get(pk=subscription.pk)
            if current.primary_probe_generation==generation:
                current.primary_probe_generation=None;current.primary_probe_started_at=None
                current.primary_probe_event_count=0
                current.fallback_state="FALLBACK" if current.active_provider=="FINNHUB" else "FAILED"
                current.last_error=f"IBKR probe failed: {exc}"[:2000];current.save()
        return None


def monitor_subscriptions(now=None, *, provider_client=None, gateway=None):
    now = now or timezone.now()
    changed = 0
    subscriptions = list(MarketDataSubscription.objects.filter(consumer_count__gt=0).select_related(
        "instrument__broker_contract",
    ))
    for subscription in subscriptions:
        if subscription.active_provider == "IBKR" and settings.MARKET_DATA_FALLBACK_ENABLED:
            baseline = subscription.last_primary_event_at or subscription.requested_at or subscription.updated_at
            if (baseline and subscription.state in {"SUBSCRIBING", "ACTIVE", "DEGRADED"}
                    and (now - baseline).total_seconds() > settings.IBKR_MARKET_DATA_FAILOVER_GRACE_SECONDS):
                failover_subscription(subscription.pk, ProviderErrorCode.IBKR_TIMEOUT, historical=False,
                                      client=provider_client)
                changed += 1
        elif subscription.active_provider == "FINNHUB":
            begin_primary_probe(subscription.pk,gateway=gateway)
            baseline = subscription.last_fallback_event_at or subscription.failed_over_at
            if (settings.FINNHUB_LIVE_FALLBACK_ENABLED and baseline
                    and (now - baseline).total_seconds() > settings.FINNHUB_LIVE_STALE_SECONDS):
                mark_subscription_unusable(subscription.pk, ProviderErrorCode.FINNHUB_UNAVAILABLE)
                changed += 1
        elif subscription.active_provider == "NONE" and subscription.fallback_state in {"FAILED","RECOVERING"}:
            retry_from=subscription.primary_probe_started_at or subscription.updated_at
            if (now-retry_from).total_seconds()>=settings.PRIMARY_PROBE_RETRY_SECONDS:
                recovered=failover_subscription(subscription.pk, ProviderErrorCode.FINNHUB_UNAVAILABLE,
                                                historical=False,client=provider_client)
                if recovered.active_provider!="FINNHUB":
                    begin_primary_probe(subscription.pk,gateway=gateway)
                changed += 1
    return changed
