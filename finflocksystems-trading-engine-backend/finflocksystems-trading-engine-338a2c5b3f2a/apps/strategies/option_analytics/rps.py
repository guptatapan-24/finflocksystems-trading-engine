"""The frozen RPS V2 heuristic, ported from ``Aryan_Options/trading/rps.py``.

The weights, interpolation nodes, ATR envelope and OI wall are the source
file's, unchanged.  What differs is where the inputs come from: the source
replays a stored feature table, this runtime reads completed IBKR daily bars
and one point-in-time option chain.  A factor whose input this runtime cannot
observe is *excluded* and the remaining frozen weights are renormalised -- the
source's own ``missing_factor_rule`` -- never scored as a fabricated neutral.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

from apps.core.lazy_imports import lazy_module

# Deferred: config/urls.py imports strategies.views, which imports this module,
# so every process that touches the URLconf paid for pandas at import time --
# 81MB, in workers that never score an RPS candidate. The annotations below are
# strings (`from __future__ import annotations`), so they never force the load.
np = lazy_module("numpy")
pd = lazy_module("pandas")

MODEL_VERSION = "rps-v2-framework-2026-09-normalized100"
FEATURE_VERSION = "rps-features-v1"
BOUNDARY_VERSION = "daily-atr075-oi-top5-v2"

WEIGHT_UNITS = {
    "distance_sr": 18,
    "oi_structure": 17,
    "expected_move": 15,
    "volume_pressure": 10,
    "atr_realized_vol": 10,
    "trend_adx": 7,
    "bollinger_compression": 4,
    "mean_reversion": 4,
}
WEIGHT_SCALE = 100
WEIGHT_UNIT_TOTAL = sum(WEIGHT_UNITS.values())
WEIGHTS = {
    name: round(weight * WEIGHT_SCALE / WEIGHT_UNIT_TOTAL, 6)
    for name, weight in WEIGHT_UNITS.items()
}

SCORE_NODES = {
    "expected_move_ratio": ((0.0, 100.0), (0.5, 80.0), (1.0, 50.0), (1.5, 20.0), (3.0, 0.0)),
    "volume_ratio": ((0.0, 100.0), (0.5, 95.0), (1.0, 75.0), (1.5, 40.0), (2.0, 15.0), (5.0, 0.0)),
    "absolute_z": ((0.0, 100.0), (0.5, 90.0), (1.0, 70.0), (2.0, 30.0), (3.0, 0.0)),
}

ATR_PERIOD = 14
ATR_MULTIPLIER = 0.75
OI_TOP_K = 5


def public_spec(parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    parameters = parameters or {}
    return {
        "model_version": MODEL_VERSION,
        "feature_version": FEATURE_VERSION,
        "boundary_engine_version": BOUNDARY_VERSION,
        "weights": WEIGHTS,
        "weight_scale": WEIGHT_SCALE,
        "source_weight_units": WEIGHT_UNIT_TOTAL,
        "score_nodes": {key: [list(node) for node in value] for key, value in SCORE_NODES.items()},
        "boundary": {
            "timeframe": "prior completed daily candle",
            "atr_period": ATR_PERIOD,
            "atr_method": "Wilder EWM alpha=1/14",
            "atr_multiplier": float(parameters.get("rps_atr_multiplier", ATR_MULTIPLIER) or ATR_MULTIPLIER),
            "oi_top_k": int(parameters.get("rps_oi_top_k", OI_TOP_K) or OI_TOP_K),
            "oi_role": "independent confirmation; never mutates ATR boundary",
        },
        "missing_factor_rule": "exclude missing score and renormalize available frozen weights",
        "rounding": "full precision internally; JSON values rounded to 6 decimals",
    }


def _number(value):
    if value is None:
        return np.nan
    try:
        result = float(value)
    except (TypeError, ValueError):
        return np.nan
    return result if math.isfinite(result) else np.nan


def _json_number(value, digits=6):
    number = _number(value)
    return round(number, digits) if np.isfinite(number) else None


def _linear(value: float, nodes) -> float | None:
    if not np.isfinite(value):
        return None
    xs = np.array([node[0] for node in nodes], dtype=float)
    ys = np.array([node[1] for node in nodes], dtype=float)
    return float(np.interp(float(value), xs, ys))


def _inverse_percentile(value: float, history: pd.Series) -> float | None:
    valid = pd.to_numeric(history, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna().tail(252)
    if not np.isfinite(value) or len(valid) < 20:
        return None
    percentile = 100.0 * (float((valid < value).sum()) + 0.5 * float((valid == value).sum())) / len(valid)
    return float(np.clip(100.0 - percentile, 1.0, 99.0))


def _wilder(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def range_score(spot: float, support: float | None, resistance: float | None) -> float | None:
    """How centred `spot` is inside the range, on the source's 0-100 scale."""
    if support is None or resistance is None or support > resistance:
        return None
    if resistance == support:
        # A zero-width range isn't invalid the way an inverted pair is -- there
        # is just no interior to score distance within.
        return 100.0 if spot == support else 0.0
    nearest = min(spot - support, resistance - spot)
    return float(np.clip(200.0 * nearest / (resistance - support), 0.0, 100.0))


def daily_features(bars: Iterable[Any]) -> pd.DataFrame:
    """The source's `_daily_frame`, over completed daily bars of this runtime.

    `bars` are dicts carrying `window_end`, `open`, `high`, `low`, `close` and
    `volume`; the source reads the same six fields off its candle objects.
    """
    records = []
    for bar in bars:
        close = _number(bar["close"])
        if not np.isfinite(close):
            continue
        records.append({
            "timestamp": pd.Timestamp(bar["window_end"]),
            "open": _number(bar["open"]) if np.isfinite(_number(bar["open"])) else close,
            "high": _number(bar["high"]) if np.isfinite(_number(bar["high"])) else close,
            "low": _number(bar["low"]) if np.isfinite(_number(bar["low"])) else close,
            "close": close,
            "volume": _number(bar.get("volume")),
        })
    if not records:
        return pd.DataFrame()
    frame = pd.DataFrame(records).sort_values("timestamp").set_index("timestamp")
    session = pd.Series(frame.index.date, index=frame.index, name="session")
    daily = frame.groupby(session).agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
    })
    # Index underlying bars commonly report zero instead of a meaningful traded
    # volume. Treat that as missing so it cannot earn a false high range score.
    daily.loc[daily["volume"] <= 0, "volume"] = np.nan
    daily.index = pd.DatetimeIndex(pd.to_datetime(daily.index))
    daily.index.name = "session"
    previous_close = daily["close"].shift(1)
    true_range = pd.concat([
        daily["high"] - daily["low"],
        (daily["high"] - previous_close).abs(),
        (daily["low"] - previous_close).abs(),
    ], axis=1).max(axis=1)
    daily["atr14"] = _wilder(true_range, ATR_PERIOD)
    daily["atr50"] = _wilder(true_range, 50)
    returns = np.log(daily["close"] / previous_close)
    daily["hv20"] = returns.rolling(20, min_periods=20).std(ddof=1) * math.sqrt(252)
    daily["hv60"] = returns.rolling(60, min_periods=60).std(ddof=1) * math.sqrt(252)
    daily["ma20"] = daily["close"].rolling(20, min_periods=20).mean()
    daily["ma50"] = daily["close"].rolling(50, min_periods=50).mean()
    daily["std20"] = daily["close"].rolling(20, min_periods=20).std(ddof=1)
    daily["bb_width"] = 4 * daily["std20"] / daily["ma20"]
    daily["volume_avg20"] = daily["volume"].rolling(20, min_periods=1).mean()
    daily["volume_ratio"] = daily["volume"] / daily["volume_avg20"]

    up = daily["high"].diff()
    down = -daily["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=daily.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=daily.index)
    tr_smoothed = _wilder(true_range, ATR_PERIOD)
    plus_di = 100 * _wilder(plus_dm, ATR_PERIOD) / tr_smoothed
    minus_di = 100 * _wilder(minus_dm, ATR_PERIOD) / tr_smoothed
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    daily["adx14"] = _wilder(dx, ATR_PERIOD)
    daily["z20"] = (daily["close"] - daily["ma20"]) / daily["std20"].replace(0, np.nan)
    daily["vol_blend"] = pd.concat([
        daily["atr14"] / daily["close"], daily["atr50"] / daily["close"],
        daily["hv20"], daily["hv60"],
    ], axis=1).mean(axis=1, skipna=True)
    return daily


def weighted_oi_wall(rows, right: str, spot: float, *, top_k: int = OI_TOP_K) -> float | None:
    """The source's `_weighted_wall`: the OI-weighted strike of the heaviest
    strikes on one side of spot, with the extreme 1% of OI winsorised so a
    single outsized line does not define the level on its own.

    `rows` are chain observations carrying `right` ("C"/"P"), `strike` and `oi`.
    A zero-OI strike is kept, exactly as the source keeps it: only which side of
    spot a strike sits on decides eligibility.
    """
    strikes = np.array([float(row["strike"]) for row in rows], dtype=float)
    if strikes.size == 0:
        return None
    oi = np.array([float(row["oi"] or 0) for row in rows], dtype=float)
    side = np.array([row["right"] == right for row in rows], dtype=bool)
    side &= strikes <= spot if right == "P" else strikes >= spot
    strikes, oi = strikes[side], oi[side]
    if strikes.size == 0:
        return None
    lower, upper = np.quantile(oi, [0.01, 0.99])
    score = np.clip(oi, lower, upper)
    # Descending, ties broken by position, to match DataFrame.nlargest(keep="first").
    top = np.argsort(-score, kind="stable")[:top_k]
    weights = score[top]
    if weights.sum() <= 0:
        return None
    return float(np.average(strikes[top], weights=weights))


def chain_expected_move(rows, spot: float) -> float | None:
    """The ATM straddle price: the source's `atm_call + atm_put`.

    Taken at the strike nearest spot that quotes *both* rights, because a sparse
    chain may not carry both at the same strike; the factor stays unavailable
    rather than mixing two strikes.
    """
    by_strike: dict[float, dict[str, float]] = {}
    for row in rows:
        price = _number(row.get("price"))
        if np.isfinite(price):
            by_strike.setdefault(float(row["strike"]), {})[row["right"]] = price
    for strike in sorted(by_strike, key=lambda value: abs(value - spot)):
        quotes = by_strike[strike]
        if "C" in quotes and "P" in quotes:
            return quotes["C"] + quotes["P"]
    return None


def _chain_value(chain_features, name, fallback=None):
    """A precomputed per-session chain figure, or the local fallback."""
    if not chain_features:
        return fallback
    value = chain_features.get(name)
    return fallback if value is None else value


def build_observation(
    *, features: pd.DataFrame, session_date, spot: float, chain_rows,
    parameters: dict[str, Any] | None = None,
    chain_features: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """One causal RPS observation for `session_date`.

    Every derived input is cut strictly before the session: the ATR envelope and
    the daily indicators come from the last completed prior session, and only
    the observed spot is contemporaneous.  This is the source's causal contract,
    and it is why the OI wall -- which this runtime can only read from the
    current chain -- scores the `oi_structure` factor and never moves the ATR
    boundary.
    """
    parameters = parameters or {}
    atr_multiplier = float(parameters.get("rps_atr_multiplier", ATR_MULTIPLIER) or ATR_MULTIPLIER)
    oi_top_k = int(parameters.get("rps_oi_top_k", OI_TOP_K) or OI_TOP_K)
    if features.empty:
        return None
    history = features[features.index.date < session_date]
    if history.empty:
        return None
    feature = history.iloc[-1]
    atr_daily = _number(feature["atr14"])
    reference = _number(feature["close"])
    if not all(np.isfinite([atr_daily, reference])) or atr_daily <= 0:
        return None
    support = reference - atr_multiplier * atr_daily
    resistance = reference + atr_multiplier * atr_daily
    oi_support = weighted_oi_wall(chain_rows, "P", spot, top_k=oi_top_k)
    oi_resistance = weighted_oi_wall(chain_rows, "C", spot, top_k=oi_top_k)
    if oi_support is not None and oi_resistance is not None and oi_support >= oi_resistance:
        oi_support = oi_resistance = None
    expected_move = chain_expected_move(chain_rows, spot)
    range_width = resistance - support
    current_z = (
        (spot - _number(feature["ma20"])) / _number(feature["std20"])
        if np.isfinite(_number(feature["ma20"])) and _number(feature["std20"]) > 0 else np.nan
    )

    scores = {
        "distance_sr": range_score(spot, support, resistance),
        "oi_structure": range_score(spot, oi_support, oi_resistance),
        "expected_move": _linear(
            min(float(expected_move) / range_width, 3.0), SCORE_NODES["expected_move_ratio"]
        ) if expected_move is not None and range_width > 0 else None,
        "volume_pressure": _linear(
            min(_number(feature["volume_ratio"]), 5.0), SCORE_NODES["volume_ratio"]
        ),
        "atr_realized_vol": _inverse_percentile(_number(feature["vol_blend"]), history["vol_blend"]),
        "trend_adx": _inverse_percentile(min(_number(feature["adx14"]), 60.0), history["adx14"].clip(upper=60)),
        "bollinger_compression": (
            _inverse_percentile(_number(feature["bb_width"]), history["bb_width"])
            if history["bb_width"].notna().sum() >= 60 else None
        ),
        "mean_reversion": _linear(min(abs(current_z), 3.0), SCORE_NODES["absolute_z"]),
    }
    available = {name: value for name, value in scores.items() if value is not None and np.isfinite(value)}
    available_weight = sum(WEIGHTS[name] for name in available)
    rps_base = (
        sum(WEIGHTS[name] * value for name, value in available.items()) / available_weight
        if available_weight else None
    )
    flags = []
    if history["volume"].notna().sum() < 20:
        flags.append("VOLUME_HISTORY_LT_20")
    if oi_support is None or oi_resistance is None:
        flags.append("OI_FACTOR_UNAVAILABLE")
    for name in ("atr_realized_vol", "trend_adx", "bollinger_compression"):
        if scores[name] is None:
            flags.append(f"{name.upper()}_HISTORY_INSUFFICIENT")
    # Nothing in this runtime observes a corporate/macro event calendar with a
    # genuine per-event `known_at`, so -- exactly as the source does when its
    # own feature table has no row -- every session takes the neutral fallback
    # and `event_risk_data_available` tells consumers it is a fallback.
    return {
        "observation_id": f"{MODEL_VERSION}:{session_date.isoformat()}",
        "decision_session": session_date.isoformat(),
        "feature_cutoff": history.index[-1].isoformat(),
        "spot": _json_number(spot),
        "support": _json_number(support), "resistance": _json_number(resistance),
        "atr_support": _json_number(support), "atr_resistance": _json_number(resistance),
        "oi_support": _json_number(oi_support), "oi_resistance": _json_number(oi_resistance),
        "atr14_daily": _json_number(atr_daily),
        "atr14": _json_number(feature["atr14"]), "atr50": _json_number(feature["atr50"]),
        "hv20": _json_number(feature["hv20"]), "hv60": _json_number(feature["hv60"]),
        "adx14": _json_number(feature["adx14"]), "ma20": _json_number(feature["ma20"]),
        "ma50": _json_number(feature["ma50"]), "bb_width": _json_number(feature["bb_width"]),
        "volume_ratio": _json_number(feature["volume_ratio"]), "z20": _json_number(current_z),
        "expected_move": _json_number(expected_move),
        # The remote session row wins where it exists: `realized_volatility_20`
        # is the source's own engineered figure, and the locally derived `hv20`
        # is the fallback the source uses when no row is available.
        "realized_volatility_20": _json_number(
            _chain_value(chain_features, "realized_volatility_20", feature["hv20"])
        ),
        "atm_iv": _json_number(_chain_value(chain_features, "atm_iv")),
        "iv_rank_252": _json_number(_chain_value(chain_features, "iv_rank_252")),
        "iv_percentile_252": _json_number(_chain_value(chain_features, "iv_percentile_252")),
        "iv_rv_ratio": _json_number(_chain_value(chain_features, "iv_rv_ratio")),
        "term_structure_slope": _json_number(_chain_value(chain_features, "term_structure_slope")),
        "skew_25d": _json_number(_chain_value(chain_features, "skew_25d")),
        "chain_features_available": bool(chain_features),
        "component_scores": {name: _json_number(value) for name, value in scores.items()},
        "rps_base": _json_number(rps_base), "rps_final": _json_number(rps_base),
        "available_weight": _json_number(available_weight),
        "event_risk": 0.0, "pir": 1.25, "event_expiry_gap_sessions": 1.0,
        "event_risk_data_available": False,
        "data_quality_ok": True,
        "flags": flags,
        "model_version": MODEL_VERSION,
        "boundary_version": BOUNDARY_VERSION,
    }
