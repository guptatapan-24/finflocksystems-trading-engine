"""Journal events that come from the engine's own rows: new strategy versions (so every parameter / risk-profile /
timeframe change), strategy actions (enable, disable, flatten, ...) and manual orders.

Read by a cursor sweep, not by hooks, so nothing runs inside the engine's transactions (R16) and every row is journalled
exactly once (idempotency key = the engine row id) even if the broker or worker was down for a while.
"""
import logging
from datetime import timedelta

from django.utils import timezone

from apps.intelligence import conf
from apps.intelligence.models import IntelCursor, OperatorEvent

from . import journal
from .evidence import sanitize_text

logger = logging.getLogger(__name__)
BATCH = 200
VERSION_CURSOR, ACTION_CURSOR, ORDER_CURSOR = "journal_version", "journal_action", "journal_manual_order"
SETTLE_SECONDS = 300  # a PROCESSING action is re-read until it settles or this long has passed


def _identity(instance):
    return {"strategy_key": instance.definition.key, "instance_name": instance.name,
            "instrument_symbol": getattr(instance.instrument, "symbol", ""), "execution_mode": instance.execution_mode}


def _advance(cursor, last_id):
    if last_id and last_id > cursor.last_id:
        cursor.last_id = last_id
        cursor.save(update_fields=["last_id", "updated_at"])


def _sync_versions():
    from apps.strategies.models import StrategyVersion

    cursor, _ = IntelCursor.objects.get_or_create(name=VERSION_CURSOR)
    rows = list(StrategyVersion.objects.filter(pk__gt=cursor.last_id).select_related(
        "strategy_instance__definition", "strategy_instance__instrument").order_by("pk")[:BATCH])
    for version in rows:
        instance = version.strategy_instance
        previous = StrategyVersion.objects.filter(strategy_instance=instance, version=version.version - 1).first()
        first = previous is None
        changes = journal.diff_snapshots(previous.configuration_snapshot if previous else {}, version.configuration_snapshot)
        payload = {"version": version.version, "parameter_hash": version.parameter_hash,
                   "timeframe": (version.configuration_snapshot or {}).get("timeframe"),
                   "risk_profile": ((version.configuration_snapshot or {}).get("target_configuration") or {}).get("risk_profile")}
        payload["changes" if not first else "initial_configuration"] = (
            changes if not first else version.configuration_snapshot)
        journal.record(OperatorEvent.Type.INSTANCE_CREATED if first else OperatorEvent.Type.INSTANCE_UPDATED,
                       instance=instance, occurred_at=version.created_at, payload=payload,
                       idempotency_key=f"version:{version.pk}")
    if rows:
        _advance(cursor, rows[-1].pk)
    return len(rows)


def _sync_actions():
    from apps.strategies.models import StrategyAction

    cursor, _ = IntelCursor.objects.get_or_create(name=ACTION_CURSOR)
    rows = list(StrategyAction.objects.filter(pk__gt=cursor.last_id).select_related(
        "strategy_instance__definition", "strategy_instance__instrument").order_by("pk")[:BATCH])
    settle_before = timezone.now() - timedelta(seconds=SETTLE_SECONDS)
    done, last = 0, 0
    for action in rows:
        if action.status == "PROCESSING" and action.created_at > settle_before:
            break  # not final yet: wait, and do not move the cursor past it
        journal.record(OperatorEvent.Type.INSTANCE_ACTION, instance=action.strategy_instance,
                       occurred_at=action.created_at,
                       payload={"action": action.action, "status": action.status, "attempts": action.attempt_count,
                                "error": sanitize_text(action.last_error, 200)},
                       idempotency_key=f"action:{action.pk}")
        done, last = done + 1, action.pk
    if last:
        _advance(cursor, last)
    return done


def _sync_manual_orders():
    from apps.oms.models import OrderIntent

    cursor, _ = IntelCursor.objects.get_or_create(name=ORDER_CURSOR)
    rows = list(OrderIntent.objects.filter(pk__gt=cursor.last_id, origin="MANUAL").select_related(
        "instrument", "strategy_instance__definition", "strategy_instance__instrument").order_by("pk")[:BATCH])
    for intent in rows:
        payload = {"symbol": intent.instrument.symbol, "side": intent.side, "quantity": str(intent.quantity),
                   "order_type": intent.order_type, "limit_price": None if intent.limit_price is None else str(intent.limit_price),
                   "time_in_force": intent.time_in_force, "portfolio_id": intent.portfolio_id, "source": intent.source}
        identity = _identity(intent.strategy_instance) if intent.strategy_instance_id else {
            "instrument_symbol": intent.instrument.symbol, "execution_mode": intent.mode}
        journal.record(OperatorEvent.Type.MANUAL_ORDER, instance=None, instance_id=intent.strategy_instance_id,
                       identity=identity, occurred_at=intent.created_at, payload=payload,
                       idempotency_key=f"manual_order:{intent.pk}")
    if rows:
        _advance(cursor, rows[-1].pk)
    return len(rows)


def sync():
    """Run the three syncs; one failing never stops the others. Returns counts."""
    if not conf.enabled():
        return {"skipped": "disabled"}
    out = {}
    for name, fn in (("versions", _sync_versions), ("actions", _sync_actions), ("manual_orders", _sync_manual_orders)):
        try:
            out[name] = fn()
        except Exception:
            logger.exception("intelligence: journal sync of %s failed", name)
            out[name] = "error"
    return out
