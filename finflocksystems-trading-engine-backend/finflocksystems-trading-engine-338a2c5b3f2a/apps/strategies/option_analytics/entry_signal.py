"""The approved range/IV/breach entry rules, ported from
``Aryan_Options/trading/entry_signal.py``.

The component tables, weights, normalisation, breach definition and the gate
itself are the source file's, unchanged.  What differs is the input shape: the
source reads a replayed snapshot object, this runtime reads the point-in-time
IBKR option chain it already fetched for selection.

Two of the source's five IV components need history this runtime does not keep.
``iv_rank`` and ``iv_percentile`` therefore take the source's *own* fallback for
a short history -- a neutral 50.0 -- and ``iv_rank_source`` records that they
did.  The other three (IV/RV, term structure, 25-delta skew) are measured.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
import hashlib
import math
from typing import Any

import numpy as np

from .range_entry import EntryInputs, entry_allowed as approved_entry_allowed, range_confidence


ENTRY_SIGNAL_VERSION = "range-iv-breach-terminal-v3"
# Named so a stored ledger says which question its numbers answered: the
# probability of settling beyond the boundary at expiry, not of touching it
# at some point before. See `breach_probabilities`.
BREACH_PROBABILITY_METHOD = "DETERMINISTIC_GBM_TERMINAL_CONTRACT_IV"


class IVCondition(str, Enum):
    FAVOURABLE = "FAVOURABLE"
    NOT_FAVOURABLE = "NOT_FAVOURABLE"


# The reachable span of each component, so the blended score can be stated on a
# real 0-100 scale. Three of the five are step functions with a floor well above
# zero and a ceiling below a hundred, so the raw blend cannot reach either end;
# normalising makes a threshold a percentage of what is actually achievable.
_COMPONENT_RANGES = {
    "iv_rank": (0.0, 100.0),
    "iv_percentile": (0.0, 100.0),
    "iv_rv": (20.0, 100.0),
    "term_structure": (25.0, 70.0),
    "skew": (20.0, 70.0),
}


@dataclass(frozen=True)
class EntrySignalConfig:
    iv_rank_weight: float = 0.25
    iv_percentile_weight: float = 0.20
    iv_rv_weight: float = 0.30
    term_structure_weight: float = 0.15
    skew_weight: float = 0.10
    minimum_history: int = 20
    history_window: int = 252
    favourable_score: float = 60.0
    # Shares the same operator-facing RPS floor as the standalone RPS gate
    # (rps_threshold) rather than a second, independent value, so the two
    # gates a candidate must clear cannot silently disagree.
    min_rps: float = 60.0
    # 0.50 is the natural ceiling once this measures a *settlement*, not a
    # touch: "no more likely than not to finish beyond the strike I sold".
    max_upper_breach_probability: float = 0.50
    max_lower_breach_probability: float = 0.50
    # Ceiling for a structure that sold BOTH sides, judged on the combined
    # probability of finishing beyond either short. `None` keeps the tighter of
    # the two per-side ceilings.
    max_two_sided_breach_probability: float | None = None
    # The risk-neutral drift the simulated settlement distribution carries,
    # matched to what the payoff engine puts in its forward so breach and POP
    # cannot describe different worlds for the same structure.
    risk_free_rate: float = 0.068
    dividend_yield: float = 0.012
    simulation_paths: int = 10_000
    # Retained for API compatibility only: the terminal distribution is the same
    # however finely the horizon is sliced.
    simulation_steps: int = 50

    @property
    def component_weights(self) -> dict[str, float]:
        return {
            "iv_rank": self.iv_rank_weight, "iv_percentile": self.iv_percentile_weight,
            "iv_rv": self.iv_rv_weight, "term_structure": self.term_structure_weight,
            "skew": self.skew_weight,
        }

    def normalise_score(self, raw: float) -> float:
        """Put `raw` on a 0-100 scale over the blend's own reachable span."""
        weights = self.component_weights
        low = sum(weights[name] * _COMPONENT_RANGES[name][0] for name in weights)
        high = sum(weights[name] * _COMPONENT_RANGES[name][1] for name in weights)
        if high - low <= 1e-9:
            return 0.0
        return float(np.clip(100.0 * (raw - low) / (high - low), 0.0, 100.0))

    @property
    def two_sided_ceiling(self) -> float:
        """The ceiling a both-sides structure's combined probability is held to."""
        if self.max_two_sided_breach_probability is not None:
            return self.max_two_sided_breach_probability
        return min(self.max_upper_breach_probability, self.max_lower_breach_probability)

    @classmethod
    def from_parameters(cls, parameters: dict[str, Any]) -> "EntrySignalConfig":
        defaults = cls()
        return cls(
            iv_rank_weight=float(parameters.get("rps_iv_rank_weight", defaults.iv_rank_weight)),
            iv_percentile_weight=float(parameters.get("rps_iv_percentile_weight", defaults.iv_percentile_weight)),
            iv_rv_weight=float(parameters.get("rps_iv_rv_weight", defaults.iv_rv_weight)),
            term_structure_weight=float(parameters.get("rps_iv_term_structure_weight", defaults.term_structure_weight)),
            skew_weight=float(parameters.get("rps_iv_skew_weight", defaults.skew_weight)),
            minimum_history=int(parameters.get("rps_iv_min_history", 20)),
            history_window=int(parameters.get("rps_iv_history_window", 252)),
            favourable_score=float(parameters.get("rps_iv_score_threshold", 60)),
            min_rps=float(parameters.get("rps_threshold", 60) or 60),
            max_upper_breach_probability=_probability(
                parameters.get("rps_max_upper_breach_probability", defaults.max_upper_breach_probability)
            ),
            max_lower_breach_probability=_probability(
                parameters.get("rps_max_lower_breach_probability", defaults.max_lower_breach_probability)
            ),
            max_two_sided_breach_probability=(
                None if parameters.get("rps_max_two_sided_breach_probability") in (None, "")
                else _probability(parameters["rps_max_two_sided_breach_probability"])
            ),
            risk_free_rate=float(parameters.get("rps_pop_risk_free_rate", defaults.risk_free_rate)),
            dividend_yield=float(parameters.get("rps_pop_dividend_yield", defaults.dividend_yield)),
            simulation_paths=int(parameters.get("rps_breach_simulation_paths", 10_000)),
            simulation_steps=int(parameters.get("rps_breach_simulation_steps", 50)),
        )


def entry_signal_enabled(parameters: dict[str, Any]) -> bool:
    """Whether the approved IV/breach gate runs.

    The source ships this off by default so saved backtest payloads written
    before the gate existed keep replaying unchanged.  This runtime has no such
    payloads and no degraded RPS-only mode to preserve, so the approved gate is
    the default here and only an explicit `false` turns it off.
    """
    value = parameters.get("rps_iv_breach_enabled", True)
    return value if isinstance(value, bool) else str(value).strip().lower() in {"1", "true", "yes", "on"}


def public_entry_signal_spec(parameters: dict[str, Any]) -> dict[str, Any]:
    config = EntrySignalConfig.from_parameters(parameters)
    return {
        "version": ENTRY_SIGNAL_VERSION,
        "enabled": entry_signal_enabled(parameters),
        "component_weights": config.component_weights,
        "market_data_source": "POINT_IN_TIME_OPTION_CHAIN",
        "iv_source": "measured IBKR contract IV at the decision timestamp",
        "realized_volatility_source": "completed IBKR daily bars, prior sessions only",
        "range_source": "causal RPS prior-session daily ATR boundaries",
        "minimum_iv_history": config.minimum_history,
        "iv_history_window": config.history_window,
        "favourable_iv_score": config.favourable_score,
        "iv_score_scale": "NORMALISED_0_100",
        "iv_score_raw_span": [
            round(sum(config.component_weights[n] * _COMPONENT_RANGES[n][0] for n in config.component_weights), 4),
            round(sum(config.component_weights[n] * _COMPONENT_RANGES[n][1] for n in config.component_weights), 4),
        ],
        "iv_rank_source": "per-session iv_rank_252 when the remote feature source is configured, else measured history, else neutral 50.0",
        "maximum_upper_breach_probability": config.max_upper_breach_probability,
        "maximum_lower_breach_probability": config.max_lower_breach_probability,
        "maximum_two_sided_breach_probability": config.two_sided_ceiling,
        "breach_probability_method": BREACH_PROBABILITY_METHOD,
        "breach_probability_event": (
            "the underlying settles beyond the short (or wing) strike at expiry; "
            "a mid-life excursion that reverts is not a breach, because nothing "
            "closes the position on one -- positions are held to expiry"
        ),
        "simulation_paths": config.simulation_paths,
        "simulation_steps": config.simulation_steps,
        "missing_metric_rule": "neutral score when IV history is short; history length is informational, not a blocking gate",
    }


def atm_surface(chains: dict[date, list], spot) -> dict[date, dict[str, Any]]:
    """One ATM IV per expiry, averaged over the strikes nearest spot.

    `chains` maps an expiry to that expiry's chain rows, each carrying `strike`,
    `right` and `iv`.  An IV at or above 500% is a broken tick, not a quote.
    """
    spot = _finite_positive(spot)
    surface = {}
    if spot is None:
        return surface
    for expiry, rows in chains.items():
        quotes = [
            row for row in rows
            if (iv := _finite_positive(row.get("iv"))) is not None and iv < 5
        ]
        if not quotes:
            continue
        distance = min(abs(float(row["strike"]) - spot) for row in quotes)
        nearest = [row for row in quotes if abs(float(row["strike"]) - spot) == distance]
        surface[expiry] = {
            "iv": sum(float(row["iv"]) for row in nearest) / len(nearest),
            "contracts": sorted(f"{row['right']}{row['strike']}" for row in nearest),
        }
    return surface


def delta_skew(rows) -> float | None:
    """25-delta put IV minus 25-delta call IV, within one expiry."""
    quotes = [row for row in rows if row.get("iv") is not None and row.get("delta") is not None]
    puts = [row for row in quotes if row["right"] == "P"]
    calls = [row for row in quotes if row["right"] == "C"]
    if not puts or not calls:
        return None
    put = min(puts, key=lambda row: abs(abs(float(row["delta"])) - 0.25))
    call = min(calls, key=lambda row: abs(abs(float(row["delta"])) - 0.25))
    return float(put["iv"]) - float(call["iv"])


def _iv_rank(current: float | None, history: list[float]) -> float | None:
    if current is None or not history:
        return None
    low, high = min(history), max(history)
    return 50.0 if high == low else float(np.clip(100 * (current - low) / (high - low), 0, 100))


def _iv_percentile(current: float | None, history: list[float]) -> float | None:
    if current is None or not history:
        return None
    below = sum(value < current for value in history)
    equal = sum(value == current for value in history)
    return 100 * (below + 0.5 * equal) / len(history)


def _score_iv_rv(ratio: float | None) -> float:
    """Implied over realized volatility, scored for a premium *seller*.

    Monotone on purpose: the single richest regime for selling volatility must
    not score near the single poorest. The "too good to be true" reading is
    event risk, which the B-block factors and GATE_EVENT carry instead.
    """
    if ratio is None:
        return 50.0
    if ratio < 0.90:
        return 20.0
    if ratio < 1.00:
        return 30.0
    if ratio < 1.15:
        return 60.0
    return 85.0 if ratio <= 1.75 else 100.0


def _score_term(ratio: float | None) -> float:
    if ratio is None:
        return 50.0
    if 0.90 <= ratio <= 1.10:
        return 70.0
    return 55.0 if ratio <= 1.20 else 25.0


def _score_skew(skew: float | None) -> float:
    if skew is None:
        return 50.0
    points = abs(skew) * 100
    if points <= 0.5:
        return 70.0
    if points <= 1.0:
        return 60.0
    if points <= 2.0:
        return 50.0
    return 35.0 if points <= 3.0 else 20.0


def _terminal_prices(seed_text: str, spot: float, volatility: float, dte: int,
                     paths: int, price_cache: dict | None, drift: float = 0.0):
    """The deterministic GBM price at `dte` that `seed_text` identifies, one per
    path, memoised.

    Only the settlement price is drawn, never the journey to it. Under the
    terminal breach definition the answer depends on the horizon alone, so the
    sum of `steps` independent log-increments -- `N(-0.5 s^2 t, s^2 t)` however
    it is sliced -- is drawn in one shot instead of accumulated step by step.
    """
    cache_key = (seed_text, paths) if price_cache is not None else None
    prices = price_cache.get(cache_key) if price_cache is not None else None
    if prices is None:
        seed = int.from_bytes(hashlib.sha256(seed_text.encode("utf-8")).digest()[:8], "big")
        rng = np.random.default_rng(seed)
        horizon = dte / 365.0
        shocks = rng.standard_normal(paths)
        shocks *= volatility * math.sqrt(horizon)
        # `drift` is the same r - q the payoff engine puts in its forward, so the
        # two answer for one world.
        shocks += (drift - 0.5 * volatility * volatility) * horizon
        prices = spot * np.exp(shocks, out=shocks)
        if price_cache is not None:
            price_cache[cache_key] = prices
    return prices


def _moneyness(spot: float, strike: float | None, option_type: str) -> str | None:
    """Where `strike` sits relative to `spot` for that option type, at entry."""
    if strike is None:
        return None
    if abs(spot - strike) < 1e-9:
        return "ATM"
    if option_type == "CALL":
        return "ITM" if spot > strike else "OTM"
    return "ITM" if spot < strike else "OTM"


def breach_probabilities(
    *, spot, observed_at, observation, expiry, volatility, paths: int, steps: int,
    short_put_strike: float | None = None, short_call_strike: float | None = None,
    long_put_strike: float | None = None, long_call_strike: float | None = None,
    loss_lower: float | None = None, loss_upper: float | None = None,
    drift: float = 0.0, path_cache: dict | None = None,
) -> dict[str, Any]:
    """Deterministic GBM Monte Carlo probability that the underlying SETTLES
    beyond the upper/lower boundary AT `expiry`, seeded causally from
    (observed_at, expiry, spot, volatility) so repeated calls with the same
    inputs reproduce exactly.

    This is a terminal probability, not a touch/barrier one, and the distinction
    is the whole point.  A touch figure counts a path that pokes through the
    boundary mid-life and comes back; for a driftless GBM that runs at roughly
    twice the terminal figure.  It is the right number only if something closes
    the position when the boundary is crossed, and nothing here does -- the exit
    policy states the premise outright: positions are held to expiry, and the
    three surviving soft exits all bank profit, none watches the range.  So a
    mid-life excursion that reverts costs this strategy nothing, and gating
    entry on it prices a risk the strategy never takes.

    **Which boundary is gated (`breach_boundary_basis`).** In preference order:

    1. `BREAKEVEN` -- the structure's breakevens, the only basis that means the
       same thing for all four families: *past here the position is down money*.
    2. `SHORT_STRIKE` -- the shorts, when no premiums were supplied. That is
       assignment risk, not loss risk.
    3. `WING_STRIKE` -- used only when the shorts coincide or invert (a
       butterfly) and no breakevens were supplied.
    4. `ATR_RANGE` -- the RPS support/resistance, for a pre-candidate check with
       no strikes at all.

    A single-sided credit spread has no second boundary: that side is `None`,
    never silently substituted with an unrelated ATR-range figure.

    `either_breach_probability` is the probability of settling outside the range
    on either side.  At a single instant the two sides are mutually exclusive,
    so it is simply their sum.

    `path_cache` reuses one draw across the candidates of a single decision: the
    simulation depends only on (observed_at, expiry, spot, volatility, paths),
    never on the strikes, so only the cheap comparison runs per candidate.
    """
    spot = _finite_positive(spot)
    short_upper = _finite_positive(short_call_strike)
    short_lower = _finite_positive(short_put_strike)
    loss_upper = _finite_positive(loss_upper)
    loss_lower = _finite_positive(loss_lower)
    if loss_upper is not None or loss_lower is not None:
        basis, upper, lower = "BREAKEVEN", loss_upper, loss_lower
    elif short_upper is not None or short_lower is not None:
        basis, upper, lower = "SHORT_STRIKE", short_upper, short_lower
        if upper is not None and lower is not None and not lower < upper:
            wing_upper = _finite_positive(long_call_strike)
            wing_lower = _finite_positive(long_put_strike)
            basis = "WING_STRIKE"
            upper, lower = (
                (wing_upper, wing_lower)
                if wing_upper is not None and wing_lower is not None and wing_lower < wing_upper
                else (None, None)
            )
    else:
        basis = "ATR_RANGE"
        upper = _finite_positive(observation.get("resistance"))
        lower = _finite_positive(observation.get("support"))
    dte = (expiry - observed_at.date()).days if expiry else 0
    unavailable = {
        "breach_probability_method": BREACH_PROBABILITY_METHOD,
        "available": False,
        "breach_boundary_basis": basis,
        "breach_boundaries": {"upper": None, "lower": None},
        "upper_breach_probability": None,
        "lower_breach_probability": None,
        "either_breach_probability": None,
        "breach_asymmetry": None,
        "strike_breach": None,
        "moneyness": None,
        "number_of_paths": 0,
    }
    if spot is None or volatility is None or dte <= 0 or (upper is None and lower is None):
        return unavailable
    if upper is not None and lower is not None and not lower < upper:
        return unavailable
    seed_text = (
        f"{ENTRY_SIGNAL_VERSION}|{observed_at.isoformat()}|{expiry}"
        f"|{spot}|{volatility}|{drift}"
    )
    terminal = _terminal_prices(seed_text, spot, volatility, dte, paths, path_cache, drift)

    def _beyond(upper_bound, lower_bound):
        """Settlement, not the journey: one comparison per path against the
        boundary that decides the payoff. Strict, so a path landing exactly on
        it has not finished beyond it. The two sides are mutually exclusive at a
        single instant, so `either` is their union -- which is their sum."""
        above = None if upper_bound is None else terminal > upper_bound
        below = None if lower_bound is None else terminal < lower_bound
        high = float(np.mean(above)) if above is not None else None
        low = float(np.mean(below)) if below is not None else None
        if above is not None and below is not None:
            both = float(np.mean(above | below))
        else:
            both = high if high is not None else low
        return high, low, both

    upper_probability, lower_probability, either_probability = _beyond(upper, lower)
    asymmetry = (
        abs(upper_probability - lower_probability)
        if upper_probability is not None and lower_probability is not None
        else None
    )
    strike_breach = None
    if short_upper is not None or short_lower is not None:
        # Reported whatever the gate ran on, and per side even when the pair is
        # degenerate (a butterfly's coincident shorts) -- as a *range* those two
        # are unusable, but each one on its own is still a real ITM probability.
        high, low, both = _beyond(short_upper, short_lower)
        strike_breach = {
            "upper_breach_probability": _round(high),
            "lower_breach_probability": _round(low),
            "either_breach_probability": _round(both),
            "upper_strike": _round(short_upper),
            "lower_strike": _round(short_lower),
        }
    moneyness = {
        "spot": _round(spot),
        "short_call": {
            "strike": _round(short_upper),
            "position": _moneyness(spot, short_upper, "CALL"),
            "settles_itm_probability": _round(
                float(np.mean(terminal > short_upper)) if short_upper is not None else None
            ),
        },
        "short_put": {
            "strike": _round(short_lower),
            "position": _moneyness(spot, short_lower, "PUT"),
            "settles_itm_probability": _round(
                float(np.mean(terminal < short_lower)) if short_lower is not None else None
            ),
        },
    }
    return {
        "breach_probability_method": BREACH_PROBABILITY_METHOD,
        "available": True,
        "breach_boundary_basis": basis,
        "breach_boundaries": {"upper": _round(upper), "lower": _round(lower)},
        "upper_breach_probability": _round(upper_probability),
        "lower_breach_probability": _round(lower_probability),
        "either_breach_probability": _round(either_probability),
        "breach_asymmetry": _round(asymmetry),
        "strike_breach": strike_breach,
        "moneyness": moneyness,
        "number_of_paths": paths,
    }


class DecisionEntrySignal:
    """Evaluate the approved range/IV/breach rules for one decision timestamp.

    One instance is built per decision and scores every candidate structure
    against it, so the GBM draw -- which depends on the snapshot and expiry but
    never on the strikes -- is taken once and reused, while each candidate's own
    strikes still get an independently correct comparison.
    """

    def __init__(self, parameters: dict[str, Any], *, iv_history: list[float] | None = None):
        self.enabled = entry_signal_enabled(parameters)
        self.config = EntrySignalConfig.from_parameters(parameters)
        self.iv_history = list(iv_history or [])[-self.config.history_window:]
        self._path_cache: dict[tuple, Any] = {}
        self._cache: dict[tuple, dict[str, Any]] = {}

    def evaluate(self, *, spot, observed_at, surface, chains, observation, expiry) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "entry_allowed": True, "version": ENTRY_SIGNAL_VERSION}

        # Never substitute another contract's IV when a caller requests an
        # explicit expiry. Missing same-expiry IV must fail closed.
        target_expiry = expiry if expiry is not None else (min(surface) if surface else None)
        # Breach probability depends on the candidate's own short strikes, so two
        # candidates sharing an expiry must not collide on one cache entry --
        # they can legitimately get different entry_allowed results.
        cache_key = (
            target_expiry or date.min,
            observation.get("short_put_strike"), observation.get("short_call_strike"),
            observation.get("breakeven_lower"), observation.get("breakeven_upper"),
        )
        if cache_key in self._cache:
            return self._cache[cache_key]

        current = surface.get(target_expiry) if target_expiry is not None else None
        current_iv = current["iv"] if current else None
        history = self.iv_history
        # The source's order exactly: the precomputed per-session iv_rank_252 /
        # iv_percentile_252 first, then a locally measured history, then the
        # short-history fallback of a neutral 50.0 in `components` below.
        iv_rank = _finite_number(observation.get("iv_rank_252"))
        iv_percentile = _finite_number(observation.get("iv_percentile_252"))
        iv_rank_source = "RPS_WARMUP"
        if iv_rank is None or iv_percentile is None:
            iv_rank = _iv_rank(current_iv, history)
            iv_percentile = _iv_percentile(current_iv, history)
            iv_rank_source = "MEASURED_HISTORY" if history else "UNAVAILABLE_NEUTRAL"
        realized = _finite_positive(observation.get("hv20"))
        iv_rv_ratio = current_iv / realized if current_iv is not None and realized is not None else None

        later_expiries = sorted(item for item in surface if target_expiry is not None and item > target_expiry)
        longer_iv = surface[later_expiries[0]]["iv"] if later_expiries else None
        term_ratio = current_iv / longer_iv if current_iv and longer_iv else None
        skew = delta_skew(chains.get(target_expiry) or [])

        components = {
            "iv_rank": 50.0 if iv_rank is None else iv_rank,
            "iv_percentile": 50.0 if iv_percentile is None else iv_percentile,
            "iv_rv": _score_iv_rv(iv_rv_ratio),
            "term_structure": _score_term(term_ratio),
            "skew": _score_skew(skew),
        }
        weights = self.config.component_weights
        raw_score = sum(weights[name] * components[name] for name in weights)
        score = self.config.normalise_score(raw_score)
        # `enough_history` is informational only. Requiring `minimum_history`
        # sessions before a score can ever be "favourable" hard-blocks every
        # entry while the history is short, regardless of how low
        # `favourable_score` is tuned; the rank/percentile fallbacks already
        # keep the blend a real (if less discriminating) signal without it.
        enough_history = len(history) >= self.config.minimum_history
        favourable = current_iv is not None and score >= self.config.favourable_score

        breach = breach_probabilities(
            spot=spot, observed_at=observed_at, observation=observation,
            expiry=target_expiry, volatility=current_iv,
            paths=self.config.simulation_paths, steps=self.config.simulation_steps,
            short_put_strike=observation.get("short_put_strike"),
            short_call_strike=observation.get("short_call_strike"),
            long_put_strike=observation.get("long_put_strike"),
            long_call_strike=observation.get("long_call_strike"),
            loss_lower=observation.get("breakeven_lower"),
            loss_upper=observation.get("breakeven_upper"),
            drift=self.config.risk_free_rate - self.config.dividend_yield,
            path_cache=self._path_cache,
        )
        breach_available = breach["available"]
        upper_breach = breach["upper_breach_probability"]
        lower_breach = breach["lower_breach_probability"]
        either_breach = breach["either_breach_probability"]
        # A two-sided structure is gated on the probability of settling outside
        # the range at all, not on the worse of its two sides: checking each side
        # separately against its own ceiling hands the structure one full budget
        # per side, so two ceilings of 50% would permit a combined 100%. The two
        # sides are mutually exclusive at expiry, so their union is their sum.
        if not breach_available:
            breach_ok = False
        elif upper_breach is not None and lower_breach is not None:
            breach_ok = either_breach <= min(
                self.config.max_upper_breach_probability,
                self.config.max_lower_breach_probability,
            )
        elif upper_breach is not None:
            breach_ok = upper_breach <= self.config.max_upper_breach_probability
        elif lower_breach is not None:
            breach_ok = lower_breach <= self.config.max_lower_breach_probability
        else:
            breach_ok = False
        reasons = []
        if current_iv is None:
            reasons.append("Contract IV unavailable at entry timestamp")
        if not enough_history:
            reasons.append(
                f"IV history {len(history)}/{self.config.minimum_history} "
                "(informational; not a blocking gate)"
            )
        if score < self.config.favourable_score:
            reasons.append(f"IV score {score:.2f} below {self.config.favourable_score:.2f}")
        if not breach_available:
            reasons.append("IV range breach probability unavailable")
        elif not breach_ok:
            basis = breach.get("breach_boundary_basis")
            measured = (
                "probability of settling at a loss"
                if basis == "BREAKEVEN" else "probability of settling outside the range"
            )
            reasons.append(
                f"{measured} {either_breach:.2%} exceeds the configured entry limit"
            )

        rps = _finite_number(observation.get("rps_base"))
        # A not-applicable side contributes no risk (0.0), not a missing value;
        # only an unavailable computation as a whole fails safe (1.0).
        gate_upper = 0.0 if upper_breach is None else upper_breach
        gate_lower = 0.0 if lower_breach is None else lower_breach
        confidence = range_confidence(
            rps, gate_upper, gate_lower
        ) if rps is not None and breach_available else None
        data_quality_flagged = bool(observation.get("data_quality_ok", True))
        quality_ok = bool(
            rps is not None and current_iv is not None and breach_available
            and data_quality_flagged
        )
        entry_ok = approved_entry_allowed(EntryInputs(
            rps=rps or 0.0,
            upper_breach_probability=gate_upper if breach_available else 1.0,
            lower_breach_probability=gate_lower if breach_available else 1.0,
            iv_condition=IVCondition.FAVOURABLE.value if favourable else IVCondition.NOT_FAVOURABLE.value,
            range_confidence=confidence or 0.0,
            data_quality_ok=quality_ok,
            min_rps=self.config.min_rps,
            max_upper_breach_probability=self.config.max_upper_breach_probability,
            max_lower_breach_probability=self.config.max_lower_breach_probability,
            # Supplied only when the structure sold both sides; a single-sided
            # spread stays on the per-side rule above.
            either_breach_probability=(
                either_breach
                if breach_available and upper_breach is not None and lower_breach is not None
                else None
            ),
            max_either_breach_probability=self.config.two_sided_ceiling,
        ))
        if rps is None:
            reasons.append("RPS unavailable; RPS cannot be used as a standalone or implicit trigger")
        elif rps < self.config.min_rps:
            reasons.append(f"RPS {rps:.2f} below entry floor {self.config.min_rps:.2f}")
        if not data_quality_flagged:
            reasons.append("Upstream observation flagged a data-quality issue")
        if confidence is not None and confidence <= 0.0:
            reasons.append("Range confidence is non-positive")
        result = {
            "enabled": True,
            "version": ENTRY_SIGNAL_VERSION,
            "data_source": "POINT_IN_TIME_OPTION_CHAIN",
            "observed_at": observed_at.isoformat(),
            "expiry": target_expiry.isoformat() if target_expiry else None,
            "current_iv": _round(current_iv),
            "historical_iv_observations": len(history),
            "iv_rank": _round(iv_rank),
            "iv_percentile": _round(iv_percentile),
            "realized_volatility": _round(realized),
            "iv_rv_ratio": _round(iv_rv_ratio),
            "term_structure_ratio": _round(term_ratio),
            "skew": _round(skew),
            "component_scores": {key: _round(value) for key, value in components.items()},
            "iv_score": _round(score),
            "iv_score_raw": _round(raw_score),
            "iv_score_scale": "NORMALISED_0_100",
            "iv_rank_source": iv_rank_source,
            "iv_condition": IVCondition.FAVOURABLE.value if favourable else IVCondition.NOT_FAVOURABLE.value,
            "range_confidence": _round(confidence),
            **breach,
            "entry_allowed": entry_ok,
            "reasons": reasons or ["Approved range, IV and breach entry conditions passed"],
        }
        self._cache[cache_key] = result
        return result


def _finite_positive(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _finite_number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _probability(value) -> float:
    number = float(value)
    return number / 100 if number > 1 else number


def _round(value):
    return round(float(value), 6) if value is not None and math.isfinite(float(value)) else None
