"""Deterministic rule analyzers (Phase 3). They run before the LLM and their findings are passed to it as
established facts; they are also the whole review when the LLM is unavailable (fallback).

Each rule reads only `facts` (the flat citable map built by services/facts.py) so every finding cites keys that
exist. Thresholds are configurable through settings.INTELLIGENCE_RULE_THRESHOLDS (dict, merged over DEFAULTS).
"""
from dataclasses import dataclass, field

from django.conf import settings

DEFAULTS = {
    "high_cost_bps": 30.0,          # commissions as bps of entry notional
    "slippage_bps": 10.0,           # adverse fill vs reference, either side
    "entry_margin_tiny": 2.0,       # points over a gate threshold
    "low_tqs_coverage": 0.80,       # share of TQS weight actually measured
    "whipsaw_count": 3,
    "hedge_fill_delay_seconds": 30,
    "pop_missed_floor": 0.60,       # predicted POP at or above this and the trade lost
    "resize_ratio_floor": 0.99,
    "param_change_hours": 24.0,      # a settings change this recently before entry is worth a mention
    "news_burst_zscore": 2.0,        # news volume this many standard deviations above its 30-day norm
}

IV_FACTORS = {"C1_IVRANK", "C2_VRP", "C3_TERM"}


@dataclass
class Finding:
    rule_id: str
    category: str
    severity: str  # LOW | MEDIUM | HIGH
    description: str
    refs: list = field(default_factory=list)
    is_defect: bool = False


def thresholds():
    merged = dict(DEFAULTS)
    merged.update(getattr(settings, "INTELLIGENCE_RULE_THRESHOLDS", {}) or {})
    return merged


def _num(facts, key):
    value = facts.get(key)
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _flag_keys(facts):
    """{flag name: citable key}"""
    return {v: k for k, v in facts.items() if k.startswith("data_quality_flags[")}


def analyze(facts):
    """Return a list of Findings for one episode's fact map."""
    t = thresholds()
    out = []
    flag_key = _flag_keys(facts)
    flags = set(flag_key)
    family = facts.get("episode.asset_family")
    is_option = family == "OPTION"

    band = facts.get("metrics.opt.decision_band")
    if band == "WATCH":
        out.append(Finding(
            "WATCH_BAND_FULL_SIZE", "SIZING", "MEDIUM",
            "The entry was in the WATCH decision band. The engine computes bands but does not enforce them, "
            "so a WATCH setup is traded at the same size as a full-confidence one.",
            ["metrics.opt.decision_band", "metrics.opt.size_multiplier"], is_defect=False))

    coverage = _num(facts, "metrics.opt.tqs_coverage")
    if coverage is not None and coverage < t["low_tqs_coverage"]:
        out.append(Finding(
            "LOW_TQS_COVERAGE", "DATA_QUALITY", "MEDIUM",
            f"The trade quality score rests on only {coverage:.0%} of its configured weight "
            f"(threshold {t['low_tqs_coverage']:.0%}).",
            ["metrics.opt.tqs_coverage", "metrics.opt.tqs"]))

    unmeasured = facts.get("metrics.opt.tqs_unmeasured", "")
    neutral_iv = [f for f in str(unmeasured).split(",") if f in IV_FACTORS]
    if neutral_iv or "MISSING_REMOTE_FEATURES" in flags:
        out.append(Finding(
            "NEUTRAL_IV_COMPONENTS", "DATA_QUALITY", "MEDIUM",
            "Volatility inputs were not measured (remote feature database off or factors missing), so "
            "their score components were left out or neutral" + (f": {', '.join(neutral_iv)}." if neutral_iv else "."),
            [k for k in ("metrics.opt.tqs_unmeasured", "metrics.opt.iv_score") if k in facts]))

    tiny = []
    for key in ("metrics.margin.rps", "metrics.margin.tqs", "metrics.margin.iv_score"):
        margin = _num(facts, key)
        if margin is not None and 0 <= margin <= t["entry_margin_tiny"]:
            tiny.append((key, margin))
    if tiny:
        out.append(Finding(
            "ENTRY_MARGIN_TINY", "PARAMETER_MISFIT", "LOW",
            "The entry cleared its gate by a very small margin: "
            + ", ".join(f"{k.split('metrics.margin.')[1]} +{m:g}" for k, m in tiny)
            + ". A tiny margin means the trade was a coin-flip at the threshold, not a strong signal.",
            [k for k, _ in tiny]))

    cost = _num(facts, "metrics.outcome.cost_bps")
    if cost is not None and cost > t["high_cost_bps"]:
        out.append(Finding(
            "HIGH_COST_DRAG", "COST_DRAG", "MEDIUM",
            f"Commissions cost {cost:.1f} bps of the entry notional (threshold {t['high_cost_bps']:g} bps).",
            ["metrics.outcome.cost_bps", "metrics.outcome.commission"]))

    slip = [(k, _num(facts, k)) for k in ("metrics.exec.slippage_bps_entry", "metrics.exec.slippage_bps_exit")]
    slip = [(k, v) for k, v in slip if v is not None and v > t["slippage_bps"]]
    if slip:
        out.append(Finding(
            "SLIPPAGE_ABOVE_THRESHOLD", "EXECUTION_SLIPPAGE", "MEDIUM",
            "Fills were worse than the reference price by more than " f"{t['slippage_bps']:g} bps: "
            + ", ".join(f"{k.rsplit('_', 1)[1]} {v:.1f} bps" for k, v in slip) + ".",
            [k for k, _ in slip], is_defect=True))

    resize = _num(facts, "metrics.sizing.resize_ratio")
    if (resize is not None and resize < t["resize_ratio_floor"]) or "ATTRIBUTION_DRIFT_POSSIBLE" in flags:
        refs = [k for k in ("metrics.sizing.resize_ratio", "metrics.sizing.binding_constraint") if k in facts]
        out.append(Finding(
            "SIZE_RESIZED_BY_RISK", "SIZING", "LOW",
            "Risk checks or sizing reduced the order below its target, so attributed quantities may not "
            "match the broker exactly.", refs or [flag_key["ATTRIBUTION_DRIFT_POSSIBLE"]]))

    whip = _num(facts, "metrics.path.whipsaw_count")
    if whip is not None and whip >= t["whipsaw_count"]:
        out.append(Finding(
            "WHIPSAW_CLUSTER", "REGIME_MISMATCH", "LOW",
            f"Price crossed the entry level {whip:g} times during the trade (threshold {t['whipsaw_count']}), "
            "a choppy path for a trend-following rule.", ["metrics.path.whipsaw_count"]))

    spread = _num(facts, "metrics.opt.entry_fill_spread_seconds")
    if is_option and spread is not None and spread > t["hedge_fill_delay_seconds"]:
        out.append(Finding(
            "HEDGE_FILL_DELAY", "EXECUTION_SLIPPAGE", "MEDIUM",
            f"The legs of the structure filled over {spread:g} seconds (threshold {t['hedge_fill_delay_seconds']}), "
            "leaving the position partly unhedged in between.", ["metrics.opt.entry_fill_spread_seconds"]))

    if is_option and facts.get("metrics.exec.entry_order_type") == "MKT":
        out.append(Finding(
            "MARKET_ORDER_ON_WIDE_SPREAD", "EXECUTION_SLIPPAGE", "LOW",
            "Option legs were entered with market orders. The bid/ask spread at entry is not recorded, "
            "so its cost cannot be measured.", ["metrics.exec.entry_order_type"]))

    if "PAPER_DELAYED_DATA" in flags:
        out.append(Finding(
            "PAPER_DELAYED_DATA_CAVEAT", "DATA_QUALITY", "LOW",
            "Paper trading ran on delayed market data; bar timestamps lag the fills, so path metrics "
            "(MAE/MFE) are approximate and are not comparable with live trading.",
            [flag_key["PAPER_DELAYED_DATA"]]))

    if "OPTION_RISK_BYPASS" in flags:
        out.append(Finding(
            "OPTION_RISK_BYPASS_ACTIVE", "RULE_VIOLATION", "HIGH",
            "Option pre-trade risk checks were disabled (OPTION_PRETRADE_RISK_ENABLED=false) while this trade was open.",
            [flag_key["OPTION_RISK_BYPASS"]], is_defect=True))

    pop = _num(facts, "metrics.opt.pop")
    net = _num(facts, "metrics.outcome.net_pnl")
    if (pop is not None and net is not None and facts.get("metrics.outcome.is_closed")
            and pop >= t["pop_missed_floor"] and net < 0):
        out.append(Finding(
            "PREDICTED_POP_MISSED", "MODEL_MISCALIBRATION", "LOW",
            f"The model gave this structure a {pop:.0%} probability of profit and it lost money. One loss at "
            "that probability is expected; it says little about calibration (needs many trades).",
            ["metrics.opt.pop", "metrics.outcome.net_pnl"]))

    if facts.get("metrics.exit.reason") == "EXPIRED_NO_FILL":
        out.append(Finding(
            "EXPIRED_NO_FILL_AFTER_ERROR", "RULE_VIOLATION", "HIGH",
            "The position expired with no closing order. The strategy normally exits on expiry day; an "
            "evaluation error probably stopped it (instance in ERROR state).",
            ["metrics.exit.reason"], is_defect=True))

    profile = facts.get("operator.profile.name")
    if profile and tiny:
        out.append(Finding(
            "PROFILE_GATE_MARGIN_TINY", "PARAMETER_MISFIT", "LOW",
            f"Under the {profile} risk profile the entry cleared its gate by a very small margin ("
            + ", ".join(f"{k.split('metrics.margin.')[1]} +{m:g}" for k, m in tiny)
            + "). A different profile moves these thresholds; one trade cannot say which profile fits.",
            ["operator.profile.name"] + [k for k, _ in tiny]))

    since_change = _num(facts, "operator.hours_since_last_change_before_entry")
    if since_change is not None and since_change <= t["param_change_hours"]:
        out.append(Finding(
            "PARAM_CHANGED_NEAR_ENTRY", "PARAMETER_MISFIT", "LOW",
            f"The strategy's settings were changed {since_change:g} hours before this entry (threshold "
            f"{t['param_change_hours']:g} h). A fresh change has no track record, so this trade tests the change, "
            "not the previous settings.", ["operator.hours_since_last_change_before_entry"]))

    if facts.get("operator.profile_changed_during_hold") is True:
        out.append(Finding(
            "PROFILE_CHANGED_DURING_HOLD", "MANUAL_INTERVENTION", "MEDIUM",
            "The risk profile was changed while the trade was open, so the exit rules may not be the ones in force "
            "at entry.", ["operator.profile_changed_during_hold"]))

    manual_orders = _num(facts, "operator.manual_orders_during_hold")
    if manual_orders and "MANUAL_INTERVENTION" not in flags:
        out.append(Finding(
            "MANUAL_ORDER_DURING_HOLD", "MANUAL_INTERVENTION", "MEDIUM",
            f"{manual_orders:g} manual order(s) were placed on this instrument while the trade was open, so the "
            "outcome is not purely the strategy's.", ["operator.manual_orders_during_hold"], is_defect=True))

    high_events = _num(facts, "metrics.ctx.high_impact_events_in_hold")
    has_note = bool(facts.get("run_config.intent_note")) or any(k.startswith("operator.events[") and k.endswith(".note") for k in facts)
    if facts.get("metrics.ctx.event_in_hold") is True and not has_note:
        out.append(Finding(
            "EVENT_IN_HOLD_UNACKNOWLEDGED", "REGIME_MISMATCH", "MEDIUM" if (high_events or 0) >= 1 else "LOW",
            "A scheduled event that matters to this market fell inside the holding period"
            + (f" ({high_events:g} high-impact)" if high_events else "")
            + ", and no operator note says it was considered.",
            [k for k in ("metrics.ctx.event_in_hold", "metrics.ctx.high_impact_events_in_hold") if k in facts]))

    if facts.get("metrics.ctx.earnings_within_dte") is True:
        out.append(Finding(
            "EARNINGS_INSIDE_DTE", "REGIME_MISMATCH", "MEDIUM",
            "An earnings date for the traded symbol fell inside the holding period"
            + (" (before the structure's expiry)." if is_option else "."), ["metrics.ctx.earnings_within_dte"]))

    burst = _num(facts, "metrics.ctx.news_burst_zscore")
    if burst is not None and burst >= t["news_burst_zscore"]:
        out.append(Finding(
            "ENTRY_DURING_NEWS_BURST", "ENTRY_TIMING", "LOW",
            f"News volume in the 24 hours before entry was unusually high (z-score {burst:g} against the previous 30 days; "
            f"threshold {t['news_burst_zscore']:g}).", ["metrics.ctx.news_burst_zscore", "metrics.ctx.news_count_24h_before_entry"]))

    if facts.get("context.available") is False and getattr(settings, "INTELLIGENCE_CONTEXT_ENABLED", False):
        out.append(Finding(
            "CONTEXT_UNAVAILABLE", "DATA_QUALITY", "LOW",
            "No outside context (news, calendars) was stored for this trade's period, so no statement about the world around it "
            "can be made.", ["context.available"]))

    if "MANUAL_INTERVENTION" in flags or facts.get("metrics.exit.reason") in {"MANUAL", "BROKER_IMPORT"}:
        out.append(Finding(
            "MANUAL_INTERVENTION_PRESENT", "MANUAL_INTERVENTION", "MEDIUM",
            "A manual or imported order touched this position, so the outcome is not purely the strategy's.",
            [k for k in ("metrics.exit.reason", flag_key.get("MANUAL_INTERVENTION")) if k and k in facts], is_defect=True))
    return out
