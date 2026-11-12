from apps.core.lazy_imports import lazy_module

# Deferred: reached from config.urls via portfolio_construction.views ->
# research.services.recommendations. See apps/core/lazy_imports.py.
np = lazy_module("numpy")
_scipy_optimize = lazy_module("scipy.optimize")


class RecommendationOptimizationError(ValueError):
    pass


def optimize_sleeves(candidates, *, constraints, current_weights=None, covariance=None):
    """GICS- and strategy-aware long-only sleeve optimizer, separate from manual Markowitz."""
    candidates = list(candidates)
    if not candidates:
        return {"weights": [], "cash_weight": 1.0, "objective": 0.0, "iterations": 0}
    count = len(candidates)
    expected = np.asarray([float(item.get("expected_return", 0)) for item in candidates], dtype=float)
    volatility = np.asarray([max(float(item.get("expected_volatility", 0.20)), 1e-6) for item in candidates], dtype=float)
    if covariance is None:
        correlation = np.full((count, count), float(constraints.get("default_correlation", 0.20)))
        np.fill_diagonal(correlation, 1.0)
        covariance = np.outer(volatility, volatility) * correlation
    covariance = np.asarray(covariance, dtype=float)
    if covariance.shape != (count, count):
        raise RecommendationOptimizationError("Covariance shape does not match sleeve candidates")
    minimum_cash = float(constraints.get("minimum_cash", 0))
    investable = 1.0 - minimum_cash
    if not 0 <= investable <= 1:
        raise RecommendationOptimizationError("Minimum cash must be between zero and one")
    per_stock_cap = float(constraints.get("per_stock_cap", 1))
    family_cap = float(constraints.get("strategy_family_cap", 1))
    group_caps = {
        "sector": float(constraints.get("sector_cap", 1)),
        "industry": float(constraints.get("industry_cap", 1)),
        "sub_industry": float(constraints.get("sub_industry_cap", 1)),
    }
    bounds = []
    for item in candidates:
        capacity = float(item.get("capacity_weight", 1))
        bounds.append((0.0, min(per_stock_cap, capacity, investable)))
    scipy_constraints = [{"type": "eq", "fun": lambda weights: np.sum(weights) - investable}]

    def add_group_constraints(key, cap):
        groups = {}
        for index, item in enumerate(candidates):
            value = item.get(key)
            if value:
                groups.setdefault(value, []).append(index)
        for indices in groups.values():
            scipy_constraints.append({
                "type": "ineq",
                "fun": lambda weights, selected=tuple(indices), maximum=cap: maximum - float(np.sum(weights[list(selected)])),
            })

    add_group_constraints("instrument_id", per_stock_cap)
    add_group_constraints("strategy_family", family_cap)
    for key, cap in group_caps.items():
        add_group_constraints(key, cap)
    minimum_sectors = int(constraints.get("minimum_sectors", 0))
    if minimum_sectors:
        sectors = []
        for item in candidates:
            if item.get("sector") and item["sector"] not in sectors:
                sectors.append(item["sector"])
        if len(sectors) < minimum_sectors:
            raise RecommendationOptimizationError(
                f"Only {len(sectors)} sectors are available; policy requires {minimum_sectors}"
            )
        minimum_sector_weight = float(constraints.get("minimum_sector_weight", 0.00000001))
        for sector in sectors[:minimum_sectors]:
            indices = tuple(index for index, item in enumerate(candidates) if item.get("sector") == sector)
            scipy_constraints.append({
                "type": "ineq",
                "fun": lambda weights, selected=indices, minimum=minimum_sector_weight: float(np.sum(weights[list(selected)])) - minimum,
            })
    maximum_turnover = constraints.get("maximum_turnover")
    current = np.asarray([
        float((current_weights or {}).get(str(item.get("identity", index)), (current_weights or {}).get(item.get("identity", index), 0)))
        for index, item in enumerate(candidates)
    ])
    if maximum_turnover is not None:
        scipy_constraints.append({
            "type": "ineq",
            "fun": lambda weights: float(maximum_turnover) - float(np.sum(np.abs(weights - current))),
        })
    risk_aversion = float(constraints.get("risk_aversion", 3.0))

    def objective(weights):
        expected_net = expected - np.asarray([float(item.get("cost_penalty", 0)) for item in candidates])
        instability = np.asarray([float(item.get("instability_penalty", 0)) for item in candidates])
        recommended = np.asarray([float(item.get("recommended_weight", investable / count)) for item in candidates])
        turnover = np.sum(np.abs(weights - current))
        concentration = np.sum(weights ** 2)
        recommendation_drift = np.sum((weights - recommended) ** 2)
        return (-float(expected_net @ weights) + risk_aversion * float(weights @ covariance @ weights)
                + 0.01 * turnover + 0.02 * concentration + float(instability @ weights)
                + float(constraints.get("recommendation_penalty", 2.0)) * recommendation_drift)

    start = np.full(count, investable / count)
    for _ in range(25):
        start = np.minimum(start, np.asarray([bound[1] for bound in bounds]))
        total = np.sum(start)
        if total > 0:
            start *= investable / total
    result = _scipy_optimize.minimize(
        objective, start, method="SLSQP", bounds=bounds, constraints=scipy_constraints,
        options={"maxiter": 1000, "ftol": 1e-12},
    )
    if not result.success:
        raise RecommendationOptimizationError(f"Sleeve optimization is infeasible: {result.message}")
    weights = np.maximum(result.x, 0)
    weights[np.abs(weights) < 1e-10] = 0
    cash = 1.0 - float(np.sum(weights))
    if cash + 1e-8 < minimum_cash:
        raise RecommendationOptimizationError("Optimizer violated the live cash floor")
    return {
        "weights": weights.tolist(),
        "cash_weight": cash,
        "objective": float(result.fun),
        "iterations": int(result.nit),
        "expected_return": float(expected @ weights),
        "expected_volatility": float(np.sqrt(max(0, weights @ covariance @ weights))),
    }
