import hashlib
import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from apps.audit.models import OperationAttempt
from apps.broker_gateway.client import (
    GatewayClient,
    GatewayCommandRejected,
    GatewayError,
    GatewaySessionUnavailable,
    GatewayTransportError,
)
from apps.broker_gateway.crypto import BrokerCredentialError
from apps.core.idempotency import IdempotencyConflict, canonical_request_hash
from apps.execution.modes import (
    ExecutionMode,
    require_execution_mode_chain,
    require_live_trading_allowed,
    require_portfolio_execution_mode,
)
from apps.oms.models import Order, OrderIntent
from apps.oms.services import create_order, transition
from apps.reconciliation.models import ReconciliationBreak
from apps.risk.services import _matching_kill_switches, evaluate_intent

from .models import BrokerCommand


logger = logging.getLogger(__name__)


GATEWAY_TYPES = {
    BrokerCommand.CommandType.PLACE: "PLACE_ORDER",
    BrokerCommand.CommandType.MODIFY: "MODIFY_ORDER",
    BrokerCommand.CommandType.CANCEL: "CANCEL_ORDER",
}
ACTIVE_GATEWAY_COMMAND_STATUSES = {"PENDING", "PROCESSING", "COMPLETED"}


def _backoff(attempt_count):
    base = int(getattr(settings, "BROKER_COMMAND_RETRY_BASE_SECONDS", 5))
    cap = int(getattr(settings, "BROKER_COMMAND_RETRY_MAX_SECONDS", 300))
    return min(cap, base * (2 ** max(0, int(attempt_count) - 1)))


def _command_hash(command_type, payload):
    return canonical_request_hash(
        "broker_command", {"command_type": command_type, "payload": payload}
    )


def _command_key(prefix, internal_id, key):
    digest = hashlib.sha256(str(key).encode()).hexdigest()
    return f"broker:{prefix.lower()}:{internal_id}:{digest}"[:128]


@transaction.atomic
def enqueue_broker_command(order, command_type, payload, idempotency_key):
    order = (
        Order.objects.select_for_update(of=("self",))
        .select_related("intent__portfolio__gateway_session")
        .get(pk=order.pk)
    )
    session = order.intent.portfolio.gateway_session
    if session is None:
        raise ValueError("Order portfolio is not bound to a Gateway session")
    mode = require_portfolio_execution_mode(
        order.intent.portfolio,
        order.intent.mode,
    )
    command_type = str(command_type).upper()
    if command_type not in BrokerCommand.CommandType.values:
        raise ValueError(f"Unsupported broker command type: {command_type}")
    normalized = {**payload, "internal_id": order.internal_id}
    request_hash = _command_hash(command_type, normalized)
    command, created = BrokerCommand.objects.select_for_update().get_or_create(
        idempotency_key=idempotency_key,
        defaults={
            "order": order,
            "internal_order_id": order.internal_id,
            "gateway_session": session,
            "command_type": command_type,
            "request_payload": normalized,
            "request_hash": request_hash,
            "mode": mode,
            "next_attempt_at": timezone.now(),
        },
    )
    if not created and (
        command.order_id != order.pk
        or command.command_type != command_type
        or command.request_hash != request_hash
        or command.mode != mode
    ):
        raise IdempotencyConflict(
            "Broker command idempotency key was reused with a different request"
        )
    return command


def _place_payload(order):
    intent = order.intent
    instrument = intent.instrument
    contract = getattr(instrument, "broker_contract", None)
    payload = {
        "internal_id": order.internal_id,
        "account": intent.portfolio.account.account_id,
        "conid": contract.conid if contract else None,
        "symbol": instrument.symbol,
        "asset_class": instrument.asset_class,
        "exchange": instrument.exchange,
        "currency": instrument.currency,
        "side": intent.side,
        "quantity": str(order.quantity),
        "order_type": intent.order_type,
        "limit_price": str(intent.limit_price) if intent.limit_price else None,
        "stop_price": str(intent.stop_price) if intent.stop_price else None,
        "time_in_force": intent.time_in_force,
    }
    option = getattr(instrument, "option_contract", None)
    if instrument.asset_class == "OPT" and option is not None:
        payload.update({
            "expiration": option.expiration.isoformat(),
            "strike": str(option.strike),
            "right": option.right,
            "multiplier": str(option.multiplier),
            "trading_class": option.trading_class,
            "underlying_conid": option.underlying_conid,
            "local_symbol": contract.local_symbol if contract else "",
            "primary_exchange": contract.primary_exchange if contract else instrument.primary_exchange,
        })
    return payload


def enqueue_place_command(order):
    return enqueue_broker_command(
        order,
        BrokerCommand.CommandType.PLACE,
        _place_payload(order),
        f"broker:place:{order.internal_id}",
    )


def request_order_modification(order, changes, idempotency_key):
    return enqueue_broker_command(
        order,
        BrokerCommand.CommandType.MODIFY,
        changes,
        _command_key("modify", order.internal_id, idempotency_key),
    )


@transaction.atomic
def confirm_advanced_reject(order):
    """Atomically queue one new order using only IBKR-provided override codes."""
    order = (
        Order.objects.select_for_update(of=("self",))
        .select_related("intent")
        .get(pk=order.pk)
    )
    intent = order.intent
    if intent.origin != OrderIntent.Origin.MANUAL:
        raise ValueError("Only manual orders can be operator-confirmed")
    warning = order.status_history.filter(reason_code="201").order_by(
        "-occurred_at", "-pk"
    ).first()
    codes=(warning.details or {}).get("advanced_override_codes") if warning else []
    codes=list(dict.fromkeys(str(code).strip() for code in (codes or []) if str(code).strip()))
    if not codes:
        raise ValueError("IBKR did not provide an advanced reject override code; resubmission is unsafe")
    fingerprint=hashlib.sha256(f"{warning.event_key}:{','.join(codes)}".encode()).hexdigest()[:20]
    command_key=f"broker:advanced-confirm:{order.internal_id}:{fingerprint}"
    existing=order.broker_commands.filter(idempotency_key=command_key).first()
    if existing:
        return existing
    if intent.operation_status != "CONFIRMATION_REQUIRED":
        raise ValueError("Order does not require IBKR advanced-reject confirmation")
    payload={**_place_payload(order),"advanced_error_override":",".join(codes),
        "original_broker_order_id":order.broker_order_id}
    command = enqueue_broker_command(
        order,
        BrokerCommand.CommandType.PLACE,
        payload,
        command_key,
    )
    intent.operation_status = "CONFIRMING"
    intent.operation_error = ""
    intent.retryable = False
    intent.save(update_fields=["operation_status", "operation_error", "retryable"])
    logger.info(
        "order_execution stage=user_confirmation application_order_id=%s original_ibkr_order_id=%s warning_code=201 confirmation=accepted override_codes=%s command_id=%s",
        order.internal_id, order.broker_order_id or "", codes, command.pk,
    )
    return command


@transaction.atomic
def decline_advanced_reject(order):
    order = Order.objects.select_for_update().select_related("intent").get(pk=order.pk)
    intent = order.intent
    if intent.origin != OrderIntent.Origin.MANUAL:
        raise ValueError("Only manual orders can be operator-confirmed")
    if intent.operation_status == "USER_CANCELLED":
        return order
    if intent.operation_status != "CONFIRMATION_REQUIRED":
        raise ValueError("Order does not require IBKR advanced-reject confirmation")
    order = transition(
        order, "REJECTED", "operator",
        f"order:{order.internal_id}:advanced-confirmation-declined",
        "Operator declined the IBKR surveillance warning",
        reason_code="IBKR_CONFIRMATION_DECLINED", operator_requested=True,
    )
    intent.operation_status = "USER_CANCELLED"
    intent.operation_error = "Operator cancelled after the IBKR surveillance warning"
    intent.retryable = False
    intent.eligible = False
    intent.save(update_fields=["operation_status", "operation_error", "retryable", "eligible"])
    logger.info(
        "order_execution stage=user_confirmation internal_id=%s broker_order_id=%s confirmation=declined",
        order.internal_id, order.broker_order_id or "",
    )
    return order


@transaction.atomic
def request_order_cancellation(order, idempotency_key, reason=""):
    order = Order.objects.select_for_update().get(pk=order.pk)
    prior_status = order.status
    if order.status not in {
        "QUEUED",
        "SUBMITTED",
        "ACKNOWLEDGED",
        "PARTIALLY_FILLED",
        "UNKNOWN",
        "CANCEL_PENDING",
    }:
        raise ValueError("Order cannot be cancelled in its current state")
    if order.status != "CANCEL_PENDING":
        order = transition(
            order,
            "CANCEL_PENDING",
            "operator",
            f"order:{order.internal_id}:cancel:{idempotency_key}",
            str(reason or "")[:255],
            reason_code="OPERATOR_CANCEL_REQUEST",
            details={"operator_reason": str(reason or "")[:255]},
            operator_requested=True,
        )
    return enqueue_broker_command(
        order,
        BrokerCommand.CommandType.CANCEL,
        {
            "reason": str(reason or "")[:255],
            "prior_order_status": prior_status,
        },
        _command_key("cancel", order.internal_id, idempotency_key),
    )


@transaction.atomic
def claim_next_broker_command(now=None):
    now = now or timezone.now()
    command = (
        BrokerCommand.objects.select_for_update(skip_locked=True)
        .filter(
            Q(status=BrokerCommand.Status.PENDING)
            | Q(
                status__in=[
                    BrokerCommand.Status.RETRY,
                    BrokerCommand.Status.UNCERTAIN,
                ],
                next_attempt_at__lte=now,
            )
        )
        .order_by("next_attempt_at", "created_at", "pk")
        .first()
    )
    if command is None:
        return None
    command.status = BrokerCommand.Status.CLAIMED
    command.claimed_at = now
    command.attempt_count += 1
    command.save(
        update_fields=["status", "claimed_at", "attempt_count", "updated_at"]
    )
    return command.pk


def _set_order_unknown(command, reason):
    order = Order.objects.get(pk=command.order_id)
    if command.command_type == BrokerCommand.CommandType.CANCEL:
        return order
    if order.status != "UNKNOWN" and "UNKNOWN" in __import__(
        "apps.oms.services", fromlist=["ALLOWED"]
    ).ALLOWED.get(order.status, set()):
        return transition(
            order,
            "UNKNOWN",
            "broker_command",
            f"broker-command:{command.pk}:uncertain:{command.attempt_count}",
            reason[:255],
            reason_code="BROKER_COMMAND_UNCERTAIN",
        )
    return order


@transaction.atomic
def _mark_uncertain(command_id, reason):
    command = BrokerCommand.objects.select_for_update().get(pk=command_id)
    command.status = BrokerCommand.Status.UNCERTAIN
    command.last_error = reason[:1000]
    command.uncertainty_reason = reason[:1000]
    command.next_attempt_at = timezone.now() + timedelta(
        seconds=_backoff(command.attempt_count)
    )
    command.save(
        update_fields=[
            "status",
            "last_error",
            "uncertainty_reason",
            "next_attempt_at",
            "updated_at",
        ]
    )
    _set_order_unknown(command, reason)
    if command.command_type != BrokerCommand.CommandType.CANCEL:
        OrderIntent.objects.filter(pk=command.order.intent_id).update(
            operation_status="UNKNOWN",
            operation_error=reason[:1000],
            retryable=False,
        )
    return command


@transaction.atomic
def _mark_failed(command_id, reason, response=None):
    command = BrokerCommand.objects.select_for_update().get(pk=command_id)
    command.status = BrokerCommand.Status.FAILED
    command.last_error = str(reason)[:1000]
    command.response_payload = response or {}
    command.next_attempt_at = None
    command.save(
        update_fields=[
            "status",
            "last_error",
            "response_payload",
            "next_attempt_at",
            "updated_at",
        ]
    )
    order = command.order
    if command.command_type == BrokerCommand.CommandType.PLACE:
        if "REJECTED" in __import__(
            "apps.oms.services", fromlist=["ALLOWED"]
        ).ALLOWED.get(order.status, set()):
            transition(
                order,
                "REJECTED",
                "gateway",
                f"broker-command:{command.pk}:failed",
                command.last_error[:255],
                reason_code="GATEWAY_COMMAND_FAILED",
            )
        OrderIntent.objects.filter(pk=order.intent_id).update(
            operation_status="BROKER_REJECTED",
            operation_error=command.last_error,
            retryable=False,
        )
    elif (
        command.command_type == BrokerCommand.CommandType.CANCEL
        and order.status == "CANCEL_PENDING"
    ):
        prior = str(command.request_payload.get("prior_order_status") or "UNKNOWN")
        if prior in __import__(
            "apps.oms.services", fromlist=["ALLOWED"]
        ).ALLOWED.get(order.status, set()):
            transition(
                order,
                prior,
                "gateway",
                f"broker-command:{command.pk}:cancel-failed",
                command.last_error[:255],
                reason_code="GATEWAY_CANCEL_FAILED",
            )
    return command


@transaction.atomic
def _schedule_retry(command_id, reason, *, clear_uncertainty=False):
    command = BrokerCommand.objects.select_for_update().get(pk=command_id)
    command.status = BrokerCommand.Status.RETRY
    command.last_error = reason[:1000]
    if clear_uncertainty:
        command.uncertainty_reason = ""
    command.next_attempt_at = timezone.now() + timedelta(
        seconds=_backoff(command.attempt_count)
    )
    fields = ["status", "last_error", "next_attempt_at", "updated_at"]
    if clear_uncertainty:
        fields.append("uncertainty_reason")
    command.save(update_fields=fields)
    order = command.order
    if command.command_type == BrokerCommand.CommandType.PLACE:
        if order.status in {"QUEUED", "UNKNOWN"}:
            order = transition(
                order,
                "BROKER_BLOCKED",
                "broker_command",
                f"broker-command:{command.pk}:retry:{command.attempt_count}",
                reason[:255],
                reason_code="BROKER_COMMAND_RETRY",
            )
        OrderIntent.objects.filter(pk=order.intent_id).update(
            operation_status="BROKER_BLOCKED",
            operation_error=reason[:1000],
            retryable=True,
        )
    return command


def _final_dispatch_checks(command, client):
    order = command.order
    intent = order.intent
    session = command.gateway_session
    state = client.health() or {}
    mode = require_execution_mode_chain(
        intent_mode=intent.mode,
        command_mode=command.mode,
        session_mode=session.mode,
        health_mode=state.get("mode"),
    )
    if not state.get("connected"):
        raise GatewayError("Final dispatch requires a connected Gateway")
    if command.command_type == BrokerCommand.CommandType.CANCEL:
        return
    require_live_trading_allowed(mode)
    if (
        settings.GLOBAL_KILL_SWITCH
        or intent.portfolio.kill_switch
        or intent.portfolio.account.kill_switch
        or bool(intent.strategy_instance_id and intent.strategy_instance.kill_switch)
        or _matching_kill_switches(intent).exists()
    ):
        raise GatewayError("Final dispatch blocked by an active kill switch")
    if not intent.portfolio.account.is_reconciled or ReconciliationBreak.objects.filter(
        run__broker_account=intent.portfolio.account,
        material=True,
        resolved=False,
    ).filter(
        Q(run__gateway_session=session)
        | Q(run__gateway_session__isnull=True)
    ).exists():
        raise GatewayError("Final dispatch blocked until broker reconciliation is clean")
    if (
        not state.get("reconciled")
    ):
        raise GatewayError(
            f"Final dispatch requires a connected, reconciled {mode} Gateway"
        )


def _send(command, client):
    key = command.idempotency_key
    if command.command_type == BrokerCommand.CommandType.PLACE:
        if str(command.request_payload.get("asset_class") or "").upper() == "OPT":
            return client.place_option_order(command.request_payload, key)
        return client.place_order(command.request_payload, key)
    if command.command_type == BrokerCommand.CommandType.MODIFY:
        if command.request_payload.get("override_percentage_constraints") is True:
            logger.info(
                "order_execution stage=percentage_override_resubmission command_id=%s internal_id=%s broker_order_id=%s",
                command.pk, command.internal_order_id, command.order.broker_order_id or "",
            )
        payload = {
            key: value
            for key, value in command.request_payload.items()
            if key != "internal_id"
        }
        return client.modify_order(command.internal_order_id, payload, key)
    return client.cancel_order(command.internal_order_id, key)


@transaction.atomic
def _begin_sending(command_id):
    command = BrokerCommand.objects.select_for_update().get(pk=command_id)
    if command.status != BrokerCommand.Status.CLAIMED:
        return False
    order = command.order
    if (
        command.command_type == BrokerCommand.CommandType.PLACE
        and order.status in {"BROKER_BLOCKED", "UNKNOWN"}
    ):
        transition(
            order,
            "QUEUED",
            "broker_command",
            f"broker-command:{command.pk}:sending:{command.attempt_count}",
            reason_code="BROKER_COMMAND_SENDING",
        )
    command.status = BrokerCommand.Status.SENDING
    command.sent_at = timezone.now()
    command.save(update_fields=["status", "sent_at", "updated_at"])
    return True


def _broker_identity(payload):
    payload = payload or {}
    return (
        str(payload.get("broker_order_id") or ""),
        str(payload.get("permanent_id") or payload.get("broker_permanent_id") or ""),
    )


@transaction.atomic
def _acknowledge(command_id, response, *, recovered=False):
    command = (
        BrokerCommand.objects.select_for_update()
        .select_related("order__intent")
        .get(pk=command_id)
    )
    now = timezone.now()
    broker_order_id, permanent_id = _broker_identity(response)
    command.status = BrokerCommand.Status.ACKNOWLEDGED
    command.acknowledged_at = command.acknowledged_at or now
    command.next_attempt_at = None
    command.response_payload = response or {}
    command.last_error = ""
    command.uncertainty_reason = ""
    command.broker_order_id = broker_order_id or command.broker_order_id
    command.broker_permanent_id = permanent_id or command.broker_permanent_id
    gateway_command_id = (response or {}).get("command_id")
    if gateway_command_id is not None:
        command.gateway_command_id = int(gateway_command_id)
    command.save(
        update_fields=[
            "status",
            "acknowledged_at",
            "next_attempt_at",
            "response_payload",
            "last_error",
            "uncertainty_reason",
            "broker_order_id",
            "broker_permanent_id",
            "gateway_command_id",
            "updated_at",
        ]
    )
    order = command.order
    order_updates = []
    if command.broker_order_id and order.broker_order_id != command.broker_order_id:
        order.broker_order_id = command.broker_order_id
        order_updates.append("broker_order_id")
    if (
        command.broker_permanent_id
        and order.broker_permanent_id != command.broker_permanent_id
    ):
        order.broker_permanent_id = command.broker_permanent_id
        order_updates.append("broker_permanent_id")
    if order_updates:
        order.save(update_fields=[*order_updates, "updated_at"])
    if command.command_type == BrokerCommand.CommandType.PLACE:
        target = "ACKNOWLEDGED" if recovered and (
            command.broker_order_id or command.broker_permanent_id
        ) else "SUBMITTED"
        if target in __import__("apps.oms.services", fromlist=["ALLOWED"]).ALLOWED.get(
            order.status, set()
        ):
            order = transition(
                order,
                target,
                "broker_command",
                f"broker-command:{command.pk}:acknowledged",
                reason_code="GATEWAY_COMMAND_ACKNOWLEDGED",
                details={"gateway_command_id": command.gateway_command_id},
            )
        OrderIntent.objects.filter(pk=order.intent_id).update(
            operation_status="QUEUED", operation_error="", retryable=False
        )
    elif (
        command.command_type == BrokerCommand.CommandType.MODIFY
        and command.request_payload.get("override_percentage_constraints") is True
    ):
        if "SUBMITTED" in __import__("apps.oms.services", fromlist=["ALLOWED"]).ALLOWED.get(
            order.status, set()
        ):
            order = transition(
                order, "SUBMITTED", "broker_command",
                f"broker-command:{command.pk}:percentage-override-acknowledged",
                reason_code="IBKR_PERCENTAGE_OVERRIDE_SUBMITTED",
                details={"gateway_command_id": command.gateway_command_id},
                operator_requested=True,
            )
        OrderIntent.objects.filter(pk=order.intent_id).update(
            operation_status="QUEUED", operation_error="", retryable=False
        )
        logger.info(
            "order_execution stage=percentage_override_acknowledged command_id=%s internal_id=%s broker_order_id=%s",
            command.pk, command.internal_order_id,
            command.broker_order_id or order.broker_order_id or "",
        )
    return command


def _matching_gateway_command(command, state):
    expected_type = GATEWAY_TYPES[command.command_type]
    rows = [
        row
        for row in (state.get("commands") or [])
        if row.get("command_type") == expected_type
    ]
    if command.gateway_command_id:
        exact = [
            row
            for row in rows
            if int(row.get("command_id") or 0) == command.gateway_command_id
        ]
        if exact:
            return exact[-1]
    return rows[-1] if rows else None


def reconcile_uncertain_command(command_id, client=None):
    command = BrokerCommand.objects.select_related(
        "gateway_session", "order__intent__portfolio__account"
    ).get(pk=command_id)
    client = client or GatewayClient(command.gateway_session, purpose="read")
    try:
        state = client.order_state(command.internal_order_id) or {}
    except (GatewayError, BrokerCredentialError) as exc:
        _mark_uncertain(
            command.pk,
            f"Gateway state unavailable during uncertain-command reconciliation: {exc}",
        )
        return "UNCERTAIN"
    broker_order = state.get("broker_order") or {}
    reference = state.get("reference") or {}
    matched = _matching_gateway_command(command, state)
    if broker_order or reference.get("broker_order_id") or reference.get(
        "permanent_id"
    ):
        result = {**reference, **broker_order, "recovered": True}
        _acknowledge(command.pk, result, recovered=True)
        return "ACKNOWLEDGED"
    if matched and matched.get("status") in ACTIVE_GATEWAY_COMMAND_STATUSES:
        result = {
            **(matched.get("result") or {}),
            "command_id": matched.get("command_id"),
            "status": matched.get("status"),
            "recovered": True,
        }
        _acknowledge(command.pk, result, recovered=True)
        return "ACKNOWLEDGED"
    if not state.get("non_submission_established", False):
        _mark_uncertain(
            command.pk,
            "Gateway or broker still cannot establish whether the command was submitted",
        )
        return "UNCERTAIN"
    _schedule_retry(
        command.pk,
        "Gateway and broker state established that no submission occurred",
        clear_uncertainty=True,
    )
    return "RETRY"


def dispatch_broker_command(command_id, client=None):
    command = BrokerCommand.objects.select_related(
        "gateway_session",
        "order__intent__portfolio__account",
        "order__intent__instrument",
        "order__intent__strategy_instance",
    ).get(pk=command_id)
    logger.info(
        "order_execution stage=broker_command_loaded command_id=%s intent_id=%s internal_id=%s command_type=%s status=%s attempt=%s",
        command.pk, command.order.intent_id, command.internal_order_id,
        command.command_type, command.status, command.attempt_count,
    )
    if command.status != BrokerCommand.Status.CLAIMED:
        logger.info(
            "order_execution stage=broker_command_skipped command_id=%s status=%s",
            command.pk, command.status,
        )
        return command.status
    if command.uncertainty_reason:
        logger.info(
            "order_execution stage=broker_reconciliation_start command_id=%s internal_id=%s",
            command.pk, command.internal_order_id,
        )
        return reconcile_uncertain_command(command.pk, client=client)
    try:
        logger.info(
            "order_execution stage=final_dispatch_checks_start command_id=%s internal_id=%s",
            command.pk, command.internal_order_id,
        )
        client = client or GatewayClient(command.gateway_session, purpose="command")
        _final_dispatch_checks(command, client)
        logger.info(
            "order_execution stage=final_dispatch_checks_succeeded command_id=%s internal_id=%s",
            command.pk, command.internal_order_id,
        )
    except (GatewayError, BrokerCredentialError, ValueError) as exc:
        _schedule_retry(command.pk, str(exc))
        logger.warning(
            "order_execution stage=final_dispatch_checks_failed command_id=%s internal_id=%s error_type=%s error=%s",
            command.pk, command.internal_order_id, type(exc).__name__, str(exc),
        )
        return "RETRY"
    if not _begin_sending(command.pk):
        status = BrokerCommand.objects.get(pk=command.pk).status
        logger.warning(
            "order_execution stage=broker_send_not_started command_id=%s internal_id=%s status=%s",
            command.pk, command.internal_order_id, status,
        )
        return status
    try:
        logger.info(
            "order_execution stage=gateway_send_start command_id=%s internal_id=%s command_type=%s",
            command.pk, command.internal_order_id, command.command_type,
        )
        response = _send(command, client)
    except GatewayCommandRejected as exc:
        _mark_failed(command.pk, str(exc))
        logger.warning(
            "order_execution stage=gateway_send_rejected command_id=%s internal_id=%s error_type=%s error=%s",
            command.pk, command.internal_order_id, type(exc).__name__, str(exc),
        )
        return "FAILED"
    except GatewayTransportError as exc:
        _mark_uncertain(
            command.pk,
            f"Gateway transport outcome is uncertain after send began: {exc}",
        )
        logger.error(
            "order_execution stage=gateway_send_transport_failed command_id=%s internal_id=%s error_type=%s error=%s",
            command.pk, command.internal_order_id, type(exc).__name__, str(exc),
        )
        return "UNCERTAIN"
    except GatewayError as exc:
        _mark_uncertain(
            command.pk,
            f"Gateway rejected or lost the response after send began: {exc}",
        )
        logger.error(
            "order_execution stage=gateway_send_failed command_id=%s internal_id=%s error_type=%s error=%s",
            command.pk, command.internal_order_id, type(exc).__name__, str(exc),
        )
        return "UNCERTAIN"
    except Exception:
        logger.exception(
            "order_execution stage=gateway_send_unexpected_error command_id=%s internal_id=%s",
            command.pk, command.internal_order_id,
        )
        raise
    _acknowledge(command.pk, response or {})
    acknowledged = BrokerCommand.objects.get(pk=command.pk)
    logger.info(
        "order_execution stage=broker_command_acknowledged command_id=%s internal_id=%s broker_order_id=%s gateway_command_id=%s",
        command.pk, command.internal_order_id,
        acknowledged.broker_order_id or "", acknowledged.gateway_command_id or "",
    )
    return "ACKNOWLEDGED"


def process_broker_commands(limit=None):
    limit = int(limit or getattr(settings, "BROKER_COMMAND_BATCH_SIZE", 50))
    results = {
        "claimed": 0,
        "acknowledged": 0,
        "retry": 0,
        "uncertain": 0,
        "failed": 0,
    }
    for _ in range(limit):
        command_id = claim_next_broker_command()
        if command_id is None:
            break
        results["claimed"] += 1
        outcome = dispatch_broker_command(command_id).lower()
        if outcome in results:
            results[outcome] += 1
    return results


@transaction.atomic
def recover_stuck_broker_commands(now=None):
    now = now or timezone.now()
    cutoff = now - timedelta(
        seconds=int(getattr(settings, "BROKER_COMMAND_CLAIM_TIMEOUT_SECONDS", 120))
    )
    claimed = list(
        BrokerCommand.objects.select_for_update()
        .filter(status=BrokerCommand.Status.CLAIMED, claimed_at__lte=cutoff)
        .order_by("pk")
    )
    for command in claimed:
        command.status = BrokerCommand.Status.PENDING
        command.claimed_at = None
        command.next_attempt_at = now
        command.last_error = "Recovered command whose worker stopped before send"
        command.save(
            update_fields=[
                "status",
                "claimed_at",
                "next_attempt_at",
                "last_error",
                "updated_at",
            ]
        )
    sending = list(
        BrokerCommand.objects.select_for_update()
        .filter(status=BrokerCommand.Status.SENDING, sent_at__lte=cutoff)
        .order_by("pk")
    )
    for command in sending:
        command.status = BrokerCommand.Status.UNCERTAIN
        command.uncertainty_reason = (
            "Recovered command whose worker stopped after send began"
        )
        command.last_error = command.uncertainty_reason
        command.next_attempt_at = now
        command.save(
            update_fields=[
                "status",
                "uncertainty_reason",
                "last_error",
                "next_attempt_at",
                "updated_at",
            ]
        )
        _set_order_unknown(command, command.uncertainty_reason)
    return {"claimed": len(claimed), "sending": len(sending)}


@transaction.atomic
def claim_next_order_intent(exclude_ids=None):
    candidates = (
        OrderIntent.objects.select_for_update(skip_locked=True)
        .annotate(
            _has_order=Exists(
                Order.objects.filter(intent_id=OuterRef("pk"))
            )
        )
        .filter(
            operation_status="PENDING",
            eligible=True,
            mode__in=ExecutionMode.values,
            _has_order=False,
        )
        .order_by("execution_priority", "created_at", "pk")
    )
    if exclude_ids:
        candidates = candidates.exclude(pk__in=exclude_ids)
    intent = candidates.first()
    if intent is None:
        return None
    if OperationAttempt.objects.filter(
        operation_type="ORDER_INTENT",
        operation_id=str(intent.pk),
        attempt_number=intent.attempt_count,
    ).exists():
        intent.attempt_count += 1
    intent.operation_status = "CLAIMED"
    intent.save(update_fields=["operation_status", "attempt_count"])
    OperationAttempt.objects.create(
        operation_type="ORDER_INTENT",
        operation_id=str(intent.pk),
        attempt_number=intent.attempt_count,
        request_hash=intent.request_hash,
    )
    return intent.pk


@transaction.atomic
def recover_stuck_order_intents(now=None):
    """Return intents to the queue when a worker died after claiming them."""
    now = now or timezone.now()
    cutoff = now - timedelta(
        seconds=int(getattr(settings, "ORDER_INTENT_CLAIM_TIMEOUT_SECONDS", 120))
    )
    attempts = list(
        OperationAttempt.objects.select_for_update()
        .filter(
            operation_type="ORDER_INTENT",
            status="PROCESSING",
            started_at__lte=cutoff,
        )
        .order_by("started_at", "pk")
    )
    recovered = 0
    for attempt in attempts:
        try:
            intent_id = int(attempt.operation_id)
        except (TypeError, ValueError):
            attempt.status = "FAILED"
            attempt.retryable = False
            attempt.error = "Invalid order-intent operation identifier"
            attempt.completed_at = now
            attempt.save(
                update_fields=[
                    "status",
                    "retryable",
                    "error",
                    "completed_at",
                ]
            )
            continue
        intent = (
            OrderIntent.objects.select_for_update()
            .filter(
                pk=intent_id,
                operation_status="CLAIMED",
            )
            .first()
        )
        if intent is None or Order.objects.filter(intent_id=intent_id).exists():
            continue
        intent.operation_status = "PENDING"
        intent.operation_error = "Recovered intent whose worker lease expired"
        intent.retryable = True
        intent.save(
            update_fields=[
                "operation_status",
                "operation_error",
                "retryable",
            ]
        )
        attempt.status = "FAILED"
        attempt.retryable = True
        attempt.error = intent.operation_error
        attempt.completed_at = now
        attempt.save(
            update_fields=[
                "status",
                "retryable",
                "error",
                "completed_at",
            ]
        )
        recovered += 1
    return recovered


def execute_order_intent(intent_id):
    logger.info("order_execution stage=intent_execution_start intent_id=%s", intent_id)
    with transaction.atomic():
        claimed = OrderIntent.objects.select_for_update().get(pk=intent_id)
        if claimed.operation_status == "PENDING":
            if OperationAttempt.objects.filter(
                operation_type="ORDER_INTENT",
                operation_id=str(claimed.pk),
                attempt_number=claimed.attempt_count,
            ).exists():
                claimed.attempt_count += 1
            claimed.operation_status = "CLAIMED"
            claimed.save(update_fields=["operation_status", "attempt_count"])
            OperationAttempt.objects.create(
                operation_type="ORDER_INTENT",
                operation_id=str(claimed.pk),
                attempt_number=claimed.attempt_count,
                request_hash=claimed.request_hash,
            )
    intent = OrderIntent.objects.select_related(
        "portfolio__account",
        "portfolio__gateway_session",
        "instrument",
        "strategy_instance",
    ).get(pk=intent_id)
    logger.info(
        "order_execution stage=intent_loaded intent_id=%s status=%s attempt=%s portfolio_id=%s instrument_id=%s mode=%s",
        intent.pk, intent.operation_status, intent.attempt_count,
        intent.portfolio_id, intent.instrument_id, intent.mode,
    )
    if intent.operation_status not in {"CLAIMED", "PENDING"}:
        if hasattr(intent, "order"):
            command = enqueue_place_command(intent.order)
            logger.info(
                "order_execution stage=intent_idempotent_command_ready intent_id=%s internal_id=%s command_id=%s",
                intent.pk, intent.order.internal_id, command.pk,
            )
            return command
        logger.info(
            "order_execution stage=intent_skipped intent_id=%s status=%s reason=terminal_or_ineligible_state",
            intent.pk, intent.operation_status,
        )
        return None
    if not intent.eligible:
        logger.warning(
            "order_execution stage=intent_skipped intent_id=%s status=%s reason=not_eligible",
            intent.pk, intent.operation_status,
        )
        return None
    try:
        logger.info("order_execution stage=execution_mode_check_start intent_id=%s", intent.pk)
        require_portfolio_execution_mode(intent.portfolio, intent.mode)
        logger.info("order_execution stage=execution_mode_check_succeeded intent_id=%s", intent.pk)
    except ValueError as exc:
        OrderIntent.objects.filter(pk=intent.pk).update(
            operation_status="FAILED",
            operation_error=str(exc)[:1000],
            retryable=False,
            eligible=False,
        )
        OperationAttempt.objects.filter(
            operation_type="ORDER_INTENT",
            operation_id=str(intent.pk),
            attempt_number=intent.attempt_count,
        ).update(
            status="FAILED",
            retryable=False,
            error=str(exc)[:1000],
            completed_at=timezone.now(),
        )
        logger.warning(
            "order_execution stage=execution_mode_check_failed intent_id=%s error_type=%s error=%s",
            intent.pk, type(exc).__name__, str(exc),
        )
        return None
    try:
        logger.info("order_execution stage=gateway_health_start intent_id=%s", intent.pk)
        state = GatewayClient.for_portfolio(intent.portfolio).health()
        logger.info(
            "order_execution stage=gateway_health_succeeded intent_id=%s connected=%s reconciled=%s mode=%s",
            intent.pk, bool((state or {}).get("connected")),
            bool((state or {}).get("reconciled")), (state or {}).get("mode") or "",
        )
    except GatewaySessionUnavailable as exc:
        OrderIntent.objects.filter(pk=intent.pk).update(
            operation_status="FAILED",
            operation_error=str(exc)[:1000],
            retryable=False,
            eligible=False,
        )
        OperationAttempt.objects.filter(
            operation_type="ORDER_INTENT",
            operation_id=str(intent.pk),
            attempt_number=intent.attempt_count,
        ).update(
            status="FAILED",
            retryable=False,
            error=str(exc)[:1000],
            completed_at=timezone.now(),
        )
        logger.warning(
            "order_execution stage=gateway_session_unavailable intent_id=%s error=%s retryable=false",
            intent.pk, str(exc),
        )
        return None
    except (GatewayError, BrokerCredentialError) as exc:
        OrderIntent.objects.filter(pk=intent.pk).update(
            operation_status="PENDING",
            operation_error=str(exc)[:1000],
            retryable=True,
        )
        OperationAttempt.objects.filter(
            operation_type="ORDER_INTENT",
            operation_id=str(intent.pk),
            attempt_number=intent.attempt_count,
        ).update(
            status="FAILED",
            retryable=True,
            error=str(exc)[:1000],
            completed_at=timezone.now(),
        )
        logger.warning(
            "order_execution stage=gateway_health_failed intent_id=%s error_type=%s error=%s retryable=true",
            intent.pk, type(exc).__name__, str(exc),
        )
        return None
    try:
        with transaction.atomic():
            intent = OrderIntent.objects.select_for_update().get(pk=intent.pk)
            logger.info("order_execution stage=risk_check_start intent_id=%s", intent.pk)
            decision, approved, checks = evaluate_intent(intent, state)
            logger.info(
                "order_execution stage=risk_check_completed intent_id=%s decision=%s requested_quantity=%s approved_quantity=%s checks=%s",
                intent.pk, decision, intent.quantity, approved, len(checks),
            )
            if decision not in {"APPROVED", "RESIZED"}:
                retryable = decision == "HELD"
                intent.operation_status = "PENDING" if retryable else "RISK_REJECTED"
                intent.operation_error = (
                    checks[-1].reason if checks else "Order did not pass pre-trade risk"
                )
                intent.retryable = retryable
                intent.save(
                    update_fields=[
                        "operation_status",
                        "operation_error",
                        "retryable",
                    ]
                )
                OperationAttempt.objects.filter(
                    operation_type="ORDER_INTENT",
                    operation_id=str(intent.pk),
                    attempt_number=intent.attempt_count,
                ).update(
                    status="FAILED",
                    retryable=retryable,
                    error=intent.operation_error,
                    completed_at=timezone.now(),
                )
                logger.warning(
                    "order_execution stage=risk_check_blocked intent_id=%s decision=%s retryable=%s error=%s",
                    intent.pk, decision, retryable, intent.operation_error,
                )
                return None
            logger.info("order_execution stage=oms_order_create_start intent_id=%s", intent.pk)
            order = create_order(intent, approved)
            logger.info(
                "order_execution stage=oms_order_created intent_id=%s internal_id=%s status=%s quantity=%s",
                intent.pk, order.internal_id, order.status, order.quantity,
            )
            order = transition(
                order,
                "QUEUED",
                "oms",
                f"order:{order.internal_id}:queued",
            )
            logger.info(
                "order_execution stage=broker_command_create_start intent_id=%s internal_id=%s",
                intent.pk, order.internal_id,
            )
            command = enqueue_place_command(order)
            logger.info(
                "order_execution stage=broker_command_created intent_id=%s internal_id=%s command_id=%s status=%s",
                intent.pk, order.internal_id, command.pk, command.status,
            )
            intent.operation_status = "QUEUED"
            intent.operation_error = ""
            intent.retryable = False
            intent.save(
                update_fields=[
                    "operation_status",
                    "operation_error",
                    "retryable",
                ]
            )
            OperationAttempt.objects.filter(
                operation_type="ORDER_INTENT",
                operation_id=str(intent.pk),
                attempt_number=intent.attempt_count,
            ).update(
                status="COMPLETED",
                retryable=False,
                result={"order_id": order.internal_id, "broker_command_id": command.pk},
                completed_at=timezone.now(),
            )
            logger.info(
                "order_execution stage=intent_execution_completed intent_id=%s internal_id=%s command_id=%s",
                intent.pk, order.internal_id, command.pk,
            )
            return command
    except Exception:
        logger.exception(
            "order_execution stage=intent_execution_unexpected_error intent_id=%s",
            intent_id,
        )
        raise


def process_order_intents(limit=None):
    limit = int(limit or getattr(settings, "ORDER_INTENT_BATCH_SIZE", 50))
    result = {"claimed": 0, "commands_created": 0}
    attempted = set()
    for _ in range(limit):
        intent_id = claim_next_order_intent(exclude_ids=attempted)
        if intent_id is None:
            break
        attempted.add(intent_id)
        result["claimed"] += 1
        if execute_order_intent(intent_id) is not None:
            result["commands_created"] += 1
    return result


def _command_for_gateway_event(payload, gateway_session):
    command_type = {
        "PLACE_ORDER": BrokerCommand.CommandType.PLACE,
        "MODIFY_ORDER": BrokerCommand.CommandType.MODIFY,
        "CANCEL_ORDER": BrokerCommand.CommandType.CANCEL,
    }.get(str(payload.get("command_type") or ""))
    internal_id = str((payload.get("payload") or {}).get("internal_id") or "")
    if not command_type or not internal_id:
        return None
    query = BrokerCommand.objects.filter(
        gateway_session=gateway_session,
        internal_order_id=internal_id,
        command_type=command_type,
    )
    gateway_command_id = payload.get("command_id")
    if gateway_command_id is not None:
        exact = query.filter(gateway_command_id=gateway_command_id).first()
        if exact:
            return exact
    return query.order_by("-pk").first()


def record_gateway_command_completed(payload, gateway_session):
    command = _command_for_gateway_event(payload, gateway_session)
    if command is None:
        return None
    result = {
        key: value
        for key, value in payload.items()
        if key not in {"command_type", "payload"}
    }
    result["command_id"] = payload.get("command_id")
    return _acknowledge(command.pk, result)


@transaction.atomic
def record_gateway_command_failed(payload, gateway_session):
    command = _command_for_gateway_event(payload, gateway_session)
    if command is None:
        return None
    reason = str(payload.get("error") or "Gateway broker command failed")[:1000]
    if payload.get("retryable"):
        return _mark_uncertain(command.pk, reason)
    return _mark_failed(command.pk, reason, payload)


def record_broker_order_observation(order, payload):
    command = order.broker_commands.filter(
        command_type=BrokerCommand.CommandType.PLACE
    ).order_by("-pk").first()
    if command is None:
        return None
    result = {
        **(payload or {}),
        "broker_order_id": str(
            (payload or {}).get("broker_order_id") or order.broker_order_id or ""
        ),
        "permanent_id": str(
            (payload or {}).get("permanent_id")
            or order.broker_permanent_id
            or ""
        ),
        "recovered": command.status == BrokerCommand.Status.UNCERTAIN,
    }
    return _acknowledge(
        command.pk,
        result,
        recovered=command.status == BrokerCommand.Status.UNCERTAIN,
    )


def command_summary(command):
    return {
        "id": command.pk,
        "command_type": command.command_type,
        "status": command.status,
        "attempt_count": command.attempt_count,
        "gateway_command_id": command.gateway_command_id,
        "broker_order_id": command.broker_order_id,
        "broker_permanent_id": command.broker_permanent_id,
        "last_error": command.last_error,
        "uncertainty_reason": command.uncertainty_reason,
    }
