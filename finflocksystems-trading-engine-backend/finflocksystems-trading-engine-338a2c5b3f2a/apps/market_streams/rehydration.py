"""Rebuild Flink's working state from PostgreSQL through the transactional outbox.

Flink keyed state used to be the only memory of two things the backend already
owns durably in PostgreSQL:

* the requirement registry (``StrategyInputRequirement``/``StrategyInputBinding``),
  which Flink rebuilt only by replaying ``strategy.inputs.v1`` from the earliest
  offset -- a topic with no compaction and broker-default retention; and
* the bar history indicators are computed over (``MarketBar``), kept as up to
  10,000 bars per key in heap state with no checkpoint storage outside the
  JobManager.

The Flink stages now keep bounded state only, and this module re-feeds them
through ``OutboxEvent`` rows, so every message is written in a database
transaction and published at least once by ``publish_outbox_events``:

``publish_strategy_inputs_snapshot``
    One ``strategy.inputs.snapshot`` per (instrument, timeframe) with the full
    active requirement set. Flink applies it with replace semantics (see
    ``streaming/flink/jobs/processing.py:plan_registry_update``). The row is
    re-queued every ``STRATEGY_INPUTS_SNAPSHOT_INTERVAL_SECONDS``, so the
    registry never depends on Kafka retention.

``publish_bar_history_seed``
    Recent final bars per active indicator (instrument, timeframe), chunked, to
    ``market.bars.seed.v1``. The indicator stage merges them into its rolling
    history without emitting anything.

``rehydrate_flink_state_if_needed``
    Beat task body: when a required Flink job is RUNNING with a job id this
    process has not seen and was *not* restored from a checkpoint, its state is
    empty, so publish a snapshot and a seed.
"""

import logging
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.audit.models import OutboxEvent
from apps.event_bus.models import StreamHealthMetric

logger = logging.getLogger(__name__)

STRATEGY_INPUTS_TOPIC = "strategy.inputs.v1"
STRATEGY_INPUTS_SNAPSHOT_EVENT_TYPE = "strategy.inputs.snapshot"
SNAPSHOT_KEY_PREFIX = "strategy-inputs-snapshot:"
BAR_HISTORY_SEED_TOPIC = "market.bars.seed.v1"
BAR_HISTORY_SEED_EVENT_TYPE = "market.bar.seed"
SEED_KEY_PREFIX = "bar-history-seed:"
REHYDRATION_COMPONENT = "flink-state-rehydration"
REHYDRATION_METRIC = "last_seen_jobs"


def _kafka_enabled():
    return bool(getattr(settings, "KAFKA_ENABLED", False))


def _requeue(event, payload, now):
    """Force an existing idempotent outbox row back to PENDING with a new payload.

    The UPDATE is conditional so it never rewrites a row a publisher has
    claimed (PUBLISHING with a live lease): the publisher would otherwise mark
    it PUBLISHED without ever sending the new payload. Such a row is simply
    picked up by the next run. ``created_at`` moves to now so the envelope's
    ``occurred_at`` is the moment this payload was read from the database,
    which is what Flink compares against newer incremental events.
    """
    return OutboxEvent.objects.filter(pk=event.pk).exclude(
        status="PUBLISHING", available_at__gt=now,
    ).update(
        payload=payload, status="PENDING", published_at=None, attempt_count=0,
        last_error="", available_at=now, created_at=now,
    )


def _active_requirements_by_pair(input_type=None):
    from apps.strategies.models import StrategyInputRequirement

    query = StrategyInputRequirement.objects.filter(bindings__active=True)
    if input_type:
        query = query.filter(input_type=input_type)
    grouped = {}
    for requirement in query.distinct().order_by("instrument_id", "timeframe", "identity_hash"):
        grouped.setdefault((requirement.instrument_id, requirement.timeframe), []).append(requirement)
    return grouped


def _requirement_payload(requirement):
    return {
        "identity_hash": requirement.identity_hash,
        "input_type": requirement.input_type,
        "name": requirement.name,
        "role": requirement.role,
        "parameters": requirement.parameters,
        "implementation_version": requirement.implementation_version,
        "warmup_bars": requirement.warmup_bars,
    }


def snapshot_payload(instrument_id, timeframe, requirements, now):
    return {
        "snapshot": True,
        "snapshot_at": now.isoformat(),
        "instrument_id": instrument_id,
        "timeframe": timeframe,
        "requirements": [_requirement_payload(item) for item in requirements],
        "removed_requirement_hashes": [],
    }


@transaction.atomic
def _queue_snapshot(instrument_id, timeframe, requirements, now):
    payload = snapshot_payload(instrument_id, timeframe, requirements, now)
    event, created = OutboxEvent.objects.get_or_create(
        idempotency_key=f"{SNAPSHOT_KEY_PREFIX}{instrument_id}:{timeframe}",
        defaults={
            "topic": STRATEGY_INPUTS_TOPIC,
            "event_type": STRATEGY_INPUTS_SNAPSHOT_EVENT_TYPE,
            "aggregate_type": "instrument",
            "aggregate_id": str(instrument_id),
            # Same partition key as incremental strategy.inputs.changed events,
            # so Kafka keeps them in order on one partition.
            "partition_key": str(instrument_id),
            "payload": payload,
        },
    )
    return 1 if created else _requeue(event, payload, now)


def publish_strategy_inputs_snapshot(now=None):
    """Queue the authoritative requirement registry, one row per (instrument, timeframe)."""
    if not _kafka_enabled():
        return {"skipped": "KAFKA_DISABLED"}
    now = now or timezone.now()
    grouped = _active_requirements_by_pair()
    queued = 0
    for (instrument_id, timeframe), requirements in grouped.items():
        queued += _queue_snapshot(instrument_id, timeframe, requirements, now)
    # A pair whose last snapshot still listed requirements but now has none gets
    # one empty snapshot, which removes them from Flink. After that its row is
    # left alone and compact_operational_records deletes it after retention.
    cleared = 0
    for event in OutboxEvent.objects.filter(idempotency_key__startswith=SNAPSHOT_KEY_PREFIX):
        pair = (event.payload.get("instrument_id"), event.payload.get("timeframe"))
        if pair in grouped or not event.payload.get("requirements"):
            continue
        cleared += _queue_snapshot(pair[0], pair[1], [], now)
    return {"pairs": len(grouped), "queued": queued, "cleared": cleared}


def _latest_final_bars(instrument_id, timeframe, limit):
    """Latest version of the newest ``limit`` final LIVE/WARMUP bars, oldest first."""
    from .models import MarketBar

    rows = MarketBar.objects.filter(
        instrument_id=instrument_id,
        interval=timeframe,
        is_final=True,
        processing_mode__in=["LIVE", "WARMUP"],
    ).order_by("-window_end", "-version", "-pk")
    selected = {}
    for bar in rows.iterator(chunk_size=500):
        if bar.bar_id in selected:
            continue
        selected[bar.bar_id] = bar
        if len(selected) >= limit:
            break
    return sorted(selected.values(), key=lambda item: (item.window_end, item.version))


def _seed_bar(bar):
    return {
        "bar_id": bar.bar_id,
        "instrument_id": str(bar.instrument_id),
        "interval": bar.interval,
        "version": bar.version,
        "window_start": bar.window_start.isoformat(),
        "window_end": bar.window_end.isoformat(),
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": str(bar.volume),
        "is_final": True,
    }


def publish_bar_history_seed(rehydration_id, *, instrument_id=None, timeframe=None, reason=""):
    """Queue recent bar history for every active indicator (instrument, timeframe).

    Rows are chunked (``FLINK_REHYDRATION_SEED_CHUNK_BARS``) to stay well under
    Kafka's default 1 MB message limit: a FAILED outbox row blocks execution
    readiness, so a seed must never be too large to publish.
    """
    if not _kafka_enabled():
        return {"skipped": "KAFKA_DISABLED"}
    seed_bars = int(getattr(settings, "FLINK_REHYDRATION_SEED_BARS", 500))
    chunk_size = max(1, int(getattr(settings, "FLINK_REHYDRATION_SEED_CHUNK_BARS", 100)))
    safety = int(getattr(settings, "WARMUP_SAFETY_BARS", 5))
    grouped = _active_requirements_by_pair("INDICATOR")
    pairs = {
        pair: requirements for pair, requirements in grouped.items()
        if (instrument_id is None or pair[0] == int(instrument_id))
        and (timeframe is None or pair[1] == timeframe)
    }
    rows = 0
    bars_total = 0
    for (pair_instrument, pair_timeframe), requirements in pairs.items():
        limit = max(seed_bars, max(item.warmup_bars for item in requirements) + safety)
        bars = [_seed_bar(bar) for bar in _latest_final_bars(pair_instrument, pair_timeframe, limit)]
        if not bars:
            continue
        chunks = [bars[index:index + chunk_size] for index in range(0, len(bars), chunk_size)]
        with transaction.atomic():
            for index, chunk in enumerate(chunks):
                _, created = OutboxEvent.objects.get_or_create(
                    idempotency_key=(
                        f"{SEED_KEY_PREFIX}{rehydration_id}:{pair_instrument}:{pair_timeframe}:{index}"
                    ),
                    defaults={
                        "topic": BAR_HISTORY_SEED_TOPIC,
                        "event_type": BAR_HISTORY_SEED_EVENT_TYPE,
                        "aggregate_type": "instrument",
                        "aggregate_id": str(pair_instrument),
                        "partition_key": str(pair_instrument),
                        "payload": {
                            "instrument_id": str(pair_instrument),
                            "interval": pair_timeframe,
                            "rehydration_id": str(rehydration_id),
                            "reason": reason,
                            "chunk": index,
                            "chunks": len(chunks),
                            "bars": chunk,
                        },
                    },
                )
                rows += int(created)
        bars_total += len(bars)
    return {"pairs": len(pairs), "rows": rows, "bars": bars_total}


def rehydrate_flink_state(rehydration_id=None, *, reason="MANUAL", instrument_id=None,
                          timeframe=None, snapshot=True, seed=True):
    rehydration_id = str(rehydration_id or uuid.uuid4().hex)
    result = {"rehydration_id": rehydration_id, "reason": reason}
    if snapshot:
        result["snapshot"] = publish_strategy_inputs_snapshot()
    if seed:
        result["seed"] = publish_bar_history_seed(
            rehydration_id, instrument_id=instrument_id, timeframe=timeframe, reason=reason,
        )
    logger.info("flink_state_rehydration %s", result)
    return result


def fetch_running_flink_jobs(session=None):
    """Return ``[{job_id, name, restored_checkpoint_id}]`` for running required jobs."""
    import requests

    http = session or requests
    base = str(settings.FLINK_REST_URL).rstrip("/")
    names = set(getattr(settings, "EXECUTION_REQUIRED_FLINK_JOBS", ()) or ("market-pipeline-v1",))
    overview = http.get(f"{base}/jobs/overview", timeout=5)
    overview.raise_for_status()
    jobs = []
    for job in overview.json().get("jobs", []):
        if job.get("name") not in names or job.get("state") != "RUNNING":
            continue
        checkpoints = http.get(f"{base}/jobs/{job['jid']}/checkpoints", timeout=5)
        checkpoints.raise_for_status()
        restored = ((checkpoints.json() or {}).get("latest") or {}).get("restored")
        jobs.append({
            "job_id": job["jid"],
            "name": job["name"],
            "restored_checkpoint_id": restored.get("id") if restored else None,
        })
    return jobs


def _job_marker(job):
    return f"{job['job_id']}:{job['restored_checkpoint_id'] if job['restored_checkpoint_id'] is not None else 'fresh'}"


def rehydrate_flink_state_if_needed(fetch_jobs=None):
    """Publish a snapshot and seed once per Flink job that started without state.

    Fails open: if Flink is unreachable nothing is published and nothing that
    execution readiness reads is touched.
    """
    if not _kafka_enabled() or not getattr(settings, "FLINK_REHYDRATION_ENABLED", True):
        return {"skipped": "DISABLED"}
    try:
        jobs = (fetch_jobs or fetch_running_flink_jobs)()
    except Exception as exc:
        logger.warning("flink_state_rehydration skipped: Flink REST unavailable (%s)", type(exc).__name__)
        return {"skipped": "FLINK_UNAVAILABLE", "error": str(exc)[:255]}
    if not jobs:
        return {"skipped": "NO_RUNNING_JOB"}
    metric = StreamHealthMetric.objects.filter(
        component=REHYDRATION_COMPONENT, metric=REHYDRATION_METRIC,
    ).first()
    seen = set((metric.value or {}).get("jobs", [])) if metric else set()
    current = {_job_marker(job) for job in jobs}
    fresh = sorted(
        job["job_id"] for job in jobs
        if job["restored_checkpoint_id"] is None and _job_marker(job) not in seen
    )
    result = {"jobs": sorted(current), "rehydrated": False}
    if fresh:
        result.update(rehydrate_flink_state(fresh[0], reason="FLINK_JOB_STARTED_WITHOUT_STATE"))
        result["rehydrated"] = True
    if current != seen:
        StreamHealthMetric.objects.update_or_create(
            component=REHYDRATION_COMPONENT, metric=REHYDRATION_METRIC,
            defaults={"status": "HEALTHY", "value": {
                "jobs": sorted(current), "last_rehydration_id": fresh[0] if fresh else
                (metric.value or {}).get("last_rehydration_id") if metric else None,
            }},
        )
    return result
