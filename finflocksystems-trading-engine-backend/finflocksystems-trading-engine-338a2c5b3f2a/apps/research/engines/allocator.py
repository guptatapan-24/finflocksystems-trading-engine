from __future__ import annotations

import numpy as np
from scipy.cluster.hierarchy import linkage, leaves_list
from scipy.optimize import minimize

from .base import SleeveAllocator


def _normalise(weights, maximum=1.0):
    values = np.maximum(np.asarray(weights, dtype=float), 0)
    values = np.minimum(values, float(maximum))
    total = float(np.sum(values))
    return values / total if total > 0 else np.repeat(1.0 / len(values), len(values))


class PortfolioAllocator(SleeveAllocator):
    """Deterministic long-only implementations for every catalogue allocation role."""

    def __init__(self, method):
        self.method = method

    def allocate(self, returns, holdings, constraints, parameters):
        matrix = np.asarray(returns, dtype=float)
        if matrix.ndim != 2 or matrix.shape[1] == 0:
            raise ValueError("Allocator returns must be observations by assets")
        count = matrix.shape[1]
        maximum = float(parameters.get("max_weight", constraints.get("per_stock_cap", 1.0)))
        covariance = np.cov(matrix, rowvar=False)
        covariance = np.atleast_2d(covariance) + np.eye(count) * 1e-8
        volatility = np.sqrt(np.maximum(np.diag(covariance), 1e-12))
        if self.method == "EQUAL_WEIGHT":
            weights = np.ones(count)
        elif self.method == "SECTOR_NEUTRAL_EQUAL_WEIGHT":
            sectors = list(constraints.get("sector_vector", ["UNKNOWN"] * count))
            groups = {sector: [i for i, value in enumerate(sectors) if value == sector] for sector in set(sectors)}
            weights = np.zeros(count)
            for indices in groups.values():
                weights[indices] = 1.0 / len(groups) / len(indices)
        elif self.method == "INVERSE_VOLATILITY":
            weights = 1.0 / np.maximum(volatility, float(parameters.get("vol_floor", 0.01)))
        elif self.method == "RISK_PARITY":
            def objective(candidate):
                portfolio_vol = np.sqrt(candidate @ covariance @ candidate)
                contribution = candidate * (covariance @ candidate) / max(portfolio_vol, 1e-12)
                return float(np.sum((contribution - np.mean(contribution)) ** 2))
            weights = minimize(objective, np.repeat(1 / count, count), bounds=[(0, maximum)] * count,
                               constraints={"type": "eq", "fun": lambda value: np.sum(value) - 1}).x
        elif self.method == "MINIMUM_VARIANCE":
            weights = minimize(lambda value: float(value @ covariance @ value), np.repeat(1 / count, count),
                               bounds=[(0, maximum)] * count,
                               constraints={"type": "eq", "fun": lambda value: np.sum(value) - 1}).x
        elif self.method == "MAXIMUM_DIVERSIFICATION":
            weights = minimize(lambda value: -float((value @ volatility) / np.sqrt(value @ covariance @ value)),
                               np.repeat(1 / count, count), bounds=[(0, maximum)] * count,
                               constraints={"type": "eq", "fun": lambda value: np.sum(value) - 1}).x
        elif self.method == "HIERARCHICAL_RISK_PARITY":
            correlation = np.corrcoef(matrix, rowvar=False)
            distance = np.sqrt(np.maximum((1 - correlation) / 2, 0))
            condensed = distance[np.triu_indices(count, 1)]
            order = leaves_list(linkage(condensed, method=str(parameters.get("linkage", "single")))) if count > 1 else [0]
            weights = np.zeros(count)
            weights[order] = _normalise(1.0 / volatility[order])
        elif self.method == "MINIMUM_CVAR":
            alpha = float(parameters.get("alpha", 0.95))
            def cvar(candidate):
                losses = -(matrix @ candidate)
                threshold = np.quantile(losses, alpha)
                tail = losses[losses >= threshold]
                return float(np.mean(tail)) if len(tail) else 0.0
            weights = minimize(cvar, np.repeat(1 / count, count), bounds=[(0, maximum)] * count,
                               constraints={"type": "eq", "fun": lambda value: np.sum(value) - 1}).x
        elif self.method == "CORE_BLEND":
            inverse = _normalise(1.0 / volatility, maximum)
            weights = 0.5 * np.repeat(1 / count, count) + 0.5 * inverse
        else:
            raise ValueError(f"Unknown allocation method {self.method}")
        return _normalise(weights, maximum).tolist()


class ConstrainedAllocator(PortfolioAllocator):
    def __init__(self):
        super().__init__("MINIMUM_VARIANCE")
