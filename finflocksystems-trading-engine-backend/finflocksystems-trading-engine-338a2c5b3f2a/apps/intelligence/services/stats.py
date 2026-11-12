"""Honest statistics (R5): always n, always an interval, deterministic (seeded) so reprocessing is idempotent.
numpy is imported lazily to keep the web process light."""
import math


def mean(values):
    return sum(values) / len(values) if values else None


def bootstrap_ci(values, seed=0, resamples=1000, alpha=0.05):
    """(mean, low, high). With fewer than 2 values the interval is None: one number has no spread."""
    if not values:
        return None, None, None
    m = mean(values)
    if len(values) < 2:
        return m, None, None
    import numpy as np

    rng = np.random.default_rng(seed)
    data = np.asarray(values, dtype=float)
    means = rng.choice(data, size=(resamples, len(data)), replace=True).mean(axis=1)
    return float(m), float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2))


def diff_ci(a, b, seed=0, resamples=1000, alpha=0.05):
    """Mean(a) - mean(b) with a bootstrap interval; None parts when either side has fewer than 2 values."""
    if not a or not b:
        return None, None, None
    delta = mean(a) - mean(b)
    if len(a) < 2 or len(b) < 2:
        return delta, None, None
    import numpy as np

    rng = np.random.default_rng(seed)
    xa, xb = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    diffs = (rng.choice(xa, size=(resamples, len(xa))).mean(axis=1) - rng.choice(xb, size=(resamples, len(xb))).mean(axis=1))
    return float(delta), float(np.quantile(diffs, alpha / 2)), float(np.quantile(diffs, 1 - alpha / 2))


def beta_mean(alpha, beta):
    return alpha / (alpha + beta)


def brier(pairs):
    """pairs of (predicted probability, outcome 0/1)."""
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs) if pairs else None


def finite(value):
    return value is not None and not (isinstance(value, float) and (math.isnan(value) or math.isinf(value)))


# ------------------------------------------------------------------------------------------------
# Phase 11: multiple-testing control, partial pooling and change-point detection.
# All deterministic (seeded), numpy imported lazily, and honest about small samples (None, never a guess).
# ------------------------------------------------------------------------------------------------
def permutation_pvalue(a, b, direction="positive", seed=0, resamples=2000):
    """One-sided two-sample permutation p-value for mean(a) - mean(b) in `direction` ("positive": a larger,
    "negative": a smaller). Add-one smoothing, so it is never exactly 0. None when either side has fewer than 2 values."""
    if len(a) < 2 or len(b) < 2:
        return None
    import numpy as np

    rng = np.random.default_rng(seed)
    pooled = np.asarray(list(a) + list(b), dtype=float)
    sign = 1.0 if direction == "positive" else -1.0
    observed = sign * (np.mean(pooled[:len(a)]) - np.mean(pooled[len(a):]))
    picks = rng.random((resamples, len(pooled))).argsort(axis=1)
    in_a = pooled[picks[:, :len(a)]].mean(axis=1)
    in_b = pooled[picks[:, len(a):]].mean(axis=1)
    hits = int(np.sum(sign * (in_a - in_b) >= observed - 1e-12))
    return (1 + hits) / (1 + resamples)


def signflip_pvalue(values, direction="positive", seed=0, resamples=2000):
    """One-sided sign-flip test that the mean of `values` differs from 0 in `direction` (null: symmetric around 0).
    None with fewer than 3 values."""
    if len(values) < 3:
        return None
    import numpy as np

    rng = np.random.default_rng(seed)
    data = np.asarray(values, dtype=float)
    sign = 1.0 if direction == "positive" else -1.0
    observed = sign * data.mean()
    flips = rng.choice([-1.0, 1.0], size=(resamples, len(data)))
    hits = int(np.sum(sign * (flips * data).mean(axis=1) >= observed - 1e-12))
    return (1 + hits) / (1 + resamples)


def bh_adjust(pvalues):
    """Benjamini-Hochberg adjusted p-values (q-values), in the input order. None stays None and is not counted:
    a test that could not be run is not part of the family."""
    indexed = sorted(((p, i) for i, p in enumerate(pvalues) if p is not None))
    m = len(indexed)
    out = [None] * len(pvalues)
    running = 1.0
    for rank in range(m, 0, -1):
        p, i = indexed[rank - 1]
        running = min(running, p * m / rank)
        out[i] = min(1.0, running)
    return out


def benjamini_hochberg(pvalues, q=0.05):
    """True for every hypothesis rejected at false-discovery rate q (None -> False)."""
    return [adj is not None and adj <= q for adj in bh_adjust(pvalues)]


def sample_variance(values):
    if len(values) < 2:
        return None
    m = sum(values) / len(values)
    return sum((v - m) ** 2 for v in values) / (len(values) - 1)


def partial_pool(cells):
    """Empirical-Bayes (normal-normal, DerSimonian-Laird) shrinkage of slice means toward the family mean.

    cells: [{"key", "n", "mean", "var"}] (var = sample variance of the slice's values, None when n < 2).
    Returns {key: {"shrunk_mean", "shrunk_sd", "shrinkage"}} where shrinkage is the weight kept on the slice's own
    mean (1 = none pooled). With fewer than 3 slices there is nothing to pool across, so every slice is returned unchanged."""
    unchanged = {c["key"]: {"shrunk_mean": c.get("mean"), "shrunk_sd": None, "shrinkage": None} for c in cells}
    usable = [c for c in cells if c.get("n", 0) >= 1 and c.get("mean") is not None]
    if len(usable) < 3:
        return unchanged
    variances = [c["var"] for c in usable if c.get("var") is not None and c["var"] > 0]
    pooled_var = (sum(variances) / len(variances)) if variances else 1.0
    se2 = [((c["var"] if c.get("var") not in (None, 0) else pooled_var) / c["n"]) for c in usable]
    weights = [1.0 / v for v in se2]
    total_w = sum(weights)
    mu = sum(w * c["mean"] for w, c in zip(weights, usable)) / total_w
    q_stat = sum(w * (c["mean"] - mu) ** 2 for w, c in zip(weights, usable))
    denominator = total_w - sum(w * w for w in weights) / total_w
    tau2 = max(0.0, (q_stat - (len(usable) - 1)) / denominator) if denominator > 0 else 0.0
    out = dict(unchanged)
    for c, v in zip(usable, se2):
        b = 1.0 if tau2 == 0 else v / (v + tau2)  # weight on the family mean
        posterior_var = 1.0 / (1.0 / v + 1.0 / tau2) if tau2 > 0 else 1.0 / total_w
        out[c["key"]] = {"shrunk_mean": b * mu + (1 - b) * c["mean"], "shrunk_sd": math.sqrt(posterior_var), "shrinkage": 1.0 - b}
    return out


def cusum_known(zs, k=0.75, h=10.0, clip=3.0):
    """Two-sided CUSUM on already-standardised observations (target 0). Each observation is clipped to +-clip so one wild
    trade cannot raise an alarm alone. Returns the first alarm, if any."""
    s_hi = s_lo = 0.0
    peak_hi = peak_lo = 0.0
    start_hi = start_lo = 0
    out = {"alarm": False, "direction": None, "index": None, "change_index": None, "k": k, "h": h}
    for t, z in enumerate(zs, start=1):
        z = max(-clip, min(clip, z)) if clip else z
        if s_hi == 0.0:
            start_hi = t
        if s_lo == 0.0:
            start_lo = t
        s_hi = max(0.0, s_hi + z - k)
        s_lo = max(0.0, s_lo - z - k)
        peak_hi, peak_lo = max(peak_hi, s_hi), max(peak_lo, s_lo)
        if not out["alarm"] and (s_lo > h or s_hi > h):
            down = s_lo > h
            out.update(alarm=True, direction="DOWN" if down else "UP", index=t, change_index=start_lo if down else start_hi)
    out["statistic"] = max(peak_hi, peak_lo)
    out["peak_down"], out["peak_up"] = peak_lo, peak_hi
    return out


def cusum(values, ref_n=10, k=0.75, h=10.0, min_after=5, clip=3.0):
    """Page-style CUSUM of a series in time order against a baseline taken from its first `ref_n` values.

    Standardised by the baseline mean and standard deviation, each observation clipped to +-clip. The settings were chosen
    by simulation (tests/tools: 600 runs each) because a baseline of only 10 trades makes the textbook k = 0.5, h = 5
    false-alarm 39% of the time on stable normal data and 53% on fat-tailed data over 50 monitored trades. With k = 0.75,
    h = 10, clip = 3 and a 10-trade baseline: false alarms about 5% (normal) and 4% (fat-tailed) over 50 monitored trades;
    a drop of 2 standard deviations is caught in about 10 trades (99%); a drop of 1 standard deviation is caught within 50
    trades only 69% of the time (about 22 trades when caught): small drifts need many trades. Needs `ref_n + min_after`
    values; below that the answer is {"insufficient": True}. `index` and `change_index` are 1-based positions in the series."""
    data = [float(v) for v in values if v is not None]
    if len(data) < ref_n + min_after:
        return {"insufficient": True, "n": len(data), "need": ref_n + min_after}
    base = data[:ref_n]
    mean0 = sum(base) / ref_n
    var0 = sample_variance(base)
    if not var0 or var0 <= 1e-18:
        return {"insufficient": True, "n": len(data), "need": ref_n + min_after, "reason": "the baseline period has no spread"}
    sd0 = math.sqrt(var0)
    result = cusum_known([(v - mean0) / sd0 for v in data[ref_n:]], k=k, h=h, clip=clip)
    for key in ("index", "change_index"):
        if result[key] is not None:
            result[key] += ref_n
    result.update(insufficient=False, n=len(data), reference_n=ref_n, baseline_mean=mean0, baseline_sd=sd0)
    return result
