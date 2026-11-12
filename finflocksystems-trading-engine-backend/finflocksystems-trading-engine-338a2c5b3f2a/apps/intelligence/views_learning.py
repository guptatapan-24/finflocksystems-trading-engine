"""Phase 5 endpoints: learning feed, lessons, performance, calibration, digest. Staff-only read APIs."""
from apps.core.views import method_guard, response

from .api_utils import error, staff_required
from .models import CalibrationStat, ChangePointState, LearningEvent, Lesson, LessonEvidence, PerformanceStat, Suggestion
from .services import digest as digest_service

MAX_LIMIT = 200
NOTE = ("n is always shown. Lessons stay hypotheses until they hold up over many trades; with few trades per strategy "
        "that takes a long time, and that is expected.")


def lesson_row(lesson, detail=False):
    total = lesson.support_n + lesson.contradict_n
    row = {
        "id": lesson.pk, "strategy_key": lesson.strategy_key, "statement": lesson.statement, "status": lesson.status,
        "predicate": lesson.predicate, "expected_effect": lesson.expected_effect, "supporting_trades": lesson.support_n,
        "contradicting_trades": lesson.contradict_n, "n": total, "forward_trades": lesson.n_forward,
        "posterior_mean": round(lesson.beta_alpha / (lesson.beta_alpha + lesson.beta_beta), 4),
        "p_value": lesson.p_value, "q_value": lesson.q_value,
        "effect_size_bps": lesson.effect_size, "effect_ci_low": lesson.effect_ci_low, "effect_ci_high": lesson.effect_ci_high,
        "supported_at": lesson.supported_at, "established_at": lesson.established_at,
        "last_matched_at": lesson.last_matched_at, "created_at": lesson.created_at,
    }
    if detail:
        row["evidence"] = [{"episode_id": e.episode_id, "verdict": e.verdict, "outcome_value": e.outcome_value,
                            "weight": e.weight, "forward": e.forward_only, "origin": e.is_origin}
                           for e in LessonEvidence.objects.filter(lesson=lesson).order_by("episode__opened_at")]
        row["note"] = ("The trade that proposed this lesson is shown with weight 0: it never counts as evidence for it. "
                       "Paper and delayed-data trades count at half weight.")
    return row


def _int(request, name, default, minimum=0, maximum=None):
    try:
        value = int(request.GET.get(name, default))
    except ValueError:
        return None
    value = max(value, minimum)
    return min(value, maximum) if maximum else value


@staff_required
def learning_feed(request):
    invalid = method_guard(request, "GET")
    if invalid:
        return invalid
    since, limit = _int(request, "since", 0), _int(request, "limit", 100, 1, MAX_LIMIT)
    if since is None or limit is None:
        return error(400, "INVALID_PAGINATION", "since and limit must be integers")
    query = LearningEvent.objects.filter(pk__gt=since)
    if request.GET.get("type"):
        query = query.filter(type=request.GET["type"])
    rows = list(query.order_by("pk")[:limit])
    data = [{"id": e.pk, "type": e.type, "episode_id": e.episode_id, "lesson_id": e.lesson_id, "payload": e.payload,
             "created_at": e.created_at} for e in rows]
    return response(data, meta={"cursor": rows[-1].pk if rows else since, "limit": limit, "note": NOTE})


@staff_required
def lessons(request, lesson_id=None):
    invalid = method_guard(request, "GET")
    if invalid:
        return invalid
    if lesson_id is not None:
        lesson = Lesson.objects.filter(pk=lesson_id).first()
        if lesson is None:
            return error(404, "LESSON_NOT_FOUND", "Lesson not found")
        return response(lesson_row(lesson, detail=True), meta={"note": NOTE})
    limit, offset = _int(request, "limit", 50, 1, MAX_LIMIT), _int(request, "offset", 0)
    if limit is None or offset is None:
        return error(400, "INVALID_PAGINATION", "limit and offset must be integers")
    query = Lesson.objects.all()
    for field in ("status", "strategy_key"):
        if request.GET.get(field):
            query = query.filter(**{field: request.GET[field]})
    total = query.count()
    rows = [lesson_row(l) for l in query.order_by("-created_at", "-pk")[offset:offset + limit]]
    return response(rows, meta={"limit": limit, "offset": offset, "total": total, "note": NOTE})


@staff_required
def performance(request):
    invalid = method_guard(request, "GET")
    if invalid:
        return invalid
    query = PerformanceStat.objects.all()
    for field in ("strategy_key", "currency", "instrument", "mode", "regime", "structure", "risk_profile", "context"):
        if request.GET.get(field):
            query = query.filter(**{field: request.GET[field]})
    if request.GET.get("strategy_version", "").isdigit():
        query = query.filter(strategy_version=int(request.GET["strategy_version"]))
    rows = [{"strategy_key": s.strategy_key, "strategy_version": s.strategy_version or None, "instrument": s.instrument,
             "regime": s.regime, "structure": s.structure, "mode": s.mode, "risk_profile": s.risk_profile, "context": s.context,
             "currency": s.currency, "n": s.n, "wins": s.wins, "mean_return_bps": s.mean_return_bps, "ci_low_bps": s.ci_low,
             "ci_high_bps": s.ci_high, "shrunk_mean_bps": s.shrunk_mean_bps, "shrunk_sd_bps": s.shrunk_sd_bps, "shrinkage": s.shrinkage,
             "sum_net_pnl": str(s.sum_net_pnl), "as_of": s.as_of}
            for s in query.order_by("strategy_key", "instrument", "currency")[:500]]
    return response(rows, meta={"note": "Money is per currency and never summed across currencies. Intervals are 95% "
                                        "bootstrap; a missing interval means fewer than 2 trades. The shrunk mean pulls slices with few "
                                        "trades towards the average of slices of the same kind (empirical Bayes); trust it more than the raw "
                                        "mean when n is small.", "total": len(rows)})


@staff_required
def calibration(request):
    invalid = method_guard(request, "GET")
    if invalid:
        return invalid
    query = CalibrationStat.objects.all()
    if request.GET.get("strategy_key") is not None:
        query = query.filter(strategy_key=request.GET["strategy_key"])
    rows = [{"metric": c.metric, "strategy_key": c.strategy_key, "bin": c.bin, "n": c.n, "predicted_mean": c.predicted_mean,
             "observed_rate": c.observed_rate, "brier": c.brier, "as_of": c.as_of} for c in query.order_by("strategy_key", "bin")]
    return response(rows, meta={"note": "Calibration needs many trades. Bins with small n are shown for transparency only "
                                        "and never raise an alarm."})


@staff_required
def digest(request):
    invalid = method_guard(request, "GET")
    if invalid:
        return invalid
    days = _int(request, "days", 1, 1, 90)
    if days is None:
        return error(400, "INVALID_PARAMETER", "days must be an integer")
    data = digest_service.build_digest(days=days)
    if request.GET.get("format") == "text":
        from django.http import HttpResponse

        return HttpResponse(digest_service.to_text(data), content_type="text/plain; charset=utf-8")
    return response(data)


def _describe(predicate):
    return " AND ".join(f"{c['metric']} {c['op']} {c['value']}" for c in (predicate or {}).get("all", []))


@staff_required
def experiments(request):
    """The "try next time" playbook: measurement plans with their history check, forward progress and (when resolved) outcome."""
    from .services import playbook

    invalid = method_guard(request, "GET")
    if invalid:
        return invalid
    query = Suggestion.objects.filter(kind="EXPERIMENT").select_related("outcome")
    if request.GET.get("status"):
        query = query.filter(status=request.GET["status"].upper())
    if request.GET.get("strategy_key"):
        query = query.filter(strategy_key=request.GET["strategy_key"])
    rows = []
    for s in query.order_by("-created_at")[:200]:
        info = s.experiment or {}
        done, needed = playbook.forward_progress(s)
        outcome = getattr(s, "outcome", None)
        rows.append({
            "id": s.pk, "review_id": s.review_id, "strategy_key": s.strategy_key, "instance_name": s.instance_name, "status": s.status,
            "title": info.get("title"), "try": info.get("try"), "condition": _describe(info.get("predicate")), "metric": info.get("metric"),
            "direction": info.get("direction"), "min_n": info.get("min_n"), "paper_only": info.get("paper_only", True),
            "stop_rule": info.get("stop_rule"), "rule_id": info.get("rule_id"), "expected_effect": s.expected_effect,
            "history_check": info.get("history_check"), "progress": {"matching_forward_trades": done, "needed": needed},
            "outcome": None if outcome is None else {
                "result": outcome.result, "n_condition": outcome.n_after, "n_rest": outcome.n_before, "delta_bps": outcome.delta_bps,
                "ci_low": outcome.ci_low, "ci_high": outcome.ci_high, **(info.get("resolution") or {})},
            "created_at": s.created_at, "decided_by": s.decided_by, "decided_at": s.decided_at})
    return response(rows, meta={"note": "Measurement plans, not advice: every one is a hypothesis. A result only counts after the minimum number of "
                                        "matching trades, and is judged together with the other experiments (false-discovery control).",
                                "total": len(rows)})


@staff_required
def changepoints(request):
    """Edge-decay monitor: one row per strategy, currency and series, always with n and the baseline."""
    invalid = method_guard(request, "GET")
    if invalid:
        return invalid
    query = ChangePointState.objects.all()
    if request.GET.get("strategy_key"):
        query = query.filter(strategy_key=request.GET["strategy_key"])
    rows = [{"strategy_key": c.strategy_key, "currency": c.currency, "metric": c.metric, "status": c.status, "n": c.n,
             "statistic": c.statistic, "threshold": c.threshold, "reference_n": c.reference_n, "baseline_mean": c.baseline_mean,
             "baseline_sd": c.baseline_sd, "change_index": c.change_index, "change_at": c.change_at, "note": c.note, "as_of": c.as_of}
            for c in query.order_by("strategy_key", "currency", "metric")]
    return response(rows, meta={"note": "A flag to look at a strategy, not a verdict: it needs at least 15 trades, small samples raise false "
                                        "alarms, and a benign change in market conditions looks the same as a broken edge."})
