import hashlib
import logging
import uuid

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from apps.allocation.models import (
    AllocationDecision,
    OrderIntentAttribution,
    RebalanceRun,
    StrategyCapitalSnapshot,
)
from apps.audit.models import AuditEvent, OutboxEvent
from apps.market_streams.models import MarketDataSubscription
from apps.oms.models import Order, OrderIntent
from apps.risk.models import KillSwitch

from .models import (
    StrategyAllocation,
    StrategyAction,
    StrategyAttributedPosition,
    StrategyInputBinding,
    StrategyInputRequirement,
    StrategyInstance,
    StrategyRun,
    StrategySignal,
    StrategyTarget,
    StrategyVersion,
    StrategyWarmupReadiness,
)
from .plugins import get_plugin


logger = logging.getLogger(__name__)


ACTIVE_ORDER_STATES = {
    "CREATED",
    "RISK_APPROVED",
    "QUEUED",
    "BROKER_BLOCKED",
    "SUBMITTED",
    "ACKNOWLEDGED",
    "PARTIALLY_FILLED",
    "CANCEL_PENDING",
    "UNKNOWN",
}


class StrategyDeletionError(Exception):
    def __init__(self, code, message, *, status=400, details=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}


def _attempt_token(value=None):
    return str(value or uuid.uuid4())


def _audit_key(instance_id, attempt_key, outcome):
    digest = hashlib.sha256(str(attempt_key).encode()).hexdigest()[:40]
    return f"audit:strategy-delete:{instance_id}:{digest}:{outcome}"


def _actor_name(actor):
    return str(actor or "frontend_operator")[:128]


def audit_strategy_deletion_rejection(instance_id, *, attempt_key=None, actor=None, attempted_name="", error):
    attempt_key = _attempt_token(attempt_key)
    data = {
        "outcome": "REJECTED",
        "attempted_name": str(attempted_name or "")[:128],
        "error": {
            "code": error.code,
            "message": str(error),
            "status": error.status,
            "details": error.details,
        },
    }
    AuditEvent.objects.get_or_create(
        idempotency_key=_audit_key(instance_id, attempt_key, "rejected"),
        defaults={
            "event_type": "strategy.deletion.rejected",
            "actor": _actor_name(actor),
            "aggregate_type": "strategy_instance",
            "aggregate_id": str(instance_id),
            "data": data,
        },
    )
    return error


def _existing_attempt(instance_id, attempt_key):
    success = AuditEvent.objects.filter(
        idempotency_key=_audit_key(instance_id, attempt_key, "succeeded")
    ).first()
    if success:
        return success.data.get("result") or {
            "id": instance_id,
            "name": success.data.get("strategy_name", ""),
        }
    rejected = AuditEvent.objects.filter(
        idempotency_key=_audit_key(instance_id, attempt_key, "rejected")
    ).first()
    if rejected:
        saved = rejected.data.get("error") or {}
        raise StrategyDeletionError(
            saved.get("code", "STRATEGY_DELETION_REJECTED"),
            saved.get("message", "Strategy deletion was rejected"),
            status=int(saved.get("status", 409)),
            details=saved.get("details") or {},
        )
    return None


def _identity_snapshot(instance, version=None):
    snapshot = {
        "strategy_id": instance.pk,
        "strategy_name": instance.name,
        "strategy_instance_id": instance.pk,
        "strategy_instance_name": instance.name,
        "definition_key": instance.definition.key,
    }
    if version:
        snapshot.update({
            "strategy_version_id": version.pk,
            "strategy_version": version.version,
        })
    return snapshot


def _matches_instance(instance, version_ids):
    return (
        Q(strategy_instance_id=instance.pk)
        | Q(strategy_version_id__in=version_ids)
        | Q(attributions__strategy_instance_id=instance.pk)
        | Q(attributions__strategy_version_id__in=version_ids)
    )


def _blockers(instance, version_ids):
    intent_match = _matches_instance(instance, version_ids)
    matching_intent_ids = OrderIntent.objects.filter(intent_match).values("pk")
    open_orders = list(
        Order.objects.filter(intent_id__in=matching_intent_ids, status__in=ACTIVE_ORDER_STATES)
        .order_by("pk")
        .values("id", "internal_id", "status")
        .distinct()[:20]
    )
    pending_intents = list(
        OrderIntent.objects.filter(intent_match, eligible=True, order__isnull=True)
        .order_by("pk")
        .values("id", "source", "mode")
        .distinct()[:20]
    )
    running_runs = list(
        StrategyRun.objects.filter(strategy_instance=instance, status="RUNNING")
        .order_by("pk")
        .values("id", "status")[:20]
    )
    pending_rebalances = list(
        RebalanceRun.objects.filter(portfolio_id=instance.portfolio_id)
        .filter(
            Q(status__in=["CALCULATING", "EXECUTING"])
            | (
                Q(status="INTENTS_CREATED")
                & (Q(orderintent__order__isnull=True) | Q(orderintent__order__status__in=ACTIVE_ORDER_STATES))
            )
        )
        .order_by("pk")
        .values("id", "status", "phase")
        .distinct()[:20]
    )
    positions = [
        {"id": row.pk, "symbol": row.instrument.symbol, "quantity": str(row.quantity)}
        for row in StrategyAttributedPosition.objects.filter(strategy_instance=instance)
        .exclude(quantity=0)
        .select_related("instrument")
        .order_by("pk")[:20]
    ]

    blockers = []
    if open_orders:
        blockers.append({
            "code": "OPEN_ORDERS",
            "message": f"Strategy has {len(open_orders)} open order(s). Cancel them or wait for a terminal status before deleting.",
            "count": len(open_orders),
            "records": open_orders,
        })
    active_executions = [
        {"type": "ORDER_INTENT", **row} for row in pending_intents
    ] + [{"type": "STRATEGY_RUN", **row} for row in running_runs]
    if active_executions:
        blockers.append({
            "code": "ACTIVE_EXECUTIONS",
            "message": f"Strategy has {len(active_executions)} active execution record(s). Wait for processing to finish before deleting.",
            "count": len(active_executions),
            "records": active_executions,
        })
    if pending_rebalances:
        blockers.append({
            "code": "PENDING_REBALANCES",
            "message": f"Portfolio has {len(pending_rebalances)} pending rebalance(s). Complete or cancel them before deleting this strategy.",
            "count": len(pending_rebalances),
            "records": pending_rebalances,
        })
    if positions:
        blockers.append({
            "code": "NON_ZERO_POSITIONS",
            "message": f"Strategy has {len(positions)} non-zero attributed position(s). Flatten them before deleting.",
            "count": len(positions),
            "records": positions,
        })
    return blockers


def _merge_snapshot(existing, identity):
    return {**identity, **(existing or {})}


def _detach_financial_history(instance, version_ids):
    base = _identity_snapshot(instance)
    versions = {item.pk: item for item in StrategyVersion.objects.filter(pk__in=version_ids)}
    intent_match = _matches_instance(instance, version_ids)

    intents = list(
        OrderIntent.objects.filter(intent_match)
        .select_related("strategy_version")
        .distinct()
    )
    for intent in intents:
        version = versions.get(intent.strategy_version_id)
        intent.strategy_snapshot = _merge_snapshot(intent.strategy_snapshot, _identity_snapshot(instance, version))
        intent.strategy_instance = None
        intent.strategy_version = None
    if intents:
        OrderIntent.objects.bulk_update(
            intents,
            ["strategy_snapshot", "strategy_instance", "strategy_version"],
        )

    attributions = list(
        OrderIntentAttribution.objects.filter(
            Q(strategy_instance_id=instance.pk)
            | Q(strategy_version_id__in=version_ids)
        ).select_related("strategy_version")
    )
    for attribution in attributions:
        version = versions.get(attribution.strategy_version_id)
        attribution.strategy_snapshot = _merge_snapshot(
            attribution.strategy_snapshot,
            _identity_snapshot(instance, version),
        )
        attribution.strategy_instance = None
        attribution.strategy_version = None
    if attributions:
        OrderIntentAttribution.objects.bulk_update(
            attributions,
            ["strategy_snapshot", "strategy_instance", "strategy_version"],
        )

    capital_snapshots = list(
        StrategyCapitalSnapshot.objects.filter(strategy_instance=instance)
    )
    for item in capital_snapshots:
        item.strategy_snapshot = _merge_snapshot(item.strategy_snapshot, base)
        item.strategy_instance = None
    if capital_snapshots:
        StrategyCapitalSnapshot.objects.bulk_update(
            capital_snapshots, ["strategy_snapshot", "strategy_instance"]
        )

    decisions = list(
        AllocationDecision.objects.filter(strategy_instance=instance)
    )
    for item in decisions:
        item.strategy_snapshot = _merge_snapshot(item.strategy_snapshot, base)
        item.strategy_instance = None
    if decisions:
        AllocationDecision.objects.bulk_update(
            decisions, ["strategy_snapshot", "strategy_instance"]
        )

    return {
        "order_intents_preserved": len(intents),
        "attributions_preserved": len(attributions),
        "capital_snapshots_preserved": len(capital_snapshots),
        "allocation_decisions_preserved": len(decisions),
    }


def _refresh_input_requirements(requirement_ids):
    removed_hashes = []
    for requirement in StrategyInputRequirement.objects.select_for_update().filter(
        pk__in=requirement_ids
    ):
        active_count = requirement.bindings.filter(active=True).count()
        if active_count == 0:
            removed_hashes.append(requirement.identity_hash)
        if not requirement.bindings.exists():
            requirement.delete()
        elif requirement.active_ref_count != active_count:
            requirement.active_ref_count = active_count
            requirement.save(update_fields=["active_ref_count", "updated_at"])
    return sorted(removed_hashes)


def _publish_input_removal(*, instance_id, trace_id, version, instrument_id,
                           timeframe, removed_hashes, attempt_key):
    """Leave a durable Flink registry tombstone after hard deletion.

    The strategy aggregate and its older outbox rows are intentionally removed,
    so this event is owned by the surviving instrument aggregate.  Shared input
    identities are excluded because their requirements still have bindings.
    """
    if not removed_hashes:
        return None
    digest = hashlib.sha256(str(attempt_key).encode()).hexdigest()[:40]
    event, _ = OutboxEvent.objects.get_or_create(
        idempotency_key=f"strategy-inputs-delete:{instance_id}:{digest}",
        defaults={
            "topic": "strategy.inputs.v1",
            "event_type": "strategy.inputs.changed",
            "aggregate_type": "instrument",
            "aggregate_id": str(instrument_id),
            "partition_key": str(instrument_id),
            "correlation_id": trace_id,
            "payload": {
                "strategy_instance_id": instance_id,
                "trace_id": str(trace_id),
                "strategy_version": version,
                "instrument_id": instrument_id,
                "timeframe": timeframe,
                "requirements": [],
                "removed_requirement_hashes": removed_hashes,
                "deletion": True,
            },
        },
    )
    return event


def _refresh_subscription(instrument_id, timeframe,gateway_session_id=None):
    remaining = list(
        StrategyInstance.objects.filter(
            enabled=True,
            instrument_id=instrument_id,
            timeframe=timeframe,
            portfolio__gateway_session_id=gateway_session_id,
        ).select_related("definition")
    )
    required = max(
        (get_plugin(item.definition).warmup_bars(item.parameters) for item in remaining),
        default=0,
    )
    history = required + int(getattr(settings, "WARMUP_SAFETY_BARS", 5)) if remaining else 0
    MarketDataSubscription.objects.filter(
        gateway_session_id=gateway_session_id,instrument_id=instrument_id, timeframe=timeframe
    ).update(consumer_count=len(remaining), required_history_bars=history, updated_at=timezone.now())

    if settings.KAFKA_ENABLED:
        def reconcile_after_commit():
            try:
                from apps.instruments.models import Instrument
                from apps.market_streams.subscriptions import reconcile_market_subscription

                instrument = Instrument.objects.select_related("broker_contract").get(pk=instrument_id)
                from apps.broker_gateway.models import BrokerGatewaySession
                session=BrokerGatewaySession.objects.get(pk=gateway_session_id) if gateway_session_id else None
                reconcile_market_subscription(instrument, timeframe,gateway_session=session)
            except Exception:
                # The persisted consumer count remains the recovery source of truth.
                logger.exception(
                    "Failed to reconcile market-data subscription after deleting strategy for instrument %s at %s",
                    instrument_id,
                    timeframe,
                )

        transaction.on_commit(reconcile_after_commit)


def _delete_mutable_records(instance, version_ids, *, attempt_key):
    requirement_ids = list(
        StrategyInputBinding.objects.filter(strategy_instance=instance)
        .values_list("requirement_id", flat=True)
        .distinct()
    )
    counts = {
        "signals": StrategySignal.objects.filter(
            Q(strategy_instance=instance) | Q(strategy_version_id__in=version_ids)
        ).count(),
        "targets": StrategyTarget.objects.filter(
            Q(strategy_instance=instance)
            | Q(strategy_version_id__in=version_ids)
            | Q(run__strategy_instance=instance)
        ).count(),
        "runs": StrategyRun.objects.filter(
            Q(strategy_instance=instance)
        ).count(),
        "versions": len(version_ids),
        "allocations": StrategyAllocation.objects.filter(
            strategy_instance=instance
        ).count(),
        "positions": StrategyAttributedPosition.objects.filter(
            strategy_instance=instance
        ).count(),
        "input_bindings": StrategyInputBinding.objects.filter(
            strategy_instance=instance
        ).count(),
        "actions": StrategyAction.objects.filter(strategy_instance=instance).count(),
        "warmup_readiness": StrategyWarmupReadiness.objects.filter(
            strategy_instance=instance
        ).count(),
        "construction_assignments_detached": instance.construction_assignments.count(),
    }

    StrategySignal.objects.filter(
        Q(strategy_instance=instance) | Q(strategy_version_id__in=version_ids)
    ).delete()
    StrategyTarget.objects.filter(
        Q(strategy_instance=instance)
        | Q(strategy_version_id__in=version_ids)
        | Q(run__strategy_instance=instance)
    ).delete()
    StrategyRun.objects.filter(
        Q(strategy_instance=instance)
        | Q(strategy_version_id__in=version_ids)
    ).delete()
    StrategyInputBinding.objects.filter(strategy_instance=instance).delete()
    StrategyAttributedPosition.objects.filter(strategy_instance=instance).delete()
    StrategyAllocation.objects.filter(strategy_instance=instance).delete()
    StrategyAction.objects.filter(strategy_instance=instance).delete()
    StrategyWarmupReadiness.objects.filter(strategy_instance=instance).delete()
    instance.construction_assignments.update(created_strategy_instance=None)
    StrategyVersion.objects.filter(strategy_instance=instance).delete()
    OutboxEvent.objects.select_for_update().filter(
        Q(aggregate_type="strategy_instance", aggregate_id=str(instance.pk))
    ).delete()
    KillSwitch.objects.filter(
        Q(scope__iexact="STRATEGY_INSTANCE", scope_id=str(instance.pk))
        | Q(scope__iexact="STRATEGY", scope_id=str(instance.pk))
    ).delete()

    instance_id = instance.pk
    trace_id = instance.workflow_trace_id
    version = instance.version
    instrument_id = instance.instrument_id
    timeframe = instance.timeframe
    gateway_session_id=instance.portfolio.gateway_session_id
    instance.delete()
    removed_hashes = _refresh_input_requirements(requirement_ids)
    _publish_input_removal(
        instance_id=instance_id,
        trace_id=trace_id,
        version=version,
        instrument_id=instrument_id,
        timeframe=timeframe,
        removed_hashes=removed_hashes,
        attempt_key=attempt_key,
    )
    _refresh_subscription(instrument_id, timeframe,gateway_session_id)
    counts["input_requirements_removed"] = len(removed_hashes)
    return counts


def delete_strategy_instance(instance_id, expected_name, *, attempt_key=None, actor=None):
    attempt_key = _attempt_token(attempt_key)
    existing = _existing_attempt(instance_id, attempt_key)
    if existing:
        return existing

    failure = None
    try:
        with transaction.atomic():
            try:
                instance = (
                    StrategyInstance.objects.select_for_update()
                    .select_related("definition")
                    .get(pk=instance_id)
                )
            except StrategyInstance.DoesNotExist:
                failure = StrategyDeletionError(
                    "STRATEGY_NOT_FOUND",
                    "Strategy instance was not found",
                    status=404,
                )
                audit_strategy_deletion_rejection(
                    instance_id,
                    attempt_key=attempt_key,
                    actor=actor,
                    attempted_name=expected_name,
                    error=failure,
                )
            else:
                version_ids = list(instance.versions.values_list("pk", flat=True))
                if str(expected_name or "") != instance.name:
                    failure = StrategyDeletionError(
                        "STRATEGY_NAME_CONFIRMATION_MISMATCH",
                        "Type the exact strategy name to confirm deletion",
                        status=400,
                        details={"required_name": instance.name},
                    )
                else:
                    blockers = _blockers(instance, version_ids)
                    if blockers:
                        failure = StrategyDeletionError(
                            "STRATEGY_DELETION_BLOCKED",
                            " ".join(blocker["message"] for blocker in blockers),
                            status=409,
                            details={"blockers": blockers},
                        )

                if failure:
                    audit_strategy_deletion_rejection(
                        instance_id,
                        attempt_key=attempt_key,
                        actor=actor,
                        attempted_name=expected_name,
                        error=failure,
                    )
                else:
                    result = {
                        "id": instance.pk,
                        "name": instance.name,
                        "portfolio_id": instance.portfolio_id,
                        "instrument_id": instance.instrument_id,
                    }
                    preserved = _detach_financial_history(instance, version_ids)
                    deleted = _delete_mutable_records(
                        instance, version_ids, attempt_key=attempt_key
                    )
                    result.update({"deleted": deleted, "preserved": preserved})
                    AuditEvent.objects.create(
                        event_type="strategy.deletion.succeeded",
                        actor=_actor_name(actor),
                        aggregate_type="strategy_instance",
                        aggregate_id=str(instance_id),
                        data={
                            "outcome": "SUCCEEDED",
                            "strategy_name": result["name"],
                            "portfolio_id": result["portfolio_id"],
                            "result": result,
                        },
                        idempotency_key=_audit_key(instance_id, attempt_key, "succeeded"),
                    )
    except (IntegrityError, ProtectedError) as exc:
        failure = StrategyDeletionError(
            "STRATEGY_DELETION_CONFLICT",
            "Strategy deletion could not complete because a protected related record still exists",
            status=409,
            details={"reason": str(exc)},
        )
        audit_strategy_deletion_rejection(
            instance_id,
            attempt_key=attempt_key,
            actor=actor,
            attempted_name=expected_name,
            error=failure,
        )

    if failure:
        raise failure
    return result
