"""Operator journal (R17): an append-only record of what the operator chose and did.

`record` never raises and is a no-op while the layer is disabled, so it is safe to call from engine hooks (R13, R16).
Free text is sanitised (R8). Engine-derived events (new strategy versions, actions, manual orders) are written by the
cursor sweep in `engine_events`, so they are recorded exactly once and never depend on a hook running.
"""
import hashlib
import json
import logging
import uuid

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.intelligence import conf
from apps.intelligence.models import OperatorEvent

from .evidence import sanitize_text, sanitize_tree

logger = logging.getLogger(__name__)
UNKNOWN_ACTOR = "UNKNOWN"
NOTE_LIMIT = 500
# What of a create / change request is kept. Everything else (and any secret-looking key) is dropped.
REQUEST_FIELDS = (
    "name", "definition_key", "timeframe", "ticker", "exchange", "currency", "primary_exchange", "asset_class",
    "instrument_id", "portfolio_id", "risk_profile", "execution_mode", "activate", "risk_policy_id", "order_policy_id",
    "qualify", "target_configuration", "parameters",
)


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def clean_actor(actor):
    text = sanitize_text(actor or "", 150)
    return text or UNKNOWN_ACTOR


def request_view(payload):
    """The whitelisted, sanitised part of a request body."""
    if not isinstance(payload, dict):
        return {}
    return sanitize_tree({k: payload[k] for k in REQUEST_FIELDS if k in payload}, 200)


def record(event_type, *, instance=None, instance_id=None, actor=None, payload=None, occurred_at=None,
           idempotency_key=None, identity=None):
    """Append one event. Returns the row, the existing row for a repeated idempotency key, or None. Never raises.

    `identity` overrides the denormalised columns (strategy_key, instance_name, instrument_symbol, execution_mode)
    when no live instance object is at hand."""
    if not conf.enabled():
        return None
    try:
        who = dict(identity or {})
        if instance is not None:
            instance_id = instance.pk
            who.setdefault("strategy_key", getattr(getattr(instance, "definition", None), "key", "") or "")
            who.setdefault("instance_name", instance.name)
            who.setdefault("instrument_symbol", getattr(getattr(instance, "instrument", None), "symbol", "") or "")
            who.setdefault("execution_mode", instance.execution_mode or "")
        key = (idempotency_key or f"event:{uuid.uuid4().hex}")[:190]
        try:
            with transaction.atomic():  # savepoint: a failure here cannot poison a caller's transaction
                return OperatorEvent.objects.create(
                    occurred_at=occurred_at or timezone.now(), event_type=event_type, actor=clean_actor(actor),
                    instance_id=instance_id, strategy_key=sanitize_text(who.get("strategy_key"), 64),
                    instance_name=sanitize_text(who.get("instance_name"), 128),
                    instrument_symbol=sanitize_text(who.get("instrument_symbol"), 32),
                    execution_mode=sanitize_text(who.get("execution_mode"), 16),
                    payload=sanitize_tree(payload or {}, 300), idempotency_key=key)
        except IntegrityError:  # the same key again: the first row wins (the savepoint is already rolled back)
            return OperatorEvent.objects.filter(idempotency_key=key).first()
    except Exception:
        logger.exception("intelligence: journal write failed (%s)", event_type)
        return None


def record_note(instance, note, actor=None, idempotency_key=None):
    """The operator's free-text intent: untrusted (R8), length-limited."""
    text = sanitize_text(note, NOTE_LIMIT)
    if not text:
        return None
    return record(OperatorEvent.Type.NOTE, instance=instance, actor=actor, payload={"note": text},
                  idempotency_key=idempotency_key)


def events_for(instance_id, since, until, limit=200):
    """Journal slice for one strategy instance in [since, until], oldest first."""
    return list(OperatorEvent.objects.filter(instance_id=instance_id, occurred_at__gte=since, occurred_at__lte=until)
                .order_by("occurred_at", "pk")[:limit])


def diff_snapshots(old, new, limit=40):
    """{dotted.key: [old, new]} for every leaf that differs between two configuration snapshots."""
    from .facts import flatten

    before, after = flatten(old or {}), flatten(new or {})
    out = {}
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            out[key] = [before.get(key), after.get(key)]
            if len(out) >= limit:
                break
    return out
