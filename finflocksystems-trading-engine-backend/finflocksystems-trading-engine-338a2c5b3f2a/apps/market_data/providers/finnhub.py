import base64
import hashlib
import time
from datetime import datetime, time as day_time, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

import requests
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.utils import timezone

from apps.market_data.models import MarketDataProviderConfiguration

from .base import ProviderCandle, ProviderError, ProviderErrorCode, ProviderQuote


class FinnhubError(ProviderError):
    def __init__(self, message, *, code="FINNHUB_UNAVAILABLE", status_code=503, retryable=False, details=None):
        super().__init__(message, code=code, provider="FINNHUB", status_code=status_code,
                         retryable=retryable, details=details)


def _fernet():
    secret = settings.FINNHUB_ENCRYPTION_KEY or settings.SECRET_KEY
    try:
        return Fernet(secret.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest()))


def encrypt_api_key(api_key):
    value = str(api_key or "").strip()
    if not value:
        raise ValueError("Finnhub API key cannot be empty")
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_api_key(encrypted_api_key):
    try:
        return _fernet().decrypt(encrypted_api_key.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError) as exc:
        raise FinnhubError("Stored Finnhub credential cannot be decrypted", code="FINNHUB_CREDENTIAL_INVALID") from exc


def effective_api_key():
    config = MarketDataProviderConfiguration.objects.filter(provider="FINNHUB").first()
    environment_key = settings.FINNHUB_API_KEY.strip()
    if config and not config.enabled:
        raise FinnhubError("Finnhub provider is disabled", code="FINNHUB_DISABLED", status_code=400)
    database_allowed = bool(
        config and config.enabled and config.encrypted_api_key
        and (not environment_key or (settings.FINNHUB_API_KEY_OVERRIDE_ENABLED and config.override_environment))
    )
    if database_allowed:
        return decrypt_api_key(config.encrypted_api_key), "DATABASE", config
    if environment_key:
        return environment_key, "ENVIRONMENT", config
    if config and config.enabled and config.encrypted_api_key:
        return decrypt_api_key(config.encrypted_api_key), "DATABASE", config
    raise FinnhubError("Finnhub API key is not configured", code="FINNHUB_NOT_CONFIGURED", status_code=400)


def provider_status():
    config = MarketDataProviderConfiguration.objects.filter(provider="FINNHUB").first()
    environment_key = bool(settings.FINNHUB_API_KEY.strip())
    database_key = bool(config and config.encrypted_api_key)
    database_override_active = bool(
        environment_key and database_key and config.enabled and config.override_environment
        and settings.FINNHUB_API_KEY_OVERRIDE_ENABLED
    )
    if config and not config.enabled:
        source = "NONE"
    else:
        source = ("DATABASE" if database_key and (not environment_key or database_override_active)
                  else "ENVIRONMENT" if environment_key else "NONE")
    last_four = (settings.FINNHUB_API_KEY[-4:] if source == "ENVIRONMENT" and environment_key
                 else config.api_key_last_four if config and database_key else "")
    return {
        "provider": "FINNHUB", "configured": environment_key or database_key,
        "enabled": config.enabled if config else True, "effective_source": source,
        "environment_configured": environment_key, "database_configured": database_key,
        "database_override_requested": config.override_environment if config else False,
        "database_override_allowed": settings.FINNHUB_API_KEY_OVERRIDE_ENABLED,
        "database_override_active": database_override_active,
        "masked_api_key": f"\u2022\u2022\u2022\u2022{last_four}" if last_four else "",
        "last_success_at": config.last_success_at if config else None,
        "last_tested_at": config.last_tested_at if config else None,
        "last_test_success_at": config.last_test_success_at if config else None,
        "last_error": config.last_error if config else "",
        "rate_limit_state": config.rate_limit_state if config else {},
        "updated_at": config.updated_at if config else None,
    }


def _positive_decimal(value, field):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FinnhubError(f"Finnhub returned an invalid {field}", code="FINNHUB_INVALID_RESPONSE") from exc
    if not result.is_finite() or result <= 0:
        raise FinnhubError(f"Finnhub returned an invalid {field}", code="FINNHUB_INVALID_RESPONSE")
    return result


def _nonnegative_decimal(value, field):
    try:
        result=Decimal(str(value))
    except (InvalidOperation,TypeError,ValueError) as exc:
        raise FinnhubError(f"Finnhub returned an invalid {field}",code="FINNHUB_INVALID_RESPONSE") from exc
    if not result.is_finite() or result<0:
        raise FinnhubError(f"Finnhub returned an invalid {field}",code="FINNHUB_INVALID_RESPONSE")
    return result


class FinnhubClient:
    RESOLUTIONS = {"1m": ("1", 60), "5m": ("5", 300), "15m": ("15", 900),
                   "1h": ("60", 3600), "1d": ("D", 86400)}

    def __init__(self, *, api_key=None, base_url=None, session=None):
        if api_key:
            self.api_key, self.source, self.configuration = api_key, "TRANSIENT", None
        else:
            self.api_key, self.source, self.configuration = effective_api_key()
        self.base_url = (base_url or settings.FINNHUB_BASE_URL).rstrip("/")
        self.session = session or requests.Session()
        self.last_response_metadata = {}

    def _safe_text(self, value):
        return str(value).replace(self.api_key, "[REDACTED]") if self.api_key else str(value)

    def _record_state(self, response=None, error="", tested=False):
        config, _ = MarketDataProviderConfiguration.objects.get_or_create(provider="FINNHUB")
        fields = ["last_error", "rate_limit_state", "updated_at"]
        safe_error = self._safe_text(error)
        config.last_error = safe_error[:2000]
        metadata = {"error": safe_error[:2000]} if error else {}
        if tested:
            config.last_tested_at = timezone.now()
            fields.append("last_tested_at")
            if not error and (response is None or response.ok):
                config.last_test_success_at = config.last_tested_at
                fields.append("last_test_success_at")
        if response is not None:
            rate_limit_state = {
                "limit": response.headers.get("X-Ratelimit-Limit", ""),
                "remaining": response.headers.get("X-Ratelimit-Remaining", ""),
                "reset": response.headers.get("X-Ratelimit-Reset", ""),
            }
            config.rate_limit_state = rate_limit_state
            metadata.update({"status_code": response.status_code, "rate_limit": rate_limit_state})
            if response.ok:
                config.last_success_at = timezone.now()
                fields.append("last_success_at")
        if metadata:
            self.last_response_metadata = metadata
        config.save(update_fields=list(dict.fromkeys(fields)))

    def get(self, path, params=None, *, tested=False):
        last_error = None
        for attempt in range(settings.FINNHUB_MAX_RETRIES + 1):
            try:
                response = self.session.get(
                    f"{self.base_url}/{path.lstrip('/')}", params=params or {},
                    headers={"X-Finnhub-Token": self.api_key, "Accept": "application/json"},
                    timeout=settings.FINNHUB_REQUEST_TIMEOUT_SECONDS,
                )
                if response.status_code == 429:
                    last_error = FinnhubError("Finnhub rate limit exceeded", code=ProviderErrorCode.FINNHUB_RATE_LIMITED,
                                              status_code=429, retryable=True)
                elif response.status_code >= 500:
                    last_error = FinnhubError("Finnhub is temporarily unavailable",
                                              code=ProviderErrorCode.FINNHUB_UNAVAILABLE, retryable=True)
                elif not response.ok:
                    last_error = FinnhubError(f"Finnhub rejected the request ({response.status_code})",
                                              code="FINNHUB_REQUEST_REJECTED", status_code=400)
                else:
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        raise FinnhubError("Finnhub returned invalid JSON", code="FINNHUB_INVALID_RESPONSE") from exc
                    if isinstance(payload, dict) and payload.get("error"):
                        raise FinnhubError(self._safe_text(payload["error"]), code="FINNHUB_REQUEST_REJECTED", status_code=400)
                    self._record_state(response, tested=tested)
                    return payload
                self._record_state(response, last_error, tested=tested)
                if not last_error.retryable or attempt >= settings.FINNHUB_MAX_RETRIES:
                    raise last_error
            except requests.RequestException as exc:
                last_error = FinnhubError("Unable to reach Finnhub", code=ProviderErrorCode.FINNHUB_UNAVAILABLE,
                                          retryable=True)
                self._record_state(error=last_error, tested=tested)
                if attempt >= settings.FINNHUB_MAX_RETRIES:
                    raise last_error from exc
            time.sleep(min(0.2 * (2 ** attempt), 1.0))
        raise last_error

    def test_connection(self, symbol="AAPL"):
        try:
            payload = self.get("quote", {"symbol": symbol})
        except FinnhubError as exc:
            self._record_state(error=exc, tested=True)
            raise
        if not isinstance(payload, dict) or not payload.get("c"):
            error = FinnhubError("Finnhub quote test returned no current price", code="FINNHUB_TEST_FAILED",
                                 status_code=400)
            self._record_state(error=error, tested=True)
            raise error
        self._record_state(tested=True)
        return {"connected": True, "symbol": symbol, "source": self.source}

    def capabilities(self, asset_class, timeframe):
        return str(asset_class).upper() in settings.FINNHUB_SUPPORTED_ASSET_CLASSES and timeframe in self.RESOLUTIONS

    def search_symbols(self, query):
        payload = self.get("search", {"q": str(query).strip()})
        rows = payload.get("result", []) if isinstance(payload, dict) else []
        return [{
            "provider_symbol": str(row.get("symbol") or "").strip(),
            "display_symbol": str(row.get("displaySymbol") or "").strip(),
            "description": str(row.get("description") or "").strip(),
            "type": str(row.get("type") or "").strip(),
            "currency": str(row.get("currency") or "").strip().upper(),
            "mic": str(row.get("mic") or "").strip().upper(),
            "figi": str(row.get("figi") or "").strip(),
            "isin": str(row.get("isin") or "").strip(),
            "raw": row,
        } for row in rows if row.get("symbol")]

    def profile(self, symbol):
        payload = self.get("stock/profile2", {"symbol": symbol})
        if not isinstance(payload, dict) or not payload:
            raise FinnhubError("Finnhub returned no company profile", code=ProviderErrorCode.FINNHUB_MAPPING_INVALID,
                               status_code=400)
        return {
            "provider_symbol": str(payload.get("ticker") or symbol).strip(),
            "currency": str(payload.get("currency") or "").strip().upper(),
            "provider_exchange": str(payload.get("exchange") or "").strip(),
            "country": str(payload.get("country") or "").strip().upper(),
            "isin": str(payload.get("isin") or "").strip(),
            "figi": "",
            "raw": payload,
        }

    def historical_candles(self, symbol, timeframe, start, end):
        if timeframe not in self.RESOLUTIONS:
            raise FinnhubError(f"Finnhub historical resolution {timeframe} is unsupported",
                               code=ProviderErrorCode.UNSUPPORTED_INSTRUMENT, status_code=400)
        resolution, seconds = self.RESOLUTIONS[timeframe]
        start_dt = start if isinstance(start, datetime) else datetime.fromtimestamp(int(start), tz=dt_timezone.utc)
        end_dt = end if isinstance(end, datetime) else datetime.fromtimestamp(int(end), tz=dt_timezone.utc)
        if end_dt < start_dt:
            raise FinnhubError("Finnhub candle range is invalid", code="FINNHUB_INVALID_RESPONSE", status_code=400)
        maximum_span = timedelta(days=30) if timeframe != "1d" else end_dt - start_dt
        cursor = start_dt
        payloads = []
        while cursor <= end_dt:
            chunk_end = min(end_dt, cursor + maximum_span)
            payloads.append(self.get("stock/candle", {"symbol": symbol, "resolution": resolution,
                "from": int(cursor.timestamp()), "to": int(chunk_end.timestamp())}))
            if chunk_end >= end_dt:
                break
            cursor = chunk_end + timedelta(seconds=1)
        rows = {}
        for payload in payloads:
            if payload.get("s") == "no_data":
                continue
            if payload.get("s") != "ok":
                raise FinnhubError("Finnhub candle response was not usable", code="FINNHUB_INVALID_RESPONSE")
            keys = ["t", "o", "h", "l", "c", "v"]
            lengths = {len(payload.get(key, [])) for key in keys}
            if len(lengths) != 1:
                raise FinnhubError("Finnhub candle arrays have inconsistent lengths", code="FINNHUB_INVALID_RESPONSE")
            for index in range(next(iter(lengths), 0)):
                window_start = datetime.fromtimestamp(payload["t"][index], tz=dt_timezone.utc)
                open_price=_positive_decimal(payload["o"][index],"open")
                high=_positive_decimal(payload["h"][index],"high")
                low=_positive_decimal(payload["l"][index],"low")
                close=_positive_decimal(payload["c"][index],"close")
                if low>min(open_price,close) or high<max(open_price,close) or low>high:
                    raise FinnhubError("Finnhub returned inconsistent OHLC values",code="FINNHUB_INVALID_RESPONSE")
                rows[window_start] = ProviderCandle(
                    window_start=window_start, window_end=window_start + timedelta(seconds=seconds),
                    open=open_price,high=high,low=low,close=close,
                    volume=_nonnegative_decimal(payload["v"][index],"volume"),
                )
        return [rows[key] for key in sorted(rows)]

    def daily_candles(self, symbol, start_date, end_date):
        start = datetime.combine(start_date, day_time.min, tzinfo=dt_timezone.utc)
        end = datetime.combine(end_date, day_time.max, tzinfo=dt_timezone.utc)
        return [{
            "trading_date": row.window_start.date(), "open": row.open, "high": row.high, "low": row.low,
            "close": row.close, "adjusted_close": row.close, "volume": row.volume,
            "provider_timestamp": row.window_start,
        } for row in self.historical_candles(symbol, "1d", start, end)]

    def corporate_actions(self, symbol, start_date, end_date):
        common={"symbol":symbol,"from":start_date.isoformat(),"to":end_date.isoformat()}
        dividends=self.get("stock/dividend",common)
        splits=self.get("stock/split",common)
        if not isinstance(dividends,list) or not isinstance(splits,list):
            raise FinnhubError("Finnhub corporate-action response was not usable",code="FINNHUB_INVALID_RESPONSE")
        rows=[]
        for item in dividends:
            amount=_nonnegative_decimal(item.get("amount",0),"dividend amount")
            if not item.get("date"):raise FinnhubError("Finnhub dividend date is missing",code="FINNHUB_INVALID_RESPONSE")
            rows.append({"action_type":"DIVIDEND","effective_date":str(item["date"]),
                         "announced_date":item.get("declarationDate") or None,
                         "payload":{"amount":str(amount),"currency":item.get("currency") or "",
                                    "payment_date":item.get("payDate") or item.get("paymentDate"),
                                    "record_date":item.get("recordDate"),"provider_payload":item}})
        for item in splits:
            if not item.get("date"):raise FinnhubError("Finnhub split date is missing",code="FINNHUB_INVALID_RESPONSE")
            numerator=_positive_decimal(item.get("toFactor"),"split numerator")
            denominator=_positive_decimal(item.get("fromFactor"),"split denominator")
            rows.append({"action_type":"SPLIT","effective_date":str(item["date"]),"announced_date":None,
                         "payload":{"factor":str(numerator/denominator),"from_factor":str(denominator),
                                    "to_factor":str(numerator),"provider_payload":item}})
        return sorted(rows,key=lambda item:(item["effective_date"],item["action_type"]))

    def basic_financials(self, symbol):
        payload = self.get("stock/metric", {"symbol": symbol, "metric": "all"})
        if not isinstance(payload, dict):
            raise FinnhubError("Finnhub basic-financial response was not usable", code="FINNHUB_INVALID_RESPONSE")
        return payload

    def reported_financials(self, symbol):
        payload = self.get("stock/financials-reported", {"symbol": symbol})
        if not isinstance(payload, dict):
            raise FinnhubError("Finnhub reported-financial response was not usable", code="FINNHUB_INVALID_RESPONSE")
        return list(payload.get("data") or [])

    def recommendation_trends(self, symbol):
        payload = self.get("stock/recommendation", {"symbol": symbol})
        if not isinstance(payload, list):
            raise FinnhubError("Finnhub recommendation response was not usable", code="FINNHUB_INVALID_RESPONSE")
        return payload

    def earnings_estimates(self, symbol, *, frequency="quarterly"):
        payload = self.get("stock/eps-estimate", {"symbol": symbol, "freq": frequency})
        if not isinstance(payload, dict):
            raise FinnhubError("Finnhub estimate response was not usable", code="FINNHUB_INVALID_RESPONSE")
        return list(payload.get("data") or [])

    def earnings_calendar(self, symbol, start_date, end_date):
        payload = self.get("calendar/earnings", {
            "symbol": symbol, "from": start_date.isoformat(), "to": end_date.isoformat(),
        })
        if not isinstance(payload, dict):
            raise FinnhubError("Finnhub earnings-calendar response was not usable", code="FINNHUB_INVALID_RESPONSE")
        return list(payload.get("earningsCalendar") or [])

    def quote(self, symbol):
        payload = self.get("quote", {"symbol": symbol})
        if not isinstance(payload, dict) or not payload.get("c"):
            raise FinnhubError("Finnhub returned no reference price", code=ProviderErrorCode.FINNHUB_NO_DATA,
                               status_code=404)
        event_time = datetime.fromtimestamp(int(payload.get("t") or timezone.now().timestamp()), tz=dt_timezone.utc)

        def optional(key):
            return _positive_decimal(payload[key],key) if payload.get(key) not in (None,"",0) else None

        return ProviderQuote(price=_positive_decimal(payload["c"], "current price"), event_time=event_time,
                             open=optional("o"), high=optional("h"), low=optional("l"),
                             previous_close=optional("pc"))
