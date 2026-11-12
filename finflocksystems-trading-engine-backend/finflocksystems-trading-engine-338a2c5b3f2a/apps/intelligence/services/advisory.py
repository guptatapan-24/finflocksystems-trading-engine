"""Pre-trade advisory (Phase 6). Shown when a strategy is created, its parameters change, or it is activated.

It never blocks, never changes anything and never raises into the caller: it is context ("last 3 times you used a
setting like this, trades did badly"), built only from lessons that held up, per-currency statistics with n and
intervals, open suggestions, calibration and past runs of the same strategy. The optional acknowledgement for HIGH
advisories is informational and off by default.
"""
import hashlib
import json
import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.intelligence import conf
from apps.intelligence.models import (
    CalibrationStat, Lesson, PerformanceStat, PreTradeAdvisory, RunConfigSnapshot, Suggestion, TradeEpisode,
)

from . import dsl, stats
from .evidence import sanitize_text, sanitize_tree
from .learning import TERMINAL

logger = logging.getLogger(__name__)
CALIBRATION_ALARM = 0.10
MIN_CALIBRATION_N = 20
MIN_COMPARISON_N = 8


def _known_metrics(parameters):
    known = {}
    if parameters.get("rps_threshold") is not None:
        try:
            known["opt.rps_threshold"] = float(parameters["rps_threshold"])
        except (TypeError, ValueError):
            pass
    return known


def _applies(predicate, known):
    result = dsl.evaluate(predicate, known)
    return "yes" if result is True else "no" if result is False else "maybe"


def _lessons(strategy_key, parameters):
    known = _known_metrics(parameters)
    order = {"ESTABLISHED": 0, "SUPPORTED": 1, "HYPOTHESIS": 2}
    rows = []
    for lesson in Lesson.objects.filter(strategy_key=strategy_key).exclude(status__in=list(TERMINAL)):
        applies = _applies(lesson.predicate, known)
        if applies == "no" or (lesson.status == Lesson.Status.HYPOTHESIS and applies != "yes"):
            continue
        rows.append({
            "id": lesson.pk, "statement": sanitize_text(lesson.statement, 300), "status": lesson.status, "applies": applies,
            "n": lesson.support_n + lesson.contradict_n, "supporting": lesson.support_n,
            "contradicting": lesson.contradict_n, "expected_effect": lesson.expected_effect,
            "pattern": dsl_text(lesson.predicate)})
    rows.sort(key=lambda r: (order[r["status"]], -r["n"]))
    return rows[:8]


def dsl_text(predicate):
    mode, conditions = next(iter(predicate.items())) if predicate else ("all", [])
    return (" OR " if mode == "any" else " AND ").join(f"{c['metric']} {c['op']} {c['value']}" for c in conditions)


def _stats(strategy_key, instrument):
    rows = []
    query = PerformanceStat.objects.filter(strategy_key=strategy_key, regime="", structure="", mode="", risk_profile="", context="")
    for row in query.filter(strategy_version=0, instrument="").order_by("currency"):
        rows.append(_stat_row(row, "all instruments"))
    if instrument:
        for row in query.filter(strategy_version=0, instrument=instrument).order_by("currency"):
            rows.append(_stat_row(row, instrument))
    return rows


def _stat_row(row, label):
    return {"scope": label, "currency": row.currency, "n": row.n, "wins": row.wins, "mean_return_bps": row.mean_return_bps,
            "ci_low_bps": row.ci_low, "ci_high_bps": row.ci_high}


def _calibration(strategy_key):
    row = (CalibrationStat.objects.filter(metric="POP", bin="ALL", strategy_key=strategy_key).first()
           or CalibrationStat.objects.filter(metric="POP", bin="ALL", strategy_key="").first())
    if row is None:
        return []
    if row.n < MIN_CALIBRATION_N:
        return [{"level": "INFO", "text": f"Not enough trades to judge the model's probability of profit (n = {row.n}, need {MIN_CALIBRATION_N})."}]
    gap = (row.predicted_mean or 0) - (row.observed_rate or 0)
    if abs(gap) > CALIBRATION_ALARM:
        direction = "over-optimistic" if gap > 0 else "too cautious"
        return [{"level": "WARNING", "text": f"The model's probability of profit looks {direction}: predicted {row.predicted_mean:.2f}, "
                                              f"observed {row.observed_rate:.2f} over n = {row.n} trades."}]
    return []


def _comparisons(strategy_key, parameters, exclude_instance=None):
    rows = []
    snapshots = RunConfigSnapshot.objects.filter(strategy_key=strategy_key).order_by("-captured_at")
    for snap in snapshots[:12]:
        if exclude_instance is not None and snap.instance_id == exclude_instance:
            continue
        version = snap.strategy_version_ref
        old = ((version.configuration_snapshot or {}).get("parameters") or {}) if version is not None else {}
        diff = {k: {"then": old.get(k), "now": parameters.get(k)} for k in sorted(set(old) | set(parameters))
                if old.get(k) != parameters.get(k)}
        episodes = TradeEpisode.objects.filter(instance_id=snap.instance_id, strategy_version=snap.strategy_version,
                                               status__in=["CLOSED", "EXPIRED"]).select_related("metrics_row")
        returns = [float(e.metrics_row.metrics["outcome.net_return_bps"]) for e in episodes
                   if getattr(e, "metrics_row", None) and e.metrics_row.metrics.get("outcome.net_return_bps") is not None]
        mean, low, high = stats.bootstrap_ci(returns, seed=snap.pk, resamples=500)
        rows.append({"snapshot_id": snap.pk, "instance": sanitize_text(snap.instance_name, 64), "version": snap.strategy_version,
                     "same_parameters": not diff, "changed": diff, "n": len(returns), "mean_return_bps": mean,
                     "ci_low_bps": low, "ci_high_bps": high})
        if len(rows) == 5:
            break
    return rows


def build_advisory(strategy_key, parameters, *, instance=None, instrument_symbol="", currency="", trigger="PREVIEW"):
    """Return the advisory as a dict (nothing is written)."""
    parameters = dict(parameters or {})
    lessons = _lessons(strategy_key, parameters)
    stat_rows = _stats(strategy_key, instrument_symbol)
    suggestions = Suggestion.objects.filter(status=Suggestion.Status.PROPOSED, strategy_key=strategy_key)
    if instance is not None:
        suggestions = suggestions.filter(instance_id=instance.pk)
    open_ids = list(suggestions.values_list("pk", flat=True)[:10])
    calibration = _calibration(strategy_key)
    comparisons = _comparisons(strategy_key, parameters, exclude_instance=getattr(instance, "pk", None))

    severity, lines = PreTradeAdvisory.Severity.INFO, []
    for lesson in lessons:
        if lesson["status"] in ("ESTABLISHED", "SUPPORTED"):
            lines.append(f"{lesson['status'].title()} lesson ({'applies' if lesson['applies'] == 'yes' else 'may apply'}, "
                         f"n = {lesson['n']}): {lesson['statement']}")
            if lesson["status"] == "ESTABLISHED" and lesson["expected_effect"].get("direction") == "negative":
                severity = PreTradeAdvisory.Severity.HIGH
            elif severity == PreTradeAdvisory.Severity.INFO:
                severity = PreTradeAdvisory.Severity.WARNING
        else:
            lines.append(f"Hypothesis being tested (n = {lesson['n']}, not evidence yet): {lesson['statement']}")
    for item in comparisons:
        if item["same_parameters"] and item["n"] >= MIN_COMPARISON_N and item["ci_high_bps"] is not None and item["ci_high_bps"] < 0:
            lines.append(f"These exact settings were run before ({item['instance']} v{item['version']}) and lost: "
                         f"mean {item['mean_return_bps']:.0f} bps over n = {item['n']} trades (interval {item['ci_low_bps']:.0f} to {item['ci_high_bps']:.0f}).")
            severity = PreTradeAdvisory.Severity.HIGH
        elif not item["same_parameters"] and item["n"] >= MIN_COMPARISON_N and item["mean_return_bps"] is not None:
            lines.append(f"A past run ({item['instance']} v{item['version']}) with different settings averaged "
                         f"{item['mean_return_bps']:.0f} bps over n = {item['n']} trades.")
    for warning in calibration:
        lines.append(warning["text"])
        if warning["level"] == "WARNING" and severity == PreTradeAdvisory.Severity.INFO:
            severity = PreTradeAdvisory.Severity.WARNING
    if open_ids:
        lines.append(f"{len(open_ids)} suggestion(s) for this strategy are still waiting for your decision.")
    for row in stat_rows:
        lines.append(f"Track record, {row['scope']} ({row['currency']}): n = {row['n']}, {row['wins']} winning"
                     + ("" if row["mean_return_bps"] is None else f", mean {row['mean_return_bps']:.0f} bps")
                     + ("" if row["ci_low_bps"] is None else f" (interval {row['ci_low_bps']:.0f} to {row['ci_high_bps']:.0f})") + ".")
    if not lines:
        lines.append("No history yet for this strategy: nothing to compare against. Early trades are noise, not evidence.")
    payload = {
        "trigger": trigger, "strategy_key": strategy_key, "parameters": parameters, "severity": severity, "summary": " ".join(lines)[:2000],
        "lines": lines, "lessons": lessons, "stats": stat_rows, "open_suggestion_ids": open_ids,
        "calibration_warnings": calibration, "comparisons": comparisons,
        "requires_ack": bool(severity == PreTradeAdvisory.Severity.HIGH and getattr(settings, "INTELLIGENCE_ADVISORY_REQUIRE_ACK", False)),
        "note": "Advice only. It never blocks creating, changing or activating a strategy.",
    }
    return sanitize_tree(payload)  # parameters and names are operator text: neutralise before storing or showing


def record_advisory(payload, *, instance=None, instrument_symbol="", currency=""):
    """Persist (once per instance, parameters, trigger and day). Returns the model row."""
    digest = hashlib.sha256(json.dumps(payload.get("parameters", payload["strategy_key"]), sort_keys=True, default=str).encode()).hexdigest()[:16]
    key = f"advisory:{payload['trigger']}:{getattr(instance, 'pk', 'none')}:{digest}:{timezone.now().date().isoformat()}"
    defaults = dict(
        strategy_key=payload["strategy_key"], instance_id=getattr(instance, "pk", None),
        instance_name=getattr(instance, "name", "")[:128], instrument_symbol=instrument_symbol[:64], currency=currency[:8],
        execution_mode=getattr(instance, "execution_mode", ""), trigger=payload["trigger"],
        parameters=payload.get("parameters", {}), lessons=payload["lessons"], stats=payload["stats"],
        open_suggestion_ids=payload["open_suggestion_ids"], calibration_warnings=payload["calibration_warnings"],
        compared_snapshot_ids=[c["snapshot_id"] for c in payload["comparisons"]], comparisons=payload["comparisons"],
        severity=payload["severity"], summary=payload["summary"], requires_ack=payload["requires_ack"])
    try:
        with transaction.atomic():
            row, _ = PreTradeAdvisory.objects.get_or_create(idempotency_key=key, defaults=defaults)
    except IntegrityError:
        row = PreTradeAdvisory.objects.get(idempotency_key=key)
    return row


def advisory_for_instance(instance, trigger):
    """The additive hook used by the strategy endpoints. Returns a JSON-able dict or None. Never raises."""
    if not conf.enabled():
        return None
    try:
        with transaction.atomic():
            payload = build_advisory(instance.definition.key, instance.parameters, instance=instance,
                                     instrument_symbol=instance.instrument.symbol, currency=instance.instrument.currency,
                                     trigger=trigger)
            payload["parameters"] = dict(instance.parameters or {})
            row = record_advisory(payload, instance=instance, instrument_symbol=instance.instrument.symbol,
                                  currency=instance.instrument.currency)
            payload["id"] = row.pk
            from .capture import attach_advisory

            attach_advisory(instance, {"id": row.pk, "severity": row.severity, "summary": row.summary[:500], "trigger": trigger})
            return json.loads(json.dumps(payload, default=str))
    except Exception:
        logger.exception("intelligence: advisory failed for instance %s", getattr(instance, "pk", None))
        return None
