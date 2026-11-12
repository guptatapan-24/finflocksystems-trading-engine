from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class ProviderErrorCode(StrEnum):
    IBKR_DISCONNECTED = "IBKR_DISCONNECTED"
    IBKR_TIMEOUT = "IBKR_TIMEOUT"
    IBKR_ENTITLEMENT = "IBKR_ENTITLEMENT"
    IBKR_NO_DATA = "IBKR_NO_DATA"
    IBKR_PACING = "IBKR_PACING"
    IBKR_TEMPORARY = "IBKR_TEMPORARY"
    FINNHUB_RATE_LIMITED = "FINNHUB_RATE_LIMITED"
    FINNHUB_UNAVAILABLE = "FINNHUB_UNAVAILABLE"
    FINNHUB_NO_DATA = "FINNHUB_NO_DATA"
    FINNHUB_MAPPING_INVALID = "FINNHUB_MAPPING_INVALID"
    UNSUPPORTED_INSTRUMENT = "UNSUPPORTED_INSTRUMENT"


class ProviderError(RuntimeError):
    def __init__(self, message, *, code, provider, status_code=503, retryable=False, details=None):
        super().__init__(message)
        self.code = str(code)
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable
        self.details = details or {}


@dataclass(frozen=True)
class ProviderCandle:
    window_start: datetime
    window_end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class ProviderQuote:
    price: Decimal
    event_time: datetime
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    previous_close: Decimal | None = None
