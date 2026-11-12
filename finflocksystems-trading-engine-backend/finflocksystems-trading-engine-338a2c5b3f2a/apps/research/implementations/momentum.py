from __future__ import annotations

import numpy as np

from ..engines.base import CrossSectionalSelector, ResearchSignalResult, SingleAssetResearchStrategy
from ..services.features import momentum, realized_volatility
from ._common import bounded_long, columns, parameter


class TimeSeriesMomentumResearch(SingleAssetResearchStrategy):
    def __init__(self, lookback):
        self.lookback = lookback

    def signals(self, bars, parameters, context):
        close = columns(bars)["close"]
        lookback = parameter(parameters, "lookback_days", self.lookback, int)
        threshold = parameter(parameters, "threshold", 0.0)
        target = parameter(parameters, "vol_target", 0.15)
        signal = momentum(close, lookback)
        volatility = realized_volatility(close, min(60, max(20, lookback)))
        exposure = np.divide(target, volatility, out=np.zeros(len(close)), where=np.nan_to_num(volatility) > 0)
        exposure[(signal <= threshold) | np.isnan(signal)] = 0
        return ResearchSignalResult(bounded_long(exposure), {"warmup_bars": max(lookback, 60) + 1})


class MomentumSelector(CrossSectionalSelector):
    def __init__(self, feature, *, descending=True):
        self.feature = feature
        self.descending = descending

    def rank(self, panel, parameters, context):
        rows = []
        for row in panel:
            features = row.get("features", row)
            value = features.get(self.feature)
            if value is None and self.feature != "formation_return":
                value = features.get("formation_return")
            if value is not None:
                rows.append({**row, "model_score": float(value)})
        return sorted(rows, key=lambda row: (row["model_score"], str(row.get("symbol", ""))), reverse=self.descending)


MOM_001_TS_21 = TimeSeriesMomentumResearch(21)
MOM_002_TS_63 = TimeSeriesMomentumResearch(63)
MOM_003_TS_126 = TimeSeriesMomentumResearch(126)
MOM_004_TS_189 = TimeSeriesMomentumResearch(189)
MOM_005_TS_252 = TimeSeriesMomentumResearch(252)
MOM_006_XS_63_5 = MomentumSelector("formation_return")
MOM_007_XS_126_21 = MomentumSelector("formation_return")
MOM_008_XS_252_21 = MomentumSelector("formation_return")
MOM_009_XS_252_63 = MomentumSelector("formation_return")
MOM_010_52W_HIGH = MomentumSelector("proximity")
MOM_011_RISK_ADJ = MomentumSelector("risk_adjusted_momentum")
MOM_012_RESIDUAL = MomentumSelector("residual_return")
MOM_013_SECTOR_REL = MomentumSelector("relative_return")
MOM_014_DUAL = MomentumSelector("absolute_momentum")

