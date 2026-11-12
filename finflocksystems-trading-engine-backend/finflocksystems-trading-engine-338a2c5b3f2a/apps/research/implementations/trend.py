from __future__ import annotations

import numpy as np

from ..engines.base import ResearchSignalResult, SingleAssetResearchStrategy
from ..services.features import atr, ema, shifted_donchian, sma
from ._common import bounded_long, columns, parameter


class TrendResearch(SingleAssetResearchStrategy):
    def __init__(self, variant, defaults):
        self.variant = variant
        self.defaults = defaults

    def signals(self, bars, parameters, context):
        values = columns(bars)
        close, high, low = values["close"], values["high"], values["low"]
        p = {**self.defaults, **parameters}
        exposure = np.zeros(len(close))
        warmup = 1
        if self.variant in {"sma", "ema", "triple"}:
            fast_window = parameter(p, "fast_window", 20, int)
            slow_window = parameter(p, "slow_window", 100, int)
            if fast_window >= slow_window:
                raise ValueError("fast_window must be below slow_window")
            average = ema if self.variant == "ema" else sma
            fast, slow = average(close, fast_window), average(close, slow_window)
            exposure = (fast > slow).astype(float)
            if self.variant == "triple":
                middle = average(close, max(fast_window + 1, (fast_window + slow_window) // 2))
                exposure = ((fast > middle) & (middle > slow)).astype(float)
            warmup = slow_window
        elif self.variant == "donchian":
            entry = parameter(p, "entry_lookback", 20, int)
            exit_ = parameter(p, "exit_lookback", 10, int)
            upper, _ = shifted_donchian(high, low, entry)
            _, lower = shifted_donchian(high, low, exit_)
            held = False
            for index in range(len(close)):
                if held and not np.isnan(lower[index]) and close[index] < lower[index]:
                    held = False
                elif not held and not np.isnan(upper[index]) and close[index] > upper[index]:
                    held = True
                exposure[index] = float(held)
            warmup = max(entry, exit_) + 1
        elif self.variant == "price_average":
            window = parameter(p, "window", 200, int)
            average = sma(close, window)
            slope_filter = bool(p.get("slope_filter", True))
            slope = np.r_[np.full(20, np.nan), average[20:] - average[:-20]]
            positive_slope = slope > 0 if slope_filter else np.ones(len(close), dtype=bool)
            exposure = ((close > average) & positive_slope).astype(float)
            warmup = window + (20 if slope_filter else 0)
        elif self.variant == "macd":
            fast_window = parameter(p, "fast", 12, int)
            slow_window = parameter(p, "slow", 26, int)
            signal_window = parameter(p, "signal", 9, int)
            if fast_window >= slow_window:
                raise ValueError("fast must be below slow")
            line = ema(close, fast_window) - ema(close, slow_window)
            signal = ema(np.nan_to_num(line), signal_window)
            exposure = (line > signal).astype(float)
            warmup = slow_window + signal_window
        elif self.variant == "adx":
            window = parameter(p, "window", 14, int)
            threshold = parameter(p, "threshold", 20)
            change = np.r_[0.0, np.diff(close)]
            movement = np.abs(change)
            directional_strength = np.divide(
                np.abs(sma(change, window)), sma(movement, window),
                out=np.zeros(len(close)), where=np.nan_to_num(sma(movement, window)) > 0,
            ) * 100
            exposure = ((change > 0) & (directional_strength >= threshold)).astype(float)
            warmup = window
        elif self.variant in {"supertrend", "keltner"}:
            atr_window = parameter(p, "atr_window", 14, int)
            multiplier = parameter(p, "multiplier", 2.0)
            volatility = atr(high, low, close, atr_window)
            center = ema(close, parameter(p, "ema_window", 20, int)) if self.variant == "keltner" else (high + low) / 2
            band = center + multiplier * volatility
            exposure = (close > band).astype(float)
            warmup = max(atr_window, parameter(p, "ema_window", 1, int))
        elif self.variant == "monthly":
            window = 21 * parameter(p, "window_months", 10, int)
            exposure = (close > sma(close, window)).astype(float)
            warmup = window
        else:
            raise ValueError(f"Unsupported trend variant {self.variant}")
        return ResearchSignalResult(bounded_long(exposure), {"warmup_bars": warmup, "variant": self.variant})


TR_001_SMA_020_100 = TrendResearch("sma", {"fast_window": 20, "slow_window": 100})
TR_002_SMA_050_200 = TrendResearch("sma", {"fast_window": 50, "slow_window": 200})
TR_003_EMA_012_026 = TrendResearch("ema", {"fast_window": 12, "slow_window": 26})
TR_004_EMA_050_150 = TrendResearch("ema", {"fast_window": 50, "slow_window": 150})
TR_005_TRIPLE_MA = TrendResearch("triple", {"fast_window": 20, "slow_window": 200})
TR_006_DONCHIAN_20 = TrendResearch("donchian", {"entry_lookback": 20, "exit_lookback": 10})
TR_007_DONCHIAN_55 = TrendResearch("donchian", {"entry_lookback": 55, "exit_lookback": 20})
TR_008_DONCHIAN_100 = TrendResearch("donchian", {"entry_lookback": 100, "exit_lookback": 50})
TR_009_DONCHIAN_252 = TrendResearch("donchian", {"entry_lookback": 252, "exit_lookback": 100})
TR_010_PRICE_200DMA = TrendResearch("price_average", {"window": 200, "slope_filter": True})
TR_011_MACD = TrendResearch("macd", {"fast": 12, "slow": 26, "signal": 9})
TR_012_ADX = TrendResearch("adx", {"window": 14, "threshold": 20})
TR_013_SUPERTREND = TrendResearch("supertrend", {"atr_window": 14, "multiplier": 3})
TR_014_KELTNER_BREAK = TrendResearch("keltner", {"ema_window": 20, "atr_window": 14, "multiplier": 2})
TR_015_MONTHLY_TREND = TrendResearch("monthly", {"window_months": 10})
