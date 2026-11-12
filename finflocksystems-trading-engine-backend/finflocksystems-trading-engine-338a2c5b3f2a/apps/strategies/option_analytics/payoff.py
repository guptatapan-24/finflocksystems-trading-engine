"""Entry-time IV-implied probability and expiry-payoff analytics.

The RPS gate decides *when* a candidate may be considered.  This module uses
only that decision snapshot to calculate the candidate's risk-neutral
probability of profit (POP), expected expiry P&L, breakevens and payoff shape.
It deliberately has no market-data access, which keeps the no-look-ahead
boundary explicit and testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import erf, exp, inf, isfinite, log, sqrt
from typing import Iterable

import numpy as np


MODEL_VERSION = "iv-lognormal-pop-v2"
_EPSILON = 1e-9


@dataclass(frozen=True)
class OptionLeg:
    """One European option leg with a signed lot quantity."""

    option_type: str
    strike: float
    quantity: int
    entry_premium: float
    multiplier: float = 1.0
    entry_iv: float | None = None

    def __post_init__(self):
        option_type = self.option_type.upper()
        if option_type not in {"CALL", "PUT"}:
            raise ValueError("option_type must be CALL or PUT")
        if self.strike <= 0 or self.multiplier <= 0:
            raise ValueError("strike and multiplier must be positive")
        if not isinstance(self.quantity, int) or self.quantity == 0:
            raise ValueError("quantity must be a non-zero signed integer")
        if self.entry_premium < 0:
            raise ValueError("entry_premium cannot be negative")
        if self.entry_iv is not None and self.entry_iv <= 0:
            raise ValueError("entry_iv must be positive when supplied")


def normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + erf(value / sqrt(2.0)))


def strategy_pnl(terminal_price: float, legs: Iterable[OptionLeg], transaction_cost: float = 0.0) -> float:
    """Undiscounted expiry P&L, including the entry premiums and round-trip costs."""
    pnl = -float(transaction_cost)
    for leg in legs:
        intrinsic = (
            max(terminal_price - leg.strike, 0.0)
            if leg.option_type.upper() == "CALL"
            else max(leg.strike - terminal_price, 0.0)
        )
        pnl += leg.quantity * leg.multiplier * (intrinsic - leg.entry_premium)
    return pnl


def strategy_pnl_batch(terminal_prices, legs: Iterable[OptionLeg], transaction_cost: float = 0.0):
    """Same undiscounted expiry P&L as `strategy_pnl`, evaluated for every price in
    `terminal_prices` (an array) at once instead of one Python call per price --
    `calculate_pop_mc` calling `strategy_pnl` in a per-path Python loop was measured
    as the dominant CPU cost of RPS auto-select candidate evaluation (13M calls for
    a few thousand candidate evaluations). Mathematically identical to calling
    `strategy_pnl` once per element of `terminal_prices`."""
    legs = tuple(legs)
    prices = np.asarray(terminal_prices, dtype=np.float64)
    pnl = np.full(prices.shape, -float(transaction_cost), dtype=np.float64)
    for leg in legs:
        intrinsic = (
            np.maximum(prices - leg.strike, 0.0)
            if leg.option_type.upper() == "CALL"
            else np.maximum(leg.strike - prices, 0.0)
        )
        pnl += leg.quantity * leg.multiplier * (intrinsic - leg.entry_premium)
    return pnl


def calculate_pop(
    *,
    spot: float,
    dte_days: float,
    iv: float,
    legs: Iterable[OptionLeg],
    risk_free_rate: float = 0.0,
    dividend_yield: float = 0.0,
    transaction_cost: float = 0.0,
    payoff_points: int = 121,
    forecast_iv: float | None = None,
) -> dict:
    """Calculate exact POP regions and expected P&L.

    `iv` prices the probability questions (POP, the profitable intervals) under
    the risk-neutral measure the option chain itself implies.

    `forecast_iv`, when supplied, is the volatility the **expected P&L** is
    integrated against instead of `iv`. That separation is the whole point of
    the parameter. Valuing a leg at the same volatility its own market price was
    struck at makes every term of the EV sum zero by construction, so the figure
    collapses to `-transaction_cost` and says nothing about the trade; valuing it
    at one *flat* ATM `iv` while the legs are priced across a smile makes the
    figure the smile's shape -- measured on a 6-month NIFTY replay that produced
    a systematic +65 median EV for call credit spreads against -106 for put
    credit spreads, purely because index puts carry skew, and the gate reading it
    passed 73% of the former and 21% of the latter for no economic reason.

    Passing a *realized*-volatility forecast makes the same sum the quantity a
    premium seller actually earns: the gap between what the market charged for
    the option and what the underlying went on to do. Positive means the
    structure sells volatility dearer than the forecast, which is the edge; and
    it means that identically for all four families.
    """
    legs = tuple(legs)
    if spot <= 0 or dte_days < 0 or iv < 0:
        raise ValueError("spot must be positive and dte_days/iv cannot be negative")
    if not legs:
        raise ValueError("At least one option leg is required")
    if transaction_cost < 0:
        raise ValueError("transaction_cost cannot be negative")

    profitable_intervals, breakevens = _profit_intervals(legs, transaction_cost)
    years = dte_days / 365.25
    pop = sum(
        _probability_between(lower, upper, spot, years, iv, risk_free_rate, dividend_yield)
        for lower, upper in profitable_intervals
    )
    pop = min(1.0, max(0.0, pop))
    ev_iv = iv if forecast_iv is None else float(forecast_iv)
    expected_pnl = _expected_pnl(
        spot, years, ev_iv, legs, risk_free_rate, dividend_yield, transaction_cost,
    )
    max_profit, max_loss = _payoff_extrema(legs, transaction_cost)

    strikes = sorted({leg.strike for leg in legs})
    chart_low = max(0.0, min(spot * 0.75, strikes[0] * 0.90))
    chart_high = max(spot * 1.25, strikes[-1] * 1.10)
    point_count = max(20, min(int(payoff_points), 501))
    chart_prices = {
        chart_low + (chart_high - chart_low) * index / (point_count - 1)
        for index in range(point_count)
    }
    chart_prices.update(strikes)
    chart_prices.update(breakevens)
    chart_prices.add(spot)

    return {
        "model": "LOGNORMAL_RISK_NEUTRAL_IV",
        "model_version": MODEL_VERSION,
        "pop": round(pop, 10),
        "pop_percent": round(pop * 100.0, 6),
        "probability_loss": round(1.0 - pop, 10),
        "expected_pnl": _finite_round(expected_pnl),
        "expected_pnl_iv": round(ev_iv, 10),
        "expected_pnl_basis": "IMPLIED_ATM" if forecast_iv is None else "FORECAST_VOLATILITY",
        "max_profit": _finite_round(max_profit),
        "max_loss": _finite_round(max_loss),
        "max_profit_unbounded": max_profit == inf,
        "max_loss_unbounded": max_loss == inf,
        "breakevens": [round(value, 6) for value in breakevens],
        "profitable_intervals": [
            {"lower": round(lower, 6), "upper": round(upper, 6) if isfinite(upper) else None}
            for lower, upper in profitable_intervals
        ],
        "spot": round(spot, 6),
        "dte_days": round(dte_days, 6),
        "iv": round(iv, 10),
        "risk_free_rate": round(risk_free_rate, 10),
        "dividend_yield": round(dividend_yield, 10),
        "transaction_cost": round(transaction_cost, 6),
        "payoff_points": [
            {"underlying_price": round(price, 6), "pnl": round(strategy_pnl(price, legs, transaction_cost), 6)}
            for price in sorted(chart_prices)
        ],
    }


def calculate_pop_mc(
    *,
    spot: float,
    dte_days: float,
    iv: float,
    legs: Iterable[OptionLeg],
    risk_free_rate: float = 0.0,
    dividend_yield: float = 0.0,
    transaction_cost: float = 0.0,
    paths: int = 4000,
    seed: int | None = None,
    forecast_iv: float | None = None,
) -> dict:
    """Monte Carlo companion to `calculate_pop`: simulate terminal prices with
    `_simulate_price_paths` (GBM) and evaluate the exact
    same `strategy_pnl` payoff used by the analytic model on each simulated
    path, so the empirical EV/RoR/POP are directly comparable to (not a
    replacement for) `calculate_pop`'s closed-form figures.

    Mirrors the reference Stage20 algorithm's per-candidate Monte Carlo
    (simulate once, then price every candidate structure off the same paths).

    `forecast_iv` replaces `iv` as the **diffusion** the paths are drawn from,
    so both figures it returns describe the same world as `calculate_pop`'s
    forecast-based expected P&L rather than a second, contradictory one. It is
    one simulation, so `pop_mc` moves with it: under a realized-vol forecast it
    is the probability of profit if the underlying behaves like the forecast,
    not the risk-neutral probability the chain implies. `calculate_pop`'s own
    `pop` still carries the risk-neutral reading, so both are on the record.
    """
    legs = tuple(legs)
    if spot <= 0 or dte_days < 0 or iv < 0:
        raise ValueError("spot must be positive and dte_days/iv cannot be negative")
    if not legs:
        raise ValueError("At least one option leg is required")
    if transaction_cost < 0:
        raise ValueError("transaction_cost cannot be negative")
    simulation_iv = iv if forecast_iv is None else float(forecast_iv)
    if simulation_iv < 0:
        raise ValueError("forecast_iv cannot be negative")

    years = max(dte_days / 365.25, 1.0 / 365.25)
    paths_array = _simulate_price_paths(
        spot=spot, annualized_volatility=simulation_iv, time_horizon_years=years,
        number_of_paths=int(paths), number_of_steps=1,
        annualized_drift=risk_free_rate - dividend_yield, random_seed=seed,
    )
    terminal_prices = paths_array[:, -1]
    pnl = strategy_pnl_batch(terminal_prices, legs, transaction_cost)
    max_loss = max(0.0, -float(pnl.min()))
    ev = float(pnl.mean())
    pop = float(np.mean(pnl > 0))
    return {
        "model": "MONTE_CARLO_GBM",
        "model_version": MODEL_VERSION,
        "paths": int(paths),
        "simulation_iv": round(simulation_iv, 10),
        "expected_pnl_basis": "IMPLIED_ATM" if forecast_iv is None else "FORECAST_VOLATILITY",
        "pop_mc": round(pop, 10),
        "expected_pnl_mc": _finite_round(ev),
        "max_loss_mc": _finite_round(max_loss),
        "return_on_risk_mc": _finite_round(ev / max_loss) if max_loss > _EPSILON else None,
    }


def _simulate_price_paths(
    *,
    spot: float,
    annualized_volatility: float,
    time_horizon_years: float,
    number_of_paths: int = 10000,
    number_of_steps: int = 50,
    annualized_drift: float = 0.0,
    random_seed: int | None = None,
) -> "np.ndarray":
    """Geometric-Brownian-motion price paths, spot prepended as the t=0 column.

    Inlined verbatim from the retired root-level `range_iv_breach_engine.py`,
    which `calculate_pop_mc` used to import at call time.  That module was
    deleted in 5fe0a27 once its reviewed parts were ported into
    `trading/decision_engines/`, but this one function was missed, leaving a
    `ModuleNotFoundError` on every Monte-Carlo EV call.  It survived unnoticed
    only because the running container still had a stale copy baked into its
    image -- the compose bind mounts cover `./trading` and friends, not
    root-level files -- so backtests kept working there while any fresh image,
    CI run or local run failed.

    The arithmetic, argument validation and RNG draw shape are unchanged, so
    a given `random_seed` reproduces exactly the figures the old module did.
    GBM here is the reference baseline the ported engine shipped with, not a
    claim that GBM is sufficient: it ignores volatility clustering, jumps,
    skew and event risk.
    """
    if spot <= 0:
        raise ValueError("Spot must be positive.")
    if annualized_volatility <= 0:
        raise ValueError("Volatility must be positive.")
    if time_horizon_years <= 0:
        raise ValueError("Time horizon must be positive.")
    if number_of_paths <= 0 or number_of_steps <= 0:
        raise ValueError("number_of_paths and number_of_steps must be positive.")

    rng = np.random.default_rng(random_seed)
    dt = time_horizon_years / number_of_steps
    z = rng.standard_normal(size=(number_of_paths, number_of_steps))
    increments = (
        (annualized_drift - 0.5 * annualized_volatility ** 2) * dt
        + annualized_volatility * sqrt(dt) * z
    )
    prices = spot * np.exp(np.cumsum(increments, axis=1))
    return np.concatenate([np.full((number_of_paths, 1), spot), prices], axis=1)


def _profit_intervals(legs: tuple[OptionLeg, ...], transaction_cost: float):
    breakpoints = [0.0, *sorted({leg.strike for leg in legs})]
    intervals: list[tuple[float, float]] = []
    roots: list[float] = []

    for left, right in zip(breakpoints, breakpoints[1:]):
        left_pnl = strategy_pnl(left, legs, transaction_cost)
        right_pnl = strategy_pnl(right, legs, transaction_cost)
        _finite_profit_segment(left, right, left_pnl, right_pnl, intervals, roots)

    left = breakpoints[-1]
    left_pnl = strategy_pnl(left, legs, transaction_cost)
    slope = sum(
        leg.quantity * leg.multiplier for leg in legs if leg.option_type.upper() == "CALL"
    )
    if abs(slope) <= _EPSILON:
        if left_pnl > _EPSILON:
            intervals.append((left, inf))
        elif abs(left_pnl) <= _EPSILON:
            roots.append(left)
    else:
        root = left - left_pnl / slope
        if root >= left - _EPSILON:
            root = max(left, root)
            roots.append(root)
            if slope > 0:
                intervals.append((root, inf))
            elif left_pnl > _EPSILON:
                intervals.append((left, root))
        elif left_pnl > _EPSILON:
            intervals.append((left, inf))

    return _merge_intervals(intervals), _dedupe_roots(roots)


def _finite_profit_segment(left, right, left_pnl, right_pnl, intervals, roots):
    if abs(left_pnl) <= _EPSILON:
        roots.append(left)
    if abs(right_pnl) <= _EPSILON:
        roots.append(right)
    if left_pnl * right_pnl < -_EPSILON:
        root = left + (right - left) * (-left_pnl) / (right_pnl - left_pnl)
        roots.append(root)
        if left_pnl > 0:
            intervals.append((left, root))
        else:
            intervals.append((root, right))
    elif left_pnl > _EPSILON or right_pnl > _EPSILON:
        intervals.append((left, right))


def profit_boundaries(legs: Iterable[OptionLeg], transaction_cost: float = 0.0,
                      spot: float | None = None) -> tuple[float | None, float | None]:
    """The (lower, upper) settlement prices bounding the profitable region -- the
    structure's breakevens.

    `None` on a side means the profitable region is unbounded there: a put
    credit spread profits for every price above its single breakeven, so its
    upper bound is `None` and only the lower one can be settled through.

    This is the boundary that decides whether a position *loses money*, which
    is the same question for all four families. A short strike answers a
    different one (where an option goes in-the-money), and it is not comparable
    across structures: an iron butterfly's two shorts sit on the same ATM
    strike, so "beyond a short" is true of essentially every settlement, while
    the same phrase for a condor means the edge of a 200-point corridor. Gating
    those two numbers against one ceiling compares nothing to nothing. The
    breakevens always mean "past here I am down money", whatever the structure.

    When several disjoint profitable regions exist (not produced by any of the
    credit templates here, but possible for a ratio or broken-wing structure),
    the one containing `spot` is returned, else the widest -- picking the region
    the position is actually sitting in rather than an unreachable one.
    """
    legs = tuple(legs)
    if not legs:
        return None, None
    intervals, _ = _profit_intervals(legs, float(transaction_cost))
    if not intervals:
        return None, None
    chosen = None
    if spot is not None:
        chosen = next((item for item in intervals if item[0] <= spot <= item[1]), None)
    if chosen is None:
        chosen = max(intervals, key=lambda item: item[1] - item[0])
    lower, upper = chosen
    return (lower if lower > _EPSILON else None), (upper if isfinite(upper) else None)


def _merge_intervals(intervals):
    merged: list[tuple[float, float]] = []
    for left, right in sorted(intervals):
        if right - left <= _EPSILON:
            continue
        if merged and left <= merged[-1][1] + _EPSILON:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    return merged


def _dedupe_roots(roots):
    output = []
    for value in sorted(root for root in roots if root >= 0 and isfinite(root)):
        if not output or abs(value - output[-1]) > 1e-6:
            output.append(value)
    return output


def _probability_between(lower, upper, spot, years, iv, rate, dividend_yield):
    if years <= 0 or iv <= 0:
        terminal = spot * exp((rate - dividend_yield) * max(years, 0.0))
        return 1.0 if lower < terminal < upper or (upper == inf and terminal > lower) else 0.0
    return max(
        0.0,
        min(1.0, _lognormal_cdf(upper, spot, years, iv, rate, dividend_yield)
            - _lognormal_cdf(lower, spot, years, iv, rate, dividend_yield)),
    )


def _lognormal_cdf(price, spot, years, iv, rate, dividend_yield):
    if price <= 0:
        return 0.0
    if price == inf:
        return 1.0
    deviation = iv * sqrt(years)
    mean = log(spot) + (rate - dividend_yield - 0.5 * iv * iv) * years
    return normal_cdf((log(price) - mean) / deviation)


def _expected_pnl(spot, years, iv, legs, rate, dividend_yield, transaction_cost):
    terminal_forward = spot * exp((rate - dividend_yield) * years)
    expected = -transaction_cost
    for leg in legs:
        if years <= 0 or iv <= 0:
            intrinsic = (
                max(terminal_forward - leg.strike, 0.0)
                if leg.option_type.upper() == "CALL"
                else max(leg.strike - terminal_forward, 0.0)
            )
        else:
            deviation = iv * sqrt(years)
            d1 = (log(spot / leg.strike) + (rate - dividend_yield + 0.5 * iv * iv) * years) / deviation
            d2 = d1 - deviation
            if leg.option_type.upper() == "CALL":
                intrinsic = terminal_forward * normal_cdf(d1) - leg.strike * normal_cdf(d2)
            else:
                intrinsic = leg.strike * normal_cdf(-d2) - terminal_forward * normal_cdf(-d1)
        expected += leg.quantity * leg.multiplier * (intrinsic - leg.entry_premium)
    return expected


def portfolio_margin(legs_by_expiry, *, spot, short_margin_rate, buffer=1.0):
    """Capital a set of option positions ties up, priced on actual risk.

    The old model charged every short leg `short_margin_rate` of the FULL
    underlying notional and never netted the long wings that cap the loss.  On
    NIFTY that made a defined-risk iron butterfly whose worst case is Rs 244
    reserve Rs 765,435 -- 3,140x its own maximum loss, and 76% of a Rs 10 lakh
    account -- so only one position could ever be open and the portfolio
    optimizer spent the whole replay reporting CAPITAL_EXCEEDS_BUDGET against
    candidates that passed every gate.

    An exchange does not price a hedged structure that way: SPAN scans the
    portfolio's worst-case scenario loss rather than summing naked-short
    charges leg by leg.  So per expiry:

        margin = min(worst-case expiry loss x buffer, notional charge)

    `_payoff_extrema` already nets wings against shorts and returns `inf`
    exactly when the risk is genuinely unbounded, so `isfinite` is the
    defined-risk test -- no structure tagging needed.

    The `min` is what keeps this conservative in both directions.  A hedged
    structure posts only what it can actually lose (Rs 244, not Rs 765,435).
    A naked short put, whose true worst case (strike x multiplier) far exceeds
    the notional charge, still posts the notional charge exactly as before, so
    nothing that used to be margined heavily suddenly becomes cheap.  This can
    therefore never charge MORE than the previous model.

    Grouped by expiry because a wing only offsets a short in its own expiry:
    netting a calendar's two expiries against each other would invent a hedge
    that does not exist.

    `buffer` scales the defined-risk figure only (1.0 = the theoretical floor).
    Real SPAN + exposure margin sits above pure max loss, so raise it to model
    a broker that is less generous; it can never push the charge above the
    notional fallback.
    """
    total = 0.0
    for legs in legs_by_expiry.values():
        legs = tuple(legs)
        if not legs:
            continue
        notional = 0.0
        for leg in legs:
            if leg.quantity > 0:
                notional += abs(leg.quantity) * leg.entry_premium * leg.multiplier
            else:
                notional += abs(leg.quantity) * spot * leg.multiplier * short_margin_rate
        _, max_loss = _payoff_extrema(legs, 0.0)
        total += min(max_loss * buffer, notional) if isfinite(max_loss) else notional
    return total


def _payoff_extrema(legs, transaction_cost):
    prices = [0.0, *sorted({leg.strike for leg in legs})]
    values = [strategy_pnl(price, legs, transaction_cost) for price in prices]
    right_slope = sum(
        leg.quantity * leg.multiplier for leg in legs if leg.option_type.upper() == "CALL"
    )
    max_profit = inf if right_slope > _EPSILON else max(values)
    max_loss = inf if right_slope < -_EPSILON else max(0.0, -min(values))
    return max_profit, max_loss


def _finite_round(value):
    return None if not isfinite(value) else round(value, 6)
