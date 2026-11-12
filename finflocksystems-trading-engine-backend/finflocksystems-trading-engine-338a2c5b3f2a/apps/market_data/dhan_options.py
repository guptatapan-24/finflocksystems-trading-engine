"""Small, read-only Dhan option-chain adapter for strategy selection.

Dhan supplies OI, IV and Greeks. It is never used for order routing: every
selected contract is independently resolved and qualified through IBKR before
the strategy can emit a target.
"""

from datetime import date
from decimal import Decimal, InvalidOperation
import logging
import time

import requests
from django.conf import settings
from django.utils import timezone


logger = logging.getLogger(__name__)


class DhanOptionsError(RuntimeError):
    retryable = False


class DhanOptionsTemporaryError(DhanOptionsError):
    retryable = True


UNDERLYINGS = {
    "NIFTY": (13, "IDX_I"),
    "NIFTY50": (13, "IDX_I"),
    "BANKNIFTY": (25, "IDX_I"),
    "FINNIFTY": (27, "IDX_I"),
    "MIDCPNIFTY": (44, "IDX_I"),
    "SENSEX": (51, "IDX_I"),
    "BANKEX": (52, "IDX_I"),
}


def _decimal(value):
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _integer(value):
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _iv(value):
    result = _decimal(value)
    if result is None:
        return None
    return result / Decimal("100") if result > Decimal("3") else result


def _unwrap(payload):
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if isinstance(data, dict) and isinstance(data.get("data"), (dict, list)):
        data = data["data"]
    return data


def _rate_limited(payload):
    if not isinstance(payload, dict):
        return False
    detail = payload.get("remarks") or payload.get("data") or payload
    if not isinstance(detail, dict):
        detail = payload
    code = str(
        detail.get("error_code") or detail.get("errorCode")
        or payload.get("error_code") or payload.get("errorCode") or ""
    ).upper()
    error_type = str(
        detail.get("error_type") or detail.get("errorType")
        or payload.get("error_type") or payload.get("errorType") or ""
    ).lower()
    return code == "DH-904" or "rate_limit" in error_type or "rate limit" in error_type


class DhanOptionChainClient:
    RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

    def __init__(self, http=None, sleeper=None):
        self.http = http or requests.Session()
        self.sleeper = sleeper or time.sleep

    @property
    def configured(self):
        return bool(
            settings.DHAN_OPTIONS_ENABLED
            and settings.DHAN_CLIENT_ID
            and settings.DHAN_ACCESS_TOKEN
        )

    def _identity(self, instrument, parameters):
        symbol = str(instrument.symbol or "").replace(" ", "").upper()
        security_id = str(parameters.get("dhan_security_id") or "").strip()
        segment = str(parameters.get("dhan_segment") or "").strip().upper()
        if security_id and segment:
            return int(security_id), segment
        if symbol not in UNDERLYINGS:
            raise DhanOptionsError(
                f"No Dhan option underlying mapping for {symbol}; set dhan_security_id and dhan_segment"
            )
        return UNDERLYINGS[symbol]

    def _post(self, path, body):
        if not self.configured:
            raise DhanOptionsError(
                "Dhan automatic option data is disabled or DHAN_CLIENT_ID/DHAN_ACCESS_TOKEN is missing"
            )
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "access-token": settings.DHAN_ACCESS_TOKEN,
            "client-id": settings.DHAN_CLIENT_ID,
        }
        maximum_attempts = max(1, int(settings.DHAN_MAX_ATTEMPTS))
        url = f"{settings.DHAN_API_BASE_URL}{path}"
        for attempt in range(1, maximum_attempts + 1):
            try:
                response = self.http.post(
                    url, json=body, headers=headers,
                    timeout=settings.DHAN_REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as exc:
                if attempt >= maximum_attempts:
                    raise DhanOptionsTemporaryError(
                        f"Dhan {path} is temporarily unavailable after {attempt} attempts"
                    ) from exc
                self._wait_before_retry(path, attempt)
                continue
            try:
                payload = response.json()
            except ValueError as exc:
                if response.status_code in self.RETRYABLE_STATUSES and attempt < maximum_attempts:
                    self._wait_before_retry(path, attempt, response)
                    continue
                error = DhanOptionsTemporaryError if response.status_code in self.RETRYABLE_STATUSES else DhanOptionsError
                raise error(f"Dhan {path} returned non-JSON HTTP {response.status_code}") from exc
            retryable = response.status_code in self.RETRYABLE_STATUSES or _rate_limited(payload)
            if retryable:
                if attempt >= maximum_attempts:
                    raise DhanOptionsTemporaryError(
                        f"Dhan {path} remained unavailable after {attempt} attempts"
                    )
                self._wait_before_retry(path, attempt, response)
                continue
            if not response.ok or (
                isinstance(payload, dict)
                and str(payload.get("status", "")).lower() == "failure"
            ):
                detail = (
                    payload.get("errorMessage") or payload.get("remarks")
                    or payload.get("errorType") or "request failed"
                )
                raise DhanOptionsError(f"Dhan {path} failed: {detail}")
            return _unwrap(payload)
        raise AssertionError("Dhan option-chain retry loop ended unexpectedly")

    def _wait_before_retry(self, path, attempt, response=None):
        retry_after = response.headers.get("Retry-After") if response is not None else None
        try:
            delay = float(retry_after) if retry_after is not None else None
        except (TypeError, ValueError):
            delay = None
        if delay is None:
            delay = float(settings.DHAN_RETRY_BASE_DELAY_SECONDS) * (2 ** (attempt - 1))
        delay = min(float(settings.DHAN_RETRY_MAX_DELAY_SECONDS), max(0.0, delay))
        logger.warning(
            "Dhan %s request is retrying in %.1f seconds after attempt %s",
            path, delay, attempt,
        )
        self.sleeper(delay)

    def expiries(self, instrument, parameters):
        security_id, segment = self._identity(instrument, parameters)
        data = self._post(
            "/optionchain/expirylist",
            {"UnderlyingScrip": security_id, "UnderlyingSeg": segment},
        )
        if not isinstance(data, list):
            raise DhanOptionsError("Dhan returned an invalid option expiry list")
        result = []
        for value in data:
            try:
                result.append(date.fromisoformat(str(value)[:10]))
            except ValueError:
                continue
        return sorted(set(result))

    def chain(self, instrument, parameters, expiration):
        security_id, segment = self._identity(instrument, parameters)
        data = self._post(
            "/optionchain",
            {
                "UnderlyingScrip": security_id,
                "UnderlyingSeg": segment,
                "Expiry": expiration.isoformat(),
            },
        )
        if not isinstance(data, dict) or not isinstance(data.get("oc"), dict):
            raise DhanOptionsError("Dhan returned an invalid option chain")
        observed_at = timezone.now()
        rows = []
        for strike_value, pair in data["oc"].items():
            strike = _decimal(strike_value)
            if strike is None:
                continue
            for key, right in (("ce", "C"), ("pe", "P")):
                raw = pair.get(key) or {}
                security_id = str(raw.get("security_id") or "").strip()
                if not raw or not security_id:
                    continue
                bid = _decimal(raw.get("top_bid_price", raw.get("bid_price")))
                ask = _decimal(raw.get("top_ask_price", raw.get("ask_price")))
                ltp = _decimal(raw.get("last_price"))
                mid = (bid + ask) / Decimal(2) if bid and ask and ask >= bid else ltp
                greeks = raw.get("greeks") or {}
                rows.append({
                    "security_id": security_id,
                    "expiration": expiration,
                    "strike": strike,
                    "right": right,
                    "bid": bid,
                    "ask": ask,
                    "price": mid,
                    "iv": _iv(raw.get("implied_volatility")),
                    "oi": _integer(raw.get("oi", raw.get("open_interest"))),
                    "volume": _integer(raw.get("volume")),
                    "delta": _decimal(greeks.get("delta")),
                })
        spot = _decimal(data.get("last_price"))
        if spot is None or spot <= 0 or not rows:
            raise DhanOptionsError("Dhan returned no usable live option observations")
        return {
            "provider": "DHAN", "spot": spot, "expiration": expiration,
            "observed_at": observed_at, "rows": rows,
        }
