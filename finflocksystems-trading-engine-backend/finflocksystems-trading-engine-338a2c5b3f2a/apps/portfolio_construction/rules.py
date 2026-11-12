from decimal import Decimal


D = Decimal

TIMEFRAME_OPTIONS = (
    ("NOW", "Now, up to 30 days"),
    ("HURRY", "Hurry, 1-3 months"),
    ("FAST", "Fast, 3-12 months"),
    ("BUILD", "Build, 1-3 years"),
    ("GROW", "Grow, 3-7 years"),
    ("COMPOUND", "Compound, 7+ years"),
)
RISK_OPTIONS = (
    (1, "PRESERVATION", "Capital Preservation"),
    (2, "CONSERVATIVE", "Conservative"),
    (3, "BALANCED", "Balanced"),
    (4, "GROWTH", "Growth"),
    (5, "AGGRESSIVE", "Aggressive / High Risk-High Reward"),
)
TIMEFRAME_LABELS = dict(TIMEFRAME_OPTIONS)
RISK_CODES = {level: code for level, code, _ in RISK_OPTIONS}
RISK_LABELS = {level: label for level, _, label in RISK_OPTIONS}
MAXIMUM_RISK = {"NOW": 1, "HURRY": 2, "FAST": 3, "BUILD": 4, "GROW": 5, "COMPOUND": 5}
TIMEFRAME_CASH_FLOOR = {
    "NOW": D("1"), "HURRY": D("0.70"), "FAST": D("0.40"),
    "BUILD": D("0.20"), "GROW": D("0.05"), "COMPOUND": D("0.02"),
}
RISK_CASH_FLOOR = {1: D("0.80"), 2: D("0.50"), 3: D("0.25"), 4: D("0.10"), 5: D("0.02")}
MAXIMUM_STOCK_WEIGHT = {1: D("0.05"), 2: D("0.10"), 3: D("0.15"), 4: D("0.20"), 5: D("0.25")}


def validate_timeframe_risk(timeframe, risk_level):
    if timeframe not in TIMEFRAME_LABELS:
        raise ValueError(f"Unsupported timeframe bucket {timeframe}")
    try:
        risk_level = int(risk_level)
    except (TypeError, ValueError) as exc:
        raise ValueError("Risk level must be an integer from 1 to 5") from exc
    if risk_level not in RISK_CODES:
        raise ValueError("Risk level must be an integer from 1 to 5")
    if risk_level > MAXIMUM_RISK[timeframe]:
        raise ValueError(f"Risk level {risk_level} exceeds the maximum {MAXIMUM_RISK[timeframe]} for {timeframe}")
    return timeframe, risk_level


def resolved_goal_rules(timeframe, risk_level):
    timeframe, risk_level = validate_timeframe_risk(timeframe, risk_level)
    cash_weight = max(TIMEFRAME_CASH_FLOOR[timeframe], RISK_CASH_FLOOR[risk_level])
    return {
        "timeframe_bucket": timeframe,
        "timeframe_label": TIMEFRAME_LABELS[timeframe],
        "risk_level": risk_level,
        "risk_code": RISK_CODES[risk_level],
        "risk_label": RISK_LABELS[risk_level],
        "maximum_allowed_risk": MAXIMUM_RISK[timeframe],
        "minimum_cash_weight": cash_weight,
        "maximum_stock_weight": MAXIMUM_STOCK_WEIGHT[risk_level],
        "optimizer_method": None if timeframe == "NOW" else (
            "MINIMUM_VARIANCE" if risk_level <= 3 else "MAXIMUM_SHARPE"
        ),
        "lookback_days": 252,
        "minimum_history_observations": 60,
        "long_only": True,
    }

