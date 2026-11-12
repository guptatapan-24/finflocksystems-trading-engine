"""Map measured IBKR observations into the Aryan_Options decision engines.

The order of decisions is the source's, in the source's order: the RPS floor,
then the approved range/IV/breach entry gate per candidate, then the causal
bounded-loss / POP / EV pre-filters, then the approved market-state routing,
then the hard gates and 14-factor TQS, then the risk-adjusted winner.

Two inputs are shaped by this runtime rather than the source.  Legs are priced
at the **executable** side of the book (ask to buy, bid to sell) where a replay
has only a candle close, because a live credit that assumes the mid is a credit
the account will not receive.  And the round-trip cost is the configured
`round_trip_cost_per_lot` rather than the source's modelled execution buffer.
"""

from dataclasses import replace

from .entry_signal import DecisionEntrySignal, atm_surface
from .payoff import OptionLeg, calculate_pop, profit_boundaries, strategy_pnl
from .selection import (
    MarketState, SelectionConfig, StrategyCandidate, route_strategies, select_strategy,
    strategy_family,
)

# The source's `DEFAULT_RISK_STRATEGY_KEYS`. A long straddle is not a
# defined-risk credit structure and `strategy_family` has no family for it, so
# the approved selector cannot grade it; it is reported unsupported, never
# silently scored against a credit structure's gates.
APPROVED_STRATEGIES = ("PUT_CREDIT_SPREAD", "CALL_CREDIT_SPREAD", "IRON_CONDOR", "IRON_BUTTERFLY")


def selection_market(observation, signal, capital):
    """The source's `_selection_market`.

    Breach probability here is the *modelled* probability of settling outside
    the structure's own boundaries, never an empirical "the ATR range was
    touched at some point" frequency: the ATR range is a materially tighter box
    than the strikes that actually determine P&L.

    A `None` side means the structure has no strike there at all (a put credit
    spread has no short call), so that side carries no breach risk -- 0.0,
    exactly the convention the entry gate applies to its own gate_upper/lower.
    """
    signal = signal or {}
    modelled = bool(signal.get("enabled", True)) and bool(signal.get("available"))
    if modelled:
        upper = signal.get("upper_breach_probability")
        lower = signal.get("lower_breach_probability")
        upper = 0.0 if upper is None else upper
        lower = 0.0 if lower is None else lower
    else:
        upper = lower = None
    required = {
        "rps": observation.get("rps_base"),
        "upper_breach": upper,
        "lower_breach": lower,
        "expected_move": observation.get("expected_move"),
        "event_risk": observation.get("event_risk"),
        "pir": observation.get("pir"),
        "event_expiry_gap_sessions": observation.get("event_expiry_gap_sessions"),
        "iv_rank": signal.get("iv_rank") or observation.get("iv_rank_252"),
        "realized_volatility": signal.get("realized_volatility") or observation.get("realized_volatility_20"),
        "current_iv": signal.get("current_iv") or observation.get("atm_iv"),
    }
    # `term_structure_slope` is NOT required. It needs a longer-dated expiry in
    # the same chain to exist at all, and it is one of fourteen TQS factors
    # worth 4% of the weight: a missing one makes C3_TERM neutral, it does not
    # make the candidate unmeasurable. `term_structure_available` lets consumers
    # tell a real reading from the fallback.
    term_structure = observation.get("term_structure_slope")
    term_structure_available = term_structure is not None and _finite(term_structure)
    # `iv_rank` is required by the source's quality test, and it is measured
    # here whenever the remote feature source supplies `iv_rank_252`. Without
    # that source there is no IV history to rank against, so rather than fail
    # GATE_DATA on every candidate it is excluded and C1_IVRANK drops out of
    # TQS -- the same call the source makes for a missing term structure.
    iv_rank_measured = (
        required["iv_rank"] is not None and _finite(required["iv_rank"])
    )
    quality_ok = all(
        value is not None and _finite(value)
        for name, value in required.items() if name != "iv_rank" or iv_rank_measured
    )
    return MarketState(
        rps=float(required["rps"] or 0),
        upper_breach_probability=float(required["upper_breach"] or 0),
        lower_breach_probability=float(required["lower_breach"] or 0),
        expected_move=float(required["expected_move"] or 0),
        event_risk=float(required["event_risk"] or 0),
        pir=float(required["pir"] or 0),
        event_expiry_gap_sessions=float(required["event_expiry_gap_sessions"] or 0),
        iv_rank=float(required["iv_rank"] or 0),
        iv_minus_realized=100 * (float(required["current_iv"] or 0) - float(required["realized_volatility"] or 0)),
        term_structure_slope=float(term_structure) if term_structure_available else 0.0,
        capital=float(capital),
        data_quality_ok=quality_ok and bool(observation.get("data_quality_ok", True)),
        # Nothing in this runtime observes an event calendar with a genuine
        # per-event `known_at`, so the B block drops out of TQS entirely rather
        # than awarding every candidate the same constant.
        event_risk_available=bool(observation.get("event_risk_data_available")),
        term_structure_available=term_structure_available,
    )


def selection_candidate(profile):
    """The source's `_selection_candidate`."""
    strikes = sorted({float(strike) for strike in profile["strikes"]})
    name = profile["strategy_key"]
    if "CONDOR" in name and len(strikes) >= 4:
        wing_width = min(strikes[1] - strikes[0], strikes[-1] - strikes[-2])
    elif "BUTTERFLY" in name and len(strikes) >= 3:
        wing_width = min(strikes[1] - strikes[0], strikes[-1] - strikes[-2])
    else:
        wing_width = max(strikes) - min(strikes) if len(strikes) > 1 else 0.0
    liquidity_score = max(0.0, min(1.0, 1.0 - float(profile.get("liquidity_penalty") or 1.0)))
    # Each candidate carries the breach probabilities modelled against its own
    # strikes, so gating and scoring never borrow another structure's boundaries.
    signal = profile.get("entry_signal") or {}
    breach_available = bool(signal.get("enabled", True)) and bool(signal.get("available"))
    # `StrategyCandidate.expected_pnl` is GROSS by the reference contract: the
    # selector derives `net_expected_pnl = expected_pnl - round_trip_cost`
    # itself, and GATE_EV, GATE_EV_MATERIAL, D1_EV_RATIO and D3_ROM all read
    # that derived value. `calculate_pop` already opens its accumulator at
    # `-transaction_cost`, so its expected_pnl arrives net; add the cost back so
    # the selector nets it exactly once.
    round_trip_cost = float(profile.get("round_trip_cost") or 0)
    return StrategyCandidate(
        strategy=name,
        expected_pnl=float(profile["expected_pnl"]) + round_trip_cost,
        pop=float(profile["pop"]),
        max_loss=float(profile["max_loss"]),
        margin=float(profile["max_loss"]),
        net_credit=max(0.0, float(profile.get("entry_cashflow") or 0)),
        wing_width=wing_width,
        liquidity_score=liquidity_score,
        round_trip_cost=round_trip_cost,
        max_profit=float(profile.get("max_profit") or 0),
        defined_risk=profile.get("max_profit") is not None,
        max_loss_probability=float(profile.get("probability_loss") or 0),
        upper_breach_probability=signal.get("upper_breach_probability") if breach_available else None,
        lower_breach_probability=signal.get("lower_breach_probability") if breach_available else None,
    )


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return number == number and abs(number) != float("inf")


def _leg_premium(leg):
    """The executable premium for this leg's side of the book."""
    return float(leg["ask"] if leg["side"] == "BUY" else leg["bid"])


def _payoff_legs(legs, units):
    return [
        OptionLeg(
            option_type="CALL" if leg["right"] == "C" else "PUT",
            strike=float(leg["strike"]),
            quantity=units if leg["side"] == "BUY" else -units,
            entry_premium=_leg_premium(leg),
            multiplier=float(leg["chain_definition"]["multiplier"]),
            entry_iv=float(leg["iv"]) if leg.get("iv") else None,
        )
        for leg in legs
    ]


def _liquidity_penalty(legs, parameters):
    """The source's spread/thin-book penalty, on measured bid/ask, OI and volume."""
    spreads = []
    oi = []
    volume = []
    for leg in legs:
        bid, ask = float(leg["bid"]), float(leg["ask"])
        midpoint = (bid + ask) / 2
        if ask >= bid and midpoint > 0:
            spreads.append((ask - bid) / midpoint)
        oi.append(int(leg["oi"]))
        volume.append(int(leg["volume"]))
    spread_penalty = sum(spreads) / len(spreads) if spreads else 0.05
    thin_oi = 0.10 if oi and min(oi) < int(parameters.get("minimum_open_interest", 100)) else 0.0
    thin_volume = 0.10 if volume and min(volume) < max(1, int(parameters.get("minimum_volume", 1))) else 0.0
    return min(0.50, spread_penalty + thin_oi + thin_volume), (min(oi) if oi else None), (min(volume) if volume else None)


def _strike_roles(legs):
    return {
        role: next((float(leg["strike"]) for leg in legs if leg["role"] == role), None)
        for role in ("short_put", "short_call", "long_put", "long_call")
    }


def candidate_profile(key, legs, *, spot, observation, units, round_trip_cost, entry_iv,
                      forecast_iv, expiration, observed_at, parameters, config):
    """The source's `strategy_risk_profile`, on measured IBKR quotes.

    Returns `None` when the structure cannot be priced causally -- an unpriced
    leg, mixed expiries, an unbounded call tail, or a payoff whose worst case
    rounds to zero, which is stale or crossed quotes masquerading as a risk-free
    trade rather than a real one.
    """
    if not legs:
        return None
    if len({leg["expiration"] for leg in legs}) != 1:
        return None
    payoff_legs = _payoff_legs(legs, units)
    # Negative terminal call slope means unbounded upside loss.
    if sum(leg.quantity for leg in payoff_legs if leg.option_type == "CALL") < 0:
        return None
    years = max((expiration - observed_at.date()).days, 0) / 365.25
    analysis = calculate_pop(
        spot=float(spot), dte_days=max((expiration - observed_at.date()).days, 0),
        iv=entry_iv, forecast_iv=forecast_iv, legs=payoff_legs,
        risk_free_rate=float(config.risk_free_rate), dividend_yield=float(config.dividend_yield),
        transaction_cost=round_trip_cost,
    )
    # Rolling quotes are observations, not an executable historical order book.
    # Reject apparent static arbitrage rather than letting stale or crossed leg
    # prices masquerade as a zero-risk structure.
    if analysis["max_loss"] is not None and analysis["max_loss"] <= 1e-6:
        return None
    support = float(observation.get("support") or spot)
    resistance = float(observation.get("resistance") or spot)
    expected_move = float(observation.get("expected_move") or max(resistance - support, 0.0))
    # Equal scenario weights: this runtime keeps none of the source's expanding
    # matured-decile calibration, which is exactly when the source falls back to
    # a third each.
    scenarios = (max(0.0, support - expected_move), float(spot), resistance + expected_move)
    expected_downside = sum(
        max(0.0, -strategy_pnl(point, payoff_legs, round_trip_cost)) for point in scenarios
    ) / 3
    liquidity_penalty, minimum_oi, minimum_volume = _liquidity_penalty(legs, parameters)
    max_loss = float(analysis["max_loss"] or 0)
    risk_score = (max_loss * 0.70 + expected_downside * 0.30) * (1 + liquidity_penalty)
    entry_cashflow = -sum(leg.quantity * leg.entry_premium * leg.multiplier for leg in payoff_legs)
    return {
        "strategy_key": key,
        "expiry": expiration.isoformat(),
        "strikes": [leg.strike for leg in payoff_legs],
        "max_loss": max_loss,
        "max_profit": None if analysis["max_profit_unbounded"] else analysis["max_profit"],
        "expected_downside": expected_downside,
        "risk_score": str(round(risk_score, 6)),
        "entry_cashflow": entry_cashflow,
        "liquidity_penalty": liquidity_penalty,
        "minimum_open_interest": minimum_oi,
        "minimum_volume": minimum_volume,
        "round_trip_cost": round_trip_cost,
        "pop": analysis["pop"],
        "pop_percent": analysis["pop_percent"],
        "probability_loss": analysis["probability_loss"],
        "expected_pnl": analysis["expected_pnl"],
        "breakevens": analysis["breakevens"],
        "units": units,
        "years": years,
    }


def rank_candidates(instance, candidates, *, spot, observation, chains, observed_at, expiration):
    """Score every candidate structure through the approved decision engines.

    `candidates` maps a strategy key to its selected leg quotes; `chains` maps an
    expiry to that expiry's full chain rows, which is what the IV surface, the
    term-structure ratio and the 25-delta skew are read from.
    """
    parameters = instance.parameters
    lots = int(parameters["lots"])
    capital = float(instance.portfolio.account.net_liquidation)
    config = SelectionConfig.from_parameters(parameters)
    surface = atm_surface(chains, spot)
    entry_iv = surface.get(expiration, {}).get("iv")
    if entry_iv is None:
        return [], {"reason": "Measured IBKR option IV is required for candidate evaluation"}
    later = sorted(expiry for expiry in surface if expiry > expiration)
    observation = {
        **observation,
        "term_structure_slope": (
            surface[later[0]]["iv"] - entry_iv if later else None
        ),
    }
    # The volatility the expected-P&L integral is taken against. Valuing a leg
    # at the same implied volatility its own price was struck at makes every
    # term of the sum zero by construction; the causal realized figure makes it
    # the gap between what the chain charged and what the underlying has been
    # doing, identically for all four families.
    forecast = observation.get("hv20") or observation.get("realized_volatility_20")
    forecast_iv = float(forecast) if forecast and float(forecast) > 0 else None

    signal_evaluator = DecisionEntrySignal(parameters)
    minimum_pop = _minimum_pop(parameters)
    require_positive_ev = _boolean(parameters.get("rps_require_positive_ev", False))
    max_entry_loss = float(parameters.get("rps_max_entry_loss", 0) or 0)

    evaluations = []
    eligible = []
    for key, legs in candidates.items():
        rejection = None
        profile = None
        signal = None
        if key not in APPROVED_STRATEGIES:
            rejection = "strategy is not supported by the approved selector"
        elif any(not leg.get("iv") or not leg.get("bid") or not leg.get("ask") for leg in legs):
            rejection = "required exact contracts unavailable"
        else:
            units = _units(legs, lots)
            if units is None:
                rejection = "legs disagree on contract multiplier or exchange lot size"
            else:
                round_trip_cost = float(parameters["round_trip_cost_per_lot"]) * len(legs) * lots
                roles = _strike_roles(legs)
                # Breach probability is measured against this candidate's own
                # boundaries -- its breakevens where they can be priced, its
                # shorts otherwise -- never the (much tighter) ATR range.
                lower, upper = profit_boundaries(
                    _payoff_legs(legs, units), 0.0, float(spot),
                )
                candidate_observation = {
                    **observation,
                    "short_put_strike": roles["short_put"], "short_call_strike": roles["short_call"],
                    "long_put_strike": roles["long_put"], "long_call_strike": roles["long_call"],
                    "breakeven_lower": lower, "breakeven_upper": upper,
                }
                signal = signal_evaluator.evaluate(
                    spot=spot, observed_at=observed_at, surface=surface, chains=chains,
                    observation=candidate_observation, expiry=expiration,
                )
                if not signal["entry_allowed"]:
                    rejection = "Approved range/IV/breach entry blocked: " + "; ".join(signal["reasons"])
                else:
                    profile = candidate_profile(
                        key, legs, spot=spot, observation=observation, units=units,
                        round_trip_cost=round_trip_cost, entry_iv=entry_iv,
                        forecast_iv=forecast_iv, expiration=expiration,
                        observed_at=observed_at, parameters=parameters,
                        config=signal_evaluator.config,
                    )
                    if profile is None:
                        rejection = "causal P&L profile unavailable"
                    elif max_entry_loss > 0 and profile["max_loss"] > max_entry_loss:
                        rejection = "maximum entry loss exceeded"
                    elif profile["pop"] < _minimum_pop_for(key, minimum_pop, config):
                        rejection = "minimum POP not met"
                    elif require_positive_ev and profile["expected_pnl"] <= 0:
                        rejection = "expected P&L is not positive"
        evaluation = {
            **(profile or {"strategy_key": key, "expected_pnl": None, "risk_score": None}),
            "strategy_key": key,
            "eligible": rejection is None,
            "rejection_reason": rejection,
            "entry_signal": signal,
        }
        evaluations.append(evaluation)
        if rejection is None:
            eligible.append(evaluation)

    if not eligible:
        return [], {
            "model": "aryan-options-selection-v2",
            "implied_volatility": entry_iv,
            "realized_volatility": forecast_iv,
            "rankings": _sorted(evaluations),
            "unavailable_inputs": _unavailable(observation),
            "reason": "No structure passed the approved range/IV/breach entry gate or the causal bounded-loss, POP and EV checks",
        }

    signal = eligible[0]["entry_signal"]
    market = selection_market(observation, signal, capital)
    if signal.get("iv_rank") is None:
        # There is no IV-rank history in this runtime, so C1_IVRANK has no
        # reading -- and a factor built on an input nobody measured is not a low
        # score, it is an absent one. Dropping its weight is what
        # `calculate_tqs` already does for every unmeasured factor: the
        # survivors carry their full proportional weight, `tqs_factor_coverage`
        # reports what the score rests on, and the ceiling stays honest.
        # Scoring it 0.0 instead would dock every candidate the same points and
        # separate none of them.
        config = replace(config, weights={**config.weights, "C1_IVRANK": 0})
    routed = set(route_strategies(market, config))
    approved = []
    lookup = {}
    for evaluation in eligible:
        if evaluation["strategy_key"] not in routed:
            evaluation["eligible"] = False
            evaluation["rejection_reason"] = "strategy was not routed by the approved market-state rules"
            continue
        candidate = selection_candidate(evaluation)
        approved.append(candidate)
        lookup[id(candidate)] = evaluation
    result = select_strategy(
        market, approved,
        tqs_threshold=float(parameters.get("rps_tqs_threshold", 60) or 60),
        min_net_pnl=float(parameters.get("rps_tqs_min_net_pnl", 0) or 0),
        config=config,
    )
    for candidate in approved:
        evaluation = lookup[id(candidate)]
        evaluation.update({
            "gates": candidate.gates, "gate_failures": candidate.gate_failures,
            "graded_shortfalls": candidate.advisories, "advisories": candidate.advisories,
            "tqs": candidate.tqs, "tqs_factor_coverage": candidate.tqs_factor_coverage,
            "tqs_unmeasured_factors": candidate.tqs_unmeasured_factors,
            "decision_band": candidate.decision_band, "size_multiplier": candidate.size_multiplier,
            "net_expected_pnl": candidate.net_expected_pnl,
            "risk_adjusted_return": candidate.risk_adjusted_return,
            "selection_score": candidate.selection_score,
            "selection_factors": candidate.factors,
        })
        # Graded shortfalls are recorded, never a rejection: they cost the
        # candidate points instead of deleting it.
        if candidate.gate_failures:
            evaluation["eligible"] = False
            evaluation["rejection_reason"] = ", ".join(candidate.gate_failures)
    rankings = _sorted(evaluations)
    evidence = {
        "model": "aryan-options-selection-v2",
        "implied_volatility": entry_iv,
        "realized_volatility": forecast_iv,
        "routed_strategies": result["routed_strategies"],
        "selection_rule": "approved routing, hard gates, 14-factor TQS, then risk-adjusted return",
        "selection_weights": dict(config.weights),
        "decision": result["decision"],
        "entry_signal": signal,
        "rankings": rankings,
        "unavailable_inputs": _unavailable(observation),
        "cost_basis": "configured_round_trip_cost_per_lot",
        "reason": "",
    }
    winner = result["selected_candidate"]
    if winner is None:
        evidence["reason"] = (
            "NO_TRADE: no candidate passed the approved routing, the hard risk/safety "
            "constraints or the net-EV floor"
        )
        return [], evidence
    selected = lookup[id(winner)]
    return [selected], evidence


def _units(legs, lots):
    multipliers = {float(leg["chain_definition"]["multiplier"]) for leg in legs}
    lot_sizes = {float(leg.get("lot_size", 1)) for leg in legs}
    if len(multipliers) != 1 or len(lot_sizes) != 1:
        return None
    lot_size = lot_sizes.pop()
    if lot_size < 1 or not lot_size.is_integer():
        return None
    return lots * int(lot_size)


def _minimum_pop(parameters):
    raw = float(parameters.get("rps_min_pop", 0) or 0)
    return raw / 100 if raw > 1 else raw


def _minimum_pop_for(key, minimum_pop, config):
    """The POP floor a candidate must clear at the pre-selection stage.

    `SelectionConfig.pop_floors` is the authoritative, family-aware floor
    (GATE_POP), and it is deliberately different per family: an iron butterfly
    sells both legs at the same strike, so a lower POP than a directional spread
    is inherent to the structure, not a defect. Clamping to the family floor
    keeps this an inexpensive pre-filter that can never be stricter than the
    gate that actually decides.
    """
    try:
        family_floor = config.pop_floors[strategy_family(key)]
    except (ValueError, KeyError):
        return minimum_pop
    return min(minimum_pop, float(family_floor))


def _sorted(evaluations):
    """Eligible first, best selection score first, then a stable name tie-break."""
    return sorted(evaluations, key=lambda item: (
        not item["eligible"],
        -(item.get("selection_score") if item.get("selection_score") is not None else float("-inf")),
        item["strategy_key"],
    ))


def _unavailable(observation):
    """The inputs this decision could not measure.

    Reported from the observation rather than assumed: with a remote feature
    source configured, IV rank, IV percentile and term structure are real
    readings, and listing them as unavailable would misreport what the score
    rests on. Event risk has no feed on either side of the port.
    """
    names = []
    for name, key in (
        ("iv_rank", "iv_rank_252"),
        ("iv_percentile", "iv_percentile_252"),
        ("term_structure", "term_structure_slope"),
    ):
        if observation.get(key) is None:
            names.append(name)
    if not observation.get("event_risk_data_available"):
        names.append("event_risk")
    return names


def _boolean(value):
    return value if isinstance(value, bool) else str(value).strip().lower() in {"1", "true", "yes", "on"}
