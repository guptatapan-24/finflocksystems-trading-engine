"""Operations (Phase 6): Prometheus gauges, health extras, budget alarm. Read-only; none of it is a readiness input (R2)."""
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from apps.intelligence import conf
from apps.intelligence.models import (
    CalibrationStat, LiveAlert, LlmRunLog, Lesson, Suggestion, TradeEpisode, TradeReview,
)


def reviews_pending():
    """Closed or expired episodes with a frozen snapshot but no real (non-fallback) review yet."""
    reviewed = TradeReview.objects.filter(scope="TRADE", is_fallback=False, episode__isnull=False).values("episode_id")
    return (TradeEpisode.objects.filter(status__in=["CLOSED", "EXPIRED"], evidence_snapshot__isnull=False)
            .exclude(pk__in=reviewed).count())


def failures_24h():
    since = timezone.now() - timedelta(hours=24)
    rows = LlmRunLog.objects.filter(created_at__gte=since).exclude(status="OK")
    out = {}
    for status in rows.values_list("status", flat=True):
        out[status] = out.get(status, 0) + 1
    return out


def reject_rate():
    decided = Suggestion.objects.exclude(status__in=["PROPOSED", "EXPIRED", "SUPERSEDED"]).count()
    rejected = Suggestion.objects.filter(status="REJECTED").count()
    return None if not decided else rejected / decided


def budget_state():
    from apps.intelligence.llm.runner import spent_today_usd

    budget, spent = conf.daily_budget_usd(), spent_today_usd()
    pct = float(getattr(settings, "INTELLIGENCE_BUDGET_ALARM_PCT", 80))
    return {"daily_usd": str(budget), "spent_today_usd": str(spent),
            "alarm": bool(budget > 0 and spent >= budget * Decimal(str(pct)) / 100), "exhausted": bool(budget > 0 and spent >= budget),
            "alarm_pct": pct}


def health_extras():
    last = TradeReview.objects.filter(is_fallback=False).order_by("-created_at").first()
    return {"reviews_pending": reviews_pending(), "llm_failures_24h": failures_24h(), "budget": budget_state(),
            "last_model_review_at": last.created_at if last else None,
            "open_episodes": TradeEpisode.objects.filter(status="OPEN").count()}


def prometheus_text():
    from prometheus_client import CollectorRegistry, Gauge, generate_latest

    from apps.intelligence.llm.circuit import breaker_for

    registry = CollectorRegistry()
    gauge = lambda name, doc, labels=(): Gauge(name, doc, labels, registry=registry)  # noqa: E731
    gauge("intelligence_reviews_pending", "Closed trades without a model review").set(reviews_pending())
    failures = gauge("intelligence_llm_failures_24h", "Non-OK LLM runs in the last 24 hours", ["status"])
    for status, count in failures_24h().items():
        failures.labels(status).set(count)
    state = budget_state()
    gauge("intelligence_llm_cost_usd_today", "LLM spend today in USD").set(float(state["spent_today_usd"]))
    gauge("intelligence_llm_budget_usd", "Daily LLM budget in USD (0 = unlimited)").set(float(state["daily_usd"]))
    gauge("intelligence_budget_alarm", "1 when spend passed the alarm threshold").set(1 if state["alarm"] else 0)
    gauge("intelligence_circuit_breaker_open", "1 when the circuit breaker of the configured LLM provider is open").set(1 if breaker_for(conf.provider()).state == "OPEN" else 0)
    rate = reject_rate()
    gauge("intelligence_suggestion_reject_rate", "Share of decided suggestions that were rejected").set(-1 if rate is None else rate)
    suggestions = gauge("intelligence_suggestions", "Suggestions by status", ["status"])
    for status, _ in Suggestion.Status.choices:
        suggestions.labels(status).set(Suggestion.objects.filter(status=status).count())
    lessons = gauge("intelligence_lessons", "Lessons by status", ["status"])
    for status, _ in Lesson.Status.choices:
        lessons.labels(status).set(Lesson.objects.filter(status=status).count())
    brier = gauge("intelligence_calibration_brier", "Brier score of the probability of profit", ["strategy"])
    for row in CalibrationStat.objects.filter(metric="POP", bin="ALL", brier__isnull=False):
        brier.labels(row.strategy_key or "all").set(row.brier)
    gauge("intelligence_open_episodes", "Open trade episodes").set(TradeEpisode.objects.filter(status="OPEN").count())
    gauge("intelligence_high_alerts", "HIGH live alerts on open trades").set(
        LiveAlert.objects.filter(severity="HIGH", episode__status="OPEN").count())
    return generate_latest(registry).decode()


def check_budget_alarm():
    """Write one AuditEvent per day the first time spend crosses the alarm threshold. Never raises."""
    try:
        state = budget_state()
        if state["alarm"]:
            from apps.audit.models import AuditEvent

            AuditEvent.objects.get_or_create(
                idempotency_key=f"intelligence-budget-alarm:{timezone.now().date().isoformat()}",
                defaults={"event_type": "intelligence.budget.alarm", "actor": "intelligence", "aggregate_type": "intelligence",
                          "aggregate_id": "budget", "data": state})
    except Exception:
        pass
