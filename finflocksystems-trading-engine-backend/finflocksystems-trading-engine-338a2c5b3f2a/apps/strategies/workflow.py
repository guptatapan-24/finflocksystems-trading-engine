from __future__ import annotations

from django.utils import timezone


STAGES = (
    ("CREATED", "Created"),
    ("ACTIVATION_QUEUED", "Activation queued"),
    ("CONTRACT_QUALIFIED", "Contract qualified"),
    ("SUBSCRIPTION_PENDING", "Subscription pending"),
    ("SUBSCRIPTION_ACTIVE", "Subscription active"),
    ("WARMUP_IN_PROGRESS", "Warm-up in progress"),
    ("WARMUP_COMPLETE", "Warm-up complete"),
    ("WAITING_FOR_LIVE_BAR", "Waiting for live bar"),
    ("FIRST_EVALUATION_COMPLETED", "First evaluation completed"),
    ("TARGET_GENERATED", "Target generated"),
    ("REBALANCE_CREATED", "Rebalance created"),
    ("ORDER_INTENT_CREATED", "Order intent created"),
    ("RISK_DECISION", "Risk approved or blocked"),
    ("BROKER_COMMAND_SENT", "Broker command sent"),
    ("ORDER_ACKNOWLEDGED", "Order acknowledged"),
    ("FILL_PROGRESS", "Partially filled or filled"),
)

ORDER_TERMINAL_STATES = {
    "FILLED",
    "REJECTED",
    "CANCELLED",
    "EXPIRED",
}
ORDER_ACTIVE_STATES = {
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
ACTIVATION_ACTIVE_STATES = {
    "ACTIVATING",
    "SUBSCRIBING",
    "WARMING_UP",
    "READY_WAITING_FOR_LIVE_BAR",
}


def latest_activation_action(instance):
    return instance.actions.filter(action="enable").order_by("-created_at", "-pk").first()


def activation_operation(instance):
    action = latest_activation_action(instance)
    if action is None:
        return None
    return {
        "id": action.pk,
        "status": action.status,
        "retryable": action.retryable,
        "message": action.last_error,
        "attempt_count": action.attempt_count,
        "idempotency_key": action.idempotency_key,
        "created_at": action.created_at,
        "completed_at": action.completed_at,
    }


def _stage(
    key,
    label,
    *,
    status="PENDING",
    occurred_at=None,
    detail="",
    blocker="",
    retryable=False,
    entity_type="",
    entity_id=None,
):
    return {
        "stage": key,
        "label": label,
        "status": status,
        "occurred_at": occurred_at,
        "detail": detail,
        "blocker": blocker,
        "retryable": bool(retryable),
        "entity_type": entity_type,
        "entity_id": str(entity_id) if entity_id is not None else None,
    }


def _first_order_path(instance):
    from apps.oms.models import OrderIntent

    intents = list(
        OrderIntent.objects.filter(attributions__strategy_instance=instance)
        .select_related("rebalance", "order")
        .prefetch_related(
            "risk_checks",
            "order__status_history",
            "order__broker_commands",
            "order__fills",
        )
        .distinct()
        .order_by("created_at", "pk")
    )
    if not intents:
        return None, None
    intent = intents[0]
    return intent, getattr(intent, "order", None)


def execution_workflow(instance):
    from apps.market_streams.models import MarketBar, MarketDataSubscription

    trace_id = str(instance.workflow_trace_id)
    action = latest_activation_action(instance)
    contract = getattr(instance.instrument, "broker_contract", None)
    subscription = MarketDataSubscription.objects.filter(
        gateway_session=instance.portfolio.gateway_session,
        instrument=instance.instrument,
        timeframe=instance.timeframe,
    ).first()
    warmup_bar = (
        MarketBar.objects.filter(
            instrument=instance.instrument,
            interval=instance.timeframe,
            is_final=True,
            processing_mode="WARMUP",
        )
        .order_by("-window_end", "-version", "-pk")
        .first()
    )
    live_bar = (
        MarketBar.objects.filter(
            instrument=instance.instrument,
            interval=instance.timeframe,
            is_final=True,
            processing_mode="LIVE",
        )
        .order_by("-window_end", "-version", "-pk")
        .first()
    )
    run = instance.runs.filter(status="COMPLETED").order_by("completed_at", "pk").first()
    target = (
        instance.targets.filter(run=run).order_by("created_at", "pk").first()
        if run
        else None
    )
    intent, order = _first_order_path(instance)
    rebalance = intent.rebalance if intent and intent.rebalance_id else None
    risks = list(intent.risk_checks.order_by("created_at", "pk")) if intent else []
    blocked_risk = next(
        (item for item in risks if item.decision in {"REJECTED", "HELD", "BLOCKED"}),
        None,
    )
    approved_risk = next(
        (item for item in reversed(risks) if item.decision in {"APPROVED", "RESIZED"}),
        None,
    )
    command = (
        order.broker_commands.filter(command_type="PLACE")
        .order_by("created_at", "pk")
        .first()
        if order
        else None
    )
    acknowledgement = (
        order.status_history.filter(to_status="ACKNOWLEDGED")
        .order_by("occurred_at", "pk")
        .first()
        if order
        else None
    )
    fills = list(order.fills.order_by("executed_at", "pk")) if order else []
    latest_fill = fills[-1] if fills else None

    rows = [
        _stage(
            "CREATED",
            "Created",
            status="COMPLETED",
            occurred_at=instance.created_at,
            detail=f"Strategy {instance.pk} created disabled",
            entity_type="strategy_instance",
            entity_id=instance.pk,
        ),
        _stage(
            "ACTIVATION_QUEUED",
            "Activation queued",
            status=(
                "COMPLETED"
                if action
                else ("NOT_STARTED" if instance.state == "DISABLED" else "PENDING")
            ),
            occurred_at=action.created_at if action else None,
            detail=(
                f"Durable activation attempt {action.attempt_count}"
                if action
                else "Activation has not been requested"
            ),
            entity_type="strategy_action" if action else "",
            entity_id=action.pk if action else None,
        ),
        _stage(
            "CONTRACT_QUALIFIED",
            "Contract qualified",
            status="COMPLETED" if contract and contract.conid else "PENDING",
            occurred_at=(
                (contract.qualified_at or instance.created_at)
                if contract and contract.conid
                else None
            ),
            detail=f"IBKR conId {contract.conid}" if contract and contract.conid else "",
            entity_type="broker_contract" if contract else "",
            entity_id=contract.pk if contract else None,
        ),
        _stage(
            "SUBSCRIPTION_PENDING",
            "Subscription pending",
            status="COMPLETED" if subscription else "PENDING",
            occurred_at=(
                subscription.requested_at or subscription.created_at
                if subscription
                else None
            ),
            detail=(
                f"{subscription.active_provider} subscription {subscription.state}"
                if subscription
                else ""
            ),
            entity_type="market_data_subscription" if subscription else "",
            entity_id=subscription.pk if subscription else None,
        ),
        _stage(
            "SUBSCRIPTION_ACTIVE",
            "Subscription active",
            status=(
                "COMPLETED"
                if subscription and subscription.state in {"ACTIVE", "DEGRADED"}
                else (
                    "FAILED"
                    if subscription and subscription.state == "ERROR"
                    else "PENDING"
                )
            ),
            occurred_at=instance.subscription_ready_at,
            detail=(
                f"{subscription.active_provider} provider"
                if subscription and subscription.state in {"ACTIVE", "DEGRADED"}
                else ""
            ),
            blocker=(
                subscription.last_error
                if subscription and subscription.state == "ERROR"
                else ""
            ),
            retryable=bool(subscription and subscription.state == "ERROR"),
            entity_type="market_data_subscription" if subscription else "",
            entity_id=subscription.pk if subscription else None,
        ),
        _stage(
            "WARMUP_IN_PROGRESS",
            "Warm-up in progress",
            status=(
                "COMPLETED"
                if instance.warmup_completed_at
                else ("ACTIVE" if instance.warmup_started_at else "PENDING")
            ),
            occurred_at=instance.warmup_started_at,
            detail=f"{instance.warmup_progress} persisted warm-up bars",
            entity_type="market_bar" if warmup_bar else "strategy_instance",
            entity_id=warmup_bar.pk if warmup_bar else instance.pk,
        ),
        _stage(
            "WARMUP_COMPLETE",
            "Warm-up complete",
            status="COMPLETED" if instance.warmup_completed_at else "PENDING",
            occurred_at=instance.warmup_completed_at,
            detail=(
                f"{instance.warmup_progress} warm-up bars accepted"
                if instance.warmup_completed_at
                else ""
            ),
            entity_type="strategy_warmup",
            entity_id=instance.pk,
        ),
        _stage(
            "WAITING_FOR_LIVE_BAR",
            "Waiting for live bar",
            status=(
                "COMPLETED"
                if instance.first_evaluation_completed_at
                else (
                    "ACTIVE"
                    if instance.state == "READY_WAITING_FOR_LIVE_BAR"
                    else "PENDING"
                )
            ),
            occurred_at=instance.ready_waiting_since,
            detail=(
                f"Live bar {live_bar.bar_id}"
                if live_bar
                else "Waiting is normal; no fixed client-side timeout is applied"
            ),
            entity_type="market_bar" if live_bar else "strategy_instance",
            entity_id=live_bar.pk if live_bar else instance.pk,
        ),
        _stage(
            "FIRST_EVALUATION_COMPLETED",
            "First evaluation completed",
            status="COMPLETED" if instance.first_evaluation_completed_at else "PENDING",
            occurred_at=instance.first_evaluation_completed_at,
            detail=f"Strategy run {run.pk}" if run else "",
            entity_type="strategy_run" if run else "",
            entity_id=run.pk if run else None,
        ),
        _stage(
            "TARGET_GENERATED",
            "Target generated",
            status="COMPLETED" if target else "PENDING",
            occurred_at=target.created_at if target else None,
            detail=(
                f"{target.direction} target weight {target.target_weight}"
                if target
                else ""
            ),
            entity_type="strategy_target" if target else "",
            entity_id=target.pk if target else None,
        ),
        _stage(
            "REBALANCE_CREATED",
            "Rebalance created",
            status="COMPLETED" if rebalance else "PENDING",
            occurred_at=rebalance.created_at if rebalance else None,
            detail=f"Automatic rebalance {rebalance.pk}" if rebalance else "",
            entity_type="rebalance_run" if rebalance else "",
            entity_id=rebalance.pk if rebalance else None,
        ),
        _stage(
            "ORDER_INTENT_CREATED",
            "Order intent created",
            status="COMPLETED" if intent else "PENDING",
            occurred_at=intent.created_at if intent else None,
            detail=(
                f"{intent.side} {intent.quantity} {instance.instrument.symbol}"
                if intent
                else ""
            ),
            entity_type="order_intent" if intent else "",
            entity_id=intent.pk if intent else None,
        ),
        _stage(
            "RISK_DECISION",
            "Risk approved or blocked",
            status=(
                "BLOCKED"
                if blocked_risk
                else ("COMPLETED" if approved_risk or order else "PENDING")
            ),
            occurred_at=(
                blocked_risk.created_at
                if blocked_risk
                else (approved_risk.created_at if approved_risk else None)
            ),
            detail=(
                f"Blocked by {blocked_risk.check_name}"
                if blocked_risk
                else (
                    f"Approved by {approved_risk.check_name}"
                    if approved_risk else ""
                )
            ),
            blocker=blocked_risk.reason if blocked_risk else "",
            entity_type="risk_check" if blocked_risk or approved_risk else "",
            entity_id=(
                blocked_risk.pk
                if blocked_risk
                else (approved_risk.pk if approved_risk else None)
            ),
        ),
        _stage(
            "BROKER_COMMAND_SENT",
            "Broker command sent",
            status=(
                "COMPLETED"
                if command and command.sent_at
                else (
                    "FAILED"
                    if command and command.status == "FAILED"
                    else ("ACTIVE" if command else "PENDING")
                )
            ),
            occurred_at=command.sent_at if command else None,
            detail=(
                f"{command.command_type} via Gateway session {command.gateway_session_id}"
                if command
                else ""
            ),
            blocker=command.last_error if command and command.status == "FAILED" else "",
            entity_type="broker_command" if command else "",
            entity_id=command.pk if command else None,
        ),
        _stage(
            "ORDER_ACKNOWLEDGED",
            "Order acknowledged",
            status=(
                "COMPLETED"
                if acknowledgement
                or (order and order.status in {"ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED"})
                else (
                    "FAILED"
                    if order and order.status in {"REJECTED", "CANCELLED", "EXPIRED"}
                    else "PENDING"
                )
            ),
            occurred_at=(
                acknowledgement.occurred_at
                if acknowledgement
                else (
                    command.acknowledged_at
                    if command and order and order.status in {
                        "ACKNOWLEDGED",
                        "PARTIALLY_FILLED",
                        "FILLED",
                    }
                    else None
                )
            ),
            detail=order.internal_id if order else "",
            blocker=(
                order.status_history.exclude(reason="")
                .order_by("-occurred_at", "-pk")
                .values_list("reason", flat=True)
                .first()
                or ""
                if order and order.status in {"REJECTED", "CANCELLED", "EXPIRED"}
                else ""
            ),
            entity_type="order" if order else "",
            entity_id=order.pk if order else None,
        ),
        _stage(
            "FILL_PROGRESS",
            "Partially filled or filled",
            status=(
                "COMPLETED"
                if order and order.status == "FILLED"
                else ("ACTIVE" if latest_fill else "PENDING")
            ),
            occurred_at=latest_fill.executed_at if latest_fill else None,
            detail=(
                f"{'Filled' if order.status == 'FILLED' else 'Partially filled'}: "
                f"{order.filled_quantity} of {order.quantity}"
                if order and latest_fill
                else ""
            ),
            entity_type="fill" if latest_fill else "",
            entity_id=latest_fill.pk if latest_fill else None,
        ),
    ]

    failed_action = action and action.status == "FAILED"
    if instance.state in {"BLOCKED", "ERROR"} and not any(
        row["status"] in {"FAILED", "BLOCKED"} for row in rows
    ):
        incomplete = next(
            (
                row
                for row in rows[1:]
                if row["status"] in {"PENDING", "ACTIVE", "NOT_STARTED"}
            ),
            rows[1],
        )
        incomplete["status"] = "FAILED"
        incomplete["blocker"] = (
            action.last_error if failed_action and action.last_error else instance.block_reason
        )
        incomplete["retryable"] = bool(action and action.retryable)

    order_terminal = bool(order and order.status in ORDER_TERMINAL_STATES)
    terminal = bool(
        (instance.state in {"DISABLED", "PAUSED", "KILLED"} and not action)
        or failed_action
        or blocked_risk
        or order_terminal
        or instance.state == "ERROR"
    )
    active = bool(
        not terminal
        and (
            (action and action.status == "PROCESSING")
            or instance.state in ACTIVATION_ACTIVE_STATES
            or (intent and intent.operation_status in {"PENDING", "CLAIMED"})
            or (order and order.status in ORDER_ACTIVE_STATES)
            or (target and not intent)
        )
    )
    current = next(
        (
            row
            for row in rows
            if row["status"] in {"ACTIVE", "FAILED", "BLOCKED", "PENDING", "NOT_STARTED"}
        ),
        rows[-1],
    )
    if failed_action or instance.state == "ERROR" or blocked_risk:
        status = "FAILED"
    elif order and order.status == "FILLED":
        status = "COMPLETED"
    elif instance.state == "DISABLED" and not action:
        status = "DISABLED"
    elif active:
        status = "ACTIVE"
    else:
        status = "IDLE"
    for row in rows:
        row.update(
            {
                "trace_id": trace_id,
                "workflow_status": status,
                "workflow_active": active,
                "workflow_terminal": terminal,
            }
        )
    return {
        "trace_id": trace_id,
        "status": status,
        "active": active,
        "terminal": terminal,
        "current_stage": current["stage"],
        "poll_after_ms": 0 if terminal else (1000 if active else 12000),
        "stages": rows,
        "observed_at": timezone.now(),
    }
