import hashlib
import logging
import json
from datetime import timedelta
from decimal import Decimal
from django.conf import settings
from django.db import transaction
from django.db.models import F, Max, Prefetch, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from apps.event_bus.identity import processing_mode
from apps.event_bus.services import consume_once
from .models import IndicatorValue, InstrumentMarketState, MarketBar

logger = logging.getLogger(__name__)


def _dt(value):
    return parse_datetime(value) if isinstance(value, str) else value


def _assert_same_market_fact(kind, actual, expected):
    differences = [
        field
        for field, value in expected.items()
        if getattr(actual, field) != value
    ]
    if differences:
        raise ValueError(
            f"Conflicting {kind} payload for deterministic identity: "
            + ", ".join(sorted(differences))
        )


@transaction.atomic
def persist_bar(envelope):
    payload = envelope["payload"]
    mode=processing_mode(payload.get("processing_mode"))
    was_final=MarketBar.objects.filter(
        bar_id=payload["bar_id"],
        is_final=True,
        processing_mode__in=["LIVE","WARMUP"],
    ).exists()
    bar, created = MarketBar.objects.get_or_create(bar_id=payload["bar_id"], version=payload.get("version", 1), defaults={
        "instrument_id": payload["instrument_id"], "interval": payload["interval"],
        "window_start": _dt(payload["window_start"]), "window_end": _dt(payload["window_end"]),
        "open": Decimal(payload["open"]), "high": Decimal(payload["high"]), "low": Decimal(payload["low"]),
        "close": Decimal(payload["close"]), "volume": Decimal(payload.get("volume", "0")),
        "is_final": payload.get("is_final", False), "source_event_count": payload.get("source_event_count", 0),
        "provider":payload.get("provider",""),
        "provider_generation":str(payload.get("provider_generation") or ""),
        "source":payload.get("source",""),
        "market_data_type":str(payload.get("market_data_type") or "LIVE").upper(),
        "received_at":_dt(payload.get("received_at")),
        "processing_mode":mode,"produced_at": _dt(envelope["produced_at"]),})
    promoted_to_live=False
    if not created:
        bar=MarketBar.objects.select_for_update().get(pk=bar.pk)
        _assert_same_market_fact("bar",bar,{
            "instrument_id":int(payload["instrument_id"]),
            "interval":payload["interval"],
            "window_start":_dt(payload["window_start"]),
            "window_end":_dt(payload["window_end"]),
            "open":Decimal(payload["open"]),
            "high":Decimal(payload["high"]),
            "low":Decimal(payload["low"]),
            "close":Decimal(payload["close"]),
            "volume":Decimal(payload.get("volume","0")),
            "is_final":payload.get("is_final",False),
            "source_event_count":payload.get("source_event_count",0),
            "provider":payload.get("provider",""),
            "provider_generation":str(payload.get("provider_generation") or ""),
            "source":payload.get("source",""),
            "market_data_type":str(payload.get("market_data_type") or "LIVE").upper(),
        })
        if mode=="LIVE" and bar.processing_mode!="LIVE":
            bar.processing_mode="LIVE";bar.save(update_fields=["processing_mode"])
            promoted_to_live=True
    if (created and mode in {"LIVE","WARMUP"}) or promoted_to_live:
        update_warmup_progress(bar,new_final_bar=bar.is_final and not was_final)
    if mode=="LIVE":
        coordinate_bar_readiness(bar, event_id=envelope.get("event_id"))
    return {"bar_id": bar.pk}


@transaction.atomic
def persist_indicator(envelope):
    payload = envelope["payload"]
    mode=processing_mode(payload.get("processing_mode"))
    identity_hash=str(payload.get("requirement_identity_hash") or "")
    if len(identity_hash)!=64:
        raise ValueError("Indicator event is missing a full requirement identity hash")
    item, created = IndicatorValue.objects.get_or_create(source_key=payload["source_key"],defaults={
            "instrument_id": payload["instrument_id"], "indicator": payload["indicator"],
            "indicator_name":payload["indicator_name"],"indicator_role":payload.get("indicator_role",""),
            "implementation_version":payload.get("implementation_version",1),
            "requirement_identity_hash":identity_hash,
            "value": Decimal(payload["value"]) if payload.get("value") is not None else None,
            "previous_value": Decimal(payload["previous_value"]) if payload.get("previous_value") is not None else None,
            "parameters": payload.get("parameters", {}),
            "timeframe": payload.get("timeframe", ""), "source_bar_id": payload.get("source_bar_id", ""),
            "source_bar_version": payload.get("source_bar_version", 1), "is_final": payload.get("is_final", True),
            "processing_mode":mode,"event_time": _dt(payload["event_time"])})
    if not created:
        item=IndicatorValue.objects.select_for_update().get(pk=item.pk)
        _assert_same_market_fact("indicator",item,{
            "instrument_id":int(payload["instrument_id"]),
            "indicator":payload["indicator"],
            "indicator_name":payload["indicator_name"],
            "indicator_role":payload.get("indicator_role",""),
            "implementation_version":payload.get("implementation_version",1),
            "requirement_identity_hash":identity_hash,
            "value":Decimal(payload["value"]) if payload.get("value") is not None else None,
            "previous_value":Decimal(payload["previous_value"]) if payload.get("previous_value") is not None else None,
            "parameters":payload.get("parameters",{}),
            "timeframe":payload.get("timeframe",""),
            "source_bar_id":payload.get("source_bar_id",""),
            "source_bar_version":payload.get("source_bar_version",1),
            "is_final":payload.get("is_final",True),
            "event_time":_dt(payload["event_time"]),
        })
        if mode=="LIVE" and item.processing_mode!="LIVE":
            item.processing_mode="LIVE";item.save(update_fields=["processing_mode"])
    bar=MarketBar.objects.filter(bar_id=item.source_bar_id,version=item.source_bar_version,is_final=True).first()
    if bar:
        if item.bar_id!=bar.pk:
            item.bar=bar;item.save(update_fields=["bar"])
        update_warmup_progress(bar)
        if mode=="LIVE" and bar.processing_mode=="LIVE":
            coordinate_bar_readiness(bar)
    return {"indicator_id": item.pk}


WARMUP_BLOCK_PREFIX="Warm-up timeout:"
LIVE_BAR_BLOCK_PREFIX="Live-bar timeout:"


def _current_bindings(instance):
    from apps.strategies.models import StrategyInputBinding
    return StrategyInputBinding.objects.filter(
        strategy_instance=instance,
        strategy_version__version=instance.version,
        active=True,
    ).select_related("requirement")


def current_warmup_required(instance):
    return _current_bindings(instance).aggregate(
        required=Max("requirement__warmup_bars"))["required"] or 0


def _warmup_inputs_ready(instance,bindings,bars,required):
    indicator_hashes=[
        binding.requirement.identity_hash
        for binding in bindings
        if binding.requirement.input_type=="INDICATOR"
    ]
    if len(bars)<required:
        return False,[]
    if not indicator_hashes:
        return True,bars[-required:] if required else []
    available_by_bar={}
    rows=IndicatorValue.objects.filter(
        instrument=instance.instrument,
        timeframe=instance.timeframe,
        requirement_identity_hash__in=indicator_hashes,
        is_final=True,
        processing_mode="WARMUP",
        source_bar_id__in=[bar.bar_id for bar in bars],
        value__isnull=False,
    ).values_list("source_bar_id","source_bar_version","requirement_identity_hash")
    for bar_id,version,identity_hash in rows:
        available_by_bar.setdefault((bar_id,version),set()).add(identity_hash)
    expected=set(indicator_hashes)
    for index in range(len(bars)-1,required-2,-1):
        bar=bars[index]
        if expected.issubset(available_by_bar.get((bar.bar_id,bar.version),set())):
            return True,bars[index-required+1:index+1] if required else []
    return False,[]


def _backfill_stored_windows(instance, subscription, selected, limit):
    """Count earlier windows the current history re-sent but could not store.

    A bar's id is (instrument, timeframe, window_start) only, so a window
    already stored by an earlier subscription (yesterday's live bars, or a
    previous warm-up) rejects the re-sent copy as a conflicting payload: the
    window exists, but never under the current provider generation, and the
    count stalled short of the requirement. The stored final bar for that
    window is the same market fact, so it is counted when it comes from the
    same provider and the same LIVE/DELAYED data type as the current history,
    lies before the earliest current bar (the current subscription remains the
    source of every recent window) and falls inside the backfill lookback.
    """
    newest_current=max(selected.values(),key=lambda item:(item.window_end,item.version))
    earliest_current=min(item.window_start for item in selected.values())
    since=timezone.now()-timedelta(days=int(getattr(settings,"WARMUP_BACKFILL_LOOKBACK_DAYS",10)))
    earlier=MarketBar.objects.filter(
        instrument=instance.instrument,
        interval=instance.timeframe,
        is_final=True,
        processing_mode__in=["WARMUP","LIVE"],
        provider=subscription.active_provider,
        market_data_type=newest_current.market_data_type,
        window_start__lt=earliest_current,
        window_start__gte=since,
    ).order_by("-window_end","-version","-pk")
    added=0
    for bar in earlier:
        if bar.bar_id in selected:
            continue
        selected[bar.bar_id]=bar;added+=1
        if len(selected)>=limit:
            break
    if added:
        logger.info(
            "warmup stage=stored_windows_counted instance_id=%s subscription_id=%s current_generation_bars=%s stored_bars=%s total=%s",
            instance.pk,subscription.pk,len(selected)-added,added,len(selected),
        )


def _warmup_bars(instance, subscription, required):
    if required <= 0:
        return []
    limit=required+int(getattr(settings,"WARMUP_SAFETY_BARS",5))
    query=MarketBar.objects.filter(
        instrument=instance.instrument,
        interval=instance.timeframe,
        is_final=True,
        processing_mode="WARMUP",
    )
    generation=str(subscription.provider_generation) if subscription else ""
    if generation:
        current=query.filter(provider_generation=generation).order_by("-received_at").first()
        reusable_since=(subscription.requested_at or timezone.now())-timedelta(
            seconds=int(getattr(settings,"WARMUP_REUSE_RECEIPT_SECONDS",3600)))
        reusable=Q(received_at__gte=reusable_since,provider=subscription.active_provider)
        if current is not None:
            reusable &= Q(market_data_type=current.market_data_type)
        query=query.filter(
            Q(provider_generation=generation)
            | Q(provider_generation="")
            | reusable
        )
    selected={}
    for bar in query.order_by("-window_end","-version","-pk"):
        selected.setdefault(bar.bar_id,bar)
        if len(selected)>=limit:
            break
    if generation and selected and len(selected)<limit:
        _backfill_stored_windows(instance,subscription,selected,limit)
    return sorted(
        selected.values(),
        key=lambda item:(item.window_end,item.bar_id,item.version),
    )


def _record_warmup_readiness(instance, version, subscription, bindings, bars):
    from apps.strategies.models import StrategyWarmupReadiness
    requirement_hashes=sorted(binding.requirement.identity_hash for binding in bindings)
    bar_ids=[bar.bar_id for bar in bars]
    bar_timestamps=[{
        "bar_id":bar.bar_id,
        "version":bar.version,
        "window_start":bar.window_start.isoformat(),
        "window_end":bar.window_end.isoformat(),
        "processing_mode":bar.processing_mode,
        "provider":bar.provider,
        "provider_generation":bar.provider_generation,
        "source":bar.source,
    } for bar in bars]
    requirement_snapshot_hash=hashlib.sha256(json.dumps(
        requirement_hashes,separators=(",",":"),sort_keys=True
    ).encode()).hexdigest()
    evidence={
        "strategy_instance_id":instance.pk,
        "strategy_version_id":version.pk,
        "strategy_version":version.version,
        "provider":subscription.active_provider if subscription else "",
        "provider_generation":str(subscription.provider_generation) if subscription else "",
        "requirement_snapshot_hash":requirement_snapshot_hash,
        "bar_ids":bar_ids,
        "bar_timestamps":bar_timestamps,
    }
    evidence_hash=hashlib.sha256(json.dumps(
        evidence,separators=(",",":"),sort_keys=True
    ).encode()).hexdigest()
    StrategyWarmupReadiness.objects.filter(
        strategy_instance=instance,is_current=True
    ).exclude(evidence_hash=evidence_hash).update(is_current=False)
    return StrategyWarmupReadiness.objects.get_or_create(
        evidence_hash=evidence_hash,
        defaults={
            "strategy_instance":instance,
            "strategy_version":version,
            "provider":subscription.active_provider if subscription else "",
            "provider_generation":str(subscription.provider_generation) if subscription else "",
            "requirement_hashes":requirement_hashes,
            "requirement_snapshot_hash":requirement_snapshot_hash,
            "bar_ids":bar_ids,
            "bar_timestamps":bar_timestamps,
            "is_current":True,
        },
    )[0]


def refresh_strategy_warmup_state(instance):
    from apps.strategies.models import StrategyInstance
    instance=StrategyInstance.objects.select_related(
        "instrument","portfolio__gateway_session").get(pk=instance.pk)
    if not instance.enabled:return instance
    bindings=list(_current_bindings(instance))
    required=max((binding.requirement.warmup_bars for binding in bindings),default=0)
    subscription=instance.instrument.market_subscriptions.filter(
        gateway_session=instance.portfolio.gateway_session,
        timeframe=instance.timeframe,
    ).first()
    bars=_warmup_bars(instance,subscription,required)
    progress=min(len(bars),required)
    subscription_ready=bool(subscription and subscription.state in {"ACTIVE","DEGRADED"})
    inputs_ready,readiness_bars=_warmup_inputs_ready(instance,bindings,bars,required)
    next_state=instance.state
    next_reason=instance.block_reason
    recoverable_block=(
        instance.state=="BLOCKED"
        and instance.block_reason.startswith((WARMUP_BLOCK_PREFIX,LIVE_BAR_BLOCK_PREFIX))
    )
    if subscription_ready and progress>=required and inputs_ready:
        if instance.state in {"ACTIVATING","SUBSCRIBING","WARMING_UP"} or recoverable_block:
            next_state="READY_WAITING_FOR_LIVE_BAR"
            next_reason=""
    elif subscription_ready and (
            instance.state in {"ACTIVATING","SUBSCRIBING"} or recoverable_block):
        next_state="WARMING_UP"
        next_reason=""
    changed=(
        progress!=instance.warmup_progress
        or next_state!=instance.state
        or next_reason!=instance.block_reason
    )
    if changed:
        now=timezone.now()
        if subscription_ready and instance.subscription_ready_at is None:
            instance.subscription_ready_at=now
        if progress!=instance.warmup_progress:
            instance.warmup_last_progress_at=now
        if next_state=="READY_WAITING_FOR_LIVE_BAR":
            instance.warmup_completed_at=instance.warmup_completed_at or now
            instance.ready_waiting_since=instance.ready_waiting_since or now
        instance.warmup_progress=progress
        instance.state=next_state
        instance.block_reason=next_reason
        instance.save(update_fields=[
            "subscription_ready_at","warmup_completed_at","ready_waiting_since",
            "warmup_progress","warmup_last_progress_at","state","block_reason","updated_at",
        ])
        if next_state=="READY_WAITING_FOR_LIVE_BAR":
            version=instance.versions.get(version=instance.version)
            _record_warmup_readiness(
                instance,version,subscription,bindings,readiness_bars,
            )
            buffered_live=MarketBar.objects.filter(
                instrument=instance.instrument,
                interval=instance.timeframe,
                is_final=True,
                processing_mode="LIVE",
                window_end__gte=instance.warmup_started_at,
            ).order_by("-window_end","-version","-pk").first()
            if buffered_live:
                coordinate_bar_readiness(buffered_live)
        construction_run_id=instance.target_configuration.get("construction_run_id")
        if construction_run_id:
            from apps.portfolio_construction.services import record_strategy_activation_result
            record_strategy_activation_result(construction_run_id,instance.pk)
    return instance


def sync_subscription_strategy_lifecycle(subscription):
    from apps.strategies.models import StrategyInstance
    instances=StrategyInstance.objects.filter(
        enabled=True,
        portfolio__gateway_session=subscription.gateway_session,
        instrument=subscription.instrument,
        timeframe=subscription.timeframe,
    )
    updated=0
    if subscription.state=="ERROR":
        reason=(subscription.last_error or "Market-data subscription failed")[:255]
        updated=instances.update(state="BLOCKED",block_reason=reason)
        for instance in instances:
            from apps.strategies.framework import fail_activation_workflow
            fail_activation_workflow(instance,reason,retryable=True)
            construction_run_id=instance.target_configuration.get("construction_run_id")
            if construction_run_id:
                from apps.portfolio_construction.services import record_strategy_activation_result
                record_strategy_activation_result(construction_run_id,instance.pk)
        return updated
    if subscription.state not in {"ACTIVE","DEGRADED"}:
        return 0
    for instance in instances:
        before=(instance.state,instance.block_reason,instance.warmup_progress)
        refreshed=refresh_strategy_warmup_state(instance)
        updated+=int(before!=(refreshed.state,refreshed.block_reason,refreshed.warmup_progress))
    return updated


def update_warmup_progress(bar,new_final_bar=False):
    if not bar.is_final:return 0
    from apps.strategies.models import StrategyInstance
    instances=list(StrategyInstance.objects.filter(
        enabled=True,instrument=bar.instrument,timeframe=bar.interval))
    changed=0
    for instance in instances:
        before=(instance.warmup_progress,instance.state,instance.block_reason)
        refreshed=refresh_strategy_warmup_state(instance)
        changed+=int(before!=(refreshed.warmup_progress,refreshed.state,refreshed.block_reason))
    return changed


ACTIVE_STRATEGY_STATES=["READY_WAITING_FOR_LIVE_BAR","FLAT","ENTRY_PENDING","PARTIALLY_LONG","LONG","EXIT_PENDING",
    "PARTIALLY_SHORT","SHORT"]


def _active_instances_for_bar(bar):
    from apps.strategies.models import StrategyInputBinding, StrategyInstance
    bindings=StrategyInputBinding.objects.filter(active=True).select_related("requirement","strategy_version")
    query=StrategyInstance.objects.filter(enabled=True,instrument=bar.instrument,timeframe=bar.interval,
        state__in=ACTIVE_STRATEGY_STATES)
    if bar.market_data_type=="DELAYED":
        query=query.filter(execution_mode="PAPER")
    return list(query.select_related("definition","instrument","portfolio").prefetch_related(
            Prefetch("input_bindings",queryset=bindings,to_attr="ready_bindings")))


def coordinate_bar_readiness(bar, event_id=None):
    """Schedule durable work for each ready strategy/bar/version at most once."""
    if not bar.is_final or bar.processing_mode!="LIVE":return 0
    from apps.strategies.evaluation_jobs import ensure_strategy_evaluation_job
    instances=_active_instances_for_bar(bar)
    available=set(IndicatorValue.objects.filter(instrument=bar.instrument,timeframe=bar.interval,
        source_bar_id=bar.bar_id,source_bar_version=bar.version,is_final=True,processing_mode="LIVE"
        ).values_list("requirement_identity_hash",flat=True))
    scheduled=0
    for instance in instances:
        version=next((binding.strategy_version for binding in instance.ready_bindings
            if binding.strategy_version.version==instance.version),None)
        if version is None:
            version=instance.versions.filter(version=instance.version).first()
        if version is None:continue
        expected={binding.requirement.identity_hash for binding in instance.ready_bindings
            if binding.strategy_version_id==version.pk and binding.requirement.input_type=="INDICATOR"}
        identities={binding.requirement.identity_hash for binding in instance.ready_bindings
            if binding.strategy_version_id==version.pk}
        _,became_ready=ensure_strategy_evaluation_job(
            instance,
            version,
            bar,
            expected_input_identity_hashes=sorted(identities),
            ready=expected.issubset(available),
            event_id=event_id,
        )
        scheduled+=int(became_ready)
    return scheduled


def persist_quality(envelope):
    import uuid
    payload = envelope["payload"]
    mode=processing_mode(payload.get("processing_mode"))
    if mode!="LIVE":
        return {"ignored_processing_mode":mode}
    source_value=payload.get("source_event_id")
    try:source_uuid=uuid.UUID(str(source_value)) if source_value else None
    except (ValueError,TypeError,AttributeError):source_uuid=uuid.uuid5(uuid.NAMESPACE_URL,str(source_value))
    state, _ = InstrumentMarketState.objects.update_or_create(instrument_id=payload["instrument_id"], defaults={
        "status": payload["status"], "reference_price": payload.get("reference_price"),
        "latest_event_at": _dt(payload.get("latest_event_at")), "market_event_at":_dt(payload.get("market_event_at")),
        "watermark_at": _dt(payload.get("watermark_at")),
        "stale_after_seconds": payload.get("stale_after_seconds", 300), "source_event_id":source_uuid,
        "reference_price_provider":payload.get("provider", ""),
        "reference_price_source":payload.get("source", ""),
        "market_data_type":str(payload.get("market_data_type") or "LIVE").upper(),
        "provider_generation":payload.get("provider_generation") or None})
    from apps.strategies.evaluation_jobs import resume_option_quote_evaluation_jobs
    resumed = resume_option_quote_evaluation_jobs(state.instrument_id)
    return {"market_state_id": state.pk, "option_evaluation_jobs_resumed": resumed}


def consume_market_event(consumer_name, envelope):
    handlers = {"market.bar": persist_bar, "market.indicator": persist_indicator, "market.quality": persist_quality}
    handler = handlers.get(envelope["event_type"])
    if not handler:
        raise ValueError(f"Unsupported market event type {envelope['event_type']}")
    return consume_once(consumer_name, envelope, handler)
