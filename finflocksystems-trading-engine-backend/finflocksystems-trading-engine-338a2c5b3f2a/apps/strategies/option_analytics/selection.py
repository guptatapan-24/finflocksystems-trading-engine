"""Deterministic 14-factor strategy selector from strategy_selection_engine.py (V2).

V2 relaxes several V1 gate thresholds to admit more valid opportunities, rebalances
the TQS weights away from RPS dominance toward breach/economics, uses net
(cost-adjusted) EV consistently in gates and factors, and adds a directional/
two-sided breach gate. See strategy_selection_engine.py for the full rationale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import exp, isfinite
from typing import Any, Dict, List, Optional


POP_FLOORS = {
    "IRON_BUTTERFLY": 0.10,
    "IRON_CONDOR": 0.45,
    "CREDIT_SPREAD": 0.55,
    "CALENDAR": 0.35,
}

WEIGHTS = {
    "A1_RPS": 0.10, "A2_BREACH": 0.10, "A3_BANDFIT": 0.10,
    "B1_EVRISK": 0.05, "B2_PIR": 0.04, "B3_EVGAP": 0.03,
    "C1_IVRANK": 0.05, "C2_VRP": 0.06, "C3_TERM": 0.04,
    "D1_EV_RATIO": 0.10, "D2_POP_ADJ": 0.08, "D3_ROM": 0.10,
    "E1_LIQ": 0.03, "E2_COST": 0.02,
}

# optimizer.txt design principles 1-3: "do not unnecessarily reject borderline
# candidates", "keep genuine risk/safety constraints hard", "treat POP, RPS, IV,
# breach, liquidity and EV as graded optimization factors wherever possible".
#
# Only a structural risk/safety violation may delete a candidate.  Everything
# else is measured, reported, and priced into the portfolio optimizer's
# objective -- which already ships a weight for every one of them
# (`weight_pop`, `weight_rps`, `weight_breach`, `weight_event_risk`,
# `weight_cost`, `weight_ror`) -- so a borderline candidate loses points
# instead of disappearing.
#
# GATE_DATA stays hard because an unmeasurable candidate cannot be graded at
# all, and GATE_CREDIT because a credit structure with no credit against its
# wing is malformed rather than merely poor.
HARD_GATES = ("GATE_BOUNDED", "GATE_EV", "GATE_MAXLOSS", "GATE_LIQ", "GATE_DATA", "GATE_CREDIT")
GRADED_GATES = ("GATE_POP", "GATE_RPS", "GATE_BREACH", "GATE_EVENT", "GATE_COST", "GATE_EV_MATERIAL")

MIN_RPS = 55.0
EXTREME_EVENT_RISK = 90.0
EVENT_RISK_HARD_LIMIT = 90.0
EVENT_PIR_EXCEPTION = 1.25
MIN_NET_EV = 0.0
MIN_EV_MATERIALITY = 0.005
MAX_ABSOLUTE_LOSS = 25_000.0
MAX_CAPITAL_RISK = 0.02
MIN_CREDIT_WING_RATIO = 0.08
MAX_COST_CREDIT_RATIO = 0.50
MIN_LIQUIDITY = 0.15
# Aligned with the entry signal's own 0.50 ceiling, and for the same reason:
# `strategy_breach_probability` now reads a *settlement* probability, not the
# ~1.8x-larger touch figure these 0.70s were tuned against. An operator-supplied
# `rps_max_upper/lower_breach_probability` still overrides both through
# `_entry_signal_breach_tolerance`, so the two gates cannot disagree.
MAX_DIRECTIONAL_BREACH = 0.50
MAX_TWO_SIDED_BREACH = 0.50

DECISION_BAND_FULL = 70.0
DECISION_BAND_WATCH = 50.0
DEFAULT_TQS_THRESHOLD = 60.0
DEFAULT_MIN_NET_PNL = 0.0
ROUTE_IV_RANK_THRESHOLD = 60.0
ROUTE_SKEW_GAP = 0.10


@dataclass(frozen=True)
class SelectionConfig:
    """Every gate/weight/band the TQS strategy selector uses, overridable from
    the frontend-supplied `strategy_parameters` (prefixed `rps_selection_*`)
    instead of being fixed at these module defaults."""

    pop_floors: Dict[str, float] = field(default_factory=lambda: dict(POP_FLOORS))
    weights: Dict[str, float] = field(default_factory=lambda: dict(WEIGHTS))
    min_rps: float = MIN_RPS
    extreme_event_risk: float = EXTREME_EVENT_RISK
    event_risk_hard_limit: float = EVENT_RISK_HARD_LIMIT
    event_pir_exception: float = EVENT_PIR_EXCEPTION
    min_net_ev: float = MIN_NET_EV
    min_ev_materiality: float = MIN_EV_MATERIALITY
    max_absolute_loss: float = MAX_ABSOLUTE_LOSS
    max_capital_risk: float = MAX_CAPITAL_RISK
    min_credit_wing_ratio: float = MIN_CREDIT_WING_RATIO
    max_cost_credit_ratio: float = MAX_COST_CREDIT_RATIO
    min_liquidity: float = MIN_LIQUIDITY
    max_directional_breach: float = MAX_DIRECTIONAL_BREACH
    max_two_sided_breach: float = MAX_TWO_SIDED_BREACH
    decision_band_full: float = DECISION_BAND_FULL
    decision_band_watch: float = DECISION_BAND_WATCH
    route_iv_rank_threshold: float = ROUTE_IV_RANK_THRESHOLD
    route_skew_gap: float = ROUTE_SKEW_GAP

    @classmethod
    def from_parameters(cls, parameters: dict[str, Any]) -> "SelectionConfig":
        defaults = cls()
        pop_floors = dict(defaults.pop_floors)
        for family in pop_floors:
            key = f"rps_selection_pop_floor_{family.lower()}"
            if key in parameters:
                pop_floors[family] = _percent(parameters[key])
        weights = dict(defaults.weights)
        for name in weights:
            key = f"rps_selection_weight_{name.lower()}"
            if key in parameters:
                weights[name] = float(parameters[key])
        return cls(
            pop_floors=pop_floors,
            weights=weights,
            min_rps=float(parameters.get("rps_selection_min_rps", defaults.min_rps)),
            extreme_event_risk=float(parameters.get("rps_selection_extreme_event_risk", defaults.extreme_event_risk)),
            event_risk_hard_limit=float(parameters.get("rps_selection_event_risk_hard_limit", defaults.event_risk_hard_limit)),
            event_pir_exception=float(parameters.get("rps_selection_event_pir_exception", defaults.event_pir_exception)),
            min_net_ev=float(parameters.get("rps_selection_min_net_ev", defaults.min_net_ev)),
            min_ev_materiality=_supplied_percent(parameters, "rps_selection_min_ev_materiality", defaults.min_ev_materiality),
            max_absolute_loss=float(parameters.get("rps_selection_max_absolute_loss", defaults.max_absolute_loss)),
            max_capital_risk=_supplied_percent(parameters, "rps_selection_max_capital_risk", defaults.max_capital_risk),
            min_credit_wing_ratio=_supplied_percent(parameters, "rps_selection_min_credit_wing_ratio", defaults.min_credit_wing_ratio),
            max_cost_credit_ratio=_supplied_percent(parameters, "rps_selection_max_cost_credit_ratio", defaults.max_cost_credit_ratio),
            min_liquidity=_supplied_percent(parameters, "rps_selection_min_liquidity", defaults.min_liquidity),
            max_directional_breach=_supplied_percent(
                parameters, "rps_selection_max_directional_breach",
                _entry_signal_breach_tolerance(parameters, defaults.max_directional_breach),
            ),
            max_two_sided_breach=_supplied_percent(
                parameters, "rps_selection_max_two_sided_breach",
                _two_sided_breach_tolerance(parameters, defaults.max_two_sided_breach),
            ),
            decision_band_full=float(parameters.get("rps_selection_decision_band_full", defaults.decision_band_full)),
            decision_band_watch=float(parameters.get("rps_selection_decision_band_watch", defaults.decision_band_watch)),
            route_iv_rank_threshold=float(parameters.get("rps_selection_route_iv_rank_threshold", defaults.route_iv_rank_threshold)),
            route_skew_gap=_supplied_percent(parameters, "rps_selection_route_skew_gap", defaults.route_skew_gap),
        )


def _two_sided_breach_tolerance(parameters: dict[str, Any], fallback: float) -> float:
    """GATE_BREACH's ceiling for a structure that sold both sides.

    Prefers the operator's explicit two-sided control so this gate and the entry
    signal (`EntrySignalConfig.two_sided_ceiling`) hold the same number to the
    same limit; otherwise falls back to the per-side pair exactly as before."""
    supplied = parameters.get("rps_max_two_sided_breach_probability")
    if supplied not in (None, ""):
        return _fraction(supplied)
    return _entry_signal_breach_tolerance(parameters, fallback)


def _entry_signal_breach_tolerance(parameters: dict[str, Any], fallback: float) -> float:
    """GATE_BREACH re-checks the *same* simulated breach probability the entry
    signal already gated (rps.py builds MarketState straight from the entry
    signal's upper/lower_breach_probability), so the operator-facing
    `rps_max_upper/lower_breach_probability` is the tolerance for this number.
    Defaulting this gate to its own unrelated constant meant the stricter of the
    two silently won -- a run configured for 90% tolerance was still rejected at
    the 70% default, which alone accounted for ~1,025 of 2,880 put-credit-spread
    evaluations in a 6-month replay. The explicit `rps_selection_*` override is
    still honoured first for anyone who genuinely wants a separate gate."""
    tolerances = [
        parameters[key] for key in
        ("rps_max_upper_breach_probability", "rps_max_lower_breach_probability")
        if parameters.get(key) is not None
    ]
    if not tolerances:
        return fallback
    # The tighter of the two sides, so neither side can be breached beyond what
    # the operator allowed for it.
    return min(_fraction(value) for value in tolerances)


def _fraction(value) -> float:
    """Magnitude heuristic kept ONLY for the entry-signal breach tolerances, so
    this gate keeps reading `rps_max_upper/lower_breach_probability` exactly the
    way `entry_signal._probability` does. Never use it for an `rps_selection_*`
    threshold: it cannot tell a sub-1% percent input from a fraction."""
    number = float(value)
    return number / 100 if number > 1 else number


def _percent(value) -> float:
    """Every `rps_selection_*` ratio crosses the API in percent (0-100), which is
    what the frontend sends and what the backtest validator documents, so the
    conversion is unconditional.

    It used to go through `_fraction`, whose `> 1` test silently passed through
    any percent value below 1. `min_ev_materiality` is the one threshold whose
    working range sits in that blind spot -- a UI default of 0.5 (%) arrived as
    the fraction 0.5, demanding net EV worth half of max loss. No defined-risk
    credit structure can pay that, so GATE_EV_MATERIAL rejected 1,616 of 1,616
    gated candidates in a 6-month NIFTY replay and the run booked zero trades."""
    return float(value) / 100


def _supplied_percent(parameters: dict[str, Any], key: str, default: float) -> float:
    """Percent-convert an operator-supplied override; leave the module default
    (already a fraction) alone."""
    value = parameters.get(key)
    return default if value is None else _percent(value)


DEFAULT_SELECTION_CONFIG = SelectionConfig()


@dataclass(frozen=True)
class MarketState:
    rps: float
    upper_breach_probability: float
    lower_breach_probability: float
    expected_move: float
    event_risk: float
    pir: float
    event_expiry_gap_sessions: float
    iv_rank: float
    iv_minus_realized: float
    term_structure_slope: float
    capital: float
    data_quality_ok: bool = True
    # A factor built on an input nobody measured is not a low score, it is an
    # absent one. These say which of the fields above are real readings rather
    # than the neutral placeholders `trading.rps._selection_market` substitutes
    # when the upstream feature is missing, so `calculate_factors` can return
    # `None` and `calculate_tqs` can leave that weight out of the average
    # instead of scoring every candidate on the same fabricated constant.
    event_risk_available: bool = True
    term_structure_available: bool = True


@dataclass
class StrategyCandidate:
    strategy: str
    expected_pnl: float
    pop: float
    max_loss: float
    margin: float
    net_credit: float = 0.0
    wing_width: float = 0.0
    liquidity_score: float = 0.0
    round_trip_cost: float = 0.0
    max_profit: float = 0.0
    defined_risk: bool = True
    max_loss_probability: Optional[float] = None
    # This candidate's own modelled crossing probabilities, measured against
    # its own short/wing strikes.  The shared `MarketState` pair is derived
    # from whichever candidate happened to sort first, so gating every
    # structure on it charged an iron condor for a put spread's boundaries.
    # `None` on both sides falls back to the market pair (a pre-candidate,
    # ATR-only check); `None` on one side means the structure has no strike
    # there and that side carries no breach risk.
    upper_breach_probability: Optional[float] = None
    lower_breach_probability: Optional[float] = None
    gates: Dict[str, bool] = field(default_factory=dict)
    # Hard (structural) failures only -- these delete the candidate.
    gate_failures: List[str] = field(default_factory=list)
    # Graded shortfalls: measured, reported, and priced into the optimizer's
    # objective, never a rejection. Kept separate so the ledger can still say
    # "POP below the family floor" without that having blocked the trade.
    advisories: List[str] = field(default_factory=list)
    factors: Dict[str, float] = field(default_factory=dict)
    tqs: Optional[float] = None
    # What the TQS above actually rests on: the share of the configured weight
    # that could be measured, and the names that could not. Without these a
    # score over two thirds of the factors is indistinguishable from one over
    # all of them.
    tqs_factor_coverage: Optional[float] = None
    tqs_unmeasured_factors: List[str] = field(default_factory=list)
    decision_band: str = "NO_TRADE"
    size_multiplier: float = 0.0
    net_expected_pnl: Optional[float] = None
    risk_adjusted_return: Optional[float] = None
    selection_score: Optional[float] = None


def _clip(value):
    return max(0.0, min(1.0, float(value)))


def _finite(value):
    return isinstance(value, (int, float)) and isfinite(float(value))


def lin_up(value, low, high):
    if not _finite(value) or high <= low:
        return 0.0
    return _clip((value - low) / (high - low))


def lin_dn(value, low, high):
    return 1.0 - lin_up(value, low, high)


def logit_up(value, midpoint, k):
    if not _finite(value):
        return 0.0
    z = max(-60.0, min(60.0, k * (value - midpoint)))
    return 1.0 / (1.0 + exp(-z))


def band(value, low, best_low, best_high, high):
    if not _finite(value) or not low < best_low <= best_high < high:
        return 0.0
    if value <= low or value >= high:
        return 0.0
    if best_low <= value <= best_high:
        return 1.0
    return (value - low) / (best_low - low) if value < best_low else (high - value) / (high - best_high)


def strategy_family(strategy):
    name = strategy.upper().replace("-", "_").replace(" ", "_")
    if "BUTTERFLY" in name:
        return "IRON_BUTTERFLY"
    if "CONDOR" in name:
        return "IRON_CONDOR"
    if "CALENDAR" in name:
        return "CALENDAR"
    if "SPREAD" in name:
        return "CREDIT_SPREAD"
    raise ValueError(f"Unsupported strategy: {strategy}")


def strategy_breach_probability(candidate, market):
    """The probability this structure settles outside the range it sold.

    A directional credit spread is only at risk on its short side, so it is
    judged on that side alone -- taking the worse of the two would let a
    boundary the position does not hold reject (or score) it.

    A two-sided structure is judged on the **sum** of its sides, which under a
    settlement definition is exactly the union: the index cannot finish both
    above the short call and below the short put, so the two events are
    mutually exclusive. `max(upper, lower)` was a defensible stand-in while
    this measured a barrier *touch*, where the two events overlap heavily and
    a path that tests one boundary often tests the other. It is not defensible
    for a settlement, where it understates the real risk by up to 2x: a butterfly
    at 49.3% above and 48.0% below scored 0.493 here and cleared a 0.50 ceiling
    while actually carrying a 97.3% chance of finishing outside its wings.

    Capped at 1.0 so a pair that rounds past unity stays a probability.

    Prefers the candidate's own modelled probabilities over the shared market
    pair, so each structure is judged on the boundaries it actually sold."""
    upper, lower = candidate.upper_breach_probability, candidate.lower_breach_probability
    if upper is None and lower is None:
        upper, lower = market.upper_breach_probability, market.lower_breach_probability
    else:
        upper, lower = (0.0 if upper is None else upper), (0.0 if lower is None else lower)
    family = strategy_family(candidate.strategy)
    if family == "CREDIT_SPREAD":
        name = candidate.strategy.upper()
        if "PUT" in name or "BULL" in name:
            return lower
        if "CALL" in name or "BEAR" in name:
            return upper
    return min(1.0, upper + lower)


def route_strategies(market, config: SelectionConfig = DEFAULT_SELECTION_CONFIG):
    if market.event_risk >= config.event_risk_hard_limit and market.pir < config.event_pir_exception:
        return []
    upper = market.upper_breach_probability
    lower = market.lower_breach_probability
    gap = config.route_skew_gap
    if lower + gap < upper:
        return ["PUT_CREDIT_SPREAD", "IRON_CONDOR", "IRON_BUTTERFLY", "CALL_CREDIT_SPREAD"]
    if upper + gap < lower:
        return ["CALL_CREDIT_SPREAD", "IRON_CONDOR", "IRON_BUTTERFLY", "PUT_CREDIT_SPREAD"]
    if market.iv_rank >= config.route_iv_rank_threshold:
        return ["IRON_CONDOR", "IRON_BUTTERFLY", "PUT_CREDIT_SPREAD", "CALL_CREDIT_SPREAD"]
    return ["IRON_CONDOR", "PUT_CREDIT_SPREAD", "CALL_CREDIT_SPREAD", "IRON_BUTTERFLY"]


def apply_gates(candidate, market, config: SelectionConfig = DEFAULT_SELECTION_CONFIG):
    family = strategy_family(candidate.strategy)
    max_allowed_loss = min(config.max_absolute_loss, config.max_capital_risk * market.capital)
    candidate.net_expected_pnl = candidate.expected_pnl - candidate.round_trip_cost
    ev_ratio = candidate.net_expected_pnl / candidate.max_loss if candidate.max_loss > 0 else -999
    if family in {"IRON_BUTTERFLY", "IRON_CONDOR", "CREDIT_SPREAD"}:
        credit_ok = (
            candidate.wing_width > 0 and candidate.net_credit > 0
            and candidate.net_credit >= config.min_credit_wing_ratio * candidate.wing_width
        )
    else:
        credit_ok = True
    if family == "CALENDAR":
        cost_ok = candidate.round_trip_cost <= max(1.0, candidate.margin) * config.max_cost_credit_ratio
    else:
        cost_ok = candidate.net_credit > 0 and candidate.round_trip_cost <= config.max_cost_credit_ratio * candidate.net_credit
    breach = strategy_breach_probability(candidate, market)
    breach_ok = breach <= (config.max_directional_breach if family == "CREDIT_SPREAD" else config.max_two_sided_breach)
    candidate.gates = {
        "GATE_RPS": _finite(market.rps) and market.rps >= config.min_rps,
        "GATE_EVENT": market.event_risk < config.extreme_event_risk or market.pir >= config.event_pir_exception,
        "GATE_EV": _finite(candidate.net_expected_pnl) and candidate.net_expected_pnl > config.min_net_ev,
        "GATE_EV_MATERIAL": ev_ratio >= config.min_ev_materiality,
        "GATE_POP": _finite(candidate.pop) and candidate.pop >= config.pop_floors[family],
        "GATE_MAXLOSS": _finite(candidate.max_loss) and 0.0 < candidate.max_loss <= max_allowed_loss,
        "GATE_CREDIT": credit_ok,
        "GATE_COST": cost_ok,
        "GATE_LIQ": _finite(candidate.liquidity_score) and candidate.liquidity_score >= config.min_liquidity,
        "GATE_BOUNDED": bool(candidate.defined_risk),
        "GATE_DATA": bool(market.data_quality_ok),
        "GATE_BREACH": breach_ok,
    }
    candidate.gate_failures = [name for name in HARD_GATES if not candidate.gates[name]]
    candidate.advisories = [name for name in GRADED_GATES if not candidate.gates[name]]


def calculate_factors(candidate, market):
    c = candidate
    breach = strategy_breach_probability(c, market)
    if c.wing_width > 0 and market.expected_move > 0:
        width_ratio = (c.wing_width / 2.0) / market.expected_move
        a3 = band(width_ratio, 0.50, 0.80, 1.50, 3.50)
    else:
        a3 = None
    payoff_asymmetry = c.max_profit / c.max_loss if c.max_loss > 0 and c.max_profit > 0 else 0.0
    pop_adjusted = c.pop * payoff_asymmetry
    ev_ratio = c.net_expected_pnl / c.max_loss if c.max_loss > 0 else 0
    rom = c.net_expected_pnl / c.margin if c.margin > 0 else 0
    if c.net_credit > 0:
        cost_drag = c.round_trip_cost / c.net_credit
        e2 = lin_dn(cost_drag, 0.05, 0.50)
    else:
        # A debit structure has no credit for the round trip to eat into, so
        # there is no cost drag to score -- not a maximally bad one.
        e2 = None
    events = market.event_risk_available
    return {
        "A1_RPS": lin_up(market.rps, 55.0, 85.0),
        "A2_BREACH": lin_up(1.0 - breach, 0.30, 0.85),
        "A3_BANDFIT": a3,
        "B1_EVRISK": lin_dn(market.event_risk, 20.0, 90.0) if events else None,
        "B2_PIR": logit_up(market.pir, 1.0, 4.0) if events else None,
        "B3_EVGAP": lin_up(market.event_expiry_gap_sessions, 0.0, 3.0) if events else None,
        "C1_IVRANK": lin_up(market.iv_rank, 10.0, 70.0),
        "C2_VRP": lin_up(market.iv_minus_realized, -3.0, 6.0),
        "C3_TERM": (
            lin_up(market.term_structure_slope, -3.0, 3.0)
            if market.term_structure_available else None
        ),
        "D1_EV_RATIO": lin_up(ev_ratio, 0.0, 0.15),
        "D2_POP_ADJ": lin_up(pop_adjusted, 0.05, 0.55),
        "D3_ROM": lin_up(rom, 0.0, 0.20),
        "E1_LIQ": lin_up(c.liquidity_score, 0.15, 0.80),
        "E2_COST": e2,
    }


def calculate_tqs(factors, weights: Dict[str, float] = WEIGHTS):
    """TQS on a true 0-100 scale, over the factors that could actually be measured.

    Two separate things are normalized away here.

    **The weight total.** The shipped `WEIGHTS` table sums to 0.90, so an
    unnormalized `100 * sum(w*f)` capped the attainable score at 90 and quietly
    deducted up to 10 points from every candidate before comparing against
    `rps_tqs_threshold`. Dividing by the weights' own total also keeps operator
    overrides (`rps_selection_weight_*`) on a 0-100 scale whatever they sum to.

    **Factors nobody could measure.** A factor whose input is missing now
    arrives as `None` (see `calculate_factors`) and is left out of both the
    numerator and the denominator, exactly as `trading.rps.build_rps_observations`
    already does for the RPS component scores. Scoring it instead -- which is
    what a placeholder constant amounts to -- was measurably corrosive: with no
    event-risk feed, `B1_EVRISK`/`B2_PIR`/`B3_EVGAP` sat at a fixed
    1.0/0.731/0.333 for every candidate in every session, 13.3% of the weight
    that could not separate any two candidates and handed all of them the same
    +3.34 points. `C3_TERM` did the same at a flat 0.0 slope whenever the
    session carried only a front expiry, and `A3_BANDFIT`/`E2_COST` each had a
    placeholder for structures their ratio is undefined for.

    Leaving them out means a candidate is graded on what is known about it, and
    the surviving factors carry their full proportional weight rather than
    competing with dead ones. It also makes the ceiling honest: with the event
    block absent the maximum is 100, not 74.4.

    Returns 0.0 only if nothing at all could be measured, which cannot happen
    while `select_strategy` scores candidates that have already cleared
    GATE_DATA.
    """
    available = {
        key: _clip(value) for key, value in factors.items()
        if key in weights and value is not None and _finite(value)
    }
    total_weight = sum(weights[key] for key in available)
    if total_weight <= 0:
        return 0.0
    weighted = sum(weights[key] * value for key, value in available.items())
    return round(100.0 * weighted / total_weight, 4)


def unmeasured_factors(factors, weights: Dict[str, float] = WEIGHTS):
    """The weighted factors `calculate_tqs` had to leave out, for the ledger."""
    return sorted(
        key for key in weights
        if factors.get(key) is None or not _finite(factors.get(key))
    )


def factor_coverage(factors, weights: Dict[str, float] = WEIGHTS):
    """Share of the configured weight that a candidate's TQS actually rests on.

    A TQS of 62 over 100% of the weight and a TQS of 62 over 66% of it are not
    the same statement, and the second must not be readable as the first.
    """
    total = sum(weights.values())
    if total <= 0:
        return 0.0
    missing = sum(weights[key] for key in unmeasured_factors(factors, weights))
    return round((total - missing) / total, 6)


def assign_decision_band(tqs, tqs_threshold, config: SelectionConfig = DEFAULT_SELECTION_CONFIG):
    if tqs >= config.decision_band_full:
        return "TRADE_FULL", 1.0
    if tqs >= tqs_threshold:
        return "TRADE_HALF", 0.5
    if tqs >= config.decision_band_watch:
        return "WATCH", 0.0
    return "NO_TRADE", 0.0


def calculate_selection_score(candidate, tqs_threshold):
    if candidate.tqs is None or candidate.risk_adjusted_return is None:
        return float("-inf")
    quality_multiplier = 0.5 + 0.5 * _clip((candidate.tqs - tqs_threshold) / 20.0)
    return candidate.risk_adjusted_return * quality_multiplier


def select_strategy(market: MarketState, candidates: List[StrategyCandidate],
                    tqs_threshold: float = DEFAULT_TQS_THRESHOLD, min_net_pnl: float = DEFAULT_MIN_NET_PNL,
                    config: SelectionConfig = DEFAULT_SELECTION_CONFIG):
    routed = route_strategies(market, config)
    for candidate in candidates:
        apply_gates(candidate, market, config)
        if candidate.gate_failures:
            continue
        candidate.factors = calculate_factors(candidate, market)
        candidate.tqs = calculate_tqs(candidate.factors, config.weights)
        candidate.tqs_factor_coverage = factor_coverage(candidate.factors, config.weights)
        candidate.tqs_unmeasured_factors = unmeasured_factors(candidate.factors, config.weights)
        candidate.decision_band, candidate.size_multiplier = assign_decision_band(candidate.tqs, tqs_threshold, config)
        candidate.risk_adjusted_return = (
            candidate.net_expected_pnl / candidate.max_loss if candidate.max_loss > 0 else None
        )
        if candidate.risk_adjusted_return is not None:
            candidate.selection_score = calculate_selection_score(candidate, tqs_threshold)
    ranked = sorted(candidates, key=_ranking_key, reverse=True)
    # Eligibility is the hard constraints plus the operator's explicit net-EV
    # floor, and nothing else. `tqs_threshold` used to cut here as well; it is
    # now a *grading* input only -- it still shapes `selection_score` through
    # `calculate_selection_score` and still sets the reported decision band, but
    # a candidate below it is handed to the portfolio optimizer with a lower
    # score rather than withheld from it.
    eligible = [
        item for item in candidates
        if not item.gate_failures and (item.net_expected_pnl or 0) > min_net_pnl
    ]
    winner = max(eligible, key=_ranking_key) if eligible else None
    return {
        "decision": winner.decision_band if winner else "NO_TRADE",
        "selected_strategy": winner.strategy if winner else None,
        "selected_candidate": winner,
        # The full set the winner was chosen from -- everything that cleared the
        # hard risk/safety constraints and the net-P&L floor. `winner` is its
        # argmax; a portfolio layer that may take more than one position needs
        # the set itself, and must not re-derive this eligibility rule.
        "eligible_candidates": eligible,
        "ranked_candidates": ranked,
        "routed_strategies": routed,
    }


def _ranking_key(item):
    """Score-first ordering that tolerates an unscored candidate."""
    return (
        item.selection_score if item.selection_score is not None else float("-inf"),
        item.tqs if item.tqs is not None else float("-inf"),
        item.risk_adjusted_return if item.risk_adjusted_return is not None else float("-inf"),
    )


def calculate_lots(capital, max_loss_per_lot, size_multiplier, risk_fraction=.01, max_lots=10):
    if capital <= 0 or max_loss_per_lot <= 0:
        return 0
    return max(0, min(max_lots, int(capital * risk_fraction * size_multiplier / max_loss_per_lot)))
