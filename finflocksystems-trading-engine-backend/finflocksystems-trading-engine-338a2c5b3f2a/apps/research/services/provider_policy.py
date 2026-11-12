from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderPolicy:
    primary: str
    fallbacks: tuple[str, ...]
    reconciliation_tolerance: float
    staleness_seconds: int
    required_subscription: str
    quality_rules: tuple[str, ...]


PROVIDER_POLICIES = {
    "daily_prices": ProviderPolicy("FINNHUB", ("IBKR_ADJUSTED_LAST", "IBKR_TRADES"), 0.01, 7 * 86400, "US_EQUITIES", ("POSITIVE_OHLC", "MONOTONIC_TIME", "CORPORATE_ACTION_RECONCILIATION")),
    "intraday_prices": ProviderPolicy("FINNHUB", ("IBKR_TRADES",), 0.005, 900, "REALTIME_US_EQUITIES", ("POSITIVE_OHLC", "SESSION_BOUNDS", "MONOTONIC_TIME")),
    "fundamentals": ProviderPolicy("FINNHUB", (), 0.0, 7 * 86400, "COMPANY_FUNDAMENTALS", ("PUBLIC_AVAILABILITY_REQUIRED", "REVISION_VERSIONED")),
    "analyst": ProviderPolicy("FINNHUB", (), 0.0, 2 * 86400, "ESTIMATES", ("PUBLIC_AVAILABILITY_REQUIRED", "REVISION_VERSIONED")),
    "events": ProviderPolicy("FINNHUB", (), 0.0, 3600, "CORPORATE_EVENTS", ("ANNOUNCED_TIME_REQUIRED", "PUBLIC_AVAILABILITY_REQUIRED")),
}


def provider_policy(data_family):
    try:
        return PROVIDER_POLICIES[data_family]
    except KeyError as exc:
        raise ValueError(f"No provider policy for {data_family}") from exc
