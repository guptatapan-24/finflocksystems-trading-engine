from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from apps.instruments.models import InstrumentProviderMapping
from apps.market_data.providers.finnhub import FinnhubClient

from ..models import ResearchAnalystFact, ResearchEvent, ResearchFundamentalFact


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _timestamp(value, *, default=None):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt_timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=dt_timezone.utc)
    if value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_timezone.utc)
        except ValueError:
            try:
                return datetime.combine(date.fromisoformat(str(value)[:10]), time.min, tzinfo=dt_timezone.utc)
            except ValueError:
                pass
    return default


def _decimal(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _period_end(report):
    raw = str(report.get("endDate") or report.get("year") or "").strip()
    if len(raw) == 4 and raw.isdigit():
        return date(int(raw), 12, 31)
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


@transaction.atomic
def refresh_fundamentals(member, *, client=None):
    mapping = InstrumentProviderMapping.objects.filter(instrument=member.instrument, provider="FINNHUB", status="VERIFIED").first()
    if not mapping:
        return {"stored": 0, "reason": "FINNHUB_MAPPING_MISSING"}
    client = client or FinnhubClient()
    now, stored = timezone.now(), 0
    for report in client.reported_financials(mapping.provider_symbol):
        period_end = _period_end(report)
        if period_end is None:
            continue
        available = _timestamp(report.get("filedDate") or report.get("acceptedDate"), default=now)
        filing = _timestamp(report.get("acceptedDate") or report.get("filedDate"), default=available)
        for statement in (report.get("report") or {}).values():
            if not isinstance(statement, list):
                continue
            for fact in statement:
                value = _decimal(fact.get("value"))
                if value is None or not fact.get("concept"):
                    continue
                metric = str(fact["concept"]); units = str(fact.get("unit") or ""); original = str(fact.get("value"))
                latest = ResearchFundamentalFact.objects.filter(
                    issuer=member.issuer, metric=metric, period_end=period_end,
                ).order_by("-revision_version", "-data_version").first()
                if latest and _fingerprint((latest.value, latest.units, latest.original_value)) == _fingerprint((value, units, original)):
                    continue
                ResearchFundamentalFact.objects.create(
                    issuer=member.issuer, metric=metric, period_end=period_end,
                    revision_version=(latest.revision_version + 1 if latest else 1),
                    period_start=None, filing_timestamp=filing,
                    # Without a vendor correction timestamp, a revised value becomes public no earlier
                    # than this retrieval. This prevents corrections from leaking into old decisions.
                    public_availability_timestamp=max(available, now) if latest else available,
                    value=value, units=units, original_value=original,
                    provider="FINNHUB", provider_timestamp=now, revision_timestamp=now,
                    data_version=(latest.data_version + 1 if latest else 1),
                )
                stored += 1
    return {"stored": stored}


@transaction.atomic
def refresh_analyst_and_events(member, *, client=None, start_date=None, end_date=None):
    mapping = InstrumentProviderMapping.objects.filter(instrument=member.instrument, provider="FINNHUB", status="VERIFIED").first()
    if not mapping:
        return {"analyst_stored": 0, "events_stored": 0, "reason": "FINNHUB_MAPPING_MISSING"}
    client = client or FinnhubClient()
    now = timezone.now(); analyst_stored = event_stored = 0
    for fact_type, rows in (("RECOMMENDATION_TREND", client.recommendation_trends(mapping.provider_symbol)),
                            ("EPS_ESTIMATE", client.earnings_estimates(mapping.provider_symbol))):
        for row in rows:
            event_at = _timestamp(row.get("period") or row.get("date"), default=now)
            for metric, raw_value in row.items():
                value = _decimal(raw_value)
                if value is None:
                    continue
                latest = ResearchAnalystFact.objects.filter(
                    issuer=member.issuer, fact_type=fact_type, metric=str(metric), event_timestamp=event_at,
                ).order_by("-data_version").first()
                if latest and _fingerprint((latest.value, latest.payload)) == _fingerprint((value, row)):
                    continue
                ResearchAnalystFact.objects.create(
                    issuer=member.issuer, fact_type=fact_type, metric=str(metric), event_timestamp=event_at,
                    data_version=(latest.data_version + 1 if latest else 1), instrument=member.instrument,
                    period_end=event_at.date(), public_availability_timestamp=now, value=value, payload=row,
                    provider="FINNHUB", provider_timestamp=now, revision_timestamp=now,
                )
                analyst_stored += 1
    start_date = start_date or timezone.localdate() - timedelta(days=30)
    end_date = end_date or timezone.localdate() + timedelta(days=180)
    for row in client.earnings_calendar(mapping.provider_symbol, start_date, end_date):
        effective = _timestamp(row.get("date"), default=now)
        latest = ResearchEvent.objects.filter(
            issuer=member.issuer, instrument=member.instrument, event_type="EARNINGS", effective_timestamp=effective,
        ).order_by("-data_version").first()
        if latest and _fingerprint(latest.payload) == _fingerprint(row):
            continue
        ResearchEvent.objects.create(
            issuer=member.issuer, instrument=member.instrument, event_type="EARNINGS", effective_timestamp=effective,
            data_version=(latest.data_version + 1 if latest else 1), announced_timestamp=now,
            available_timestamp=now, timezone="UTC", payload=row, quality_status="VALID",
            provider="FINNHUB", provider_timestamp=now, revision_timestamp=now,
        )
        event_stored += 1
    return {"analyst_stored": analyst_stored, "events_stored": event_stored}


def point_in_time_facts(issuer, decision_timestamp):
    """Only return records public at the simulated decision timestamp."""
    return {
        "fundamentals": ResearchFundamentalFact.objects.filter(
            issuer=issuer, public_availability_timestamp__lte=decision_timestamp,
            revision_timestamp__lte=decision_timestamp,
        ).order_by("metric", "period_end", "revision_version"),
        "analyst": ResearchAnalystFact.objects.filter(
            issuer=issuer, public_availability_timestamp__lte=decision_timestamp,
            revision_timestamp__lte=decision_timestamp,
        ).order_by("fact_type", "event_timestamp", "data_version"),
        "events": ResearchEvent.objects.filter(
            issuer=issuer, available_timestamp__lte=decision_timestamp,
            revision_timestamp__lte=decision_timestamp,
        ).order_by("effective_timestamp", "data_version"),
    }
