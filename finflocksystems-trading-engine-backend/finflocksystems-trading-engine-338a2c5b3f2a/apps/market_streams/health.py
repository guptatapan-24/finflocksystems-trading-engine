from django.conf import settings
from django.db.models import BooleanField, Exists, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from .models import IndicatorValue, MarketBar, MarketDataSubscription


def annotate_stream_health(queryset):
    from apps.strategies.models import StrategyInputBinding, StrategyRun
    subscription=MarketDataSubscription.objects.filter(
        gateway_session_id=OuterRef("portfolio__gateway_session_id"),instrument_id=OuterRef("instrument_id"),timeframe=OuterRef("timeframe"))
    legacy_subscription=MarketDataSubscription.objects.filter(gateway_session__isnull=True,
        instrument_id=OuterRef("instrument_id"),timeframe=OuterRef("timeframe"))
    def scoped(field):return Coalesce(Subquery(subscription.values(field)[:1]),Subquery(legacy_subscription.values(field)[:1]))
    canonical=MarketBar.objects.filter(
        instrument_id=OuterRef("instrument_id"),interval=OuterRef("timeframe")).order_by("-produced_at","-version")
    final_bar=MarketBar.objects.filter(
        instrument_id=OuterRef("instrument_id"),interval=OuterRef("timeframe"),is_final=True).order_by("-window_end","-version")
    indicator=IndicatorValue.objects.filter(
        instrument_id=OuterRef("instrument_id"),timeframe=OuterRef("timeframe"),is_final=True).order_by("-event_time","-id")
    run=StrategyRun.objects.filter(strategy_instance_id=OuterRef("pk")).order_by("-started_at","-id")
    indicator_binding=StrategyInputBinding.objects.filter(
        strategy_instance_id=OuterRef("pk"),active=True,strategy_version__version=OuterRef("version"),
        requirement__input_type="INDICATOR")
    return queryset.annotate(
        _stream_annotated=Value(True,output_field=BooleanField()),
        _stream_subscription_state=scoped("state"),
        _stream_subscription_conid=scoped("conid"),
        _stream_last_raw_event=scoped("last_event_at"),
        _stream_subscription_error=scoped("last_error"),
        _stream_active_provider=scoped("active_provider"),
        _stream_fallback_state=scoped("fallback_state"),
        _stream_fallback_reason=scoped("fallback_reason"),
        _stream_provider_generation=scoped("provider_generation"),
        _stream_last_canonical_event=Subquery(canonical.values("produced_at")[:1]),
        _stream_last_final_bar=Subquery(final_bar.values("window_end")[:1]),
        _stream_last_final_bar_close=Subquery(final_bar.values("close")[:1]),
        _stream_last_final_bar_provider=Subquery(final_bar.values("provider")[:1]),
        _stream_last_final_bar_source=Subquery(final_bar.values("source")[:1]),
        _stream_last_final_bar_processing_mode=Subquery(
            final_bar.values("processing_mode")[:1]
        ),
        _stream_last_indicator=Subquery(indicator.values("event_time")[:1]),
        _stream_last_strategy_run=Subquery(run.values("started_at")[:1]),
        _stream_requires_indicator=Exists(indicator_binding),
    )


def strategy_stream_status(instance):
    from apps.strategies.plugins import get_plugin
    annotated=bool(getattr(instance,"_stream_annotated",False))
    if annotated:
        subscription_state=instance._stream_subscription_state
        subscription_conid=instance._stream_subscription_conid
        last_raw_event=instance._stream_last_raw_event
        last_error=(instance._stream_subscription_error or "") or instance.block_reason
        active_provider=instance._stream_active_provider
        fallback_state=instance._stream_fallback_state
        fallback_reason=instance._stream_fallback_reason
        provider_generation=instance._stream_provider_generation
        canonical_at=instance._stream_last_canonical_event
        final_bar_at=instance._stream_last_final_bar
        indicator_at=instance._stream_last_indicator
        run_at=instance._stream_last_strategy_run
        requires_indicator=instance._stream_requires_indicator
    else:
        subscription=MarketDataSubscription.objects.filter(gateway_session=instance.portfolio.gateway_session,
            instrument=instance.instrument,timeframe=instance.timeframe).first()
        canonical=MarketBar.objects.filter(instrument=instance.instrument,interval=instance.timeframe).order_by("-produced_at","-version").first()
        final_bar=MarketBar.objects.filter(instrument=instance.instrument,interval=instance.timeframe,is_final=True).order_by("-window_end","-version").first()
        indicator=IndicatorValue.objects.filter(instrument=instance.instrument,timeframe=instance.timeframe,is_final=True).order_by("-event_time","-id").first()
        run=instance.runs.order_by("-started_at","-id").first()
        requires_indicator=instance.input_bindings.filter(active=True,strategy_version__version=instance.version,
            requirement__input_type="INDICATOR").exists()
        subscription_state=subscription.state if subscription else None
        subscription_conid=subscription.conid if subscription else None
        last_raw_event=subscription.last_event_at if subscription else None
        last_error=(subscription.last_error if subscription else "") or instance.block_reason
        active_provider=subscription.active_provider if subscription else None
        fallback_state=subscription.fallback_state if subscription else None
        fallback_reason=subscription.fallback_reason if subscription else ""
        provider_generation=subscription.provider_generation if subscription else None
        canonical_at=canonical.produced_at if canonical else None
        final_bar_at=final_bar.window_end if final_bar else None
        indicator_at=indicator.event_time if indicator else None
        run_at=run.started_at if run else None
    missing=[]
    interval_seconds={"5s":5,"1m":60,"5m":300,"15m":900,"1h":3600,"1d":86400}.get(instance.timeframe,60)
    stale_after=max(settings.MARKET_PRICE_STALE_SECONDS,interval_seconds*2+60)
    now=timezone.now();stale=False
    if not subscription_state:missing.append("subscription")
    elif subscription_state!="ACTIVE":missing.append(f"subscription {subscription_state.lower()}")
    if not last_raw_event:missing.append("raw event")
    elif (now-last_raw_event).total_seconds()>stale_after:missing.append("fresh raw event");stale=True
    if not canonical_at:missing.append("canonical event")
    elif (now-canonical_at).total_seconds()>stale_after:missing.append("fresh canonical event");stale=True
    if not final_bar_at:missing.append("final bar")
    if requires_indicator and not indicator_at:missing.append("indicator")
    if final_bar_at and not run_at:missing.append("strategy run")
    if not instance.enabled and instance.state in {"DISABLED","PAUSED"}:
        status=instance.state
    elif instance.state in {"ACTIVATING","SUBSCRIBING","WARMING_UP","READY_WAITING_FOR_LIVE_BAR","BLOCKED"}:
        status=instance.state
    else:
        status="DEGRADED" if stale or last_error or subscription_state in {"ERROR","DEGRADED"} else ("WARMING_UP" if missing else "HEALTHY")
    return {"strategy_id":instance.pk,"strategy":instance.name,"symbol":instance.instrument.symbol,
        "timeframe":instance.timeframe,"enabled":instance.enabled,"lifecycle_state":instance.state,
        "activation_status":instance.state if instance.state in {
            "DISABLED","ACTIVATING","SUBSCRIBING","WARMING_UP",
            "READY_WAITING_FOR_LIVE_BAR","BLOCKED",
        } else ("ACTIVE" if instance.enabled else "DISABLED"),
        "status":status,"subscription_state":subscription_state or "MISSING",
        "active_provider":active_provider or "NONE","fallback_state":fallback_state or "FAILED",
        "fallback_reason":fallback_reason,"provider_generation":provider_generation,
        "conid":subscription_conid or getattr(getattr(instance.instrument,"broker_contract",None),"conid",None),
        "last_raw_event":last_raw_event,
        "last_canonical_event":canonical_at,
        "last_final_bar":final_bar_at,
        "warmup_progress":instance.warmup_progress,"warmup_required":get_plugin(instance.definition).warmup_bars(instance.parameters),
        "last_indicator":indicator_at,"last_strategy_run":run_at,
        "last_error":last_error,"missing":missing,"stale_after_seconds":stale_after}
