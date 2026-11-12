"""Phase 6 endpoints: advisory, weekly review, counterfactuals, metrics. Staff-only, CSRF-protected; POSTs that change
state need an Idempotency-Key. Expensive or model-backed endpoints use database counters (no shared cache exists, R7)."""
import hmac
from datetime import timedelta

from django.conf import settings
from django.http import HttpResponse
from django.utils import timezone

from apps.core.views import method_guard, response

from . import conf
from .api_utils import error, is_staff, json_body, run_idempotent, staff_required
from .models import LlmRunLog, PreTradeAdvisory
from .services import advisory as advisory_service
from .services import counterfactuals, journal
from .services.suggestions import SuggestionError

MAX_LIMIT = 200


def _advisory_row(row):
    return {"id": row.pk, "strategy_key": row.strategy_key, "instance_id": row.instance_id, "instance_name": row.instance_name,
            "trigger": row.trigger, "severity": row.severity, "summary": row.summary, "lessons": row.lessons,
            "stats": row.stats, "open_suggestion_ids": row.open_suggestion_ids, "calibration_warnings": row.calibration_warnings,
            "comparisons": row.comparisons, "requires_ack": row.requires_ack, "acknowledged_by": row.acknowledged_by,
            "acknowledged_at": row.acknowledged_at, "created_at": row.created_at,
            "note": "Advice only. It never blocks creating, changing or activating a strategy."}


@staff_required
def advisory_preview(request):
    """Advisory for settings that are not saved yet (the wizard calls this before creating a strategy). No writes."""
    invalid = method_guard(request, "POST")
    if invalid:
        return invalid
    payload = json_body(request)
    if payload is None or not isinstance(payload.get("parameters", {}), dict):
        return error(400, "INVALID_REQUEST", "Body must be a JSON object with strategy_key and parameters")
    strategy_key = str(payload.get("strategy_key") or "").upper().strip()
    if not strategy_key:
        return error(400, "INVALID_REQUEST", "strategy_key is required")
    data = advisory_service.build_advisory(strategy_key, payload.get("parameters", {}),
                                           instrument_symbol=str(payload.get("instrument") or "")[:64], trigger="PREVIEW")
    return response(data)


@staff_required
def advisories(request):
    invalid = method_guard(request, "GET")
    if invalid:
        return invalid
    try:
        limit = min(max(int(request.GET.get("limit", 50)), 1), MAX_LIMIT)
        offset = max(int(request.GET.get("offset", 0)), 0)
    except ValueError:
        return error(400, "INVALID_PAGINATION", "limit and offset must be integers")
    query = PreTradeAdvisory.objects.all()
    if request.GET.get("instance_id", "").isdigit():
        query = query.filter(instance_id=int(request.GET["instance_id"]))
    if request.GET.get("severity") in {"INFO", "WARNING", "HIGH"}:
        query = query.filter(severity=request.GET["severity"])
    total = query.count()
    rows = [_advisory_row(r) for r in query.order_by("-created_at", "-pk")[offset:offset + limit]]
    return response(rows, meta={"limit": limit, "offset": offset, "total": total})


@staff_required
def advisory_acknowledge(request, advisory_id):
    invalid = method_guard(request, "POST")
    if invalid:
        return invalid
    payload = json_body(request)
    if payload is None:
        return error(400, "INVALID_REQUEST", "Request body must be a JSON object")
    actor = (request.user.get_username() or "anonymous")

    def handler():
        row = PreTradeAdvisory.objects.filter(pk=advisory_id).first()
        if row is None:
            raise SuggestionError(404, "ADVISORY_NOT_FOUND", "Advisory not found")
        if not row.acknowledged_at:
            row.acknowledged_by, row.acknowledged_at = actor, timezone.now()
            row.save(update_fields=["acknowledged_by", "acknowledged_at"])
            journal.record(journal.OperatorEvent.Type.ADVISORY_ACK, instance_id=getattr(row, "instance_id", None),
                           actor=actor, identity={"strategy_key": row.strategy_key},
                           payload={"advisory_id": row.pk, "trigger": getattr(row, "trigger", "")},
                           idempotency_key=f"advisory-ack:{row.pk}")
        return 200, _advisory_row(row)

    return run_idempotent(request, f"advisory-ack:{advisory_id}", payload, handler)


@staff_required
def weekly_review_request(request, strategy_key):
    """Queue the aggregated review for a strategy (model-backed, so throttled by a database counter)."""
    invalid = method_guard(request, "POST")
    if invalid:
        return invalid
    payload = json_body(request)
    if payload is None:
        return error(400, "INVALID_REQUEST", "Request body must be a JSON object")
    strategy_key = strategy_key.upper()

    def handler():
        from .tasks import run_weekly_review

        limit = int(getattr(settings, "INTELLIGENCE_RERUN_LIMIT_PER_MINUTE", 5))
        recent = LlmRunLog.objects.filter(scope="WEEKLY_REVIEW", created_at__gte=timezone.now() - timedelta(minutes=1)).count()
        if limit > 0 and recent >= limit:
            raise SuggestionError(429, "RATE_LIMITED", "Too many review runs; retry later", {"limit": limit, "window_seconds": 60})
        try:
            run_weekly_review.apply_async((strategy_key,), retry=False, ignore_result=True)
        except Exception as exc:
            raise SuggestionError(503, "QUEUE_UNAVAILABLE", "Could not queue the review") from exc
        return 202, {"status": "QUEUED", "strategy_key": strategy_key}

    return run_idempotent(request, f"weekly-review:{strategy_key}", payload, handler)


@staff_required
def options_counterfactuals(request):
    invalid = method_guard(request, "GET")
    if invalid:
        return invalid
    strategy_key = (request.GET.get("strategy_key") or "").upper().strip()
    if not strategy_key:
        return error(400, "INVALID_REQUEST", "strategy_key is required")
    return response(counterfactuals.options_filter_sweep(strategy_key, request.GET.get("currency") or None))


@staff_required
def stock_replay(request):
    """Replay a stock strategy's real plugin over persisted bars for the current parameters and alternatives."""
    invalid = method_guard(request, "POST")
    if invalid:
        return invalid
    payload = json_body(request)
    if payload is None or not str(payload.get("instance_id", "")).isdigit():
        return error(400, "INVALID_REQUEST", "instance_id is required")
    alternatives = payload.get("alternatives") or []
    if not isinstance(alternatives, list) or len(alternatives) > 6 or not all(isinstance(a, dict) for a in alternatives):
        return error(400, "INVALID_REQUEST", "alternatives must be a list of up to 6 parameter objects")
    from apps.strategies.models import StrategyInstance

    instance = StrategyInstance.objects.filter(pk=int(payload["instance_id"])).select_related("definition", "instrument").first()
    if instance is None:
        return error(404, "INSTANCE_NOT_FOUND", "Strategy instance not found")
    try:
        data = counterfactuals.compare_parameter_sets(
            instance.definition, instance.instrument, instance.timeframe, instance.parameters, alternatives,
            target_configuration=instance.target_configuration, limit=min(int(payload.get("limit", 2000)), 5000))
    except (counterfactuals.ReplayError, ValueError) as exc:
        return error(422, "REPLAY_NOT_POSSIBLE", str(exc)[:300])
    return response(data)


def _metrics_allowed(request):
    token = getattr(settings, "INTELLIGENCE_METRICS_TOKEN", "")
    header = request.headers.get("Authorization", "")
    if token and header.startswith("Bearer ") and hmac.compare_digest(header[7:].encode(), token.encode()):
        return True
    return is_staff(request)


def metrics(request):
    """Prometheus exposition. Staff session, or `Authorization: Bearer <INTELLIGENCE_METRICS_TOKEN>` for scrapers."""
    invalid = method_guard(request, "GET")
    if invalid:
        return invalid
    if not _metrics_allowed(request):
        return error(401, "AUTHENTICATION_REQUIRED", "Staff session or metrics token required")
    from .services.ops import prometheus_text

    return HttpResponse(prometheus_text(), content_type="text/plain; version=0.0.4; charset=utf-8")
