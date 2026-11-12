"""Digest of what the system learned (API and log first). Honest by construction: it states n, and says plainly
when nothing can be concluded yet."""
from datetime import timedelta

from django.utils import timezone

from apps.intelligence.models import LearningEvent, Lesson, Suggestion, TradeEpisode


def build_digest(days=1, now=None):
    now = now or timezone.now()
    since = now - timedelta(days=days)
    closed = TradeEpisode.objects.filter(status__in=["CLOSED", "EXPIRED"], closed_at__gte=since)
    per_currency = {}
    for ep in closed:
        entry = per_currency.setdefault(ep.currency, {"n": 0, "wins": 0, "net_pnl": 0})
        entry["n"] += 1
        entry["wins"] += 1 if ep.net_pnl > 0 else 0
        entry["net_pnl"] += float(ep.net_pnl)
    events = LearningEvent.objects.filter(created_at__gte=since)
    by_type = {}
    for event in events.order_by("pk"):
        by_type.setdefault(event.type, []).append(event.payload)
    lessons = Lesson.objects.all()
    status_counts = {s: lessons.filter(status=s).count() for s, _ in Lesson.Status.choices}
    digest = {
        "window_days": days, "since": since, "until": now,
        "closed_trades": {"total": closed.count(), "per_currency": per_currency},
        "lessons": {
            "created": [e.get("statement") for e in by_type.get("LESSON_CREATED", [])],
            "promoted": [{"statement": e.get("statement"), "to": e.get("to")} for e in by_type.get("LESSON_PROMOTED", [])],
            "retired_or_refuted": [{"statement": e.get("statement"), "to": e.get("to")} for e in by_type.get("LESSON_RETIRED", [])],
            "reinforced": len(by_type.get("LESSON_REINFORCED", [])), "contradicted": len(by_type.get("LESSON_CONTRADICTED", [])),
            "by_status": status_counts,
        },
        "stat_shifts": by_type.get("STAT_SHIFT", []),
        "change_points": by_type.get("CHANGE_POINT", []),
        "experiments_waiting": Suggestion.objects.filter(kind="EXPERIMENT", status=Suggestion.Status.PROPOSED).count(),
        "calibration_shifts": by_type.get("CALIBRATION_SHIFT", []),
        "suggestion_outcomes": by_type.get("SUGGESTION_RESOLVED", []),
        "open_suggestions": Suggestion.objects.filter(status=Suggestion.Status.PROPOSED).count(),
        "notes": [],
    }
    if digest["closed_trades"]["total"] < 8:
        digest["notes"].append("Fewer than 8 trades closed in this window: results are noise, not evidence.")
    if not status_counts.get("ESTABLISHED") and not status_counts.get("SUPPORTED"):
        digest["notes"].append("No lesson is supported yet. Lessons stay hypotheses until they hold up over many trades; "
                               "with about one entry per expiry that takes a long time, and that is expected.")
    return digest


def to_text(digest):
    lines = [f"Intelligence digest, last {digest['window_days']} day(s)"]
    trades = digest["closed_trades"]
    lines.append(f"Closed trades: {trades['total']}")
    for currency, entry in sorted(trades["per_currency"].items()):
        lines.append(f"  {currency}: n={entry['n']}, winning={entry['wins']}, net {entry['net_pnl']:.2f} (net of costs)")
    lessons = digest["lessons"]
    lines.append(f"Lessons: {lessons['by_status']}")
    for key in ("created", "promoted", "retired_or_refuted"):
        for item in lessons[key]:
            lines.append(f"  {key}: {item}")
    lines.append(f"  reinforced by {lessons['reinforced']} trade(s), contradicted by {lessons['contradicted']}")
    for kind in ("stat_shifts", "calibration_shifts", "suggestion_outcomes"):
        for item in digest[kind]:
            lines.append(f"  {kind[:-1].replace('_', ' ')}: {item}")
    for item in digest["change_points"]:
        lines.append(f"  change point: {item.get('strategy_key')} {item.get('metric')} {item.get('status')} (n = {item.get('n')})")
    lines.append(f"Open suggestions awaiting a decision: {digest['open_suggestions']} (of which experiments: {digest['experiments_waiting']})")
    lines += [f"Note: {n}" for n in digest["notes"]]
    return "\n".join(lines)
