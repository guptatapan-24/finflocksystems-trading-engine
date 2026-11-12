"""Edge-decay monitor (Phase 11): a CUSUM over each strategy's closed trades, in time order.

Two series per (strategy, currency):
* RETURN: net return in basis points. The first `cusum_ref_n` trades form the baseline (mean and spread); later trades are
  standardised against it. A sustained run below the baseline raises EDGE_DECAY, above it EDGE_IMPROVEMENT.
* POP_CALIBRATION (option strategies): realised win (1/0) against the model's probability of profit, standardised by the
  binomial spread of that probability, with target 0. A sustained negative run means the model is over-confident.

An alarm is a flag to look, not a verdict: it needs `ref_n + 5` trades, about 1 stable strategy in 20 still raises a false alarm within 50
monitored trades (measured by simulation), small drops need many trades to be caught, and a benign change
of market conditions looks the same as a broken edge. The state always carries n, the baseline and the statistic. Pure
reads of stored metrics; nothing here touches trading (R1, R16).
"""
import math
from collections import defaultdict

from django.utils import timezone

from apps.intelligence.models import ChangePointState, LearningEvent, TradeEpisode

from . import stats
from .learning import _event, _metrics_of, thresholds

RETURN, CALIBRATION = "RETURN", "POP_CALIBRATION"


def _series():
    """{(strategy_key, currency): [(episode, metrics)]} of closed trades in the order they closed."""
    groups = defaultdict(list)
    episodes = (TradeEpisode.objects.filter(status__in=["CLOSED", "EXPIRED"]).select_related("metrics_row")
                .order_by("closed_at", "pk"))
    for episode in episodes:
        groups[(episode.strategy_key, episode.currency)].append((episode, _metrics_of(episode)))
    return groups


def _calibration_scores(rows):
    out = []
    for episode, metrics in rows:
        pop = metrics.get("opt.pop")
        if pop is None:
            continue
        p = min(0.98, max(0.02, float(pop)))
        win = 1.0 if (metrics.get("outcome.net_return_bps") or float(episode.net_pnl)) > 0 else 0.0
        out.append((episode, (win - p) / math.sqrt(p * (1 - p))))
    return out


def _write(strategy_key, currency, metric, rows, result, now, t):
    n = len(rows)
    base = dict(n=n, as_of=now, threshold=t["cusum_h"], reference_n=result.get("reference_n", 0), baseline_mean=result.get("baseline_mean"),
                baseline_sd=result.get("baseline_sd"), statistic=None, change_index=None, change_at=None, note="")
    if result.get("insufficient"):
        base.update(status=ChangePointState.Status.INSUFFICIENT,
                    note=f"n = {n}: at least {result['need']} trades are needed before drift can be judged." + (f" ({result['reason']})" if result.get("reason") else ""))
    else:
        base["statistic"] = round(result["statistic"], 3)
        if result["alarm"]:
            idx = result["change_index"]
            base.update(status=ChangePointState.Status.EDGE_DECAY if result["direction"] == "DOWN" else ChangePointState.Status.EDGE_IMPROVEMENT,
                        change_index=idx, change_at=rows[idx - 1][0].closed_at,
                        note=(f"Alarm at trade {result['index']} of {n}: the cumulative drift reached {result['statistic']:.1f} standard deviations "
                              f"(threshold {t['cusum_h']:g}); it is estimated to have started at trade {idx}. A flag to look at the strategy, not proof."))
        else:
            base.update(status=ChangePointState.Status.STABLE, note=f"n = {n}: no sustained drift (largest cumulative drift {result['statistic']:.1f} sd, threshold {t['cusum_h']:g}).")
    state, _ = ChangePointState.objects.update_or_create(strategy_key=strategy_key, currency=currency, metric=metric, defaults=base)
    if state.status in (ChangePointState.Status.EDGE_DECAY, ChangePointState.Status.EDGE_IMPROVEMENT):
        _event(LearningEvent.Type.CHANGE_POINT, key=f"cp:{strategy_key}:{currency}:{metric}:{state.change_index}",
               payload={"strategy_key": strategy_key, "currency": currency, "metric": metric, "status": state.status, "n": n,
                        "change_index": state.change_index, "statistic": state.statistic, "note": state.note})
    return state


def refresh(now=None):
    """Recompute every state from the stored trades. Idempotent. Returns the number of states written."""
    now = now or timezone.now()
    t = thresholds()
    written = 0
    for (strategy_key, currency), rows in _series().items():
        returns = [(e, m.get("outcome.net_return_bps")) for e, m in rows]
        returns = [(e, float(v)) for e, v in returns if v is not None]
        result = stats.cusum([v for _, v in returns], ref_n=t["cusum_ref_n"], k=t["cusum_k"], h=t["cusum_h"], min_after=t["cusum_min_after"],
                             clip=t["cusum_clip"])
        _write(strategy_key, currency, RETURN, [(e,) for e, _ in returns], result, now, t)
        written += 1
        scored = _calibration_scores(rows)
        if scored:
            if len(scored) >= t["cusum_min_calibration"]:
                result = stats.cusum_known([z for _, z in scored], k=t["cusum_k"], h=t["cusum_h"], clip=t["cusum_clip"])
                result.update(insufficient=False, reference_n=0)
            else:
                result = {"insufficient": True, "need": t["cusum_min_calibration"]}
            _write(strategy_key, currency, CALIBRATION, [(e,) for e, _ in scored], result, now, t)
            written += 1
    return written
