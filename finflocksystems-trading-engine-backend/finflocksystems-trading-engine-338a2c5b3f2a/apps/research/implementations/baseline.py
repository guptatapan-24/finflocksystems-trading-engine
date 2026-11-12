from __future__ import annotations

from ..engines.base import ResearchSignalResult, SingleAssetResearchStrategy, SleeveAllocator


class BuyAndHoldResearch(SingleAssetResearchStrategy):
    def signals(self, bars, parameters, context):
        return ResearchSignalResult([1.0] * len(bars), {"warmup_bars": 0, "strategy": "buy_and_hold"})


class EqualWeightResearch(SleeveAllocator):
    def __init__(self, *, sector_neutral=False):
        self.sector_neutral = sector_neutral

    def allocate(self, returns, holdings, constraints, parameters):
        names = list(holdings) if isinstance(holdings, dict) else list(range(len(returns)))
        if not names:
            return []
        maximum = int(parameters.get("max_names", len(names)))
        names = names[:maximum]
        if not self.sector_neutral:
            return {name: 1.0 / len(names) for name in names}
        sectors = constraints.get("sectors", {})
        groups = {}
        for name in names:
            groups.setdefault(sectors.get(name, "UNKNOWN"), []).append(name)
        sector_weight = 1.0 / len(groups)
        return {name: sector_weight / len(group) for group in groups.values() for name in group}


BH_001 = BuyAndHoldResearch()
EW_001 = EqualWeightResearch()
SEC_EW_001 = EqualWeightResearch(sector_neutral=True)

