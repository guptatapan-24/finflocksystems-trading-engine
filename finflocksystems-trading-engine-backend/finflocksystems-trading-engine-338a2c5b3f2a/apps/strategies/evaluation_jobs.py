import hashlib
from datetime import timedelta

from django.conf import settings
from django.db import DataError, IntegrityError, InterfaceError, OperationalError, transaction
from django.utils import timezone

from apps.market_streams.models import (
    IndicatorValue,
    InstrumentMarketState,
    StrategyEvaluationJob,
)
from apps.strategies.framework import StrategyNotReadyError, evaluate_instance
from apps.strategies.input_identity import indicator_output_name
from apps.strategies.models import StrategyInputBinding, StrategyInstance
from apps.strategies.plugins import get_plugin


ACTIVE_STRATEGY_STATES = {
    "READY_WAITING_FOR_LIVE_BAR", "FLAT", "ENTRY_PENDING", "PARTIALLY_LONG", "LONG", "EXIT_PENDING",
    "PARTIALLY_SHORT", "SHORT",
}


class EvaluationJobError(Exception):
    error_code = "PLUGIN_LOGIC_ERROR"
    retryable = False

    def __init__(self, message, *, details=None, strategy_run_id=None):
        super().__init__(message)
        self.details = details or {}
        self.strategy_run_id = strategy_run_id


class RetryableInfrastructureError(EvaluationJobError):
    error_code = "INFRASTRUCTURE_RETRYABLE"
    retryable = True


class MissingInputError(EvaluationJobError):
    error_code = "MISSING_INPUT"


class OptionQuoteInputPending(MissingInputError):
    """Selected contracts exist, but their executable IBKR quotes are pending."""


class StaleInputError(EvaluationJobError):
    error_code = "STALE_INPUT"


class InvalidConfigurationError(EvaluationJobError):
    error_code = "INVALID_CONFIGURATION"


class PluginLogicError(EvaluationJobError):
    error_code = "PLUGIN_LOGIC_ERROR"


class DataIntegrityEvaluationError(EvaluationJobError):
    error_code = "DATA_INTEGRITY_ERROR"


def _job_key(instance_id, version_id, market_bar_id, bar_version):
    identity = f"{instance_id}:{version_id}:{market_bar_id}:{bar_version}"
    return f"strategy-evaluation:{hashlib.sha256(identity.encode()).hexdigest()}"


@transaction.atomic
def ensure_strategy_evaluation_job(
    instance,
    strategy_version,
    bar,
    *,
    expected_input_identity_hashes,
    ready,
    event_id=None,
):
    """Create/update durable work without evaluating strategy code."""
    instance = StrategyInstance.objects.select_for_update().get(pk=instance.pk)
    stable_event_id = str(event_id or f"{bar.bar_id}:{bar.version}")
    identity = {
        "strategy_instance": instance,
        "strategy_version": strategy_version,
        "market_bar_id": bar.bar_id,
        "bar_version": bar.version,
    }
    desired_status = "PENDING" if ready else "WAITING_FOR_INPUT"
    missing_details = {"message": "Required persisted indicator inputs are not complete"}
    job, created = StrategyEvaluationJob.objects.get_or_create(
        **identity,
        defaults={
            "bar": bar,
            "event_id": stable_event_id,
            "event_time": bar.window_end,
            "source_data_version": bar.version,
            "processing_mode": bar.processing_mode,
            "expected_input_identity_hashes": sorted(expected_input_identity_hashes),
            "status": desired_status,
            "error_code": "" if ready else "MISSING_INPUT",
            "error_details": {} if ready else missing_details,
            "next_attempt_at": timezone.now(),
            "idempotency_key": _job_key(
                instance.pk,
                strategy_version.pk,
                bar.bar_id,
                bar.version,
            ),
        },
    )
    became_ready = created and ready
    incoming_order = (bar.window_end, bar.bar_id, bar.version)
    last_order = (
        instance.last_market_event_at,
        instance.last_market_bar_id,
        instance.last_market_bar_version,
    ) if instance.last_market_event_at else None
    stale_live_event = bar.processing_mode != "LIVE" or (
        last_order is not None and incoming_order < last_order
    )
    if stale_live_event and job.status in {"WAITING_FOR_INPUT", "PENDING", "RETRY"}:
        job.status = "FAILED"
        job.error_code = "STALE_INPUT"
        job.error_details = {
            "message": "Live market event is older than the strategy market cursor",
            "incoming": [bar.window_end.isoformat(), bar.bar_id, bar.version],
            "last_accepted": [
                instance.last_market_event_at.isoformat() if instance.last_market_event_at else None,
                instance.last_market_bar_id,
                instance.last_market_bar_version,
            ],
            "processing_mode": bar.processing_mode,
        }
        job.completed_at = timezone.now()
        job.save(update_fields=[
            "status", "error_code", "error_details", "completed_at", "updated_at",
        ])
        return job, False
    if last_order is None or incoming_order > last_order:
        instance.last_market_event_at = bar.window_end
        instance.last_market_bar_id = bar.bar_id
        instance.last_market_bar_version = bar.version
        instance.save(update_fields=[
            "last_market_event_at", "last_market_bar_id", "last_market_bar_version", "updated_at",
        ])
    if not created:
        if job.bar_id != bar.pk:
            raise DataIntegrityEvaluationError(
                "Strategy evaluation job points to a different persisted bar",
                details={"job_bar_id": job.bar_id, "scheduled_bar_id": bar.pk},
            )
        fields = []
        expected = sorted(expected_input_identity_hashes)
        if job.expected_input_identity_hashes != expected:
            job.expected_input_identity_hashes = expected
            fields.append("expected_input_identity_hashes")
        if not job.event_id:
            job.event_id = stable_event_id
            fields.append("event_id")
        if job.status in {"WAITING_FOR_INPUT", "PENDING", "RETRY"}:
            if ready and job.status == "WAITING_FOR_INPUT":
                job.status = "PENDING"
                job.next_attempt_at = timezone.now()
                job.error_code = ""
                job.error_details = {}
                fields.extend(["status", "next_attempt_at", "error_code", "error_details"])
                became_ready = True
            elif not ready and job.status in {"PENDING", "RETRY"}:
                job.status = "WAITING_FOR_INPUT"
                job.error_code = "MISSING_INPUT"
                job.error_details = missing_details
                fields.extend(["status", "error_code", "error_details"])
        if fields:
            job.save(update_fields=[*dict.fromkeys(fields), "updated_at"])
    return job, became_ready


@transaction.atomic
def claim_next_strategy_evaluation_job(now=None):
    now = now or timezone.now()
    job = (
        StrategyEvaluationJob.objects.select_for_update(skip_locked=True)
        .filter(status__in=["PENDING", "RETRY"], next_attempt_at__lte=now)
        .order_by("next_attempt_at", "created_at", "pk")
        .first()
    )
    if job is None:
        return None
    job.status = "CLAIMED"
    job.claimed_at = now
    job.completed_at = None
    job.attempt_count += 1
    job.save(update_fields=["status", "claimed_at", "completed_at", "attempt_count", "updated_at"])
    return job.pk


@transaction.atomic
def _mark_running(job_id):
    job = StrategyEvaluationJob.objects.select_for_update().filter(pk=job_id).first()
    if job is None or job.status != "CLAIMED":
        return False
    job.status = "RUNNING"
    job.save(update_fields=["status", "updated_at"])
    return True


def _load_evaluation_inputs(job_id):
    job = StrategyEvaluationJob.objects.select_related(
        "strategy_instance__definition",
        "strategy_instance__instrument",
        "strategy_instance__portfolio",
        "strategy_version",
        "bar",
    ).get(pk=job_id)
    instance = job.strategy_instance
    version = job.strategy_version
    bar = job.bar
    if (
        not instance.enabled
        or instance.state not in ACTIVE_STRATEGY_STATES
        or instance.version != version.version
        or version.retired_at is not None
    ):
        raise StaleInputError(
            "Strategy instance or version is no longer eligible for this evaluation",
            details={
                "enabled": instance.enabled,
                "state": instance.state,
                "instance_version": instance.version,
                "job_version": version.version,
            },
        )
    if (
        not bar.is_final
        or bar.bar_id != job.market_bar_id
        or bar.version != job.bar_version
        or job.source_data_version != bar.version
        or job.processing_mode != "LIVE"
        or bar.processing_mode != "LIVE"
    ):
        raise StaleInputError(
            "The final bar identity no longer matches the evaluation job",
            details={
                "market_bar_id": bar.bar_id,
                "bar_version": bar.version,
                "is_final": bar.is_final,
                "processing_mode": bar.processing_mode,
            },
        )
    if bar.instrument_id != instance.instrument_id or bar.interval != instance.timeframe:
        raise DataIntegrityEvaluationError(
            "Evaluation job bar does not match the strategy instrument and timeframe",
            details={
                "bar_instrument_id": bar.instrument_id,
                "strategy_instrument_id": instance.instrument_id,
                "bar_interval": bar.interval,
                "strategy_timeframe": instance.timeframe,
            },
        )
    if bar.market_data_type == "DELAYED" and instance.execution_mode != "PAPER":
        raise StaleInputError(
            "Delayed market data cannot evaluate a non-paper strategy",
            details={"market_data_type": bar.market_data_type, "execution_mode": instance.execution_mode},
        )
    bindings = list(
        StrategyInputBinding.objects.filter(
            strategy_instance=instance,
            strategy_version=version,
            active=True,
        ).select_related("requirement")
    )
    current_identities = sorted(binding.requirement.identity_hash for binding in bindings)
    if current_identities != sorted(job.expected_input_identity_hashes):
        raise StaleInputError(
            "Strategy input requirements changed after the evaluation job was scheduled",
            details={
                "expected": sorted(job.expected_input_identity_hashes),
                "current": current_identities,
            },
        )
    try:
        plugin = get_plugin(instance.definition)
        plugin.validate_configuration(instance.parameters, instance.target_configuration)
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidConfigurationError(str(exc)) from exc
    indicator_bindings = [
        binding for binding in bindings if binding.requirement.input_type == "INDICATOR"
    ]
    identity_hashes = {binding.requirement.identity_hash for binding in indicator_bindings}
    values_by_hash = {}
    for value in IndicatorValue.objects.filter(
        instrument=instance.instrument,
        timeframe=instance.timeframe,
        source_bar_id=bar.bar_id,
        source_bar_version=bar.version,
        is_final=True,
        processing_mode="LIVE",
        requirement_identity_hash__in=identity_hashes,
    ).order_by("requirement_identity_hash", "-created_at"):
        values_by_hash.setdefault(value.requirement_identity_hash, value)
    missing = [
        binding.requirement.identity_hash
        for binding in indicator_bindings
        if binding.requirement.identity_hash not in values_by_hash
    ]
    if missing:
        raise MissingInputError(
            "Persisted indicator inputs are incomplete",
            details={"missing_input_identity_hashes": sorted(missing)},
        )
    values = {}
    previous = {}
    for binding in indicator_bindings:
        value = values_by_hash[binding.requirement.identity_hash]
        if value.event_time != job.event_time:
            raise StaleInputError(
                "Indicator event time does not match the final bar",
                details={
                    "input_identity_hash": binding.requirement.identity_hash,
                    "indicator_event_time": value.event_time.isoformat(),
                    "job_event_time": job.event_time.isoformat(),
                },
            )
        name = indicator_output_name(binding.requirement.name, binding.requirement.role)
        values[name] = value.value
        previous[name] = value.previous_value
    payload = {
        "bar_id": bar.bar_id,
        "event_id": job.event_id,
        "instrument_id": bar.instrument_id,
        "interval": bar.interval,
        "window_start": bar.window_start.isoformat(),
        "window_end": bar.window_end.isoformat(),
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": str(bar.volume),
        "version": bar.version,
        "is_final": True,
        "processing_mode": "LIVE",
        "market_data_type": bar.market_data_type,
        "received_at": bar.received_at.isoformat() if bar.received_at else None,
        "provider": bar.provider,
        "source": bar.source,
    }
    return job, instance, bar, values, previous, payload


@transaction.atomic
def _complete_job(job_id, run_id):
    job = StrategyEvaluationJob.objects.select_for_update().get(pk=job_id)
    if job.status == "COMPLETED":
        return job
    if job.status != "RUNNING":
        raise DataIntegrityEvaluationError(
            f"Cannot complete strategy evaluation job in {job.status}",
            details={"job_id": job.pk, "status": job.status},
        )
    now = timezone.now()
    job.status = "COMPLETED"
    job.strategy_run_id = run_id
    job.completed_at = now
    job.error_code = ""
    job.error_details = {}
    job.save(update_fields=[
        "status", "strategy_run", "completed_at", "error_code", "error_details", "updated_at",
    ])
    return job


def _backoff_seconds(attempt_count):
    base = int(getattr(settings, "STRATEGY_EVALUATION_RETRY_BASE_SECONDS", 5))
    maximum = int(getattr(settings, "STRATEGY_EVALUATION_RETRY_MAX_SECONDS", 300))
    return min(maximum, base * (2 ** max(0, attempt_count - 1)))


def _option_quote_details_ready(details, execution_mode):
    if details.get("input_kind") != "OPTION_QUOTES":
        return False
    instrument_ids = {int(value) for value in details.get("instrument_ids", [])}
    if not instrument_ids:
        return False
    states = {
        state.instrument_id: state
        for state in InstrumentMarketState.objects.filter(instrument_id__in=instrument_ids)
    }
    return len(states) == len(instrument_ids) and all(
        states[instrument_id].is_execution_usable(execution_mode=execution_mode)
        for instrument_id in instrument_ids
    )


@transaction.atomic
def resume_option_quote_evaluation_jobs(instrument_id):
    """Promote quote-blocked evaluations when every selected IBKR leg is fresh."""
    jobs = list(
        StrategyEvaluationJob.objects.select_for_update()
        .filter(
            status="WAITING_FOR_INPUT", error_code="MISSING_INPUT",
            error_details__input_kind="OPTION_QUOTES",
        )
        .select_related("strategy_instance")
    )
    resumed = 0
    for job in jobs:
        details = job.error_details or {}
        instrument_ids = {int(value) for value in details.get("instrument_ids", [])}
        if int(instrument_id) not in instrument_ids:
            continue
        if not _option_quote_details_ready(details, job.strategy_instance.execution_mode):
            continue
        job.status = "PENDING"
        job.next_attempt_at = timezone.now()
        job.error_code = ""
        job.error_details = {}
        job.save(update_fields=[
            "status", "next_attempt_at", "error_code", "error_details", "updated_at",
        ])
        resumed += 1
    return resumed


def classify_evaluation_failure(exc):
    if isinstance(exc, EvaluationJobError):
        return exc
    if isinstance(exc, (OperationalError, InterfaceError, ConnectionError, TimeoutError)):
        return RetryableInfrastructureError(
            str(exc),
            details={"exception_type": type(exc).__name__},
        )
    if isinstance(exc, (IntegrityError, DataError)):
        return DataIntegrityEvaluationError(
            str(exc),
            details={"exception_type": type(exc).__name__},
        )
    return PluginLogicError(
        str(exc),
        details={"exception_type": type(exc).__name__},
    )


@transaction.atomic
def _record_failure(job_id, raw_error):
    error = classify_evaluation_failure(raw_error)
    job = StrategyEvaluationJob.objects.select_for_update().get(pk=job_id)
    if job.status == "COMPLETED":
        return job
    now = timezone.now()
    maximum_attempts = int(getattr(settings, "STRATEGY_EVALUATION_MAX_ATTEMPTS", 5))
    message = str(error)[:1000]
    details = {
        "message": message,
        "retryable": error.retryable,
        **error.details,
    }
    if error.error_code == "MISSING_INPUT":
        if _option_quote_details_ready(details, job.strategy_instance.execution_mode):
            job.status = "PENDING"
            job.next_attempt_at = now
            error.error_code = ""
            details = {}
        else:
            job.status = "WAITING_FOR_INPUT"
        job.completed_at = None
    elif error.retryable and job.attempt_count < maximum_attempts:
        job.status = "RETRY"
        job.next_attempt_at = now + timedelta(seconds=_backoff_seconds(job.attempt_count))
        job.completed_at = None
    else:
        job.status = "FAILED"
        job.completed_at = now
        if error.retryable:
            details["maximum_attempts_exhausted"] = True
    job.error_code = error.error_code
    job.error_details = details
    if error.strategy_run_id:
        job.strategy_run_id = error.strategy_run_id
    job.save(update_fields=[
        "status", "next_attempt_at", "completed_at", "error_code", "error_details",
        "strategy_run", "updated_at",
    ])
    if job.status == "FAILED" and error.error_code in {
        "INVALID_CONFIGURATION", "PLUGIN_LOGIC_ERROR", "DATA_INTEGRITY_ERROR",
    }:
        job.strategy_instance.__class__.objects.filter(pk=job.strategy_instance_id).update(
            state="ERROR", block_reason=message[:255],
        )
    if job.status == "FAILED":
        from apps.strategies.framework import fail_activation_workflow
        instance=StrategyInstance.objects.get(pk=job.strategy_instance_id)
        fail_activation_workflow(
            instance,
            message,
            retryable=bool(error.retryable),
        )
    return job


def execute_strategy_evaluation_job(job_id):
    if not _mark_running(job_id):
        return "SKIPPED"
    try:
        job, instance, bar, values, previous, payload = _load_evaluation_inputs(job_id)
        plugin = get_plugin(instance.definition)
        if plugin.requires_option_context:
            from apps.broker_gateway.client import GatewayError
            from apps.market_data.dhan_options import DhanOptionsError
            from apps.market_data.ibkr_options import IBKROptionDataUnavailable
            from apps.strategies.option_execution import prepare_option_context
            try:
                payload["option_context"] = prepare_option_context(
                    instance, job.strategy_version, payload, values,
                )
                if payload["option_context"].get("status") == "WAITING_FOR_OPTION_QUOTES":
                    legs = payload["option_context"].get("legs") or []
                    raise OptionQuoteInputPending(
                        payload["option_context"].get("reason")
                        or "Selected option contracts are waiting for executable IBKR quotes",
                        details={
                            "input_kind": "OPTION_QUOTES",
                            "selection_snapshot_id": payload["option_context"].get("selection_snapshot_id"),
                            "instrument_ids": sorted({int(leg["instrument_id"]) for leg in legs}),
                        },
                    )
            except IBKROptionDataUnavailable as exc:
                raise RetryableInfrastructureError(str(exc), details={"provider": "IBKR"}) from exc
            except DhanOptionsError as exc:
                error_class = RetryableInfrastructureError if exc.retryable else InvalidConfigurationError
                raise error_class(str(exc), details={"provider": "DHAN"}) from exc
            except GatewayError as exc:
                error_class = RetryableInfrastructureError if exc.retryable else InvalidConfigurationError
                raise error_class(
                    str(exc),
                    details={"provider": "IBKR", "gateway_code": exc.code, **exc.details},
                ) from exc
        try:
            run = evaluate_instance(
                instance,
                bar=payload,
                indicators=values,
                previous_indicators=previous,
                event_id=job.event_id,
                source_data_version=job.source_data_version,
                event_time=job.event_time,
            )
        except (OperationalError, InterfaceError, ConnectionError, TimeoutError):
            raise
        except StrategyNotReadyError as exc:
            # The same condition the claim-time check above reports as stale
            # input. Reaching it here means the state changed underneath the
            # job, so it must be classified identically -- as PLUGIN_LOGIC_ERROR
            # it would drive the instance into the terminal ERROR state and
            # stop every later bar from being evaluated.
            raise StaleInputError(
                str(exc),
                details={"enabled": instance.enabled, "state": instance.state, "race": True},
            ) from exc
        if run.status == "ERROR":
            raise PluginLogicError(
                run.error or "Strategy plugin evaluation failed",
                strategy_run_id=run.pk,
            )
        _complete_job(job_id, run.pk)
        return "COMPLETED"
    except Exception as exc:
        return _record_failure(job_id, exc).status


def process_strategy_evaluation_jobs(limit=None):
    limit = int(limit or getattr(settings, "STRATEGY_EVALUATION_BATCH_SIZE", 50))
    results = {
        "claimed": 0,
        "completed": 0,
        "retry": 0,
        "waiting_for_input": 0,
        "failed": 0,
    }
    for _ in range(max(0, limit)):
        job_id = claim_next_strategy_evaluation_job()
        if job_id is None:
            break
        results["claimed"] += 1
        status = execute_strategy_evaluation_job(job_id)
        key = status.lower()
        if key in results:
            results[key] += 1
    return results


def recover_stuck_strategy_evaluation_jobs(now=None):
    now = now or timezone.now()
    timeout = int(getattr(settings, "STRATEGY_EVALUATION_CLAIM_TIMEOUT_SECONDS", 300))
    cutoff = now - timedelta(seconds=timeout)
    maximum_attempts = int(getattr(settings, "STRATEGY_EVALUATION_MAX_ATTEMPTS", 5))
    recovered = 0
    while True:
        with transaction.atomic():
            job = (
                StrategyEvaluationJob.objects.select_for_update(skip_locked=True)
                .filter(status__in=["CLAIMED", "RUNNING"], claimed_at__lte=cutoff)
                .order_by("claimed_at", "pk")
                .first()
            )
            if job is None:
                break
            details = {
                "message": "Strategy evaluation worker lease expired",
                "retryable": True,
                "previous_status": job.status,
            }
            job.error_code = "INFRASTRUCTURE_RETRYABLE"
            job.error_details = details
            if job.attempt_count < maximum_attempts:
                job.status = "RETRY"
                job.next_attempt_at = now + timedelta(seconds=_backoff_seconds(job.attempt_count))
                job.completed_at = None
            else:
                job.status = "FAILED"
                job.completed_at = now
                job.error_details = {**details, "maximum_attempts_exhausted": True}
            job.save(update_fields=[
                "status", "next_attempt_at", "completed_at", "error_code", "error_details", "updated_at",
            ])
            recovered += 1
    return recovered
