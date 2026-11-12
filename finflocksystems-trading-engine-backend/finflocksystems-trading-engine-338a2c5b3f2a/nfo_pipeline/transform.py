from __future__ import annotations

import hashlib
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from scipy.special import ndtr

from .schema import SUPPORTED_UNDERLYINGS


IST = ZoneInfo("Asia/Kolkata")
SOURCE_COLUMNS = ["Ticker", "Date", "Time", "Open", "High", "Low", "Close", "Volume", "Open Interest"]
UNDERLYINGS = frozenset(SUPPORTED_UNDERLYINGS)
TICKER_RE = re.compile(
    r"^(?P<underlying>.+?)(?P<expiry>\d{2}[A-Z]{3}\d{2})"
    r"(?P<strike>\d+(?:\.\d+)?)(?P<right>CE|PE)\.NFO$"
)
SCHEMA_VERSION = "NFO_INDEX_OPTION_1.1_DECISION_FEATURES"
MODEL_VERSION = "EUROPEAN_BS_1.0"
LOT_SPEC_VERSION = "NSE_INDEX_LOT_EFFECTIVE_1.0"


def select_index_options(raw: pd.DataFrame, underlyings: frozenset[str] = UNDERLYINGS) -> tuple[pd.DataFrame, pd.DataFrame]:
    missing = set(SOURCE_COLUMNS) - set(raw.columns)
    if missing:
        raise ValueError(f"Missing source columns: {sorted(missing)}")
    parsed = raw["Ticker"].astype(str).str.strip().str.extract(TICKER_RE.pattern)
    selected = parsed.underlying.isin(underlyings)
    return raw.loc[selected].copy(), parsed.loc[selected].copy()


def prepare_identity(raw: pd.DataFrame, parsed: pd.DataFrame, session_date) -> pd.DataFrame:
    frame = raw.copy()
    frame["underlying"] = parsed.underlying
    frame["expiry"] = pd.to_datetime(parsed.expiry, format="%d%b%y").dt.date
    frame["strike"] = pd.to_numeric(parsed.strike)
    frame["option_type"] = parsed.right.map({"CE": "CALL", "PE": "PUT"})
    timestamps = pd.to_datetime(frame.Date + " " + frame.Time, format="%d/%m/%Y %H:%M:%S")
    frame["observed_at"] = timestamps.dt.tz_localize(IST)
    if not frame.observed_at.dt.date.eq(session_date).all():
        raise ValueError(f"Source timestamps do not match member date {session_date}")
    frame["session_date"] = session_date
    frame["is_regular_session"] = frame.observed_at.dt.time.between(
        datetime.strptime("09:15:00", "%H:%M:%S").time(),
        datetime.strptime("15:30:59", "%H:%M:%S").time(),
    )
    frame["expiry_cycle"] = pd.to_datetime(frame.expiry).map(
        lambda value: "MONTH" if (value + pd.Timedelta(days=7)).month != value.month else "WEEK"
    )
    expiry_close = pd.to_datetime(frame.expiry).dt.tz_localize(IST) + pd.Timedelta(hours=15, minutes=30)
    frame["dte_days"] = (expiry_close - frame.observed_at).dt.total_seconds() / 86400.0
    return frame


def match_spot(frame: pd.DataFrame, spots: dict[str, pd.DataFrame], tolerance_minutes: int) -> pd.DataFrame:
    output = []
    frame = frame.copy()
    frame["_source_order"] = np.arange(len(frame))
    frame["_match_minute"] = frame.observed_at.dt.floor("min").astype("datetime64[ns, Asia/Kolkata]")
    for underlying, group in frame.groupby("underlying", sort=False):
        spot = spots[underlying].drop(columns=["underlying"]).rename(columns={
            "observed_at": "spot_observed_at", "open": "spot_open", "high": "spot_high",
            "low": "spot_low", "close": "spot_close", "volume": "spot_volume",
        }).sort_values("spot_observed_at")
        spot["spot_observed_at"] = spot.spot_observed_at.astype("datetime64[ns, Asia/Kolkata]")
        matched = pd.merge_asof(
            group.sort_values("observed_at"), spot,
            left_on="_match_minute", right_on="spot_observed_at", direction="backward",
            tolerance=pd.Timedelta(minutes=tolerance_minutes),
        )
        age = (matched._match_minute - matched.spot_observed_at).dt.total_seconds() / 60
        matched["spot_age_minutes"] = age
        matched["spot_match"] = np.select(
            [age.eq(0), age.gt(0) & age.le(tolerance_minutes)], ["EXACT", "CARRIED"], default="MISSING"
        )
        output.append(matched)
    return pd.concat(output).sort_values("_source_order").drop(columns=["_source_order", "_match_minute"])


def black_scholes(spot, strike, years, rate, yield_, vol, is_call):
    sqrt_t = np.sqrt(np.maximum(years, 1e-16))
    d1 = (np.log(np.maximum(spot, 1e-16) / np.maximum(strike, 1e-16)) + (rate - yield_ + vol * vol / 2) * years) / (vol * sqrt_t)
    d2 = d1 - vol * sqrt_t
    disc_r, disc_q = np.exp(-rate * years), np.exp(-yield_ * years)
    call = spot * disc_q * ndtr(d1) - strike * disc_r * ndtr(d2)
    put = strike * disc_r * ndtr(-d2) - spot * disc_q * ndtr(-d1)
    density = np.exp(-d1 * d1 / 2) / np.sqrt(2 * np.pi)
    return {
        "price": np.where(is_call, call, put),
        "delta": np.where(is_call, disc_q * ndtr(d1), disc_q * (ndtr(d1) - 1)),
        "gamma": disc_q * density / (spot * vol * sqrt_t),
        "theta": np.where(
            is_call,
            -(spot * disc_q * density * vol) / (2 * sqrt_t) - rate * strike * disc_r * ndtr(d2) + yield_ * spot * disc_q * ndtr(d1),
            -(spot * disc_q * density * vol) / (2 * sqrt_t) + rate * strike * disc_r * ndtr(-d2) - yield_ * spot * disc_q * ndtr(-d1),
        ) / 365.0,
        "vega": spot * disc_q * density * sqrt_t / 100.0,
        "rho": np.where(is_call, strike * years * disc_r * ndtr(d2), -strike * years * disc_r * ndtr(-d2)) / 100.0,
    }


def derive_analytics(frame: pd.DataFrame, rate: float, yield_: float) -> pd.DataFrame:
    result = frame.copy()
    spot = pd.to_numeric(result.spot_close, errors="coerce").to_numpy(float)
    strike = pd.to_numeric(result.strike, errors="coerce").to_numpy(float)
    premium = pd.to_numeric(result.Close, errors="coerce").to_numpy(float)
    years = result.dte_days.to_numpy(float) / 365.25
    is_call = result.option_type.eq("CALL").to_numpy()
    base = np.isfinite(spot) & np.isfinite(strike) & np.isfinite(premium) & (spot > 0) & (strike > 0) & (premium > 0) & (years > 0)
    disc_r, disc_q = np.exp(-rate * np.maximum(years, 0)), np.exp(-yield_ * np.maximum(years, 0))
    lower = np.where(is_call, np.maximum(0, spot * disc_q - strike * disc_r), np.maximum(0, strike * disc_r - spot * disc_q))
    upper = np.where(is_call, spot * disc_q, strike * disc_r)
    bounds = base & (premium >= lower - 1e-8) & (premium <= upper + 1e-8)
    low, high = np.full(len(result), 1e-4), np.full(len(result), 5.0)
    for _ in range(42):
        mid = (low + high) / 2
        value = black_scholes(np.maximum(spot, 1e-9), np.maximum(strike, 1e-9), np.maximum(years, 1e-12), rate, yield_, mid, is_call)["price"]
        low, high = np.where(value < premium, mid, low), np.where(value < premium, high, mid)
    sigma = (low + high) / 2
    sigma[~bounds] = np.nan
    values = black_scholes(np.maximum(spot, 1e-9), np.maximum(strike, 1e-9), np.maximum(years, 1e-12), rate, yield_, np.nan_to_num(sigma, nan=.2), is_call)
    residual = np.abs(values["price"] - premium)
    valid = bounds & np.isfinite(residual) & (residual <= 1e-5)
    sigma[~valid] = np.nan
    result["derived_iv"] = sigma
    for name in ("delta", "gamma", "theta", "vega", "rho"):
        item = values[name]
        item[~valid] = np.nan
        result[name] = item
    result["risk_free_rate"] = rate
    result["dividend_yield"] = yield_
    result["iv_repricing_error"] = np.where(valid, residual, np.nan)
    result["quality_flag"] = np.select(
        [result.spot_match.eq("MISSING"), ~base, base & ~bounds, bounds & ~valid, result.spot_match.eq("CARRIED")],
        ["MISSING_SPOT", "MISSING_MODEL_INPUT", "NO_ARBITRAGE_VIOLATION", "SOLVER_RESIDUAL_FAILURE", "CARRIED_SPOT"],
        default="OK",
    )
    return result


def fingerprint(frame: pd.DataFrame) -> str:
    values = pd.util.hash_pandas_object(frame[SOURCE_COLUMNS], index=False).to_numpy(dtype="uint64")
    digest = hashlib.sha256(values.tobytes()).hexdigest()
    return digest
