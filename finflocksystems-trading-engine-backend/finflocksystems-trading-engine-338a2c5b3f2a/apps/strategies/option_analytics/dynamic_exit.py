"""Point-in-time soft-exit policy, ported from ``Aryan_Options/trading/dynamic_exit.py``.

Positions are held to expiry: there are no hard exits, so the only ways a
position leaves the book are the contract close at expiry (``exit_dte``), an
operator flatten, or one of the three profit/regime soft exits in
``exit_policy``.

The source keeps its entry baseline and running peak P&L in memory for the
length of a replay.  This runtime is re-entered per bar, so the same three
numbers live in the strategy instance's ``state_data`` instead and are threaded
through ``evaluate`` rather than held on the controller.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite

from .exit_policy import (
    ExitConfig as PolicyExitConfig,
    ExitInput as PolicyExitInput,
    evaluate_exit as evaluate_exit_policy,
)
from .payoff import OptionLeg, calculate_pop


REASON_EXPLANATIONS = {
    "SOFT_VOLUME_IV_EXPANSION": "Volume and IV expansion indicate a possible market-regime change.",
    "SOFT_PROFIT_GIVEBACK_RISK": "Most profit is captured and the probability of a material giveback is high.",
    "SOFT_PROFIT_CAPTURE_RISK_REWARD": "Remaining maximum profit does not provide the configured holding edge.",
}


@dataclass(frozen=True)
class ExitConfig:
    max_profit_giveback_pct_soft: float = 0.25
    probability_of_giveback_soft: float = 0.40
    majority_profit_capture: float = 0.70
    volume_increase_soft: float = 0.50
    iv_increase_soft: float = 0.20
    min_ev_improvement_pct: float = 0.05
    remaining_profit_pct_of_max: float = 0.30
    require_both_volume_and_iv: bool = True

    @classmethod
    def from_parameters(cls, parameters):
        defaults = cls()
        values = {}
        # Retired keys (`dynamic_exit_hard_*`, `dynamic_exit_soft_min_pop`,
        # `dynamic_exit_soft_range_shift`, `dynamic_exit_soft_min_range_overlap`)
        # are accepted and ignored rather than rejected, so parameter sets saved
        # before hard exits were removed still load.
        mapping = {
            "max_profit_giveback_pct_soft": "dynamic_exit_soft_max_giveback",
            "probability_of_giveback_soft": "dynamic_exit_soft_giveback_probability",
            "majority_profit_capture": "dynamic_exit_soft_profit_capture",
            "volume_increase_soft": "dynamic_exit_soft_volume_increase",
            "iv_increase_soft": "dynamic_exit_soft_iv_increase",
            "min_ev_improvement_pct": "dynamic_exit_soft_min_ev_improvement",
            "remaining_profit_pct_of_max": "dynamic_exit_soft_remaining_profit",
        }
        for field, parameter in mapping.items():
            raw = parameters.get(parameter, getattr(defaults, field))
            value = float(raw)
            values[field] = value / 100.0 if value > 1 else value
        raw_both = parameters.get("dynamic_exit_require_volume_and_iv", True)
        values["require_both_volume_and_iv"] = (
            raw_both if isinstance(raw_both, bool) else str(raw_both).lower() in {"1", "true", "yes", "on"}
        )
        return cls(**values)


def dynamic_exit_enabled(parameters) -> bool:
    raw = parameters.get("dynamic_exit_enabled", True)
    return raw if isinstance(raw, bool) else str(raw).lower() in {"1", "true", "yes", "on"}


def entry_baseline(legs, *, observation, spot):
    """The three numbers the soft exits measure against, fixed at entry.

    `legs` are the entered quotes carrying `volume` and `iv`.
    """
    volumes = [float(leg["volume"]) for leg in legs if leg.get("volume") is not None]
    ivs = [float(leg["iv"]) for leg in legs if leg.get("iv") is not None and float(leg["iv"]) > 0]
    low = observation.get("support") or observation.get("atr_support") or spot
    high = observation.get("resistance") or observation.get("atr_resistance") or spot
    return {
        "original_low": float(low), "original_high": float(high),
        "baseline_volume": sum(volumes) if volumes else None,
        "baseline_iv": sum(ivs) / len(ivs) if ivs else None,
        "peak_pnl": 0.0,
    }


class DynamicExitController:
    """Evaluate the approved soft-exit policy using only current market data."""

    def __init__(self, parameters):
        self.enabled = dynamic_exit_enabled(parameters)
        self.config = ExitConfig.from_parameters(parameters)
        # Same rate/yield keys as entry-time POP selection, so exit-time POP and
        # expected-P&L recalculation cannot silently disagree with the model
        # that gated entry.
        self.risk_free_rate = float(parameters.get("rps_pop_risk_free_rate", 0.068))
        self.dividend_yield = float(parameters.get("rps_pop_dividend_yield", 0.012))

    def evaluate(self, *, positions, quotes, baseline, observation, spot, session_date, expiry):
        """Decide HOLD or SOFT_EXIT for one open structure.

        `positions` carry `right`, `strike`, `quantity`, `average_cost` and
        `multiplier`; `quotes` map the same instrument ids to a fresh `close`,
        `iv` and `volume`.  Returns `None` when the policy cannot be evaluated,
        which holds the position -- the same failure mode the source takes.
        """
        if not self.enabled or not positions or not baseline:
            return None
        try:
            return self._evaluate(positions, quotes, dict(baseline), observation, spot, session_date, expiry)
        except (TypeError, ValueError, OverflowError, KeyError):
            return None

    def _evaluate(self, positions, quotes, baseline, observation, spot, session_date, expiry):
        rows = [(position, quotes.get(position["instrument_id"])) for position in positions]
        if any(quote is None or quote.get("close") is None for _position, quote in rows):
            return None
        held_ivs = [
            float(quote["iv"]) for _position, quote in rows
            if quote.get("iv") is not None and float(quote["iv"]) > 0
        ]
        if not held_ivs:
            return None
        iv = sum(held_ivs) / len(held_ivs)
        dte = max((expiry - session_date).days, 0)
        legs = tuple(
            OptionLeg(
                option_type="CALL" if position["right"] == "C" else "PUT",
                strike=float(position["strike"]), quantity=int(position["quantity"]),
                entry_premium=float(position["average_cost"]),
                multiplier=float(position["multiplier"]),
                entry_iv=float(quote["iv"]) if quote.get("iv") and float(quote["iv"]) > 0 else None,
            )
            for position, quote in rows
        )
        analysis = calculate_pop(
            spot=float(spot), dte_days=dte, iv=iv, legs=legs,
            risk_free_rate=self.risk_free_rate, dividend_yield=self.dividend_yield,
        )
        current_pnl = float(sum(
            (float(quote["close"]) - float(position["average_cost"]))
            * float(position["quantity"]) * float(position["multiplier"])
            for position, quote in rows
        ))
        peak_pnl = max(float(baseline.get("peak_pnl") or 0.0), current_pnl)
        baseline["peak_pnl"] = peak_pnl
        max_profit = analysis["max_profit"]
        if max_profit is None or not isfinite(float(max_profit)) or float(max_profit) <= 0:
            max_profit = 0.0
        else:
            max_profit = float(max_profit)
        current_low = float(observation.get("support") or observation.get("atr_support") or baseline["original_low"])
        current_high = float(observation.get("resistance") or observation.get("atr_resistance") or baseline["original_high"])
        # No breach Monte Carlo here. It existed only to feed the removed
        # HARD_RANGE_BREACH_RISK gate; with hard exits gone the position is held
        # to expiry regardless of where the range sits. Entry-time breach
        # probability is still what decides whether it is opened at all.
        volumes = [float(quote["volume"]) for _position, quote in rows if quote.get("volume") is not None]
        current_volume = sum(volumes) if volumes else None
        significant_giveback_probability = float(analysis["probability_loss"])
        giveback_floor = peak_pnl * (1 - self.config.max_profit_giveback_pct_soft)
        if giveback_floor > 0:
            threshold_analysis = calculate_pop(
                spot=float(spot), dte_days=dte, iv=iv, legs=legs,
                risk_free_rate=self.risk_free_rate, dividend_yield=self.dividend_yield,
                transaction_cost=giveback_floor,
            )
            significant_giveback_probability = 1 - float(threshold_analysis["pop"])

        expected_pnl = float(analysis["expected_pnl"])
        policy_config = PolicyExitConfig(
            max_profit_giveback_pct_soft=self.config.max_profit_giveback_pct_soft,
            probability_of_giveback_soft=self.config.probability_of_giveback_soft,
            majority_profit_capture=self.config.majority_profit_capture,
            volume_increase_soft=self.config.volume_increase_soft,
            iv_increase_soft=self.config.iv_increase_soft,
            require_both_volume_and_iv=self.config.require_both_volume_and_iv,
            min_ev_improvement_pct=self.config.min_ev_improvement_pct,
            remaining_profit_pct_of_max=self.config.remaining_profit_pct_of_max,
        )
        policy_input = PolicyExitInput(
            current_pnl=current_pnl,
            max_profit=max_profit,
            peak_pnl=peak_pnl,
            original_range_low=baseline["original_low"],
            original_range_high=baseline["original_high"],
            current_range_low=current_low,
            current_range_high=current_high,
            probability_of_significant_giveback=significant_giveback_probability,
            expected_pnl_if_hold=expected_pnl,
            current_volume=current_volume or 0.0,
            baseline_volume=baseline.get("baseline_volume") or 0.0,
            current_iv=iv,
            baseline_iv=baseline.get("baseline_iv") or 0.0,
            timestamp=session_date.isoformat(),
        )
        # Spot leaving the ATR range used to be forced in here as
        # HARD_RANGE_BREACH -- a literal *touch* test on the support/resistance
        # box, not a probability of anything, and the single most common reason
        # a position was closed. Positions are now held through it.
        decision = evaluate_exit_policy(policy_input, policy_config)
        codes = list(decision.reason_codes)
        return {
            "decision": decision.decision.value,
            "reason_codes": codes or ["NO_EXIT_TRIGGER"],
            "explanations": [REASON_EXPLANATIONS[code] for code in codes if code in REASON_EXPLANATIONS],
            "baseline": baseline,
            "metrics": {
                "current_pnl": round(current_pnl, 6), "peak_pnl": round(peak_pnl, 6),
                "max_profit": round(max_profit, 6), "expected_pnl_if_hold": round(expected_pnl, 6),
                "profit_capture_pct": _rounded(decision.metrics.get("profit_capture")),
                "profit_giveback_pct": _rounded(decision.metrics.get("profit_giveback")),
                "range_overlap_pct": _rounded(decision.metrics.get("range_overlap_pct")),
                "max_boundary_shift_pct": _rounded(decision.metrics.get("max_boundary_shift_pct")),
                "volume_change_pct": _rounded(decision.metrics.get("volume_change")),
                "iv_change_pct": _rounded(decision.metrics.get("iv_change")),
                "probability_of_loss": analysis["probability_loss"],
                "probability_of_profit": analysis["pop"],
                "probability_of_significant_giveback": round(significant_giveback_probability, 6),
                "ev_improvement_pct": _rounded(decision.metrics.get("ev_improvement")),
                "original_range": [baseline["original_low"], baseline["original_high"]],
                "current_range": [current_low, current_high],
                "spot": float(spot), "current_iv": iv, "dte": dte,
            },
        }

    def public_config(self):
        return {
            "enabled": self.enabled, "decision_order": ["SOFT_EXIT", "HOLD"],
            "hold_to_expiry": True,
            "exit_rules": [
                "SOFT_VOLUME_IV_EXPANSION",
                "SOFT_PROFIT_GIVEBACK_RISK",
                "SOFT_PROFIT_CAPTURE_RISK_REWARD",
            ],
            "pop_model": "LOGNORMAL_RISK_NEUTRAL_IV",
            "range_source": "causal daily ATR RPS boundaries",
            "risk_free_rate": self.risk_free_rate, "dividend_yield": self.dividend_yield,
            **asdict(self.config),
        }


def _rounded(value):
    return round(float(value), 6) if value is not None else None
