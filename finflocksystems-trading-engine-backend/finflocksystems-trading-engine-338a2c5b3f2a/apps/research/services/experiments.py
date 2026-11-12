import itertools
import math
import random
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class WalkForwardWindow:
    train: tuple[int, int]
    validation: tuple[int, int]
    test: tuple[int, int]
    purge: tuple[int, int]
    embargo: tuple[int, int]


def walk_forward_windows(
    observation_count,
    *,
    train_size,
    validation_size,
    test_size,
    purge_size=0,
    embargo_size=0,
    expanding=False,
    minimum_windows=1,
):
    if min(train_size, validation_size, test_size) <= 0 or min(purge_size, embargo_size) < 0:
        raise ValueError("Window sizes are invalid")
    windows = []
    start = 0
    while True:
        train_start = 0 if expanding else start
        train_end = start + train_size
        validation_start = train_end + purge_size
        validation_end = validation_start + validation_size
        test_start = validation_end + embargo_size
        test_end = test_start + test_size
        if test_end > observation_count:
            break
        windows.append(WalkForwardWindow(
            train=(train_start, train_end),
            purge=(train_end, validation_start),
            validation=(validation_start, validation_end),
            embargo=(validation_end, test_start),
            test=(test_start, test_end),
        ))
        start += test_size
    if len(windows) < minimum_windows:
        raise ValueError(f"At least {minimum_windows} independent test windows are required")
    return windows


def parameter_candidates(parameter_grid, *, baseline=None, budget=100, seed=0):
    keys = sorted(parameter_grid)
    values = [list(parameter_grid[key]) if isinstance(parameter_grid[key], list) else [parameter_grid[key]] for key in keys]
    all_rows = [dict(zip(keys, combination)) for combination in itertools.product(*values)] if keys else [{}]
    theoretical_size = len(all_rows)
    baseline = dict(baseline or {})
    rows = []
    if baseline:
        rows.append(baseline)
    remaining = [row for row in all_rows if row != baseline]
    if len(rows) + len(remaining) > budget:
        random.Random(seed).shuffle(remaining)
        remaining = remaining[:max(0, budget - len(rows))]
    rows.extend(remaining)
    return {"theoretical_size": theoretical_size, "sampled": rows, "budget": budget, "seed": seed}


def bootstrap_confidence_interval(values, *, statistic=np.mean, confidence=0.95, samples=1000, seed=0):
    values = np.asarray(values, dtype=float)
    if len(values) == 0:
        raise ValueError("Bootstrap requires observations")
    rng = np.random.default_rng(seed)
    estimates = [statistic(rng.choice(values, size=len(values), replace=True)) for _ in range(samples)]
    alpha = (1 - confidence) / 2
    return float(np.quantile(estimates, alpha)), float(np.quantile(estimates, 1 - alpha))


def deflated_sharpe_ratio(observed_sharpe, *, number_of_trials, skew=0.0, kurtosis=3.0, observations=252):
    if number_of_trials < 1 or observations < 3:
        raise ValueError("Invalid deflated Sharpe inputs")
    expected_max = math.sqrt(max(0.0, 2.0 * math.log(number_of_trials))) / math.sqrt(observations)
    standard_error = math.sqrt(max(1e-12, (1 - skew * observed_sharpe + ((kurtosis - 1) / 4) * observed_sharpe ** 2) / (observations - 1)))
    z_score = (observed_sharpe - expected_max) / standard_error
    return 0.5 * (1.0 + math.erf(z_score / math.sqrt(2.0)))


def probability_backtest_overfitting(in_sample_scores, out_of_sample_scores):
    in_sample = np.asarray(in_sample_scores, dtype=float)
    out_sample = np.asarray(out_of_sample_scores, dtype=float)
    if in_sample.shape != out_sample.shape or in_sample.ndim != 2:
        raise ValueError("PBO score matrices must have matching two-dimensional shapes")
    failures = 0
    for row_in, row_out in zip(in_sample, out_sample):
        winner = int(np.argmax(row_in))
        failures += int(row_out[winner] < np.median(row_out))
    return failures / len(in_sample) if len(in_sample) else 0.0


def false_discovery_rate(p_values, alpha=0.05):
    """Benjamini-Hochberg decisions returned in original order."""
    values = np.asarray(p_values, dtype=float)
    if np.any((values < 0) | (values > 1)):
        raise ValueError("p-values must be between zero and one")
    order = np.argsort(values)
    passed = np.zeros(len(values), dtype=bool)
    threshold_rank = -1
    for rank, index in enumerate(order, start=1):
        if values[index] <= alpha * rank / len(values):
            threshold_rank = rank
    if threshold_rank > 0:
        passed[order[:threshold_rank]] = True
    return passed.tolist()


def neighboring_parameter_stability(center_metric, neighbor_metrics, tolerance=0.25):
    if not neighbor_metrics:
        return False
    center = float(center_metric)
    floor = center - abs(center) * tolerance
    return float(np.median(np.asarray(neighbor_metrics, dtype=float))) >= floor


def final_holdout_split(observation_count, holdout_size):
    if holdout_size <= 0 or holdout_size >= observation_count:
        raise ValueError("Final holdout must be positive and smaller than the dataset")
    boundary = observation_count - holdout_size
    return {"research": (0, boundary), "untouched_holdout": (boundary, observation_count)}


def execution_delay_stress(exposures, delay_bars):
    values = np.asarray(exposures, dtype=float)
    if delay_bars < 0:
        raise ValueError("Execution delay cannot be negative")
    if delay_bars == 0:
        return values.tolist()
    return np.r_[np.zeros(delay_bars), values[:-delay_bars]].tolist()


def missing_data_stress(values, *, missing_fraction=0.05, seed=0):
    values = np.asarray(values, dtype=float).copy()
    if not 0 <= missing_fraction < 1:
        raise ValueError("Missing fraction must be in [0, 1)")
    count = int(len(values) * missing_fraction)
    if count:
        indices = np.random.default_rng(seed).choice(len(values), size=count, replace=False)
        values[indices] = np.nan
    return values


def leave_one_group_out(rows, group_key, evaluator):
    groups = sorted({row[group_key] for row in rows})
    return {
        group: evaluator([row for row in rows if row[group_key] != group])
        for group in groups
    }


def regime_slices(rows, regime_key, evaluator):
    groups = sorted({row[regime_key] for row in rows})
    return {group: evaluator([row for row in rows if row[regime_key] == group]) for group in groups}


def quantile_slices(values, buckets=10):
    values = np.asarray(values, dtype=float)
    if buckets < 2 or len(values) < buckets:
        raise ValueError("Not enough observations for requested quantile slices")
    edges = np.quantile(values, np.linspace(0, 1, buckets + 1))
    return np.clip(np.digitize(values, edges[1:-1], right=True), 0, buckets - 1).tolist()


def subperiod_consistency(period_metrics):
    values = np.asarray(period_metrics, dtype=float)
    if len(values) == 0:
        return {"positive_fraction": 0.0, "coefficient_of_variation": None}
    mean = float(np.mean(values))
    return {
        "positive_fraction": float(np.mean(values > 0)),
        "coefficient_of_variation": float(np.std(values, ddof=1) / abs(mean)) if len(values) > 1 and mean else None,
    }
