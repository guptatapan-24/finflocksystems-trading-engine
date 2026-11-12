"""Counterfactuals (Phase 6): deterministic "what if" analyses. They produce HYPOTHESES only and say so, with the number
of comparisons made (the more filters tried, the more likely one looks good by chance).

* options_filter_sweep: re-filters the option trades that were ACTUALLY taken by tighter entry gates (RPS, IV score,
  breach ceiling, TQS cutoff, WATCH exclusion). It can only tighten: trades the gates never let through are unobserved.
* stock_replay: runs the real strategy plugin's evaluate() over persisted bars with the engine's own indicator
  functions and a simple cost model, to compare parameter sets on the same history. Where this is not faithful to live
  trading it says so in `fidelity`.
"""
import logging
from decimal import Decimal
from types import SimpleNamespace

from django.conf import settings

from apps.intelligence.models import TradeEpisode

from . import stats

logger = logging.getLogger(__name__)
MIN_KEPT = 3
D = Decimal

FILTERS = [
    ("rps_threshold", "opt.entry_score", ">="),
    ("iv_score_threshold", "opt.iv_score", ">="),
    ("tqs_cutoff", "opt.tqs", ">="),
]


def _option_rows(strategy_key, currency=None):
    query = TradeEpisode.objects.filter(strategy_key=strategy_key, asset_family="OPTION", status__in=["CLOSED", "EXPIRED"])
    if currency:
        query = query.filter(currency=currency)
    rows = []
    for episode in query.select_related("metrics_row"):
        metrics = getattr(episode, "metrics_row", None)
        metrics = metrics.metrics if metrics else {}
        bps = metrics.get("outcome.net_return_bps")
        if bps is None:
            continue
        breach = [metrics.get(k) for k in ("opt.breach_up", "opt.breach_down") if metrics.get(k) is not None]
        rows.append({"episode": episode.pk, "bps": float(bps), "currency": episode.currency, "metrics": metrics,
                     "breach": max(breach) if breach else None})
    return rows


def _grid(values, steps=5):
    values = sorted({round(v, 4) for v in values if v is not None})
    if len(values) <= 1:
        return []
    picks = {values[round(i * (len(values) - 1) / steps)] for i in range(steps)}
    return sorted(picks)[1:]  # the smallest value would keep everything: not a filter


FDR_Q = 0.10


def _summarise(label, parameter, threshold, kept, removed, seed, p_value, q_value, alpha_adjusted):
    kept_values, removed_values = [r["bps"] for r in kept], [r["bps"] for r in removed]
    mean_kept, low, high = stats.bootstrap_ci(kept_values, seed=seed, resamples=500)
    delta, d_low, d_high = stats.diff_ci(kept_values, removed_values, seed=seed, resamples=500)
    # interval at the false-coverage-rate-adjusted level: wider the more filters were compared (Benjamini-Yekutieli)
    corrected = None
    if kept_values and removed_values and len(kept_values) > 1 and len(removed_values) > 1:
        import numpy as np

        rng = np.random.default_rng(seed)
        a, b = np.asarray(kept_values), np.asarray(removed_values)
        diffs = rng.choice(a, size=(500, len(a))).mean(axis=1) - rng.choice(b, size=(500, len(b))).mean(axis=1)
        corrected = [float(np.quantile(diffs, alpha_adjusted / 2)), float(np.quantile(diffs, 1 - alpha_adjusted / 2))]
    return {
        "filter": label, "parameter": parameter, "threshold": threshold, "n_kept": len(kept), "n_removed": len(removed),
        "mean_kept_bps": mean_kept, "mean_removed_bps": stats.mean(removed_values),
        "delta_bps": delta, "delta_ci": [d_low, d_high], "delta_ci_corrected": corrected,
        "p_value": p_value, "q_value": q_value,
        "holds_after_correction": bool(q_value is not None and q_value <= FDR_Q and delta is not None and delta > 0 and len(kept) >= MIN_KEPT),
        "too_few": len(kept) < MIN_KEPT,
    }


def options_filter_sweep(strategy_key, currency=None):
    rows = _option_rows(strategy_key, currency)
    out = {"strategy_key": strategy_key, "currency": currency, "n_trades": len(rows), "results": [], "comparisons": 0,
           "warnings": ["Hypotheses only. These filters are tested on trades that already passed the live gates, so they can "
                        "only be tightened, never loosened.",
                        "Each extra filter and threshold tried is another chance to find a pattern by luck; see `comparisons` and "
                        "`holds_after_correction` (Benjamini-Hochberg false-discovery control across every filter tried)."]}
    if len(rows) < int(getattr(settings, "INTELLIGENCE_MIN_EPISODES_FOR_SUGGESTION", 8)):
        out["warnings"].append(f"Insufficient data: n = {len(rows)} option trades.")
        return out
    candidates = []
    for label, key, _op in FILTERS:
        for threshold in _grid([r["metrics"].get(key) for r in rows]):
            candidates.append((label, key, threshold, lambda r, k=key, t=threshold: r["metrics"].get(k) is not None and r["metrics"][k] >= t))
    breach_values = [r["breach"] for r in rows]
    for ceiling in _grid(breach_values):
        candidates.append(("breach_ceiling", "opt.breach_max", ceiling, lambda r, c=ceiling: r["breach"] is not None and r["breach"] <= c))
    if any(r["metrics"].get("opt.decision_band") for r in rows):
        candidates.append(("exclude_watch_band", "opt.decision_band", "WATCH", lambda r: r["metrics"].get("opt.decision_band") != "WATCH"))
    out["comparisons"] = len(candidates)
    tested = []
    for index, (label, key, threshold, keep) in enumerate(candidates):
        kept = [r for r in rows if keep(r)]
        removed = [r for r in rows if not keep(r)]
        if not kept or not removed:
            continue
        p_value = stats.permutation_pvalue([r["bps"] for r in kept], [r["bps"] for r in removed], "positive", seed=index + 1, resamples=1000)
        tested.append((label, key, threshold, kept, removed, index + 1, p_value))
    # one family: every filter and threshold tried is a hypothesis, so false discoveries are controlled across all of them
    qs = stats.bh_adjust([t[6] for t in tested])
    rejected = sum(1 for q in qs if q is not None and q <= FDR_Q)
    alpha_adjusted = max(1e-4, FDR_Q * max(1, rejected) / max(1, len(tested)))
    for (label, key, threshold, kept, removed, seed, p_value), q in zip(tested, qs):
        out["results"].append(_summarise(label, key, threshold, kept, removed, seed, p_value, q, alpha_adjusted))
    out["results"].sort(key=lambda r: -(r["delta_bps"] if r["delta_bps"] is not None else -1e18))
    return out


# ---------------------------------------------------------------------------------------------------
# stock replay
# ---------------------------------------------------------------------------------------------------
class ReplayError(ValueError):
    pass


def _bars(instrument_id, timeframe, limit):
    from apps.market_streams.models import MarketBar

    rows = (MarketBar.objects.filter(instrument_id=instrument_id, interval=timeframe, is_final=True,
                                     processing_mode__in=["LIVE", "WARMUP"]).order_by("bar_id", "-version"))
    latest = {}
    for bar in rows:
        latest.setdefault(bar.bar_id, bar)
    ordered = sorted(latest.values(), key=lambda b: b.window_end)
    return ordered[-limit:]


def replay_stock(definition, instrument, timeframe, parameters, *, target_configuration=None, cost_bps=None, limit=2000):
    """Replay a stock strategy's real plugin over persisted bars. Returns trades and summary statistics."""
    from apps.strategies.input_identity import indicator_output_name
    from apps.strategies.plugins import get_plugin
    from apps.strategies.plugins.base import EvaluationContext
    from streaming.flink.jobs.processing import compute_indicator

    plugin = get_plugin(definition)
    params = plugin.validate_configuration(parameters, target_configuration or {})
    if plugin.requires_option_context:
        raise ReplayError("Option strategies are not replayed; use the option filter sweep")
    cost = D(str(cost_bps if cost_bps is not None else getattr(settings, "INTELLIGENCE_REPLAY_COST_BPS", 5)))
    bars = _bars(instrument.pk, timeframe, limit)
    needed = plugin.warmup_bars(params)
    inputs = [i for i in plugin.required_stream_inputs(params) if i.input_type == "INDICATOR"]
    instance = SimpleNamespace(parameters=params, target_configuration=target_configuration or {"target_weight": "0.05"})
    state, state_data, previous = "FLAT", {}, {}
    trades, entry = [], None
    history = []
    for bar in bars:
        history.append({"open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close, "volume": bar.volume,
                        "window_end": bar.window_end.isoformat(), "is_final": True})
        if len(history) < max(needed, 2):
            continue
        values = {}
        for item in inputs:
            role = item.role or item.parameters.get("role", "")   # the engine reads the role from the input parameters
            value = compute_indicator(history, item.name, {**item.parameters, "role": role})
            values[indicator_output_name(item.name, role)] = value
        context = EvaluationContext(
            strategy_instance=instance, strategy_version=SimpleNamespace(version=1), instrument=SimpleNamespace(pk=instrument.pk, symbol=instrument.symbol),
            bar={"close": str(bar.close), "is_final": True, "window_end": bar.window_end.isoformat()}, indicators=values,
            previous_indicators=previous, previous_state=state, state_data=state_data)
        decision = plugin.evaluate(context)
        previous = values
        price = D(str(bar.close))
        if decision.signal_type.startswith("ENTER") and entry is None and decision.direction == "LONG":
            entry = {"price": price, "at": bar.window_end}
        elif decision.signal_type.startswith("EXIT") and entry is not None:
            gross = (price - entry["price"]) / entry["price"] * D(10000)
            trades.append({"entry_at": entry["at"].isoformat(), "exit_at": bar.window_end.isoformat(),
                           "entry": float(entry["price"]), "exit": float(price), "gross_bps": float(gross),
                           "net_bps": float(gross - cost * 2)})
            entry = None
        state, state_data = decision.next_state, decision.state_data or {}
    returns = [t["net_bps"] for t in trades]
    mean, low, high = stats.bootstrap_ci(returns, seed=len(returns) + 1, resamples=500)
    return {
        "parameters": params, "bars_used": len(bars), "trades": trades[-50:], "n_trades": len(trades),
        "open_at_end": entry is not None, "mean_net_bps": mean, "ci_low_bps": low, "ci_high_bps": high,
        "wins": sum(1 for r in returns if r > 0), "cost_bps_per_side": float(cost),
        "fidelity": [
            "Fills are assumed at the signal bar's close, one position at a time, long only; the live engine fills a moment later.",
            "Position sizing, risk checks, order types and partial fills are NOT modelled.",
            "Costs use a flat basis-point model, not your real commissions.",
            "Bars are the persisted LIVE/WARMUP bars; paper trading may have used delayed data, which this replay cannot see.",
            "Indicators come from the engine's own functions (streaming.flink.jobs.processing), so definitions such as the Cutler RSI and the "
            "Donchian channel excluding the current bar match live trading.",
        ],
    }


def compare_parameter_sets(definition, instrument, timeframe, baseline, alternatives, **kwargs):
    """Replay the baseline and each alternative. Hypotheses only; reports the number of comparisons."""
    results = [{"label": "current", **replay_stock(definition, instrument, timeframe, baseline, **kwargs)}]
    for index, alt in enumerate(alternatives):
        results.append({"label": f"alternative {index + 1}", **replay_stock(definition, instrument, timeframe, {**baseline, **alt}, **kwargs)})
    return {"results": results, "comparisons": len(alternatives),
            "warnings": ["Hypotheses only. Every extra parameter set tried is another chance to find a pattern by luck.",
                         "A replay on the same history used to choose the parameters is in-sample and flatters the winner."]}
