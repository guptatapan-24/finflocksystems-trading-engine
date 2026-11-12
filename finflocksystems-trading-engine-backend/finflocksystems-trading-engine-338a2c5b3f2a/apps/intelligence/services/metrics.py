"""Deterministic, versioned episode metrics (R3). Flat dotted keys; every value is JSON-serialisable.

Lessons and citations may only use these keys. Bump METRICS_VERSION when any definition changes (episodes with an
older `metrics_version_built` are recomputed by the next sweep/backfill). Nothing here calls an LLM.
Catalogue (key, unit, applies to): see docs/intelligence/DESIGN.md section 12.
"""
import logging
from datetime import timedelta
from decimal import Decimal

logger = logging.getLogger(__name__)
D = Decimal
ZERO = Decimal(0)
METRICS_VERSION = "3"

ZONES = {"XNSE": "Asia/Kolkata", "XNYS": "America/New_York"}
TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "1d": 86400}
DELAY_SECONDS = 900  # PAPER delayed market data is stamped about 15 minutes behind wall clock


def _num(value):
    """Decimal -> float for storage; keeps None."""
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _first(mapping, *keys):
    for key in keys:
        if isinstance(mapping, dict) and mapping.get(key) is not None:
            return mapping[key]
    return None


def _zone(calendar):
    from zoneinfo import ZoneInfo

    try:
        return ZoneInfo(ZONES.get(calendar or "", "UTC"))
    except Exception:
        from datetime import timezone

        return timezone.utc


def _bucket(moment, zone):
    local = moment.astimezone(zone)
    minute = 30 if local.minute >= 30 else 0
    return f"{local.hour:02d}:{minute:02d}", local.strftime("%a")


def _indicator_values(context, prefix, out):
    for name, value in ((context or {}).get("indicators") or {}).items():
        try:
            out[f"{prefix}.{str(name).lower()}"] = float(value)
        except (TypeError, ValueError):
            continue


# ------------------------------------------------------------------------------------------------
def _common(instance, summary, ctx, entry_info, exit_info, out):
    zone = _zone(getattr(instance.instrument, "trading_calendar", ""))
    out["outcome.gross_pnl"] = _num(summary["gross"])
    out["outcome.commission"] = _num(summary["commissions"])
    out["outcome.net_pnl"] = _num(summary["net"])
    out["outcome.currency"] = summary["currency"]
    out["outcome.entry_notional"] = _num(summary["entry_notional"])
    notional = summary["entry_notional"]
    if notional:
        out["outcome.cost_bps"] = _num(summary["commissions"] / notional * D(10000))
        out["outcome.net_return_bps"] = _num(summary["net"] / notional * D(10000))
    out["outcome.is_closed"] = bool(summary["closed"])
    if summary["closed"]:
        seconds = int((summary["closed_at"] - summary["opened_at"]).total_seconds())
        out["outcome.hold_seconds"] = seconds
        bar_seconds = TIMEFRAME_SECONDS.get(instance.timeframe)
        if bar_seconds:
            out["outcome.hold_bars"] = round(seconds / bar_seconds, 4)
        out["exit.reason"] = exit_info[0]
        if exit_info[1]:
            out["exit.subtype"] = exit_info[1]
        bucket, weekday = _bucket(summary["closed_at"], zone)
        out["time.exit_bucket"] = bucket
    bucket, weekday = _bucket(summary["opened_at"], zone)
    out["time.entry_bucket"] = bucket
    out["time.entry_weekday"] = weekday
    out["exec.fill_count"] = len(summary["parts"])
    entry_w = entry_q = exit_w = exit_q = ZERO
    for leg in summary["legs"].values():
        entry_w += leg["entry_slip_w"]
        entry_q += leg["entry_slip_q"]
        exit_w += leg["exit_slip_w"]
        exit_q += leg["exit_slip_q"]
    if entry_q:
        out["exec.slippage_bps_entry"] = _num(entry_w / entry_q)
    if exit_q:
        out["exec.slippage_bps_exit"] = _num(exit_w / exit_q)
    intent = summary["first_intent"]
    if intent is not None:
        out["exec.entry_order_type"] = intent.order_type
        out["exec.entry_origin"] = intent.origin
        try:
            sizing = intent.sizing_decision
        except Exception:
            sizing = None
        if sizing is not None:
            out["sizing.binding_constraint"] = sizing.binding_constraint
            out["sizing.target_quantity"] = _num(sizing.target_quantity)
            out["sizing.approved_quantity"] = _num(sizing.approved_quantity)
            if sizing.target_quantity:
                out["sizing.resize_ratio"] = _num(sizing.approved_quantity / sizing.target_quantity)
            if sizing.risk_budget:
                out["outcome.risk_basis"] = str(sizing.risk_budget)
    _consecutive_losses(instance, summary, out)


def _consecutive_losses(instance, summary, out):
    from apps.intelligence.models import TradeEpisode

    prior = list(TradeEpisode.objects.filter(
        instance_id=instance.pk, status__in=["CLOSED", "EXPIRED"], currency=summary["currency"],
        closed_at__lt=summary["opened_at"]).order_by("-closed_at").values_list("net_pnl", flat=True)[:20])
    streak = 0
    for pnl in prior:
        if pnl < 0:
            streak += 1
        else:
            break
    out["context.prior_closed_episodes"] = len(prior)
    out["context.consecutive_losses_before"] = streak


# ------------------------------------------------------------------------------------------------
def _stock(instance, summary, ctx, entry_info, exit_info, flags, out):
    entry_run, _snap, _linked = entry_info
    entry_ctx = (entry_run.context_snapshot or {}) if entry_run else {}
    close = ((entry_ctx.get("bar") or {}).get("close"))
    if close is not None:
        try:
            out["entry.close"] = float(close)
        except (TypeError, ValueError):
            pass
    _indicator_values(entry_ctx, "entry", out)
    for name, value in (entry_ctx.get("previous_indicators") or {}).items():
        try:
            out[f"entry.prev.{str(name).lower()}"] = float(value)
        except (TypeError, ValueError):
            pass
    exit_run = exit_info[2]
    if exit_run is not None and summary["closed"]:
        _indicator_values(exit_run.context_snapshot or {}, "exit", out)
    _excursions(instance, summary, flags, out)
    _regime(instance, summary, out)


def _latest_bars(instance, start, end):
    from apps.market_streams.models import MarketBar

    rows = (MarketBar.objects.filter(instrument_id=instance.instrument_id, interval=instance.timeframe,
                                     processing_mode__in=["LIVE", "WARMUP"],
                                     window_end__gte=start - timedelta(days=1), window_start__lte=end)
            .order_by("bar_id", "-version"))
    latest = {}
    for bar in rows:
        latest.setdefault(bar.bar_id, bar)  # first row per bar_id is the highest version
    return sorted(latest.values(), key=lambda b: b.window_start)


def _excursions(instance, summary, flags, out):
    """Bar-resolution MAE/MFE (highs and lows only). Stocks are single-instrument here by construction."""
    from apps.intelligence.services.episodes import PAPER_DELAYED_DATA

    leg = next(iter(summary["legs"].values()), None)
    if leg is None or not leg["entry_qty"]:
        return
    entry = leg["entry_notional"] / leg["entry_qty"]
    long_side = summary["direction"] == "LONG"
    end = summary["closed_at"] or summary["opened_at"]
    start = summary["opened_at"]
    bars = _latest_bars(instance, start, end if summary["closed"] else start + timedelta(days=3650))
    used, delayed = [], False
    for bar in bars:
        shift = timedelta(seconds=DELAY_SECONDS) if bar.market_data_type == "DELAYED" else timedelta(0)
        delayed = delayed or bool(shift)
        b_start, b_end = bar.window_start + shift, bar.window_end + shift
        if b_end >= start and (not summary["closed"] or b_start <= end):
            used.append((bar, b_end))
    if delayed:
        flags.add(PAPER_DELAYED_DATA)
    out["path.resolution"] = "BAR_HIGH_LOW"
    out["path.bars_used"] = len(used)
    if not used or not entry:
        return
    highs = [(b.high, t) for b, t in used]
    lows = [(b.low, t) for b, t in used]
    if long_side:
        mae = max(ZERO, (entry - min(low for low, _ in lows)) / entry * D(10000))
        top, top_t = max(highs, key=lambda item: item[0])
        mfe = max(ZERO, (top - entry) / entry * D(10000))
    else:
        mae = max(ZERO, (max(high for high, _ in highs) - entry) / entry * D(10000))
        bottom, top_t = min(lows, key=lambda item: item[0])
        mfe = max(ZERO, (entry - bottom) / entry * D(10000))
    out["path.mae_bps"] = _num(mae)
    out["path.mfe_bps"] = _num(mfe)
    out["path.time_to_mfe_seconds"] = max(0, int((top_t - start).total_seconds()))
    notional = summary["entry_notional"]
    if summary["closed"] and notional:
        realised_bps = summary["gross"] / notional * D(10000)
        out["path.giveback_bps"] = _num(mfe - realised_bps)
        if mfe > 0:
            out["path.capture_ratio"] = _num(realised_bps / mfe)
    crosses, previous = 0, None
    for bar, _t in used:
        side = 1 if bar.close > entry else (-1 if bar.close < entry else 0)
        if previous is not None and side and previous and side != previous:
            crosses += 1
        if side:
            previous = side
    out["path.whipsaw_count"] = crosses


def _regime(instance, summary, out):
    """Realised-volatility percentile at entry (20-bar std of log returns vs its own history)."""
    bars = _latest_bars(instance, summary["opened_at"] - timedelta(days=400), summary["opened_at"])
    closes = [float(b.close) for b in bars if b.window_end <= summary["opened_at"]][-260:]
    if len(closes) < 60:
        return
    import numpy as np

    arr = np.log(np.array(closes))
    returns = np.diff(arr)
    window = 20
    vols = np.array([returns[i - window:i].std() for i in range(window, len(returns) + 1)])
    if len(vols) < 20:
        return
    out["regime.realized_vol_20"] = float(vols[-1])
    out["regime.vol_pctile"] = float((vols[:-1] <= vols[-1]).mean() * 100.0) if len(vols) > 1 else None


# ------------------------------------------------------------------------------------------------
def _selected_ranking(snapshot):
    rankings = list(getattr(snapshot, "candidate_rankings", None) or [])
    chosen = getattr(snapshot, "selected_strategy", None)
    for row in rankings:
        if isinstance(row, dict) and row.get("strategy_key", row.get("strategy")) == chosen:
            return row
    return rankings[0] if rankings and isinstance(rankings[0], dict) else {}


def _option(instance, summary, ctx, entry_info, exit_info, out):
    from apps.intelligence.services.episodes import _entry_cash

    entry_run, snapshot, linked = entry_info
    params = instance.parameters or {}
    out["opt.snapshot_linked"] = bool(linked)
    credit = _entry_cash(summary["parts"])
    out["opt.entry_cash"] = _num(credit)
    out["opt.structure_legs"] = len(summary["legs"])
    spread = [l["first_at"] for l in summary["legs"].values()]
    if spread:
        out["opt.entry_fill_spread_seconds"] = int((max(spread) - min(spread)).total_seconds())
    if snapshot is None:
        return
    out["opt.selected_strategy"] = snapshot.selected_strategy
    out["opt.entry_score"] = _num(snapshot.entry_score)
    out["opt.expiration"] = snapshot.expiration.isoformat()
    source = snapshot.source_snapshot or {}
    threshold = source.get("entry_threshold", params.get("rps_threshold"))
    if threshold is not None:
        out["opt.rps_threshold"] = _num(threshold)
        out["margin.rps"] = _num(snapshot.entry_score - D(str(threshold)))
    row = _selected_ranking(snapshot)
    signal = row.get("entry_signal") if isinstance(row.get("entry_signal"), dict) else {}
    tqs = _first(row, "tqs")
    out["opt.tqs"] = _num(tqs)
    tqs_threshold = params.get("rps_tqs_threshold")
    if tqs is not None and tqs_threshold is not None:
        out["margin.tqs"] = _num(D(str(tqs)) - D(str(tqs_threshold)))
    out["opt.tqs_coverage"] = _num(_first(row, "tqs_factor_coverage"))
    unmeasured = row.get("tqs_unmeasured_factors")
    if isinstance(unmeasured, list):
        out["opt.tqs_unmeasured_count"] = len(unmeasured)
        out["opt.tqs_unmeasured"] = ",".join(sorted(str(x) for x in unmeasured))
    out["opt.pop"] = _num(_first(row, "pop", "probability_of_profit"))
    out["opt.net_ev"] = _num(_first(row, "net_expected_pnl", "expected_pnl"))
    out["opt.max_profit"] = _num(_first(row, "max_profit"))
    out["opt.max_loss"] = _num(_first(row, "max_loss"))
    out["opt.decision_band"] = _first(row, "decision_band")
    out["opt.size_multiplier"] = _num(_first(row, "size_multiplier"))
    out["opt.breach_up"] = _num(_first(row, "upper_breach_probability") or _first(signal, "upper_breach_probability"))
    out["opt.breach_down"] = _num(_first(row, "lower_breach_probability") or _first(signal, "lower_breach_probability"))
    out["opt.iv_score"] = _num(_first(signal, "iv_score"))
    out["opt.iv_rank"] = _num(_first(signal, "iv_rank"))
    out["opt.iv_percentile"] = _num(_first(signal, "iv_percentile"))
    iv_threshold = params.get("rps_iv_score_threshold")
    if out.get("opt.iv_score") is not None and iv_threshold is not None:
        out["margin.iv_score"] = out["opt.iv_score"] - float(iv_threshold)
    max_loss, max_profit = _first(row, "max_loss"), _first(row, "max_profit")
    if max_loss:
        out["outcome.risk_basis"] = str(abs(D(str(max_loss))))
    if summary["closed"]:
        if max_profit and D(str(max_profit)):
            out["opt.realized_fraction_of_max_profit"] = _num(summary["net"] / D(str(max_profit)))
        if max_loss and D(str(max_loss)):
            out["opt.realized_fraction_of_max_loss"] = _num(summary["net"] / abs(D(str(max_loss))))
        if credit > 0:
            out["opt.credit_capture"] = _num(summary["gross"] / credit)
            out["opt.round_trip_cost_over_credit"] = _num(summary["commissions"] / credit)
    snapshot_signal = _first((source.get("selection_evidence") or {}), "entry_signal") or {}
    if out.get("opt.iv_score") is None and isinstance(snapshot_signal, dict):
        out["opt.iv_score"] = _num(snapshot_signal.get("iv_score"))


def _context(instance, summary, entry_info, out):
    """ctx.* keys from stored outside context (Phase 10). Absent keys mean "not measurable", never zero. Never raises.
    Only for closed trades: an open trade is rebuilt every sweep and these keys are only used once it is final."""
    if not summary["closed"]:
        return
    try:
        from .context_view import compute

        snapshot = entry_info[1] if entry_info else None
        out.update(compute(instance, summary["opened_at"], summary["closed_at"], getattr(snapshot, "expiration", None))["metrics"])
    except Exception:
        out["error.context"] = True


def _operator(instance, summary, out):
    """Operator behaviour and the risk profile in force (Phase 8 data), for closed trades. Never raises."""
    if not summary["closed"]:
        return
    try:
        from .evidence import operator_block

        block = operator_block(instance, summary["opened_at"], summary["closed_at"], instance.instrument.symbol, summary.get("version_number"))
        for key in ("hours_since_last_change_before_entry", "changes_during_hold", "manual_orders_during_hold", "profile_changed_during_hold"):
            if block.get(key) is not None:
                out[f"operator.{key}"] = block[key]
        name = (block.get("profile") or {}).get("name")
        if name:
            out["selection.risk_profile"] = name
    except Exception:
        out["error.operator"] = True


def compute_metrics(instance, summary, ctx, entry_info, exit_info, flags):
    out = {"metrics_version": METRICS_VERSION, "asset_family": summary["family"]}
    _common(instance, summary, ctx, entry_info, exit_info, out)
    if summary["family"] == "STOCK":
        _stock(instance, summary, ctx, entry_info, exit_info, flags, out)
    else:
        _option(instance, summary, ctx, entry_info, exit_info, out)
    _context(instance, summary, entry_info, out)
    _operator(instance, summary, out)
    out["flags"] = sorted(flags)
    return {k: v for k, v in out.items() if v is not None}
