from __future__ import annotations

import itertools

import numpy as np

from .base import PairBasketResearchStrategy


class BoundedPeerModel(PairBasketResearchStrategy):
    """Scores bounded same-industry peers; it never creates a runtime target."""

    def __init__(self, method):
        self.method = method

    def targets(self, panel, parameters, context):
        maximum_neighbors = min(20, max(1, int(parameters.get("maximum_neighbors", 5))))
        minimum_correlation = float(parameters.get("minimum_correlation", 0.5))
        groups = {}
        for row in panel:
            group = row.get("sub_industry") or row.get("industry") or row.get("sector")
            if group and row.get("returns") is not None:
                groups.setdefault(group, []).append(row)
        candidates = []
        for group, rows in groups.items():
            liquid = sorted(rows, key=lambda item: float(item.get("liquidity", 0)), reverse=True)
            # The neighbour bound is applied before pair construction, preventing an all-universe O(n^2) screen.
            for left_index, left in enumerate(liquid):
                peers = liquid[max(0, left_index - maximum_neighbors):left_index] + liquid[left_index + 1:left_index + 1 + maximum_neighbors]
                for right in peers:
                    if str(left.get("symbol")) >= str(right.get("symbol")):
                        continue
                    left_returns, right_returns = np.asarray(left["returns"], dtype=float), np.asarray(right["returns"], dtype=float)
                    size = min(len(left_returns), len(right_returns))
                    if size < 60:
                        continue
                    correlation = float(np.corrcoef(left_returns[-size:], right_returns[-size:])[0, 1])
                    if not np.isfinite(correlation) or correlation < minimum_correlation:
                        continue
                    distance = float(np.mean((np.cumsum(left_returns[-size:]) - np.cumsum(right_returns[-size:])) ** 2))
                    candidates.append({
                        "left": left.get("instrument_id"), "right": right.get("instrument_id"),
                        "peer_group": group, "correlation": correlation, "distance": distance,
                        "research_score": correlation / max(distance, 1e-9), "method": self.method,
                        "runtime_eligible": False,
                    })
        identity = lambda row: (str(row["left"]), str(row["right"]))
        deduplicated = {identity(row): row for row in candidates}
        return sorted(deduplicated.values(), key=lambda row: (-row["research_score"], identity(row)))


class ResearchOnlyPairBasket(BoundedPeerModel):
    def __init__(self):
        super().__init__("RESEARCH_ONLY")
