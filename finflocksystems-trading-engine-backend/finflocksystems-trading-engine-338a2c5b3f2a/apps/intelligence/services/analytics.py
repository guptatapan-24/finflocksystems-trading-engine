"""Performance statistics, probability calibration and suggestion outcomes (Phase 5). Per currency, always with n and an
interval (R5). Deterministic and idempotent: each refresh recomputes from the episodes."""
import hashlib
from collections import defaultdict
from decimal import Decimal

from django.utils import timezone

from apps.intelligence.models import (
    CalibrationStat, LearningEvent, PerformanceStat, Suggestion, SuggestionOutcome, TradeEpisode,
)

from . import stats
from .learning import _event, thresholds

DIMENSION_SETS = [
    ("strategy_key",), ("strategy_key", "strategy_version"), ("instrument",), ("strategy_key", "instrument"),
    ("regime",), ("structure",), ("mode",), ("strategy_key", "mode"),
    ("risk_profile",), ("strategy_key", "risk_profile"), ("context",), ("strategy_key", "context"),  # Phase 11
]
POP_BINS = [(0.0, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 1.0001)]


def _regime_bucket(metrics):
    pct = metrics.get("regime.vol_pctile")
    if pct is None:
        return "UNKNOWN"
    return "LOW_VOL" if pct < 33 else "MID_VOL" if pct < 67 else "HIGH_VOL"


def _context_bucket(metrics):
    """Was there an outside event inside the holding period? NO_CONTEXT when no context had been collected."""
    if not metrics.get("ctx.available"):
        return "NO_CONTEXT"
    return "EVENT_IN_HOLD" if metrics.get("ctx.event_in_hold") else "NO_EVENT"


def _rows():
    for episode in (TradeEpisode.objects.filter(status__in=["CLOSED", "EXPIRED"]).select_related("metrics_row")):
        metrics = dict(episode.metrics_row.metrics) if getattr(episode, "metrics_row", None) else {}
        yield episode, metrics


def refresh_performance(now=None):
    """Rebuild PerformanceStat rows. Emits STAT_SHIFT when a well-sampled mean moves a lot (never on tiny n)."""
    now = now or timezone.now()
    t = thresholds()
    groups = defaultdict(list)
    for episode, metrics in _rows():
        values = {
            "strategy_key": episode.strategy_key, "strategy_version": episode.strategy_version,
            "instrument": episode.instrument_symbol, "regime": _regime_bucket(metrics),
            "structure": metrics.get("opt.selected_strategy") or ("STOCK" if episode.asset_family == "STOCK" else "OPTION"),
            "mode": episode.execution_mode, "risk_profile": metrics.get("selection.risk_profile") or "",
            "context": _context_bucket(metrics),
        }
        for dims in DIMENSION_SETS:
            if "risk_profile" in dims and not values["risk_profile"]:
                continue  # a strategy created without a risk profile has no profile slice
            key = tuple((d, values[d]) for d in dims) + (("currency", episode.currency),)
            groups[key].append((metrics.get("outcome.net_return_bps"), episode.net_pnl))
    written = 0
    families = defaultdict(list)  # slices of the same kind (same dimensions, same currency) are pooled together
    lookups = {}
    for key, items in groups.items():
        dims = dict(key)
        returns = [float(r) for r, _ in items if r is not None]
        seed = int(hashlib.sha256(repr(key).encode()).hexdigest()[:8], 16)
        mean, low, high = stats.bootstrap_ci(returns, seed=seed, resamples=t["bootstrap"])
        lookup = {"strategy_key": dims.get("strategy_key", ""), "strategy_version": dims.get("strategy_version", 0),
                  "instrument": dims.get("instrument", ""), "regime": dims.get("regime", ""),
                  "structure": dims.get("structure", ""), "mode": dims.get("mode", ""),
                  "risk_profile": dims.get("risk_profile", ""), "context": dims.get("context", ""), "currency": dims["currency"]}
        previous = PerformanceStat.objects.filter(**lookup).first()
        wins = sum(1 for _, net in items if net > 0)
        PerformanceStat.objects.update_or_create(**lookup, defaults={
            "n": len(items), "wins": wins, "mean_return_bps": mean, "ci_low": low, "ci_high": high,
            "sum_net_pnl": sum((net for _, net in items), Decimal(0)), "as_of": now})
        written += 1
        if returns:
            family = (tuple(sorted(d for d in dims if d != "currency")), dims["currency"])
            families[family].append({"key": key, "n": len(returns), "mean": mean, "var": stats.sample_variance(returns)})
            lookups[key] = lookup
        if previous is not None and previous.mean_return_bps is not None and mean is not None \
                and previous.n >= 8 and len(items) >= 8 and len(items) != previous.n:
            moved = abs(mean - previous.mean_return_bps)
            flipped = (mean > 0) != (previous.mean_return_bps > 0)
            if flipped or moved > max(50.0, 0.5 * abs(previous.mean_return_bps)):
                _event(LearningEvent.Type.STAT_SHIFT, key=f"stat:{hashlib.sha256(repr(key).encode()).hexdigest()[:16]}:{len(items)}",
                       payload={"dims": {k: v for k, v in dims.items()}, "n": len(items), "from_bps": round(previous.mean_return_bps, 2),
                                "to_bps": round(mean, 2), "note": "means over small samples move a lot; read with n and the interval"})
    for cells in families.values():  # empirical-Bayes shrinkage: a slice with few trades is pulled towards its family
        for key, pooled in stats.partial_pool(cells).items():
            PerformanceStat.objects.filter(**lookups[key]).update(
                shrunk_mean_bps=pooled["shrunk_mean"], shrunk_sd_bps=pooled["shrunk_sd"], shrinkage=pooled["shrinkage"])
    return written


def refresh_calibration(now=None):
    """Calibration of the model's probability of profit against realised wins (Brier score and reliability bins).
    CALIBRATION_SHIFT fires only with n >= 20 overall and n >= 10 in the bin (never alarm on tiny samples)."""
    now = now or timezone.now()
    per_strategy = defaultdict(list)
    for episode, metrics in _rows():
        pop = metrics.get("opt.pop")
        if pop is None:
            continue
        win = 1 if (metrics.get("outcome.net_return_bps") or float(episode.net_pnl)) > 0 else 0
        per_strategy[episode.strategy_key].append((float(pop), win))
        per_strategy[""].append((float(pop), win))
    written = 0
    for strategy_key, pairs in per_strategy.items():
        CalibrationStat.objects.update_or_create(
            metric="POP", strategy_key=strategy_key, bin="ALL",
            defaults={"n": len(pairs), "predicted_mean": stats.mean([p for p, _ in pairs]),
                      "observed_rate": stats.mean([o for _, o in pairs]), "brier": stats.brier(pairs), "as_of": now})
        written += 1
        for low, high in POP_BINS:
            subset = [(p, o) for p, o in pairs if low <= p < high]
            label = f"{low:.1f}-{min(high, 1.0):.1f}"
            if not subset:
                CalibrationStat.objects.filter(metric="POP", strategy_key=strategy_key, bin=label).delete()
                continue
            predicted, observed = stats.mean([p for p, _ in subset]), stats.mean([o for _, o in subset])
            CalibrationStat.objects.update_or_create(
                metric="POP", strategy_key=strategy_key, bin=label,
                defaults={"n": len(subset), "predicted_mean": predicted, "observed_rate": observed, "brier": None, "as_of": now})
            written += 1
            if len(pairs) >= 20 and len(subset) >= 10 and abs(predicted - observed) > 0.15:
                _event(LearningEvent.Type.CALIBRATION_SHIFT, key=f"calib:{strategy_key}:{label}:{len(subset) // 10}",
                       payload={"metric": "POP", "strategy_key": strategy_key or "ALL", "bin": label, "n": len(subset),
                                "predicted": round(predicted, 3), "observed": round(observed, 3)})
    return written


def resolve_suggestion_outcomes(now=None):
    """For APPLIED suggestions with enough trades on the new version: HELPED / HURT / INCONCLUSIVE with n and CI."""
    from apps.strategies.models import StrategyVersion

    now = now or timezone.now()
    t = thresholds()
    min_after = int(t.get("outcome_min_trades", 8))
    resolved = 0
    for suggestion in Suggestion.objects.filter(status=Suggestion.Status.APPLIED, outcome__isnull=True):
        version = StrategyVersion.objects.filter(pk=suggestion.applied_version_id).first()
        if version is None:
            continue
        rows = [(e.strategy_version, float(m.get("outcome.net_return_bps")))
                for e, m in _rows() if e.instance_id == suggestion.instance_id and m.get("outcome.net_return_bps") is not None]
        after = [v for ver, v in rows if ver >= version.version]
        before = [v for ver, v in rows if ver < version.version]
        if len(after) < min_after:
            continue
        delta, low, high = stats.diff_ci(after, before, seed=suggestion.pk, resamples=t["bootstrap"])
        if len(before) < 3 or low is None:
            result = SuggestionOutcome.Result.INCONCLUSIVE
        elif low > 0:
            result = SuggestionOutcome.Result.HELPED
        elif high < 0:
            result = SuggestionOutcome.Result.HURT
        else:
            result = SuggestionOutcome.Result.INCONCLUSIVE
        SuggestionOutcome.objects.create(suggestion=suggestion, result=result, n_before=len(before), n_after=len(after),
                                         delta_bps=delta, ci_low=low, ci_high=high, evaluated_at=now)
        _event(LearningEvent.Type.SUGGESTION_RESOLVED, key=f"suggestion-outcome:{suggestion.pk}",
               payload={"suggestion_id": suggestion.pk, "parameter": suggestion.parameter_key, "result": result,
                        "n_before": len(before), "n_after": len(after), "delta_bps": delta, "ci": [low, high]})
        resolved += 1
    return resolved
