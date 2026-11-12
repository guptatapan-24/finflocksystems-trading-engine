from __future__ import annotations

import numpy as np

from ..engines.base import CrossSectionalSelector, ResearchSignalResult, SingleAssetResearchStrategy
from ..services.features import rsi, sma
from ._common import bounded_long, columns, parameter, rolling_std


class MeanReversionResearch(SingleAssetResearchStrategy):
    def __init__(self, variant, defaults):
        self.variant = variant
        self.defaults = defaults

    def signals(self, bars, parameters, context):
        p = {**self.defaults, **parameters}
        values = columns(bars)
        close = values["close"]
        window = parameter(p, "window", 14, int)
        entry = parameter(p, "entry_threshold", 30)
        max_holding = parameter(p, "max_holding_days", 10, int)
        if self.variant in {"rsi", "connors"}:
            oscillator = rsi(close, window)
            oversold = oscillator <= entry
            recovered = oscillator >= max(50, entry + 20)
        elif self.variant in {"bollinger", "ma_distance"}:
            average = sma(close, window)
            deviation = rolling_std(close, window)
            oscillator = np.divide(close - average, deviation, out=np.zeros(len(close)), where=np.nan_to_num(deviation) > 0)
            oversold = oscillator <= -abs(entry)
            recovered = oscillator >= 0
        elif self.variant in {"stochastic", "williams"}:
            oscillator = np.full(len(close), np.nan)
            for index in range(window - 1, len(close)):
                low = np.min(values["low"][index - window + 1:index + 1])
                high = np.max(values["high"][index - window + 1:index + 1])
                oscillator[index] = 100 * (close[index] - low) / (high - low) if high > low else 50
            if self.variant == "williams":
                oscillator -= 100
                oversold, recovered = oscillator <= -80, oscillator >= -50
            else:
                oversold, recovered = oscillator <= entry, oscillator >= 50
        elif self.variant == "gap":
            gap = np.r_[0.0, values["open"][1:] / close[:-1] - 1.0]
            scale = rolling_std(gap, 20)
            oscillator = np.divide(gap, scale, out=np.zeros(len(close)), where=np.nan_to_num(scale) > 0)
            oversold, recovered = oscillator <= -abs(parameter(p, "z_threshold", 2.0)), oscillator >= 0
            max_holding = 1
        elif self.variant == "vwap":
            typical = (values["high"] + values["low"] + close) / 3
            average = sma(typical, max(2, window))
            deviation = rolling_std(typical, max(2, window))
            oscillator = np.divide(close - average, deviation, out=np.zeros(len(close)), where=np.nan_to_num(deviation) > 0)
            oversold, recovered = oscillator <= -abs(parameter(p, "z_threshold", 2.0)), oscillator >= 0
            max_holding = 1
        else:
            raise ValueError(f"Unsupported mean-reversion variant {self.variant}")
        exposure = np.zeros(len(close))
        held_for = 0
        for index in range(len(close)):
            if exposure[index - 1] if index else False:
                held_for += 1
                exposure[index] = 0 if recovered[index] or held_for >= max_holding else 1
            elif oversold[index]:
                exposure[index], held_for = 1, 0
        return ResearchSignalResult(bounded_long(exposure), {"warmup_bars": window + 1, "variant": self.variant})


class ReversalSelector(CrossSectionalSelector):
    def __init__(self, score_key="trailing_return"):
        self.score_key = score_key

    def rank(self, panel, parameters, context):
        rows = []
        for row in panel:
            value = row.get("features", row).get(self.score_key)
            if value is not None:
                rows.append({**row, "model_score": -float(value)})
        return sorted(rows, key=lambda row: (row["model_score"], str(row.get("symbol", ""))), reverse=True)


MR_001_RSI2 = MeanReversionResearch("rsi", {"window": 2, "entry_threshold": 10, "max_holding_days": 5})
MR_002_RSI14 = MeanReversionResearch("rsi", {"window": 14, "entry_threshold": 30, "max_holding_days": 10})
MR_003_BOLL = MeanReversionResearch("bollinger", {"window": 20, "entry_threshold": 2})
MR_004_MA_DIST = MeanReversionResearch("ma_distance", {"window": 50, "entry_threshold": 2})
MR_005_STOCH = MeanReversionResearch("stochastic", {"window": 14, "entry_threshold": 20})
MR_006_WILLIAMS = MeanReversionResearch("williams", {"window": 14, "entry_threshold": -80})
MR_007_CONNORS = MeanReversionResearch("connors", {"window": 3, "entry_threshold": 20})
MR_008_REV_1D = ReversalSelector("return_1d")
MR_009_REV_5D = ReversalSelector("return_5d")
MR_010_REV_20D = ReversalSelector("return_20d")
MR_011_GAP = MeanReversionResearch("gap", {"window": 20, "z_threshold": 2})
MR_012_VWAP = MeanReversionResearch("vwap", {"window": 20, "z_threshold": 2})
MR_013_PEER_Z = ReversalSelector("peer_residual_zscore")

