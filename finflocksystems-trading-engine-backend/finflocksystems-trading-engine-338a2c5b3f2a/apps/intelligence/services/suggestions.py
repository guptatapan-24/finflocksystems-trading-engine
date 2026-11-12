"""Suggestion decisions (R1): advice is only ever PROPOSED until a human decides.

* acknowledge: allowed always; records the decision, changes nothing (the only action for an enabled instance).
* accept: non-PARAMETER kinds are acknowledged; a PARAMETER suggestion is applied through the engine's own
  `update_instance` (the path the PATCH endpoint uses) and only when the instance is provably safe to change.
  It never enables or activates anything.
* reject: stores a reason; never applies.
"""
from django.db import transaction
from django.utils import timezone

from apps.intelligence.models import Suggestion, TradeEpisode

SAFE_STATES = {"FLAT", "DISABLED", "PAUSED"}


class SuggestionError(Exception):
    def __init__(self, status, code, message, details=None):
        super().__init__(message)
        self.status, self.code, self.details = status, code, details or {}


def safety_reasons(instance):
    """Why an instance may NOT be changed right now (empty list = safe)."""
    from apps.oms.models import Order
    from apps.strategies.deletion import ACTIVE_ORDER_STATES
    from apps.strategies.models import StrategyAttributedPosition

    reasons = []
    if instance.enabled:
        reasons.append("INSTANCE_ENABLED")
    if instance.state not in SAFE_STATES:
        reasons.append(f"STATE_{instance.state}")
    if TradeEpisode.objects.filter(instance_id=instance.pk, status=TradeEpisode.Status.OPEN).exists():
        reasons.append("OPEN_EPISODE")
    if StrategyAttributedPosition.objects.filter(strategy_instance=instance).exclude(quantity=0).exists():
        reasons.append("NON_ZERO_ATTRIBUTED_POSITION")
    if Order.objects.filter(intent__attributions__strategy_instance=instance,
                            status__in=ACTIVE_ORDER_STATES).exists():
        reasons.append("ACTIVE_ORDERS")
    return reasons


def _audit(suggestion, actor, event, data):
    from apps.audit.models import AuditEvent

    AuditEvent.objects.get_or_create(
        idempotency_key=f"intelligence-suggestion:{suggestion.pk}:{event}",
        defaults={"event_type": f"intelligence.suggestion.{event}", "actor": str(actor)[:128],
                  "aggregate_type": "strategy_instance", "aggregate_id": str(suggestion.instance_id),
                  "data": {"suggestion_id": suggestion.pk, **data}})


def _decide(suggestion, status, actor, reason=""):
    suggestion.status = status
    suggestion.decided_by = str(actor)[:150]
    suggestion.decided_at = timezone.now()
    suggestion.decision_reason = (reason or "")[:500]


def _locked(suggestion_id):
    suggestion = Suggestion.objects.select_for_update().filter(pk=suggestion_id).first()
    if suggestion is None:
        raise SuggestionError(404, "SUGGESTION_NOT_FOUND", "Suggestion not found")
    if suggestion.status != Suggestion.Status.PROPOSED:
        raise SuggestionError(409, "SUGGESTION_NOT_PROPOSED",
                              f"Suggestion is already {suggestion.status}", {"status": suggestion.status})
    return suggestion


@transaction.atomic
def acknowledge(suggestion_id, actor, reason=""):
    suggestion = _locked(suggestion_id)
    _decide(suggestion, Suggestion.Status.ACKNOWLEDGED, actor, reason)
    suggestion.save()
    _audit(suggestion, actor, "acknowledged", {"reason": reason[:200]})
    return suggestion


@transaction.atomic
def reject(suggestion_id, actor, reason):
    reason = (reason or "").strip()
    if not reason:
        raise SuggestionError(400, "REASON_REQUIRED", "A reason is required to reject a suggestion")
    suggestion = _locked(suggestion_id)
    _decide(suggestion, Suggestion.Status.REJECTED, actor, reason)
    suggestion.save()
    _audit(suggestion, actor, "rejected", {"reason": reason[:200]})
    return suggestion


@transaction.atomic
def accept(suggestion_id, actor):
    from apps.strategies.framework import update_instance
    from apps.strategies.models import StrategyInstance
    from apps.strategies.plugins import get_plugin

    suggestion = _locked(suggestion_id)
    if suggestion.kind != "PARAMETER":
        _decide(suggestion, Suggestion.Status.ACKNOWLEDGED, actor, "Accepted as advice; nothing to apply")
        suggestion.save()
        _audit(suggestion, actor, "acknowledged", {"kind": suggestion.kind})
        return suggestion
    instance = (StrategyInstance.objects.select_for_update(of=("self",)).select_related("definition")
                .filter(pk=suggestion.instance_id).first())
    if instance is None:
        raise SuggestionError(409, "INSTANCE_NOT_FOUND", "The strategy instance no longer exists")
    reasons = safety_reasons(instance)
    if reasons:
        raise SuggestionError(
            409, "INSTANCE_NOT_SAFE_TO_CHANGE",
            "Parameters can only be applied to a disabled, flat instance with no open trade or active orders. "
            "Use acknowledge for an enabled instance.", {"reasons": reasons})
    plugin = get_plugin(instance.definition)
    key = suggestion.parameter_key
    actual = (instance.parameters or {}).get(key, plugin.default_parameters.get(key))
    try:
        unchanged = float(actual) == float(suggestion.current_value)
    except (TypeError, ValueError):
        unchanged = actual == suggestion.current_value
    if not unchanged:
        raise SuggestionError(409, "SUGGESTION_STALE",
                              "The parameter changed since this suggestion was made", {"current": actual})
    merged = {**(instance.parameters or {}), key: suggestion.proposed_value}
    try:
        plugin.validate_configuration(merged, instance.target_configuration)
    except ValueError as exc:
        raise SuggestionError(422, "SUGGESTION_INVALID", str(exc)[:300]) from exc
    updated = update_instance(instance, {"parameters": merged})  # same service as PATCH; never enables
    version = updated.versions.get(version=updated.version)
    _decide(suggestion, Suggestion.Status.APPLIED, actor, f"Applied as strategy version {updated.version}")
    suggestion.applied_version_id = version.pk
    suggestion.save()
    _audit(suggestion, actor, "applied", {"version": updated.version, "parameter": key})
    return suggestion
