"""The "try next time" playbook (Phase 11): at most three measurement plans per closed trade.

An experiment is NOT advice and never changes anything (R1). It says: here is a condition that looked relevant in this trade
(from the deterministic rule findings), here is what to compare, here is how many matching trades are needed before anyone
should believe a difference, and here is when to stop. Every experiment:

* is labelled a hypothesis (R5) and carries a minimum sample and a stop rule;
* comes with a HISTORY CHECK: the same condition evaluated over the strategy's past closed trades, with n, the difference,
  an interval and a one-sided permutation p-value, so you see immediately whether the past already says something (it
  usually says "too few trades", and that is stated plainly);
* is tracked forward: only trades that close AFTER it was created count towards its resolution, and the result is judged
  with Benjamini-Hochberg false-discovery control across all experiments resolved together.
No model is involved: the plan comes from the rule findings, the numbers from the stored metrics.
"""
import logging
from datetime import timedelta

from django.utils import timezone

from apps.intelligence.models import LearningEvent, MistakeFinding, Suggestion, SuggestionOutcome, TradeEpisode

from . import dsl, stats
from .learning import _closed_episodes, _event, _metrics_of, thresholds

logger = logging.getLogger(__name__)
MAX_PER_TRADE = 3
OUTCOME_KEY = "outcome.net_return_bps"
SEVERITY_RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
OPEN_STATUSES = (Suggestion.Status.PROPOSED, Suggestion.Status.ACKNOWLEDGED, Suggestion.Status.ACCEPTED)


def _cond(metric, op, value):
    return {"all": [{"metric": metric, "op": op, "value": value}]}


# rule id -> how to turn the finding into a measurement plan. `predicate` is evaluated on EpisodeMetrics (entry-time facts only).
TEMPLATES = {
    "EVENT_IN_HOLD_UNACKNOWLEDGED": dict(
        title="Trades held through a scheduled market event", predicate=_cond("ctx.event_in_hold", "==", True), direction="negative",
        expected="Net return is lower when a relevant scheduled event falls inside the holding period.",
        do="When you knowingly hold through a scheduled event, say so in the strategy's intent note, and compare those trades with the others. "
           "If they do worse over the minimum sample, test avoiding or sizing down such entries on paper first."),
    "EARNINGS_INSIDE_DTE": dict(
        title="Trades whose holding period contains an earnings date", predicate=_cond("ctx.earnings_within_dte", "==", True), direction="negative",
        expected="Net return is lower (or more variable) when the traded symbol reports earnings during the trade.",
        do="Track these trades separately. If they underperform over the minimum sample, test skipping entries when earnings fall inside the planned hold, on paper first."),
    "ENTRY_DURING_NEWS_BURST": dict(
        title="Entries during an unusual news burst", predicate=_cond("ctx.news_burst_zscore", ">=", 2.0), direction="negative",
        expected="Net return is lower when news volume before entry is far above its 30-day norm.",
        do="Compare entries made during a news burst with the rest. If they do worse over the minimum sample, test waiting for the burst to pass, on paper first."),
    "PARAM_CHANGED_NEAR_ENTRY": dict(
        title="Entries soon after a settings change", predicate=_cond("operator.hours_since_last_change_before_entry", "<=", 24), direction="negative",
        expected="Net return is lower in the first day after the strategy's settings were changed.",
        do="Note when you change settings and compare the first trades after each change with the rest. If they do worse over the minimum sample, test letting a change run on paper before it trades."),
    "MANUAL_ORDER_DURING_HOLD": dict(
        title="Trades with a manual order while open", predicate=_cond("operator.manual_orders_during_hold", ">=", 1), direction="negative",
        expected="Net return is lower when a manual order is placed on the position during the hold.",
        do="Keep manual orders out of strategy positions for a while and compare trades that had one with those that did not."),
    "PROFILE_CHANGED_DURING_HOLD": dict(
        title="Risk profile changed while a trade was open", predicate=_cond("operator.profile_changed_during_hold", "==", True), direction="negative",
        expected="Net return is lower when the risk profile is changed during a trade.",
        do="Avoid changing the risk profile while a position is open and compare the trades where it happened."),
    "ENTRY_MARGIN_TINY": dict(
        title="Entries that barely cleared the RPS floor", predicate=_cond("margin.rps", "<=", 2.0), direction="negative",
        expected="Net return is lower when the entry cleared the RPS floor by two points or less.",
        do="Compare trades that cleared the floor by 2 points or less with the rest. A stricter floor is a parameter change that needs far more evidence than one trade."),
    "PROFILE_GATE_MARGIN_TINY": dict(
        title="Entries that barely cleared the risk profile's gate", predicate=_cond("margin.rps", "<=", 2.0), direction="negative",
        expected="Net return is lower when the entry cleared the profile's RPS floor by two points or less.",
        do="Compare these entries by risk profile and with the rest before concluding that another profile would fit better."),
    "WATCH_BAND_FULL_SIZE": dict(
        title="WATCH-band setups traded at full size", predicate=_cond("opt.decision_band", "==", "WATCH"), direction="negative",
        expected="Net return is lower for WATCH-band entries than for the others.",
        do="The engine does not enforce band sizing. Compare WATCH entries with the rest; if they do worse over the minimum sample, test half size for WATCH setups on paper."),
    "LOW_TQS_COVERAGE": dict(
        title="Entries scored on incomplete data", predicate=_cond("opt.tqs_coverage", "<", 0.8), direction="negative",
        expected="Net return is lower when less than 80% of the trade-quality score could be measured.",
        do="Compare entries scored on incomplete data with the rest, and check why the inputs were missing."),
    "NEUTRAL_IV_COMPONENTS": dict(
        title="Entries with volatility inputs missing", predicate=_cond("opt.tqs_unmeasured_count", ">=", 1), direction="negative",
        expected="Net return is lower when volatility score components were left out.",
        do="Compare these entries with the rest and check the remote feature database that supplies the missing inputs."),
}
ORDER = list(TEMPLATES)


def history_check(strategy_key, predicate, direction, seed=0, exclude_episode_id=None):
    """The experiment's condition evaluated over the strategy's past closed trades. Always returns n and a plain verdict."""
    values_in, values_out = [], []
    for episode in _closed_episodes(strategy_key):
        if episode.pk == exclude_episode_id:
            continue
        metrics = _metrics_of(episode)
        value = metrics.get(OUTCOME_KEY)
        if value is None or isinstance(value, bool):
            continue
        hit = dsl.evaluate(predicate, metrics)
        if hit is True:
            values_in.append(float(value))
        elif hit is False:
            values_out.append(float(value))
    mean_in, mean_out = stats.mean(values_in), stats.mean(values_out)
    delta, low, high = stats.diff_ci(values_in, values_out, seed=seed, resamples=500)
    p_value = stats.permutation_pvalue(values_in, values_out, direction, seed=seed, resamples=1000)
    if len(values_in) < 3 or len(values_out) < 3:
        verdict = (f"TOO FEW: {len(values_in)} past trade(s) matched and {len(values_out)} did not; "
                   "no comparison is possible yet (this is the normal state early on).")
    elif p_value is not None and p_value <= 0.10 and ((delta < 0) if direction == "negative" else (delta > 0)):
        verdict = (f"SUGGESTIVE: matching trades averaged {mean_in:.1f} bps against {mean_out:.1f} bps (n = {len(values_in)} vs {len(values_out)}, "
                   f"one-sided p = {p_value:.3f}). Several experiments are tested together, so this is not enough on its own.")
    else:
        verdict = (f"NOT SUPPORTED YET: matching trades averaged {mean_in:.1f} bps against {mean_out:.1f} bps "
                   f"(n = {len(values_in)} vs {len(values_out)}, one-sided p = {p_value:.3f}).")
    return {"n_match": len(values_in), "n_other": len(values_out), "mean_match_bps": mean_in, "mean_other_bps": mean_out,
            "delta_bps": delta, "ci_low": low, "ci_high": high, "p_value": p_value, "direction": direction, "verdict": verdict}


def build_for_review(review):
    """Create up to three experiments for a review's trade. Idempotent per (review, rule). Returns the created suggestions."""
    episode = review.episode
    if episode is None or episode.status == TradeEpisode.Status.OPEN:
        return []
    t = thresholds()
    findings = [f for f in MistakeFinding.objects.filter(review=review, source=MistakeFinding.Source.RULE) if f.rule_id in TEMPLATES]
    findings.sort(key=lambda f: (-SEVERITY_RANK.get(f.severity, 0), not f.is_defect, ORDER.index(f.rule_id)))
    created = []
    for finding in findings:
        if len(created) >= MAX_PER_TRADE:
            break
        spec = TEMPLATES[finding.rule_id]
        predicate = dsl.parse_predicate(spec["predicate"])
        effect = {"outcome_metric": OUTCOME_KEY, "direction": spec["direction"]}
        key = dsl.canonical_hash(predicate, effect)
        if Suggestion.objects.filter(kind="EXPERIMENT", strategy_key=episode.strategy_key, status__in=OPEN_STATUSES, experiment__key=key).exists():
            continue  # one open experiment per condition and strategy
        key_idem = f"experiment:{review.pk}:{finding.rule_id}"
        if Suggestion.objects.filter(idempotency_key=key_idem).exists():
            continue
        min_n = int(t.get("experiment_min_n", 8))
        history = history_check(episode.strategy_key, predicate, spec["direction"], seed=episode.pk, exclude_episode_id=episode.pk)
        experiment = {
            "key": key, "rule_id": finding.rule_id, "title": spec["title"], "try": spec["do"], "predicate": predicate,
            "metric": OUTCOME_KEY, "direction": spec["direction"], "min_n": min_n, "paper_only": True,
            "stop_rule": f"Resolve after {min_n} matching trades close; stop after {int(t.get('experiment_days', 90))} days if fewer have.",
            "tracking_from": (episode.closed_at or timezone.now()).isoformat(), "label": "HYPOTHESIS", "history_check": history}
        created.append(Suggestion.objects.create(
            review=review, strategy_key=episode.strategy_key, instance_id=episode.instance_id, instance_name=episode.instance_name,
            kind="EXPERIMENT", rationale=finding.description[:1000],
            expected_effect=spec["expected"], risk_of_change="None until you act: this is a measurement plan, nothing is changed.",
            evidence_needed=f"{min_n} later trades that match the condition, compared with the rest (judged with multiple-comparison control).",
            evidence_refs=list(finding.evidence_refs or []), is_hypothesis=True, idempotency_key=key_idem, experiment=experiment))
    return created


def forward_progress(suggestion):
    """(matching forward trades so far, needed) for an experiment."""
    info = suggestion.experiment or {}
    start = info.get("tracking_from")
    predicate = info.get("predicate")
    if not start or not predicate:
        return 0, int(info.get("min_n", 0))
    count = 0
    for episode in _closed_episodes(suggestion.strategy_key):
        if episode.opened_at.isoformat() < start:
            continue
        if dsl.evaluate(predicate, _metrics_of(episode)) is True:
            count += 1
    return count, int(info.get("min_n", 8))


def resolve_experiments(now=None):
    """Judge every experiment that has enough matching forward trades, together, with Benjamini-Hochberg control; expire the
    ones that ran out of time. Returns {"resolved": n, "expired": n}."""
    now = now or timezone.now()
    t = thresholds()
    days = int(t.get("experiment_days", 90))
    rows = []
    expired = 0
    for suggestion in Suggestion.objects.filter(kind="EXPERIMENT", status__in=OPEN_STATUSES, outcome__isnull=True):
        info = suggestion.experiment or {}
        if not info.get("predicate"):
            continue
        direction = info.get("direction", "negative")
        forward, others = [], []
        for episode in _closed_episodes(suggestion.strategy_key):
            metrics = _metrics_of(episode)
            value = metrics.get(OUTCOME_KEY)
            if value is None or isinstance(value, bool):
                continue
            hit = dsl.evaluate(info["predicate"], metrics)
            if hit is True and episode.opened_at.isoformat() >= info.get("tracking_from", ""):
                forward.append(float(value))
            elif hit is False:
                others.append(float(value))
        if len(forward) < int(info.get("min_n", 8)):
            if now - suggestion.created_at > timedelta(days=days):
                suggestion.status = Suggestion.Status.EXPIRED
                suggestion.decision_reason = "Expired: not enough matching trades closed in time."
                suggestion.decided_at = now
                suggestion.save(update_fields=["status", "decision_reason", "decided_at"])
                expired += 1
            continue
        delta, low, high = stats.diff_ci(forward, others, seed=suggestion.pk, resamples=t["bootstrap"])
        p_value = stats.permutation_pvalue(forward, others, direction, seed=suggestion.pk, resamples=t["bootstrap"] * 2)
        rows.append((suggestion, forward, others, delta, low, high, p_value, direction))
    qs = stats.bh_adjust([r[6] for r in rows])
    resolved = 0
    for (suggestion, forward, others, delta, low, high, p_value, direction), q in zip(rows, qs):
        survives = q is not None and q <= t["fdr_q"]
        if not survives or low is None:
            result = SuggestionOutcome.Result.INCONCLUSIVE
        elif low > 0:
            result = SuggestionOutcome.Result.HELPED  # the condition goes with better outcomes
        elif high < 0:
            result = SuggestionOutcome.Result.HURT  # the condition goes with worse outcomes
        else:
            result = SuggestionOutcome.Result.INCONCLUSIVE
        SuggestionOutcome.objects.create(suggestion=suggestion, result=result, n_before=len(others), n_after=len(forward), delta_bps=delta,
                                         ci_low=low, ci_high=high, evaluated_at=now)
        info = dict(suggestion.experiment or {})
        info["resolution"] = {"p_value": p_value, "q_value": q, "survives_fdr": survives,
                              "reading": ("matching trades did worse" if result == "HURT" else "matching trades did better" if result == "HELPED"
                                          else "no reliable difference after multiple-comparison control")}
        suggestion.experiment = info
        suggestion.save(update_fields=["experiment"])
        _event(LearningEvent.Type.SUGGESTION_RESOLVED, key=f"experiment-outcome:{suggestion.pk}",
               payload={"suggestion_id": suggestion.pk, "experiment": info.get("title"), "parameter": info.get("title"), "result": result,
                        "n_before": len(others), "n_after": len(forward), "delta_bps": delta, "ci": [low, high],
                        "p_value": p_value, "q_value": q})
        resolved += 1
    return {"resolved": resolved, "expired": expired}
