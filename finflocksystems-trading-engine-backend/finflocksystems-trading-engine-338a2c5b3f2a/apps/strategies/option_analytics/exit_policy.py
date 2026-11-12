"""Pure soft-exit policy for RPS option backtests.

Positions are held to expiry.  There is no hard exit: nothing here can
force a position closed on a risk reading, because the whole point of a
defined-risk credit structure is that its worst case is already bounded
at entry, and closing early on a transient breach/loss-probability spike
realises that worst case instead of letting it resolve.  The lifecycle
close is settlement at expiry, which this runtime reaches through the
plugin's ``exit_dte`` contract close, or an operator flatten.

Only three discretionary exits survive, and all three are about *banking
an edge that already exists*, never about fear of the range:

``SOFT_PROFIT_GIVEBACK_RISK``
    Most of the profit is captured and a material giveback is likely.
``SOFT_PROFIT_CAPTURE_RISK_REWARD``
    Most of the profit is captured and the profit still on the table does
    not pay for the risk of continuing to hold.
``SOFT_VOLUME_IV_EXPANSION``
    Volume *and* IV expanded together -- a regime change, not a price move.

Deliberately removed (do not reintroduce without changing the hold-to-expiry
premise): the five ``HARD_*`` rules, plus ``SOFT_RANGE_THESIS_DETERIORATION``
(range-based), ``SOFT_LOW_PROBABILITY_OF_PROFIT`` (risk-based) and
``SOFT_NEGATIVE_HOLDING_EDGE``.  Range overlap/shift are still reported as
diagnostics so the ledger keeps showing what the range did; they just no
longer close anything.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List


class ExitType(str, Enum):
    HOLD = "HOLD"
    SOFT_EXIT = "SOFT_EXIT"


@dataclass(frozen=True)
class ExitConfig:
    max_profit_giveback_pct_soft: float = .25
    probability_of_giveback_soft: float = .40
    majority_profit_capture: float = .70
    volume_increase_soft: float = .50
    iv_increase_soft: float = .20
    require_both_volume_and_iv: bool = True
    min_ev_improvement_pct: float = .05
    remaining_profit_pct_of_max: float = .30


@dataclass(frozen=True)
class ExitInput:
    current_pnl: float
    max_profit: float
    peak_pnl: float
    original_range_low: float
    original_range_high: float
    current_range_low: float
    current_range_high: float
    probability_of_significant_giveback: float
    expected_pnl_if_hold: float
    current_volume: float
    baseline_volume: float
    current_iv: float
    baseline_iv: float
    timestamp: str | None = None


@dataclass
class ExitDecision:
    decision: ExitType
    reason_codes: List[str] = field(default_factory=list)
    explanations: List[str] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)


def _ratio(a, b):
    return a / b if b else 0.0


def evaluate_exit(data: ExitInput, config: ExitConfig | None = None) -> ExitDecision:
    config = config or ExitConfig()
    # Range overlap/shift are diagnostics only -- reported, never gated on.
    width = max(0.0, data.original_range_high - data.original_range_low)
    overlap = max(0.0, min(data.original_range_high, data.current_range_high) - max(data.original_range_low, data.current_range_low))
    overlap_pct = _ratio(overlap, width)
    shift = max(
        _ratio(abs(data.current_range_low - data.original_range_low), width),
        _ratio(abs(data.current_range_high - data.original_range_high), width),
    )
    volume_change = _ratio(data.current_volume - data.baseline_volume, data.baseline_volume)
    iv_change = _ratio(data.current_iv - data.baseline_iv, data.baseline_iv)
    capture = _ratio(data.current_pnl, data.max_profit) if data.max_profit > 0 else 0.0
    giveback = max(0.0, _ratio(data.peak_pnl - data.current_pnl, data.peak_pnl)) if data.peak_pnl > 0 else 0.0
    ev_improvement = _ratio(data.expected_pnl_if_hold - data.current_pnl, max(abs(data.current_pnl), 1.0))
    remaining_pct = _ratio(max(0.0, data.max_profit - data.current_pnl), data.max_profit)
    metrics = {
        "profit_capture": capture, "profit_giveback": giveback,
        "range_overlap_pct": overlap_pct, "max_boundary_shift_pct": shift,
        "volume_change": volume_change, "iv_change": iv_change,
        "ev_improvement": ev_improvement,
    }
    soft = []
    volume_iv = (
        (volume_change >= config.volume_increase_soft and iv_change >= config.iv_increase_soft)
        if config.require_both_volume_and_iv
        else (volume_change >= config.volume_increase_soft or iv_change >= config.iv_increase_soft)
    )
    if volume_iv:
        soft.append("SOFT_VOLUME_IV_EXPANSION")
    if capture >= config.majority_profit_capture and data.probability_of_significant_giveback >= config.probability_of_giveback_soft:
        soft.append("SOFT_PROFIT_GIVEBACK_RISK")
    if capture >= config.majority_profit_capture and remaining_pct <= config.remaining_profit_pct_of_max and ev_improvement < config.min_ev_improvement_pct:
        soft.append("SOFT_PROFIT_CAPTURE_RISK_REWARD")
    return ExitDecision(ExitType.SOFT_EXIT if soft else ExitType.HOLD, soft, metrics=metrics)
