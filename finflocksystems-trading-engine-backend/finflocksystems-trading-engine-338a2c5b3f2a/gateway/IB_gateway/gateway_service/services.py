import hashlib
import json
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    GatewayCommand,
    GatewayCommandAttempt,
    GatewayEvent,
    GatewayHealthSnapshot,
    GatewayOrderReference,
    GatewaySession,
)


ORDER_COMMANDS = {"PLACE_ORDER", "PLACE_OPTION_ORDER", "MODIFY_ORDER", "CANCEL_ORDER"}


def compact_gateway_operational_records(now=None):
    from django.conf import settings
    now=now or timezone.now();limit=settings.GATEWAY_COMPACTION_BATCH_SIZE
    rules=[
        ("acknowledged_events",GatewayEvent.objects.filter(acknowledged=True,
            created_at__lt=now-timedelta(days=settings.GATEWAY_EVENT_RETENTION_DAYS))),
        ("health_snapshots",GatewayHealthSnapshot.objects.filter(
            created_at__lt=now-timedelta(days=settings.GATEWAY_HEALTH_RETENTION_DAYS))),
    ]
    deleted={}
    for name,query in rules:
        ids=list(query.order_by("pk").values_list("pk",flat=True)[:limit])
        count,_=query.model.objects.filter(pk__in=ids).delete()
        deleted[name]=count
    return deleted


class IdempotencyConflict(ValueError):
    pass


class CommandRetryNotAllowed(ValueError):
    pass


def _canonical(value):
    if isinstance(value, dict):
        return {str(key): _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float, Decimal)):
        number = Decimal(str(value))
        normalized = format(number.normalize(), "f")
        return "0" if normalized in {"-0", ""} else normalized
    return str(value)


def canonical_request_hash(command_type, payload):
    body = json.dumps(
        {"command_type": str(command_type), "payload": _canonical(payload)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(body.encode()).hexdigest()


@transaction.atomic
def enqueue(command_type, payload, idempotency_key, *, retry_failed=False):
    expected_hash = canonical_request_hash(command_type, payload)
    command,created = GatewayCommand.objects.select_for_update().get_or_create(idempotency_key=idempotency_key,
        defaults={"command_type":command_type,"request_hash":expected_hash,"payload":payload})
    if created:return command
    stored_hash = command.request_hash or canonical_request_hash(command.command_type, command.payload)
    if not command.request_hash:
        command.request_hash = stored_hash
        command.save(update_fields=["request_hash", "updated_at"])
    if stored_hash != expected_hash:
        raise IdempotencyConflict("Idempotency-Key was already used for a different gateway command")
    if retry_failed:
        if command.status != "FAILED" or not command.retryable:
            raise CommandRetryNotAllowed("The stored gateway command is not eligible for retry")
        command.status = "PENDING"
        command.retryable = False
        command.claimed_by = ""
        command.claimed_at = None
        command.lease_expires_at = None
        command.completed_at = None
        command.last_error = ""
        command.save(update_fields=[
            "status", "retryable", "claimed_by", "claimed_at", "lease_expires_at",
            "completed_at", "last_error", "updated_at",
        ])
    return command


def persist_event(event_key, event_type, payload):
    event, _ = GatewayEvent.objects.get_or_create(
        event_key=event_key, defaults={"event_type": event_type, "payload": payload}
    )
    GatewaySession.objects.update_or_create(pk=1, defaults={"last_callback_at": timezone.now()})
    return event


def _claim_locked(command, claimed_by, now, lease_seconds):
    command.status = "PROCESSING"
    command.claimed_by = claimed_by
    command.claimed_at = now
    command.lease_expires_at = now + timedelta(seconds=lease_seconds)
    command.attempt_count += 1
    command.last_error = ""
    command.save(update_fields=[
        "status", "claimed_by", "claimed_at", "lease_expires_at", "attempt_count",
        "last_error", "updated_at",
    ])
    GatewayCommandAttempt.objects.create(
        command=command,
        attempt_number=command.attempt_count,
        claimed_by=claimed_by,
    )
    return command


@transaction.atomic
def claim_next_command(claimed_by, *, lease_seconds=30, now=None):
    now = now or timezone.now()
    command = (
        GatewayCommand.objects.select_for_update()
        .filter(status="PENDING")
        .order_by("id")
        .first()
    )
    if command is None:
        return None
    return _claim_locked(command, claimed_by, now, lease_seconds)


@transaction.atomic
def claim_command(command, claimed_by="direct", *, lease_seconds=30):
    command = GatewayCommand.objects.select_for_update().get(pk=command.pk)
    if command.status == "PROCESSING" and command.lease_expires_at and command.lease_expires_at > timezone.now():
        return command
    if command.status != "PENDING":
        raise ValueError(f"Gateway command in {command.status} cannot be claimed")
    return _claim_locked(command, claimed_by, timezone.now(), lease_seconds)


def _find_broker_order(adapter, internal_id):
    state = adapter.refresh_state() or {}
    for row in [*(state.get("open_orders") or []), *(state.get("completed_orders") or [])]:
        if str(row.get("internal_id") or "") == str(internal_id):
            return row
    return None


def _same_requested_values(command, row):
    for key in ("quantity", "limit_price", "stop_price", "time_in_force"):
        if key in command.payload and command.payload[key] is not None:
            if _canonical(command.payload[key]) != _canonical(row.get(key)):
                return False
    return True


@transaction.atomic
def _finish_recovery(command_id, attempt_id, result):
    command = GatewayCommand.objects.select_for_update().get(pk=command_id)
    attempt = GatewayCommandAttempt.objects.select_for_update().get(pk=attempt_id)
    command.result = result
    command.status = "COMPLETED"
    command.retryable = False
    command.last_error = ""
    command.lease_expires_at = None
    command.completed_at = timezone.now()
    command.save(update_fields=[
        "result", "status", "retryable", "last_error", "lease_expires_at", "completed_at", "updated_at",
    ])
    attempt.submission_state = "RECOVERED"
    attempt.broker_result = result
    attempt.completed_at = timezone.now()
    attempt.save(update_fields=["submission_state", "broker_result", "completed_at"])
    if command.command_type in ORDER_COMMANDS:
        GatewayOrderReference.objects.update_or_create(
            internal_id=command.payload["internal_id"],
            defaults={
                "broker_order_id": str(result.get("broker_order_id", "")),
                "permanent_id": str(result.get("permanent_id", "")),
                "last_status": str(result.get("status", "")),
            },
        )
    persist_event(
        f"command:{command.pk}:completed",
        f"command.{command.command_type.lower()}.completed",
        {
            "command_id": command.pk,
            "command_type": command.command_type,
            "payload": command.payload,
            **result,
        },
    )


@transaction.atomic
def _expire_without_submission(command_id, attempt_id):
    command = GatewayCommand.objects.select_for_update().get(pk=command_id)
    attempt = GatewayCommandAttempt.objects.select_for_update().get(pk=attempt_id)
    command.status = "PENDING"
    command.claimed_by = ""
    command.claimed_at = None
    command.lease_expires_at = None
    command.last_error = "Worker lease expired before broker submission"
    command.save(update_fields=[
        "status", "claimed_by", "claimed_at", "lease_expires_at", "last_error", "updated_at",
    ])
    attempt.submission_state = "EXPIRED_BEFORE_SUBMISSION"
    attempt.error = command.last_error
    attempt.completed_at = timezone.now()
    attempt.save(update_fields=["submission_state", "error", "completed_at"])


@transaction.atomic
def _mark_unknown(command_id, attempt_id, reason):
    command = GatewayCommand.objects.select_for_update().get(pk=command_id)
    attempt = GatewayCommandAttempt.objects.select_for_update().get(pk=attempt_id)
    command.status = "UNKNOWN"
    command.retryable = False
    command.last_error = reason[:1000]
    command.lease_expires_at = None
    command.completed_at = timezone.now()
    command.save(update_fields=[
        "status", "retryable", "last_error", "lease_expires_at", "completed_at", "updated_at",
    ])
    attempt.submission_state = "UNKNOWN"
    attempt.error = reason[:1000]
    attempt.completed_at = timezone.now()
    attempt.save(update_fields=["submission_state", "error", "completed_at"])


def recover_expired_commands(adapter, *, now=None):
    now = now or timezone.now()
    expired = list(
        GatewayCommand.objects.filter(status="PROCESSING", lease_expires_at__lte=now)
        .order_by("id")
        .values_list("id", flat=True)
    )
    recovered = 0
    for command_id in expired:
        command = GatewayCommand.objects.get(pk=command_id)
        attempt = command.attempt_history.order_by("-attempt_number").first()
        if attempt is None or attempt.submission_state == "CLAIMED":
            if attempt is None:
                attempt = GatewayCommandAttempt.objects.create(
                    command=command,
                    attempt_number=command.attempt_count or 1,
                    claimed_by=command.claimed_by or "expired-worker",
                )
            _expire_without_submission(command.pk, attempt.pk)
            recovered += 1
            continue
        if command.command_type not in ORDER_COMMANDS:
            _expire_without_submission(command.pk, attempt.pk)
            recovered += 1
            continue
        result = None
        reference = GatewayOrderReference.objects.filter(internal_id=command.payload.get("internal_id")).first()
        if command.command_type in {"PLACE_ORDER","PLACE_OPTION_ORDER"} and reference and (reference.broker_order_id or reference.permanent_id):
            result = {
                **command.payload,
                "broker_order_id": reference.broker_order_id,
                "permanent_id": reference.permanent_id,
                "status": reference.last_status,
            }
        else:
            broker_order = _find_broker_order(adapter, command.payload.get("internal_id"))
            if command.command_type in {"PLACE_ORDER","PLACE_OPTION_ORDER"} and broker_order:
                result = broker_order
            elif command.command_type == "MODIFY_ORDER" and broker_order and _same_requested_values(command, broker_order):
                result = broker_order
            elif command.command_type == "CANCEL_ORDER" and broker_order and str(broker_order.get("status", "")).upper() in {"CANCELLED", "APICANCELLED"}:
                result = broker_order
        if result is not None:
            _finish_recovery(command.pk, attempt.pk, result)
        else:
            _mark_unknown(
                command.pk,
                attempt.pk,
                "Broker submission outcome could not be confirmed; command was not resubmitted",
            )
        recovered += 1
    return recovered


def _current_attempt(command):
    return GatewayCommandAttempt.objects.get(
        command=command, attempt_number=command.attempt_count
    )


def process_command(command, adapter):
    supplied_command = command
    command.refresh_from_db()
    if command.status != "PROCESSING":
        command = claim_command(command)
    attempt = _current_attempt(command)
    submission_state = "SUBMITTING" if command.command_type in ORDER_COMMANDS else "EXECUTING"
    GatewayCommandAttempt.objects.filter(pk=attempt.pk).update(submission_state=submission_state)

    if command.command_type == "RECONNECT":
        if adapter.is_connected():
            adapter.disconnect()
        result = adapter.connect()
        state = adapter.refresh_state()
        result.update(state)
    elif command.command_type == "SEARCH_CONTRACTS":
        result = {"results": adapter.search_contracts(command.payload["query"],command.payload.get("asset_classes"))}
    elif command.command_type == "OPTION_QUOTES":
        result = adapter.option_quotes(command.payload)
    elif command.command_type == "OPTION_CHAIN":
        result = adapter.option_chain(command.payload)
    elif command.command_type == "QUALIFY":
        result = adapter.qualify_contract(command.payload)
    elif command.command_type == "REQUEST_HISTORICAL_DATA":
        result = adapter.historical_bars(command.payload)
    elif command.command_type == "REQUEST_HISTORICAL_SCHEDULE":
        result = adapter.historical_schedule(command.payload)
    elif command.command_type == "SUBSCRIBE_MARKET_DATA":
        result = adapter.subscribe_market_data(command.payload)
    elif command.command_type == "CANCEL_MARKET_DATA":
        result = adapter.cancel_market_data(command.payload)
    elif command.command_type == "PLACE_ORDER":
        result = adapter.place_order(command.payload)
    elif command.command_type == "PLACE_OPTION_ORDER":
        result = adapter.place_option_order(command.payload)
    elif command.command_type == "MODIFY_ORDER":
        result = adapter.modify_order(command.payload)
    elif command.command_type == "CANCEL_ORDER":
        result = adapter.cancel_order(command.payload)
    elif command.command_type == "REFRESH":
        result = adapter.refresh_state()
    elif command.command_type == "KILL_SWITCH":
        adapter.killed = bool(command.payload.get("enabled", True))
        result = {"enabled": adapter.killed}
    else:
        raise ValueError("Unsupported command")

    with transaction.atomic():
        command = GatewayCommand.objects.select_for_update().get(pk=command.pk)
        attempt = GatewayCommandAttempt.objects.select_for_update().get(pk=attempt.pk)
        attempt.submission_state = "SUBMITTED" if command.command_type in ORDER_COMMANDS else "EXECUTED"
        attempt.broker_result = result
        attempt.save(update_fields=["submission_state", "broker_result"])
        if command.command_type in ORDER_COMMANDS:
            GatewayOrderReference.objects.update_or_create(
                internal_id=command.payload["internal_id"],
                defaults={
                    "broker_order_id": str(result.get("broker_order_id", "")),
                    "permanent_id": str(result.get("permanent_id", "")),
                    "last_status": result.get("status", ""),
                },
            )
        command.result = result
        command.status = "COMPLETED"
        command.retryable = False
        command.last_error = ""
        command.lease_expires_at = None
        command.completed_at = timezone.now()
        command.save(update_fields=[
            "result", "status", "retryable", "last_error", "lease_expires_at", "completed_at", "updated_at",
        ])
        attempt.submission_state = "COMPLETED"
        attempt.completed_at = command.completed_at
        attempt.save(update_fields=["submission_state", "completed_at"])
    persist_event(
        f"command:{command.pk}:completed",
        f"command.{command.command_type.lower()}.completed",
        {
            "command_id": command.pk,
            "command_type": command.command_type,
            "payload": command.payload,
            **result,
        },
    )
    supplied_command.refresh_from_db()
    return result


@transaction.atomic
def fail_command(command, exc, *, retryable=None):
    command = GatewayCommand.objects.select_for_update().get(pk=command.pk)
    retryable = not isinstance(exc, (KeyError, ValueError)) if retryable is None else bool(retryable)
    command.status = "FAILED"
    command.retryable = retryable
    command.last_error = str(exc)[:1000]
    command.lease_expires_at = None
    command.completed_at = timezone.now()
    command.save(update_fields=[
        "status", "retryable", "last_error", "lease_expires_at", "completed_at", "updated_at",
    ])
    attempt = command.attempt_history.filter(attempt_number=command.attempt_count).first()
    if attempt:
        attempt.submission_state = "FAILED"
        attempt.error = command.last_error
        attempt.completed_at = command.completed_at
        attempt.save(update_fields=["submission_state", "error", "completed_at"])
    return command
