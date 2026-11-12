"""Deterministic live monitor for OPEN episodes (Phase 4B). No LLM, no gateway calls, no new leases.

It only reads data the engine already persists (InstrumentMarketState reference prices, strategy run context,
orders) and writes its own intel_ rows. Option marks come from leg reference prices kept fresh by the engine's own
1-minute STRATEGY leases; live IV is NOT available between evaluations and is labelled as such.
"""
import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from apps.intelligence.models import EpisodeLiveSnapshot, LiveAlert, TradeEpisode

logger = logging.getLogger(__name__)
D = Decimal
ZERO = D(0)

DEFAULTS = {
    "snapshot_interval_seconds": 60,
    "material_pnl_change": 0.10,       # of the risk base, forces a snapshot between intervals
    "adverse_medium": 0.50,            # share of max loss / risk budget
    "adverse_high": 0.80,
    "strike_proximity_medium_pct": 1.0,
    "strike_proximity_high_pct": 0.3,
    "soft_exit_default_giveback": 0.25,
    "soft_exit_proximity": 0.80,       # share of the soft-exit giveback threshold
    "stale_alert_seconds": 120,
    "hedge_leg_unfilled_seconds": 60,
    "alert_cooldown_seconds": 600,
}


def thresholds():
    merged = dict(DEFAULTS)
    merged.update(getattr(settings, "INTELLIGENCE_LIVE_THRESHOLDS", {}) or {})
    return merged


def _f(value):
    return None if value is None else float(value)


# ------------------------------------------------------------------------------------------------
# marks
# ------------------------------------------------------------------------------------------------
def _quote(state, now):
    if state is None or state.reference_price is None or state.latest_event_at is None:
        return None, "UNAVAILABLE"
    age = (now - state.latest_event_at).total_seconds()
    if state.status != "FRESH" or age > state.stale_after_seconds:
        return state.reference_price, "STALE"
    return state.reference_price, "FRESH"


def _marks(episode, legs, now):
    from apps.market_streams.models import InstrumentMarketState

    states = {s.instrument_id: s for s in InstrumentMarketState.objects.filter(instrument_id__in=[l.instrument_id for l in legs])}
    marks, qualities = {}, set()
    for leg in legs:
        price, quality = _quote(states.get(leg.instrument_id), now)
        marks[leg.symbol] = {"price": _f(price), "quality": quality}
        # a leg that is already flat does not need a price
        if _open_signed(leg) or not leg.exit_quantity:
            qualities.add(quality)
    worst = "UNAVAILABLE" if "UNAVAILABLE" in qualities else "STALE" if "STALE" in qualities else "FRESH"
    return states, marks, worst


def _open_signed(leg):
    remaining = leg.entry_quantity - leg.exit_quantity
    return remaining if leg.entry_side == "BUY" else -remaining


def _unrealized(legs, states):
    total = ZERO
    for leg in legs:
        state = states.get(leg.instrument_id)
        signed = _open_signed(leg)
        if signed and state is not None and state.reference_price is not None:
            total += (state.reference_price - leg.entry_avg_price) * signed * leg.multiplier
    return total


# ------------------------------------------------------------------------------------------------
# distances
# ------------------------------------------------------------------------------------------------
def _latest_run_context(instance_id):
    from apps.strategies.models import StrategyRun

    run = StrategyRun.objects.filter(strategy_instance_id=instance_id).order_by("-started_at", "-pk").first()
    return (run.context_snapshot or {}) if run else {}


def _stock_distances(episode, instance, price):
    out = {}
    ctx = _latest_run_context(episode.instance_id)
    ind = ctx.get("indicators") or {}

    def number(name):
        try:
            return float(ind[name])
        except (KeyError, TypeError, ValueError):
            return None

    fast, slow = number("sma_fast"), number("sma_slow")
    if fast is not None and slow is not None:
        out["sma_gap"] = round(fast - slow, 6)
        out["sma_gap_note"] = "exit triggers when the fast average crosses below the slow one (gap <= 0)"
    rsi = number("rsi")
    exit_threshold = (instance.parameters or {}).get("exit_threshold") if instance else None
    if rsi is not None and exit_threshold is not None:
        out["rsi"] = rsi
        out["rsi_exit_threshold"] = float(exit_threshold)
        out["rsi_distance_to_exit"] = round(float(exit_threshold) - rsi, 6)
    lower = number("donchian_lower")
    if lower is not None and price is not None:
        out["donchian_lower"] = lower
        out["distance_to_donchian_lower"] = round(float(price) - lower, 6)
    out["indicators_as_of"] = ((ctx.get("bar") or {}).get("window_end"))
    return out


def _option_distances(episode, instance, legs, states, credit_points, now, params):
    from apps.market_streams.models import InstrumentMarketState

    out = {"iv_source": "EVALUATION_TIME_ONLY"}
    underlying = InstrumentMarketState.objects.filter(instrument_id=episode.instrument_id).first()
    spot, quality = _quote(underlying, now)
    out["underlying_quality"] = quality
    if spot is not None:
        out["underlying"] = float(spot)
    expiries = [l.expiration for l in legs if l.expiration and _open_signed(l)]
    if expiries:
        out["dte"] = (min(expiries) - now.date()).days
    shorts = []
    for leg in legs:
        if leg.entry_side == "SELL" and leg.strike is not None and _open_signed(leg) and spot:
            if leg.right == "C":
                gap = (leg.strike - spot) / spot * 100
            else:
                gap = (spot - leg.strike) / spot * 100
            shorts.append({"symbol": leg.symbol, "strike": float(leg.strike), "right": leg.right,
                           "distance_pct": round(float(gap), 4)})
    if shorts:
        out["short_strikes"] = shorts
        out["nearest_short_strike_pct"] = min(s["distance_pct"] for s in shorts)
    calls = [l for l in legs if l.entry_side == "SELL" and l.right == "C" and l.strike is not None]
    puts = [l for l in legs if l.entry_side == "SELL" and l.right == "P" and l.strike is not None]
    if credit_points and calls:
        out["breakeven_upper"] = float(calls[0].strike + credit_points)
    if credit_points and puts:
        out["breakeven_lower"] = float(puts[0].strike - credit_points)
    state_data = (instance.state_data or {}) if instance else {}
    baseline = state_data.get("exit_baseline") or {}
    plugin_peak = state_data.get("peak_pnl", baseline.get("peak_pnl") if isinstance(baseline, dict) else None)
    if plugin_peak is not None:
        try:
            out["plugin_peak_pnl"] = float(plugin_peak)
        except (TypeError, ValueError):
            pass
    return out


# ------------------------------------------------------------------------------------------------
# alerts (templated text only)
# ------------------------------------------------------------------------------------------------
def _raise(episode, now, rule_id, severity, text, dedupe_key, details=None):
    """Create the alert, or refresh the existing one. Returns True only for a NEW alert."""
    cooldown = timedelta(seconds=thresholds()["alert_cooldown_seconds"])
    alert = LiveAlert.objects.filter(episode=episode, dedupe_key=dedupe_key).first()
    if alert is None:
        LiveAlert.objects.create(episode=episode, rule_id=rule_id, severity=severity, text=text[:400],
                                 dedupe_key=dedupe_key, details=details or {}, first_seen=now, last_seen=now)
        return True
    escalated = severity == "HIGH" and alert.severity != "HIGH"
    renewed = now - alert.last_seen > cooldown
    alert.last_seen, alert.occurrences = now, alert.occurrences + 1
    alert.severity = "HIGH" if escalated else alert.severity
    if renewed or escalated:
        alert.text = text[:400]
        alert.details = details or {}
    alert.save()
    return bool(renewed or escalated)


def _evaluate_alerts(episode, instance, snapshot, legs, now, risk_base, t):
    new_alerts = 0

    def raise_(*a, **k):
        nonlocal new_alerts
        if _raise(episode, now, *a, **k):
            new_alerts += 1

    if snapshot.mark_pnl is not None and risk_base:
        loss_share = float(-snapshot.mark_pnl / risk_base)
        for level, sev in (("high", "HIGH"), ("medium", "MEDIUM")):
            if loss_share >= t[f"adverse_{level}"]:
                raise_("ADVERSE_EXCURSION", sev,
                       f"Open loss is {loss_share:.0%} of the maximum risk for this trade "
                       f"(alert level {t[f'adverse_{level}']:.0%}).", f"adverse:{level}",
                       {"loss_share": round(loss_share, 4)})
                break
    near = snapshot.distances.get("short_strikes") or []
    for item in near:
        pct = item["distance_pct"]
        if pct <= t["strike_proximity_high_pct"]:
            raise_("SHORT_STRIKE_PROXIMITY", "HIGH",
                   f"The underlying is {pct:.2f}% from the short {item['right']} strike {item['strike']:g}"
                   + (" (strike breached)." if pct <= 0 else "."), f"strike:{item['symbol']}:high", item)
        elif pct <= t["strike_proximity_medium_pct"]:
            raise_("SHORT_STRIKE_PROXIMITY", "MEDIUM",
                   f"The underlying is {pct:.2f}% from the short {item['right']} strike {item['strike']:g}.",
                   f"strike:{item['symbol']}:medium", item)
    giveback = snapshot.distances.get("profit_giveback_pct")
    if giveback is not None:
        soft = float((instance.parameters or {}).get("dynamic_exit_soft_max_giveback", t["soft_exit_default_giveback"])) if instance else t["soft_exit_default_giveback"]
        if giveback >= soft:
            raise_("SOFT_EXIT_PROXIMITY", "HIGH",
                   f"{giveback:.0%} of the peak profit has been given back (soft-exit giveback level {soft:.0%}).",
                   "soft:high", {"giveback": giveback, "level": soft})
        elif giveback >= soft * t["soft_exit_proximity"]:
            raise_("SOFT_EXIT_PROXIMITY", "MEDIUM",
                   f"{giveback:.0%} of the peak profit has been given back, close to the soft-exit giveback level {soft:.0%}.",
                   "soft:medium", {"giveback": giveback, "level": soft})
    return new_alerts


def _structural_alerts(episode, instance, legs, now, t):
    """Alerts that do not need fresh quotes."""
    from apps.oms.models import Order, OrderIntent

    new_alerts = 0
    expected = 0
    if instance is not None:
        expected = len((instance.state_data or {}).get("legs") or [])
    filled = len([l for l in legs if l.entry_quantity])
    age = (now - episode.opened_at).total_seconds()
    if episode.asset_family == "OPTION" and expected and filled < expected and age > t["hedge_leg_unfilled_seconds"]:
        if _raise(episode, now, "HEDGE_LEG_UNFILLED", "HIGH",
                  f"Only {filled} of {expected} legs have filled {int(age)} seconds after the first fill.",
                  "hedge:unfilled", {"filled": filled, "expected": expected}):
            new_alerts += 1
    blocked = Order.objects.filter(intent__attributions__strategy_instance_id=episode.instance_id,
                                   status="BROKER_BLOCKED", created_at__gte=episode.opened_at).count()
    if blocked:
        if _raise(episode, now, "STAGE1_BLOCKED", "MEDIUM",
                  f"{blocked} order(s) for this trade are blocked at the broker.", "stage:blocked", {"orders": blocked}):
            new_alerts += 1
    manual = OrderIntent.objects.filter(portfolio_id=episode.portfolio_id,
                                        instrument_id__in=[l.instrument_id for l in legs],
                                        origin__in=["MANUAL", "BROKER_IMPORT"], created_at__gte=episode.opened_at)
    for intent in manual[:5]:
        if _raise(episode, now, "UNEXPECTED_MANUAL_ORDER", "HIGH",
                  f"A {intent.origin.lower().replace('_', ' ')} {intent.side} order for {intent.quantity.normalize():f} "
                  "was placed on a contract of this open trade.", f"manual:{intent.pk}", {"intent_id": intent.pk}):
            new_alerts += 1
    ctx = _latest_run_context(episode.instance_id)
    if "SOFT_VOLUME_IV_EXPANSION" in str((ctx.get("bar") or {}).get("option_context") or ""):
        if _raise(episode, now, "IV_EXPANSION_AT_LAST_EVAL", "MEDIUM",
                  "The last strategy evaluation flagged volume and implied-volatility expansion (live IV is only "
                  "measured at evaluations).", "iv:expansion"):
            new_alerts += 1
    return new_alerts


# ------------------------------------------------------------------------------------------------
# one episode
# ------------------------------------------------------------------------------------------------
def monitor_episode(episode, now=None):
    """Return the new EpisodeLiveSnapshot or None (stale quotes, throttled, or nothing to measure)."""
    from apps.strategies.models import StrategyInstance

    now = now or timezone.now()
    t = thresholds()
    legs = list(episode.legs.all())
    if not legs or episode.status != TradeEpisode.Status.OPEN:
        return None
    instance = StrategyInstance.objects.filter(pk=episode.instance_id).select_related("definition").first()
    metrics = getattr(getattr(episode, "metrics_row", None), "metrics", {}) or {}
    states, marks, quality = _marks(episode, legs, now)
    new_alerts = _structural_alerts(episode, instance, legs, now, t)

    last = episode.live_snapshots.order_by("-observed_at").first()
    if quality != "FRESH":
        since = (last.observed_at if last else episode.opened_at)
        if (now - since).total_seconds() > t["stale_alert_seconds"]:
            _raise(episode, now, "QUOTE_STALENESS", "LOW",
                   f"No fresh price for {int((now - since).total_seconds())} seconds; live values are paused.",
                   "quotes:stale", {"quality": quality})
        return None

    unrealized = _unrealized(legs, states)
    mark_pnl = episode.net_pnl + unrealized  # realised so far (net of commissions) plus open mark-to-market
    notional = sum((l.entry_quantity * l.entry_avg_price * l.multiplier for l in legs), ZERO)
    pnl_bps = (mark_pnl / notional * D(10000)) if notional else None
    max_loss, max_profit = metrics.get("opt.max_loss"), metrics.get("opt.max_profit")
    risk_base = None
    if metrics.get("outcome.risk_basis"):
        risk_base = D(str(metrics["outcome.risk_basis"]))
    elif max_loss:
        risk_base = abs(D(str(max_loss)))
    prior = list(episode.live_snapshots.order_by("-observed_at").values_list("mark_pnl", "mae_bps_so_far", "mfe_bps_so_far")[:500])
    mae = max([p[1] for p in prior if p[1] is not None] + [ZERO])
    mfe = max([p[2] for p in prior if p[2] is not None] + [ZERO])
    if pnl_bps is not None:
        mae, mfe = max(mae, -pnl_bps), max(mfe, pnl_bps)
    peak = max([p[0] for p in prior if p[0] is not None] + [mark_pnl])
    distances = {}
    credit_points = None
    if episode.asset_family == "OPTION":
        unit = min((l.entry_quantity for l in legs if l.entry_quantity), default=ZERO)
        entry_cash = metrics.get("opt.entry_cash")
        if unit and entry_cash:
            multiplier = next(iter(legs)).multiplier or D(1)
            credit_points = D(str(entry_cash)) / (unit * multiplier)
        distances = _option_distances(episode, instance, legs, states, credit_points, now, instance.parameters if instance else {})
        distances["predicted_pop"] = metrics.get("opt.pop")
        distances["predicted_net_ev"] = metrics.get("opt.net_ev")
    else:
        price = states.get(legs[0].instrument_id).reference_price if states.get(legs[0].instrument_id) else None
        distances = _stock_distances(episode, instance, price)
    if peak > 0:
        distances["peak_mark_pnl"] = float(peak)
        distances["profit_giveback_pct"] = float((peak - mark_pnl) / peak) if peak > mark_pnl else 0.0
    snapshot = EpisodeLiveSnapshot(
        episode=episode, observed_at=now, quote_freshness="FRESH", marks=marks, mark_pnl=mark_pnl,
        pnl_bps=None if pnl_bps is None else pnl_bps.quantize(D("0.0001")),
        pct_of_max_loss=(mark_pnl / abs(D(str(max_loss))) * 100).quantize(D("0.0001")) if max_loss else None,
        pct_of_max_profit=(mark_pnl / D(str(max_profit)) * 100).quantize(D("0.0001")) if max_profit else None,
        mae_bps_so_far=mae.quantize(D("0.0001")), mfe_bps_so_far=mfe.quantize(D("0.0001")), distances=distances,
        flags=sorted(episode.data_quality_flags or []))
    new_alerts += _evaluate_alerts(episode, instance, snapshot, legs, now, risk_base, t)

    previous = prior[0][0] if prior else None
    changed = previous is not None and risk_base and abs(mark_pnl - previous) >= risk_base * D(str(t["material_pnl_change"]))
    due = last is None or (now - last.observed_at).total_seconds() >= t["snapshot_interval_seconds"]
    if not (due or changed or new_alerts):
        return None
    snapshot.material = bool(changed or new_alerts)
    snapshot.save()
    return snapshot


def monitor_open_episodes(now=None):
    """Monitor every OPEN episode; one failing episode never stops the others."""
    done = 0
    for episode in (TradeEpisode.objects.filter(status=TradeEpisode.Status.OPEN)
                    .select_related("metrics_row").prefetch_related("legs")):
        try:
            if monitor_episode(episode, now) is not None:
                done += 1
        except Exception:
            logger.exception("intelligence: live monitor failed for episode %s", episode.pk)
    return done


def prune_snapshots(keep_days=30):
    """Thin old snapshots of closed episodes to one per 5 minutes; keep everything for open or recent ones."""
    cutoff = timezone.now() - timedelta(days=keep_days)
    removed = 0
    for episode in TradeEpisode.objects.filter(status__in=["CLOSED", "EXPIRED"], closed_at__lt=cutoff):
        last_kept = None
        for snap in episode.live_snapshots.order_by("observed_at"):
            if last_kept is None or (snap.observed_at - last_kept).total_seconds() >= 300 or snap.material:
                last_kept = snap.observed_at
            else:
                snap.delete()
                removed += 1
    return removed
