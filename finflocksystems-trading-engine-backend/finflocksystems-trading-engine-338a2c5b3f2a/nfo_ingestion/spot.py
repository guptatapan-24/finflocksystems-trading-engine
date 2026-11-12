"""Index spot bars for the nightly ingestion, fetched through the Backend's own read-only spot access
(`apps.market_data.nfo_spot`, which asks the paper IB Gateway's HTTP history endpoint) instead of a direct IBKR API connection.
Drop-in replacement for `nfo_pipeline.ibkr.ReadOnlyIBKRClient`, installed by run_nightly.py at runtime; `nfo_pipeline` is not edited.

Guardrails enforced here in code (`nfo_spot` enforces the window, the lock, the gap and the daily budget again on its side):
  * never call outside the idle window 16:30-08:30 IST (the strategy trades 09:00-16:00 IST) -> OutsideWindow
  * the five index requests go strictly one at a time, SPACING seconds apart
  * a failed request is retried ONCE after RETRY_WAIT seconds; if that fails too, the adapter halts and sends nothing more
  * every reply is checked before anything is downloaded or written: right index, right contract, non-empty, every bar on the
    session date, enough bars, sane prices. Any doubt -> SpotDataInvalid
"""
from __future__ import annotations

import threading
import time as _time
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")
WINDOW_OPENS, WINDOW_CLOSES = time(16, 30), time(8, 30)
# Same pinned identities as nfo_pipeline.ibkr.INDEX_REQUESTS (checked against the reply on every request).
INDEXES = {
    "NIFTY": (51497778, "NIFTY50"), "BANKNIFTY": (56994300, "BANKNIFTY"), "FINNIFTY": (468436436, "NSEFIN"),
    "MIDCPNIFTY": (657342268, "MIDCAPSEL"), "NIFTYNXT50": (279064251, "NIFTYNXT50"),
}
CANARY_FIRST = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50")
RETRYABLE_CODES = {"TOO_SOON", "BUSY", "GATEWAY_TIMEOUT", "GATEWAY_UNAVAILABLE", "THROTTLE_UNAVAILABLE", "GATEWAY_COMMAND_FAILED"}


class SpotError(RuntimeError):
    code = "spot_error"


class OutsideWindow(SpotError):
    code = "outside_window"


class SpotUnavailable(SpotError):
    """Infrastructure: the gateway or its session is not answering. Retrying later is safe."""
    code = "spot_unavailable"


class SpotDataInvalid(SpotError):
    """The reply cannot be trusted (empty, wrong date, wrong contract...). Nothing is ingested."""
    code = "spot_invalid"


def in_window(now: datetime) -> bool:
    local = now.astimezone(IST).time()
    return local >= WINDOW_OPENS or local < WINDOW_CLOSES


def backend_transport(body: dict) -> dict:
    """Ask the Backend's own spot access, in this process. Needs Django set up (the scheduler's worker process does that)."""
    from apps.market_data import nfo_spot

    try:
        return nfo_spot.fetch_spot_bars(body)
    except nfo_spot.SpotError as exc:
        label = f"{body.get('underlying')}: {exc.code}: {str(exc)[:200]}"
        if exc.code == "OUTSIDE_WINDOW":
            raise OutsideWindow(str(exc)) from None
        if exc.code == "WRONG_CONTRACT":
            raise SpotDataInvalid(label) from None
        failure = SpotUnavailable(label)
        failure.retryable = exc.code in RETRYABLE_CODES
        raise failure from None


def validate_bars(underlying: str, session_date: date, reply: dict, min_bars: int) -> list[dict]:
    """Strict, before-anything-is-written checks. Returns the parsed bars (UTC datetimes and floats)."""
    conid, symbol = INDEXES[underlying]
    if reply.get("underlying") != underlying or int(reply.get("conid") or 0) != conid or str(reply.get("symbol", "")).upper() != symbol.upper():
        raise SpotDataInvalid(f"{underlying}: the reply is for a different contract")
    if str(reply.get("session_date")) != session_date.isoformat():
        raise SpotDataInvalid(f"{underlying}: the reply is for a different session date")
    raw = reply.get("bars") or []
    if not raw:
        raise SpotDataInvalid(f"{underlying}: no bars returned for {session_date}")
    parsed = []
    for bar in raw:
        try:
            stamp = datetime.fromisoformat(str(bar["observed_at"]).replace("Z", "+00:00"))
            values = {name: float(bar[name]) for name in ("open", "high", "low", "close")}
            volume = float(bar.get("volume") or 0)
        except (KeyError, ValueError, TypeError):
            raise SpotDataInvalid(f"{underlying}: a bar is malformed") from None
        if stamp.tzinfo is None:
            raise SpotDataInvalid(f"{underlying}: a bar timestamp has no time zone")
        if stamp.astimezone(IST).date() != session_date:
            raise SpotDataInvalid(f"{underlying}: a bar falls outside session date {session_date}")
        if min(values.values()) <= 0 or values["high"] < values["low"]:
            raise SpotDataInvalid(f"{underlying}: a bar has non-positive or inconsistent prices")
        parsed.append({"observed_at": stamp, **values, "volume": volume})
    if len(parsed) < min_bars:
        raise SpotDataInvalid(f"{underlying}: only {len(parsed)} bars, expected at least {min_bars}")
    return parsed


def to_frame(underlying: str, bars: list[dict]) -> pd.DataFrame:
    """Same columns and types as ReadOnlyIBKRClient.historical_index returns."""
    frame = pd.DataFrame(bars)
    frame["observed_at"] = pd.to_datetime(frame["observed_at"], utc=True).dt.tz_convert(IST).dt.floor("min")
    frame["underlying"] = underlying
    frame["ibkr_con_id"] = INDEXES[underlying][0]
    return frame[["observed_at", "underlying", "open", "high", "low", "close", "volume", "ibkr_con_id"]].drop_duplicates("observed_at", keep="last")


class GatewaySpotClient:
    """Duck-types ReadOnlyIBKRClient for NFOIndexIngestion (connect_ready, resolve_index, historical_index, close)."""

    def __init__(self, *, transport=backend_transport, run_id, spacing=7.0, retry_wait=60.0, min_bars=300,
                 clock=lambda: datetime.now(IST), sleep=_time.sleep, log=print):
        self._transport, self.run_id = transport, run_id
        self.spacing, self.retry_wait, self.min_bars = spacing, retry_wait, min_bars
        self._clock, self._sleep, self._log = clock, sleep, log
        self._lock = threading.Lock()   # the pipeline calls historical_index from 5 threads: serialize them
        self._cache: dict[tuple[str, date], pd.DataFrame] = {}
        self._halted: SpotError | None = None
        self._last_request_at: float | None = None
        self.requests_sent = 0

    # -- the ReadOnlyIBKRClient surface the pipeline uses --------------------------------------
    def connect_ready(self, host, port, client_id, timeout):
        self._assert_window()   # nothing to connect: the first real request is the connectivity check

    def resolve_index(self, underlying, timeout=None):
        conid, symbol = INDEXES[underlying]
        return {"con_id": conid, "symbol": symbol, "sec_type": "IND", "exchange": "NSE", "currency": "INR"}

    def historical_index(self, underlying, session_date, timeout=None):
        cached = self._cache.get((underlying, session_date))
        return cached if cached is not None else self._fetch(underlying, session_date)

    def close(self):
        pass

    # -- fetching ---------------------------------------------------------------------------------
    def prefetch(self, session_date: date):
        """Fetch and validate all five underlyings (NIFTY first as a canary) BEFORE anything is downloaded or written."""
        for underlying in CANARY_FIRST:
            self._fetch(underlying, session_date)

    def _assert_window(self):
        if not in_window(self._clock()):
            raise OutsideWindow("Spot requests are only allowed 16:30-08:30 IST (the strategy trades 09:00-16:00 IST)")

    def _fetch(self, underlying, session_date):
        with self._lock:
            if (underlying, session_date) in self._cache:
                return self._cache[(underlying, session_date)]
            if self._halted:
                raise self._halted
            try:
                frame = to_frame(underlying, validate_bars(underlying, session_date, self._ask_with_retry(underlying, session_date),
                                                           self.min_bars))
            except SpotError as exc:
                self._halted = exc   # after any failure no further request is sent in this run
                raise
            self._cache[(underlying, session_date)] = frame
            return frame

    def _ask_with_retry(self, underlying, session_date):
        for attempt in (1, 2):
            self._assert_window()
            self._respect_spacing()
            try:
                return self._ask(underlying, session_date, attempt)
            except SpotUnavailable as exc:
                if attempt == 2 or not getattr(exc, "retryable", True):
                    raise
                self._log(f"{underlying}: {exc}; retrying once in {self.retry_wait:g}s")
                self._sleep(self.retry_wait)

    def _respect_spacing(self):
        if self._last_request_at is not None:
            wait = self.spacing - (_time.monotonic() - self._last_request_at)
            if wait > 0:
                self._sleep(wait)

    def _ask(self, underlying, session_date, attempt):
        body = {"underlying": underlying, "session_date": session_date.isoformat(), "run_id": self.run_id, "attempt": attempt}
        self._last_request_at = _time.monotonic()
        self.requests_sent += 1
        return self._transport(body)


def audit_gateway_adapter(client) -> int:
    """Stands in for nfo_pipeline.ibkr.audit_api_surface: this adapter has no order, account or subscription method at all."""
    forbidden = [name for name in dir(client)
                 if name.startswith(("req", "cancel", "place", "exercise", "replace", "subscribe")) and callable(getattr(client, name))]
    if forbidden:
        raise SpotError(f"Unsafe methods present on the spot adapter: {forbidden}")
    return 1
