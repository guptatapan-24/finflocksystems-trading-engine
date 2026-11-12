"""Lesson predicate DSL (Phase 5). Machine-checkable, deterministic, and limited to EpisodeMetrics keys (R3).

    {"all": [{"metric": "entry.rsi", "op": "<", "value": 25}, {"metric": "regime.vol_pctile", "op": ">", "value": 80}]}

Unknown metrics and operators are rejected. Money-valued metrics are not allowed (R5: no cross-currency money);
use unitless measures such as basis points and ratios. Missing metrics make a predicate "not applicable" (None).
"""
import hashlib
import json

from jsonschema import Draft202012Validator

OPS = ("<", "<=", ">", ">=", "==", "!=", "in")
MAX_CONDITIONS = 5

# Exact keys (see docs/intelligence/METRICS.md). "num" = numeric, "txt" = text, "bool".
NUMERIC = {
    "outcome.cost_bps", "outcome.net_return_bps", "outcome.hold_seconds", "outcome.hold_bars",
    "exec.fill_count", "exec.slippage_bps_entry", "exec.slippage_bps_exit", "sizing.resize_ratio",
    "context.prior_closed_episodes", "context.consecutive_losses_before",
    "path.mae_bps", "path.mfe_bps", "path.time_to_mfe_seconds", "path.capture_ratio", "path.giveback_bps",
    "path.whipsaw_count", "path.bars_used", "regime.realized_vol_20", "regime.vol_pctile",
    "opt.entry_score", "opt.rps_threshold", "margin.rps", "margin.tqs", "margin.iv_score", "opt.tqs", "opt.tqs_coverage",
    "opt.tqs_unmeasured_count", "opt.pop", "opt.breach_up", "opt.breach_down", "opt.iv_score", "opt.iv_rank",
    "opt.iv_percentile", "opt.size_multiplier", "opt.realized_fraction_of_max_profit",
    "opt.realized_fraction_of_max_loss", "opt.credit_capture", "opt.round_trip_cost_over_credit",
    "opt.entry_fill_spread_seconds", "opt.structure_legs", "entry.close",
    # outside context and operator behaviour (Phase 10 / 8, metrics_version 3); absent = not measurable
    "ctx.high_impact_events_in_hold", "ctx.days_to_central_bank_at_entry", "ctx.news_count_24h_before_entry", "ctx.news_burst_zscore",
    "operator.hours_since_last_change_before_entry", "operator.changes_during_hold", "operator.manual_orders_during_hold",
}
TEXT = {
    "exit.reason", "exit.subtype", "exec.entry_order_type", "exec.entry_origin", "sizing.binding_constraint",
    "opt.selected_strategy", "opt.decision_band", "time.entry_bucket", "time.exit_bucket", "time.entry_weekday",
    "asset_family", "selection.risk_profile",
}
BOOL = {"opt.snapshot_linked", "outcome.is_closed", "ctx.available", "ctx.event_in_hold", "ctx.macro_release_in_hold",
        "ctx.central_bank_in_hold", "ctx.earnings_within_dte", "operator.profile_changed_during_hold"}
# Indicator values captured at the entry/exit decision: entry.<indicator>, entry.prev.<indicator>, exit.<indicator>
PATTERNS = ("entry.prev.", "entry.", "exit.")
FORBIDDEN_MONEY = {
    "outcome.gross_pnl", "outcome.commission", "outcome.net_pnl", "outcome.entry_notional", "outcome.risk_basis",
    "opt.entry_cash", "opt.max_profit", "opt.max_loss", "opt.net_ev", "sizing.target_quantity", "sizing.approved_quantity",
}
OUTCOME_METRICS = {
    "net_return_bps": "outcome.net_return_bps", "cost_bps": "outcome.cost_bps",
    "capture_ratio": "path.capture_ratio", "mae_bps": "path.mae_bps", "mfe_bps": "path.mfe_bps",
    "slippage_bps_entry": "exec.slippage_bps_entry", "slippage_bps_exit": "exec.slippage_bps_exit",
}


class DslError(ValueError):
    pass


def metric_kind(key):
    if key in FORBIDDEN_MONEY:
        raise DslError(f"metric '{key}' is money-valued and cannot be used in a lesson (use basis points or ratios)")
    if key in NUMERIC:
        return "num"
    if key in TEXT:
        return "txt"
    if key in BOOL:
        return "bool"
    if any(key.startswith(p) and len(key) > len(p) for p in PATTERNS):
        return "num"
    raise DslError(f"unknown metric '{key}'")


CONDITION = {
    "type": "object", "required": ["metric", "op", "value"], "additionalProperties": False,
    "properties": {"metric": {"type": "string"}, "op": {"enum": list(OPS)},
                   "value": {"type": ["number", "string", "boolean", "array"]}},
}
SCHEMA = {
    "type": "object", "minProperties": 1, "maxProperties": 1, "additionalProperties": False,
    "properties": {"all": {"type": "array", "minItems": 1, "maxItems": MAX_CONDITIONS, "items": CONDITION},
                   "any": {"type": "array", "minItems": 1, "maxItems": MAX_CONDITIONS, "items": CONDITION}},
}
_VALIDATOR = Draft202012Validator(SCHEMA)


def _norm_value(kind, op, value):
    if op == "in":
        if not isinstance(value, list) or not value or len(value) > 12:
            raise DslError("'in' needs a non-empty list of up to 12 values")
        return sorted(_norm_value(kind, "==", v) for v in value)
    if kind == "num":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise DslError("numeric metrics need a numeric value")
        return float(value)
    if kind == "bool":
        if not isinstance(value, bool):
            raise DslError("boolean metrics need true or false")
        return value
    if op not in ("==", "!="):
        raise DslError("text metrics only support ==, != and in")
    if not isinstance(value, str):
        raise DslError("text metrics need a string value")
    return value.strip()[:64]


def parse_predicate(raw):
    """Parse (string or object), validate and normalise. Raises DslError. Returns the normalised dict."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise DslError("predicate is not valid JSON") from exc
    errors = sorted(_VALIDATOR.iter_errors(raw), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        friendly = {"maxItems": f"too many conditions (at most {MAX_CONDITIONS})", "minItems": "at least one condition is needed"}
        raise DslError(friendly.get(first.validator, first.message[:200]))
    mode, conditions = next(iter(raw.items()))
    out = []
    for cond in conditions:
        kind = metric_kind(cond["metric"])
        if cond["op"] in ("<", "<=", ">", ">=") and kind != "num":
            raise DslError(f"operator {cond['op']} needs a numeric metric")
        out.append({"metric": cond["metric"], "op": cond["op"], "value": _norm_value(kind, cond["op"], cond["value"])})
    out.sort(key=lambda c: (c["metric"], c["op"], json.dumps(c["value"])))
    return {mode: out}


def parse_effect(raw):
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise DslError("expected_effect is not valid JSON") from exc
    if not isinstance(raw, dict) or set(raw) != {"outcome_metric", "direction"}:
        raise DslError("expected_effect needs exactly outcome_metric and direction")
    name = str(raw["outcome_metric"])
    key = OUTCOME_METRICS.get(name) or (name if name in OUTCOME_METRICS.values() else None)
    if key is None:
        raise DslError(f"outcome_metric must be one of {sorted(OUTCOME_METRICS)}")
    if raw["direction"] not in ("positive", "negative"):
        raise DslError("direction must be positive or negative")
    return {"outcome_metric": key, "direction": raw["direction"]}


def canonical_hash(predicate, effect):
    body = json.dumps({"p": predicate, "e": effect}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def metrics_used(predicate):
    return {c["metric"] for conds in predicate.values() for c in conds}


def _cmp(op, actual, expected):
    if op == "in":
        return actual in expected
    if op == "==":
        return actual == expected
    if op == "!=":
        return actual != expected
    return {"<": actual < expected, "<=": actual <= expected, ">": actual > expected, ">=": actual >= expected}[op]


def _eval_condition(cond, metrics):
    if cond["metric"] == "asset_family":
        actual = metrics.get("asset_family")
    else:
        actual = metrics.get(cond["metric"])
    if actual is None:
        return None
    try:
        return _cmp(cond["op"], actual, cond["value"])
    except TypeError:
        return None


def evaluate(predicate, metrics):
    """True / False / None (not applicable: a needed metric is missing)."""
    mode, conditions = next(iter(predicate.items()))
    results = [_eval_condition(c, metrics) for c in conditions]
    if mode == "all":
        if any(r is False for r in results):
            return False
        return None if any(r is None for r in results) else True
    if any(r is True for r in results):
        return True
    return None if any(r is None for r in results) else False


def equivalent(a, b):
    """Duplicate test (no embeddings in v1): identical normalised predicate, or the same metrics with the same
    comparison direction and a numeric threshold within 10% on every condition."""
    if a == b:
        return True
    (mode_a, conds_a), (mode_b, conds_b) = next(iter(a.items())), next(iter(b.items()))
    if mode_a != mode_b or len(conds_a) != len(conds_b):
        return False
    for ca, cb in zip(conds_a, conds_b):
        if ca["metric"] != cb["metric"] or ca["op"] != cb["op"]:
            return False
        va, vb = ca["value"], cb["value"]
        if isinstance(va, float) and isinstance(vb, float):
            if abs(va - vb) > 0.10 * max(abs(va), abs(vb), 1e-9):
                return False
        elif va != vb:
            return False
    return True
