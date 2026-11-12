from decimal import Decimal


SCORE_WEIGHTS = {
    "out_of_sample_sharpe": Decimal("0.20"),
    "calmar": Decimal("0.15"),
    "drawdown_fit": Decimal("0.15"),
    "regime_consistency": Decimal("0.15"),
    "parameter_stability": Decimal("0.10"),
    "cost_resilience": Decimal("0.10"),
    "turnover_efficiency": Decimal("0.05"),
    "capacity": Decimal("0.05"),
    "diversification_contribution": Decimal("0.05"),
}


def hard_rejection_reasons(metrics):
    reasons = []
    checks = (
        (not metrics.get("data_quality_pass", False), "DATA_QUALITY_FAILURE"),
        (not metrics.get("timestamps_unambiguous", False), "LEAKAGE_OR_TIMESTAMP_AMBIGUITY"),
        (float(metrics.get("high_cost_net_return", -1)) < 0, "NEGATIVE_HIGH_COST_RESULT"),
        (float(metrics.get("maximum_drawdown", 1)) > float(metrics.get("maximum_allowed_drawdown", 0)), "DRAWDOWN_ABOVE_PROFILE"),
        (not metrics.get("activity_threshold_pass", int(metrics.get("trade_count",0)) >= int(metrics.get("minimum_trades",1))), "INSUFFICIENT_STRATEGY_ACTIVITY"),
        (not metrics.get("parameter_neighborhood_stable", False), "UNSTABLE_PARAMETER_NEIGHBORHOOD"),
        (not metrics.get("capacity_pass", False), "LIQUIDITY_OR_CAPACITY_FAILURE"),
        (float(metrics.get("largest_subperiod_contribution", 1)) > float(metrics.get("maximum_subperiod_contribution", 0.60)), "SUBPERIOD_DEPENDENCE"),
        (not metrics.get("multiple_testing_pass", False), "MULTIPLE_TESTING_FAILURE"),
        (not metrics.get("holdout_untouched", False), "FINAL_HOLDOUT_NOT_PROTECTED"),
    )
    for failed, reason in checks:
        if failed:
            reasons.append(reason)
    return reasons


def candidate_score(normalized_components, metrics):
    missing = sorted(set(SCORE_WEIGHTS) - set(normalized_components))
    if missing:
        raise ValueError(f"Missing normalized score components: {', '.join(missing)}")
    score = Decimal(0)
    for key, weight in SCORE_WEIGHTS.items():
        value = Decimal(str(normalized_components[key]))
        if not Decimal(0) <= value <= Decimal(100):
            raise ValueError(f"Normalized score {key} must be between 0 and 100")
        score += value * weight
    reasons = hard_rejection_reasons(metrics)
    eligible = not reasons and score >= Decimal(65)
    return {"score": score.quantize(Decimal("0.001")), "eligible": eligible, "hard_rejection_reasons": reasons}
