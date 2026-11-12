from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from ..models import ResearchDailyBar, ResearchEvent, ResearchFundamentalFact


D = Decimal


def validate_bar_values(bar):
    values = {key: D(str(bar[key])) for key in ("raw_open", "raw_high", "raw_low", "raw_close")}
    if any(value <= 0 for value in values.values()):
        raise ValueError("OHLC values must be positive")
    if values["raw_high"] < max(values["raw_open"], values["raw_close"], values["raw_low"]):
        raise ValueError("High is below another OHLC value")
    if values["raw_low"] > min(values["raw_open"], values["raw_close"], values["raw_high"]):
        raise ValueError("Low is above another OHLC value")
    if D(str(bar.get("volume", 0))) < 0:
        raise ValueError("Volume cannot be negative")


@transaction.atomic
def ingest_research_bar(*, instrument, provider, provider_timestamp, revision_timestamp=None, quality_status="VALID", **values):
    validate_bar_values(values)
    latest = ResearchDailyBar.objects.filter(
        instrument=instrument, trading_date=values["trading_date"]
    ).aggregate(version=Max("data_version"))["version"] or 0
    return ResearchDailyBar.objects.create(
        instrument=instrument,
        provider=provider,
        provider_timestamp=provider_timestamp,
        revision_timestamp=revision_timestamp or timezone.now(),
        data_version=latest + 1,
        quality_status=quality_status,
        **values,
    )


def latest_point_in_time_bars(instrument, *, as_of_timestamp, start_date=None, end_date=None, valid_only=True):
    """Latest revision available at a decision timestamp, never a future revision."""
    query = ResearchDailyBar.objects.filter(
        instrument=instrument,
        provider_timestamp__lte=as_of_timestamp,
        revision_timestamp__lte=as_of_timestamp,
    )
    if start_date:
        query = query.filter(trading_date__gte=start_date)
    if end_date:
        query = query.filter(trading_date__lte=end_date)
    if valid_only:
        query = query.filter(quality_status="VALID")
    rows = query.order_by("trading_date", "-data_version", "-revision_timestamp")
    seen = set()
    result = []
    for row in rows:
        if row.trading_date not in seen:
            seen.add(row.trading_date)
            result.append(row)
    return result


def available_fundamentals(issuer, *, as_of_timestamp, metric=None):
    query = ResearchFundamentalFact.objects.filter(
        issuer=issuer, public_availability_timestamp__lte=as_of_timestamp
    )
    if metric:
        query = query.filter(metric=metric)
    return query.order_by("metric", "period_end", "revision_version")


def available_events(*, as_of_timestamp, issuer=None, instrument=None, event_type=None):
    query = ResearchEvent.objects.filter(available_timestamp__lte=as_of_timestamp)
    if issuer:
        query = query.filter(Q(issuer=issuer) | Q(instrument__issuer=issuer))
    if instrument:
        query = query.filter(instrument=instrument)
    if event_type:
        query = query.filter(event_type=event_type)
    return query.order_by("available_timestamp")


def conservative_event_availability(effective_timestamp, announced_timestamp=None):
    """Unknown announcement times become available at the next weekday session open."""
    if announced_timestamp:
        return announced_timestamp
    candidate = effective_timestamp.date() + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    tz = effective_timestamp.tzinfo or timezone.get_current_timezone()
    return datetime.combine(candidate, time(9, 30), tzinfo=tz)


def adjusted_total_return_series(raw_closes, dividends=None, split_factors=None):
    """Build a forward total-return index without mutating raw-price meaning."""
    closes = [D(str(value)) for value in raw_closes]
    dividends = [D(str(value)) for value in (dividends or [0] * len(closes))]
    splits = [D(str(value)) for value in (split_factors or [1] * len(closes))]
    if not (len(closes) == len(dividends) == len(splits)):
        raise ValueError("Close, dividend, and split series lengths must match")
    if not closes:
        return []
    result = [D(1)]
    for index in range(1, len(closes)):
        if closes[index - 1] <= 0 or splits[index] <= 0:
            raise ValueError("Prices and split factors must be positive")
        comparable_previous = closes[index - 1] / splits[index]
        period_return = (closes[index] + dividends[index]) / comparable_previous
        result.append(result[-1] * period_return)
    return result


def delisting_total_return(previous_close, cash_proceeds):
    previous = D(str(previous_close))
    if previous <= 0:
        raise ValueError("Previous close must be positive")
    return D(str(cash_proceeds)) / previous - D(1)
