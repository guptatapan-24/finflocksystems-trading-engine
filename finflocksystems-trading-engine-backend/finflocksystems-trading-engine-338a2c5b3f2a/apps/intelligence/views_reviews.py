"""Phase 3 endpoints: reviews, suggestions, feedback. All staff-only, not CSRF-exempt; state changes need an
Idempotency-Key and are audited."""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.core.views import method_guard, response

from . import conf
from .api_utils import error, json_body, run_idempotent, staff_required
from .models import LlmRunLog, ReviewFeedback, Suggestion, TradeEpisode, TradeReview
from .services import journal
from .services import suggestions as suggestion_service

MAX_LIMIT = 200


def _actor(request):
    return (request.user.get_username() or "anonymous")


def suggestion_row(item):
    return {
        "id": item.pk, "review_id": item.review_id, "strategy_key": item.strategy_key, "instance_id": item.instance_id,
        "instance_name": item.instance_name, "kind": item.kind, "parameter_key": item.parameter_key,
        "current_value": item.current_value, "proposed_value": item.proposed_value, "rationale": item.rationale,
        "expected_effect": item.expected_effect, "risk_of_change": item.risk_of_change,
        "confidence": None if item.confidence is None else str(item.confidence),
        "evidence_needed": item.evidence_needed, "evidence_refs": item.evidence_refs,
        "is_hypothesis": item.is_hypothesis, "status": item.status, "decided_by": item.decided_by,
        "decided_at": item.decided_at, "decision_reason": item.decision_reason,
        "applied_version_id": item.applied_version_id, "created_at": item.created_at, "experiment": item.experiment,
    }


def needs_rereview(review):
    """A late commission correction matters only if net P&L moved more than the configured percentage (R4)."""
    episode = review.episode
    if episode is None or review.net_pnl_at_review is None:
        return False
    base = abs(review.net_pnl_at_review) or 1
    return abs(episode.net_pnl - review.net_pnl_at_review) / base * 100 > conf.rereview_pnl_delta_pct()


def review_row(review, detail=False):
    row = {
        "id": review.pk, "episode_id": review.episode_id, "scope": review.scope, "strategy_key": review.strategy_key,
        "instance_id": review.instance_id, "instance_name": review.instance_name,
        "instrument": review.instrument_symbol, "currency": review.currency, "verdict": review.verdict,
        "process_grade": review.process_grade, "outcome_class": review.outcome_class, "summary": review.summary,
        "confidence": None if review.confidence is None else str(review.confidence),
        "is_fallback": review.is_fallback, "prompt_version": review.prompt_version, "model": review.model,
        "sample_n": review.sample_n, "created_at": review.created_at,
        "net_pnl_at_review": None if review.net_pnl_at_review is None else str(review.net_pnl_at_review),
    }
    if detail:
        row.update({
            "strengths": review.strengths, "open_questions": review.open_questions,
            "rejected_items": review.rejected_items,
            "mistakes": [{"id": m.pk, "category": m.category, "severity": m.severity, "description": m.description,
                          "evidence_refs": m.evidence_refs, "source": m.source, "rule_id": m.rule_id,
                          "is_defect": m.is_defect} for m in review.mistakes.all().order_by("pk")],
            "suggestions": [suggestion_row(s) for s in review.suggestions.all().order_by("pk")],
            "needs_rereview": needs_rereview(review),
        })
    return row


def _paginate(request):
    try:
        return min(max(int(request.GET.get("limit", 50)), 1), MAX_LIMIT), max(int(request.GET.get("offset", 0)), 0)
    except ValueError:
        return None


@staff_required
def reviews(request):
    invalid = method_guard(request, "GET")
    if invalid:
        return invalid
    page = _paginate(request)
    if page is None:
        return error(400, "INVALID_PAGINATION", "limit and offset must be integers")
    limit, offset = page
    query = TradeReview.objects.all()
    if request.GET.get("instance_id", "").isdigit():
        query = query.filter(instance_id=int(request.GET["instance_id"]))
    if request.GET.get("scope") in {"TRADE", "WEEKLY", "STRATEGY"}:
        query = query.filter(scope=request.GET["scope"])
    if request.GET.get("fallback") in {"true", "false"}:
        query = query.filter(is_fallback=request.GET["fallback"] == "true")
    total = query.count()
    rows = [review_row(r) for r in query.order_by("-created_at", "-pk")[offset:offset + limit]]
    return response(rows, meta={"limit": limit, "offset": offset, "total": total})


@staff_required
def episode_review(request, episode_id):
    invalid = method_guard(request, "GET")
    if invalid:
        return invalid
    review = (TradeReview.objects.filter(episode_id=episode_id).select_related("episode")
              .prefetch_related("mistakes", "suggestions").order_by("-created_at", "-pk").first())
    if review is None:
        return error(404, "REVIEW_NOT_FOUND", "No review exists for this episode yet")
    return response(review_row(review, detail=True))


@staff_required
def episode_review_rerun(request, episode_id):
    invalid = method_guard(request, "POST")
    if invalid:
        return invalid
    payload = json_body(request)
    if payload is None:
        return error(400, "INVALID_REQUEST", "Request body must be a JSON object")
    if not conf.reviews_enabled():
        return error(409, "REVIEWS_DISABLED", "LLM reviews are disabled (INTELLIGENCE_REVIEWS_ENABLED)")

    def handler():
        from .services.prompts import get_active_template
        from .tasks import run_trade_review

        episode = TradeEpisode.objects.filter(pk=episode_id).select_related("evidence_snapshot").first()
        if episode is None or not hasattr(episode, "evidence_snapshot"):
            raise suggestion_service.SuggestionError(404, "EPISODE_NOT_REVIEWABLE",
                                                     "Episode not found or not closed yet")
        template = get_active_template()
        existing = TradeReview.objects.filter(
            episode=episode, evidence_hash=episode.evidence_snapshot.evidence_hash,
            prompt_version=f"{template.name}:v{template.version}", model=conf.model_for("review")).first()
        if existing is not None and not existing.is_fallback:
            return 200, {"status": "UNCHANGED", "review_id": existing.pk}
        limit = int(getattr(settings, "INTELLIGENCE_RERUN_LIMIT_PER_MINUTE", 5))
        recent = LlmRunLog.objects.filter(scope="TRADE_REVIEW",
                                          created_at__gte=timezone.now() - timedelta(minutes=1)).count()
        if limit > 0 and recent >= limit:
            raise suggestion_service.SuggestionError(429, "RATE_LIMITED", "Too many review runs; retry later",
                                                     {"limit": limit, "window_seconds": 60})
        try:
            run_trade_review.apply_async((episode.pk,), retry=False, ignore_result=True)
        except Exception as exc:
            raise suggestion_service.SuggestionError(503, "QUEUE_UNAVAILABLE", "Could not queue the review") from exc
        return 202, {"status": "QUEUED", "episode_id": episode.pk}

    return run_idempotent(request, f"review-rerun:{episode_id}", payload, handler)


@staff_required
def suggestions(request):
    invalid = method_guard(request, "GET")
    if invalid:
        return invalid
    page = _paginate(request)
    if page is None:
        return error(400, "INVALID_PAGINATION", "limit and offset must be integers")
    limit, offset = page
    query = Suggestion.objects.all()
    for field in ("status", "kind"):
        if request.GET.get(field):
            query = query.filter(**{field: request.GET[field]})
    if request.GET.get("instance_id", "").isdigit():
        query = query.filter(instance_id=int(request.GET["instance_id"]))
    total = query.count()
    rows = [suggestion_row(s) for s in query.order_by("-created_at", "-pk")[offset:offset + limit]]
    return response(rows, meta={"limit": limit, "offset": offset, "total": total})


@staff_required
def suggestion_action(request, suggestion_id, action):
    invalid = method_guard(request, "POST")
    if invalid:
        return invalid
    if action not in {"accept", "reject", "acknowledge"}:
        return error(404, "NOT_FOUND", "Unknown action")
    payload = json_body(request)
    if payload is None:
        return error(400, "INVALID_REQUEST", "Request body must be a JSON object")
    actor = _actor(request)

    def handler():
        if action == "accept":
            item = suggestion_service.accept(suggestion_id, actor)
        elif action == "reject":
            item = suggestion_service.reject(suggestion_id, actor, str(payload.get("reason", "")))
        else:
            item = suggestion_service.acknowledge(suggestion_id, actor, str(payload.get("reason", "")))
        journal.record(
            journal.OperatorEvent.Type.SUGGESTION_DECISION, instance_id=item.instance_id, actor=actor,
            identity={"strategy_key": item.strategy_key, "instance_name": item.instance_name},
            payload={"decision": action, "suggestion_id": item.pk, "status": item.status, "kind": item.kind,
                     "parameter_key": item.parameter_key, "reason": str(payload.get("reason", ""))[:300]},
            idempotency_key=f"suggestion-decision:{item.pk}:{action}:{item.decided_at.isoformat() if item.decided_at else ''}")
        return 200, suggestion_row(item)

    return run_idempotent(request, f"suggestion-{action}:{suggestion_id}", {"id": suggestion_id, **payload}, handler)


@staff_required
def review_feedback(request, review_id):
    invalid = method_guard(request, "POST")
    if invalid:
        return invalid
    payload = json_body(request)
    if payload is None:
        return error(400, "INVALID_REQUEST", "Request body must be a JSON object")
    rating, comment = payload.get("rating"), str(payload.get("comment", ""))[:500]
    if isinstance(rating, bool) or rating not in (-1, 0, 1):
        return error(400, "INVALID_RATING", "rating must be -1, 0 or 1")
    if not TradeReview.objects.filter(pk=review_id).exists():
        return error(404, "REVIEW_NOT_FOUND", "Review not found")
    actor = _actor(request)

    def handler():
        row = ReviewFeedback.objects.create(review_id=review_id, rating=rating, comment=comment, created_by=actor)
        review = TradeReview.objects.filter(pk=review_id).first()
        journal.record(
            journal.OperatorEvent.Type.REVIEW_FEEDBACK, instance_id=getattr(review, "instance_id", None), actor=actor,
            identity={"instance_name": getattr(review, "instance_name", "")},
            payload={"review_id": review_id, "rating": rating, "comment": comment},
            idempotency_key=f"review-feedback:{row.pk}")
        return 201, {"review_id": review_id, "rating": rating}

    return run_idempotent(request, f"review-feedback:{review_id}", {"rating": rating, "comment": comment}, handler)
