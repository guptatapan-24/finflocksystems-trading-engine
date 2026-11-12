from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from ibapi.client import EClient
from ibapi.contract import Contract
from ibapi.wrapper import EWrapper


IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")
INFO_CODES = {2104, 2106, 2107, 2108, 2158}
INDEX_REQUESTS = {
    "NIFTY": {"symbol": "NIFTY50", "con_id": 51497778},
    "BANKNIFTY": {"symbol": "BANKNIFTY", "con_id": 56994300},
    # IBKR's own symbol for each of these differs from NSE's ticker (same as
    # NIFTY itself being "NIFTY50" on IBKR) -- resolved and verified via
    # reqContractDetails through trading/management/commands/
    # resolve_ibkr_index_contract.py, cross-checked against TWS's own
    # contract search UI before pinning (see that command's docstring for why
    # this is pinned once by hand rather than looked up live on every run).
    "FINNIFTY": {"symbol": "NSEFIN", "con_id": 468436436},
    "MIDCPNIFTY": {"symbol": "MIDCAPSEL", "con_id": 657342268},
    "NIFTYNXT50": {"symbol": "NIFTYNXT50", "con_id": 279064251},
}

# The IB API logs wire messages and account identifiers at INFO. Ingestion only
# needs actionable warnings/errors; protocol dumps also corrupt the live bar.
logging.getLogger("ibapi.client").setLevel(logging.WARNING)
logging.getLogger("ibapi.wrapper").setLevel(logging.WARNING)


class SafetyViolation(RuntimeError):
    pass


@dataclass
class Pending:
    event: threading.Event = field(default_factory=threading.Event)
    rows: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)


class ReadOnlyIBKRClient(EWrapper, EClient):
    """Small synchronous facade with every non-market request blocked."""

    ALLOWED = frozenset({"reqHistoricalData", "cancelHistoricalData"})

    def __init__(self) -> None:
        EWrapper.__init__(self)
        EClient.__init__(self, self)
        self.ready = threading.Event()
        self.lock = threading.Lock()
        self.next_request_id = 30_000
        self.pending: dict[int, Pending] = {}
        self.thread: threading.Thread | None = None

    def __getattribute__(self, name):
        if (
            name.startswith(("req", "cancel", "place", "exercise", "replace"))
            and name in EClient.__dict__
            and name not in ReadOnlyIBKRClient.ALLOWED
        ):
            raise SafetyViolation(f"Blocked non-market TWS method: {name}")
        return super().__getattribute__(name)

    def nextValidId(self, orderId):  # noqa: N802
        self.ready.set()

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson="", *args):  # noqa: N802
        pending = self.pending.get(reqId)
        if pending is None:
            return
        pending.errors.append({"code": errorCode, "message": str(errorString)})
        if errorCode not in INFO_CODES:
            pending.event.set()

    def contractDetails(self, reqId, details):  # noqa: N802
        pending = self.pending.get(reqId)
        if pending is not None:
            contract = details.contract
            pending.rows.append({
                "con_id": int(contract.conId), "symbol": contract.symbol,
                "sec_type": contract.secType, "exchange": contract.exchange,
                "currency": contract.currency,
            })

    def contractDetailsEnd(self, reqId):  # noqa: N802
        self._finish(reqId)

    def historicalData(self, reqId, bar):  # noqa: N802
        pending = self.pending.get(reqId)
        if pending is not None:
            pending.rows.append({
                "date": bar.date,
                "open": float(bar.open), "high": float(bar.high),
                "low": float(bar.low), "close": float(bar.close),
                "volume": float(getattr(bar, "volume", 0) or 0),
            })

    def historicalDataEnd(self, reqId, start, end):  # noqa: N802
        self._finish(reqId)

    def _allocate(self) -> tuple[int, Pending]:
        with self.lock:
            request_id = self.next_request_id
            self.next_request_id += 1
            pending = Pending()
            self.pending[request_id] = pending
        return request_id, pending

    def _finish(self, request_id: int) -> None:
        if request_id in self.pending:
            self.pending[request_id].event.set()

    def _wait(self, request_id: int, pending: Pending, timeout: float, historical=False) -> Pending:
        if not pending.event.wait(timeout):
            if historical:
                EClient.cancelHistoricalData(self, request_id)
            pending.errors.append({"code": "TIMEOUT", "message": f"Timed out after {timeout}s"})
        self.pending.pop(request_id, None)
        return pending

    def connect_ready(self, host: str, port: int, client_id: int, timeout: float) -> None:
        self.connect(host, port, clientId=client_id)
        self.thread = threading.Thread(target=self.run, name="nfo-ibkr-reader", daemon=True)
        self.thread.start()
        if not self.ready.wait(timeout):
            self.close()
            raise TimeoutError("TWS/Gateway did not complete the API handshake")

    def resolve_index(self, underlying: str, timeout: float | None = None) -> dict[str, Any]:
        """Return the audited, pinned NSE index identity without a sec-def request.

        TWS can accept API and historical-data requests while indefinitely
        delaying reqContractDetails. These stable conIds are therefore part of
        the ingestion model profile and historical requests validate them by
        either returning the requested session or failing closed.
        """
        try:
            spec = INDEX_REQUESTS[underlying]
        except KeyError:
            raise ValueError(f"Unsupported IBKR index: {underlying}") from None
        return {
            "con_id": spec["con_id"], "symbol": spec["symbol"],
            "sec_type": "IND", "exchange": "NSE", "currency": "INR",
        }

    def historical_index(self, underlying: str, session_date: date, timeout: float) -> pd.DataFrame:
        resolved = self.resolve_index(underlying, timeout)
        contract = Contract()
        contract.conId = resolved["con_id"]
        contract.symbol = resolved["symbol"]
        contract.secType = "IND"
        contract.exchange = "NSE"
        contract.currency = "INR"
        end_local = datetime.combine(session_date + timedelta(days=1), datetime.min.time(), IST)
        request_id, pending = self._allocate()
        EClient.reqHistoricalData(
            self, request_id, contract, end_local.astimezone(UTC).strftime("%Y%m%d-%H:%M:%S"),
            "1 D", "1 min", "TRADES", 0, 2, False, [],
        )
        result = self._wait(request_id, pending, timeout, historical=True)
        if not result.rows:
            raise RuntimeError(f"IBKR returned no {underlying} bars for {session_date}: {result.errors}")
        frame = pd.DataFrame(result.rows)
        numeric = pd.to_numeric(frame["date"], errors="coerce")
        if numeric.notna().all():
            timestamp = pd.to_datetime(numeric.astype("int64"), unit="s", utc=True).dt.tz_convert(IST)
        else:
            timestamp = pd.to_datetime(frame["date"], errors="coerce", utc=True).dt.tz_convert(IST)
        frame["observed_at"] = timestamp.dt.floor("min")
        frame = frame[frame.observed_at.dt.date.eq(session_date)].dropna(subset=["observed_at", "close"])
        if frame.empty:
            raise RuntimeError(f"IBKR returned no in-session {underlying} bars for {session_date}")
        frame["underlying"] = underlying
        frame["ibkr_con_id"] = resolved["con_id"]
        return frame[["observed_at", "underlying", "open", "high", "low", "close", "volume", "ibkr_con_id"]].drop_duplicates("observed_at", keep="last")

    def close(self) -> None:
        if self.isConnected():
            self.disconnect()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)


def audit_api_surface(client: ReadOnlyIBKRClient) -> int:
    checked = 0
    for name in EClient.__dict__:
        if name.startswith(("req", "cancel", "place", "exercise", "replace")) and name not in client.ALLOWED:
            try:
                getattr(client, name)
            except SafetyViolation:
                checked += 1
            else:
                raise SafetyViolation(f"Unsafe IBKR method remains accessible: {name}")
    if checked == 0:
        raise SafetyViolation("The installed IBKR API surface was not audited")
    return checked
