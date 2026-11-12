from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.db.models import Count, Max, Min, Q
from django.utils import timezone

from apps.instruments.models import BrokerContract

from ..enums import MappingStatus
from ..models import (
    ResearchAnalystFact,
    ResearchCorporateAction,
    ResearchDailyBar,
    ResearchDataCoverageSummary,
    ResearchDatasetVersion,
    ResearchEvent,
    ResearchFundamentalFact,
    ResearchIntradayBar,
    ResearchUniverse,
    ResearchUniverseMember,
)
from .eligibility import calculate_member_eligibility
from .observability import record_pipeline_failure
from .research_data import refresh_research_history
from .universe_mapping import map_universe_member, qualify_member_exact


@dataclass(frozen=True)
class QualificationResult:
    selected: tuple[ResearchUniverseMember, ...]
    substitutions: tuple[dict, ...]
    failures: tuple[dict, ...]


def active_recommendation_universe(*, require_complete=False):
    dataset = ResearchDatasetVersion.objects.filter(status="ACTIVE").order_by("-snapshot_date").first()
    if not dataset:
        raise ValueError("No active research dataset")
    universe = ResearchUniverse.objects.filter(
        dataset_version=dataset, key=settings.RECOMMENDATION_UNIVERSE_KEY, active=True,
    ).first()
    if not universe:
        raise ValueError(f"Active recommendation universe {settings.RECOMMENDATION_UNIVERSE_KEY} is unavailable")
    count = universe.members.filter(active=True, membership_end__isnull=True).count()
    if require_complete and count != 500:
        raise ValueError(f"The active recommendation universe must contain exactly 500 active members; found {count}")
    return universe


def map_universe_batch(*, offset=0, batch_size=50, create_unqualified=True):
    universe = active_recommendation_universe()
    members = list(universe.members.filter(active=True).select_related("issuer").order_by("pk")[offset:offset + batch_size])
    counts = {value: 0 for value in MappingStatus.values}
    failed = []
    for member in members:
        try:
            mapped = map_universe_member(member, create_unqualified=create_unqualified)
            counts[mapped.mapping_status] += 1
        except Exception as exc:
            failed.append({"member_id": member.pk, "symbol": member.source_symbol, "error": str(exc)[:500]})
            record_pipeline_failure("mapping", member.pk, exc, symbol=member.source_symbol)
    return {"offset": offset, "processed": len(members), "next_offset": offset + len(members), "counts": counts, "failed": failed}


def _exact_contract_candidate(member, rows):
    expected_symbol = member.source_symbol.upper()
    expected_currency = member.currency.upper()
    candidates = [row for row in rows if (
        str(row.get("asset_class", "")).upper() == "STK"
        and str(row.get("currency", "")).upper() == expected_currency
        and expected_symbol in {str(row.get("symbol", "")).upper(), str(row.get("local_symbol", "")).upper()}
    )]
    if member.exchange_hint:
        exchange_matches = [row for row in candidates if member.exchange_hint.upper() in {
            str(row.get("exchange", "")).upper(), str(row.get("primary_exchange", "")).upper(), "SMART",
        }]
        candidates = exchange_matches or candidates
    unique = {int(row["conid"]): row for row in candidates if int(row.get("conid") or 0) > 0}
    return next(iter(unique.values())) if len(unique) == 1 else None


def qualify_member_background(member, *, gateway=None):
    if not member.instrument_id:
        member = map_universe_member(member)
    if not member.instrument_id:
        raise ValueError("Member could not be mapped to a canonical instrument")
    if BrokerContract.objects.filter(instrument_id=member.instrument_id, qualified_at__isnull=False).exists():
        return member
    if gateway is None:raise ValueError("An explicit broker gateway session client is required for contract qualification")
    client = gateway
    candidate = _exact_contract_candidate(member, client.search_contracts(member.source_symbol))
    if not candidate:
        member.mapping_notes = "IBKR qualification was ambiguous or returned no exact identity"
        member.save(update_fields=["mapping_notes"])
        raise ValueError(member.mapping_notes)
    return qualify_member_exact(
        member, conid=int(candidate["conid"]), primary_exchange=candidate.get("primary_exchange") or candidate.get("exchange") or "",
        local_symbol=candidate.get("local_symbol") or "", description=candidate.get("description") or "", gateway=client,
    )


def qualify_universe_batch(*, offset=0, batch_size=25, gateway=None):
    members = list(active_recommendation_universe().members.filter(active=True, instrument__isnull=False).order_by("pk")[offset:offset + batch_size])
    qualified, failed = 0, []
    for member in members:
        try:
            qualify_member_background(member, gateway=gateway)
            qualified += 1
        except Exception as exc:
            failed.append({"member_id": member.pk, "symbol": member.source_symbol, "error": str(exc)[:500]})
    return {"processed": len(members), "qualified": qualified, "failed": failed, "next_offset": offset + len(members)}


def qualify_and_substitute_finalists(ranked_members, required_count, *, gateway=None):
    selected, failures, substitutions = [], [], []
    for rank, member in enumerate(ranked_members):
        if len(selected) >= required_count:
            break
        try:
            qualified = qualify_member_background(member, gateway=gateway)
            if rank >= required_count:
                substitutions.append({"symbol": qualified.source_symbol, "replacement_rank": rank + 1})
            selected.append(qualified)
        except Exception as exc:
            failures.append({"symbol": member.source_symbol, "rank": rank + 1, "error": str(exc)[:500]})
    return QualificationResult(tuple(selected), tuple(substitutions), tuple(failures))


def update_coverage(member, *, as_of_date=None):
    as_of_date = as_of_date or timezone.localdate()
    daily = ResearchDailyBar.objects.filter(instrument_id=member.instrument_id, quality_status="VALID") if member.instrument_id else ResearchDailyBar.objects.none()
    intraday = ResearchIntradayBar.objects.filter(instrument_id=member.instrument_id, quality_status="VALID") if member.instrument_id else ResearchIntradayBar.objects.none()
    daily_summary = daily.aggregate(count=Count("id"), start=Min("trading_date"), end=Max("trading_date"))
    intraday_summary = intraday.aggregate(count=Count("id"), start=Min("window_start"), end=Max("window_end"))
    fundamental_count = ResearchFundamentalFact.objects.filter(issuer=member.issuer, public_availability_timestamp__lte=timezone.now()).count()
    analyst_count = ResearchAnalystFact.objects.filter(issuer=member.issuer, public_availability_timestamp__lte=timezone.now()).count()
    event_count = ResearchEvent.objects.filter(Q(issuer=member.issuer) | Q(instrument_id=member.instrument_id), available_timestamp__lte=timezone.now()).count()
    coverage, _ = ResearchDataCoverageSummary.objects.update_or_create(
        universe_member=member,
        defaults={
            "as_of_date": as_of_date, "daily_bar_count": daily_summary["count"], "daily_start_date": daily_summary["start"],
            "daily_end_date": daily_summary["end"], "intraday_bar_count": intraday_summary["count"],
            "intraday_start_at": intraday_summary["start"], "intraday_end_at": intraday_summary["end"],
            "corporate_action_count": ResearchCorporateAction.objects.filter(instrument_id=member.instrument_id, quality_status="VALID").count() if member.instrument_id else 0,
            "fundamental_fact_count": fundamental_count, "analyst_fact_count": analyst_count, "event_count": event_count,
            "last_successful_update": timezone.now() if daily_summary["count"] else None,
            "recommendation_eligible": daily_summary["count"] >= settings.RESEARCH_MINIMUM_DAILY_BARS,
            "provider_status": {"daily": "READY" if daily_summary["count"] else "MISSING", "fundamentals": "READY" if fundamental_count else "MISSING", "events": "READY" if event_count else "MISSING"},
            "quality_rules": {"minimum_daily_bars": settings.RESEARCH_MINIMUM_DAILY_BARS, "point_in_time": True},
        },
    )
    return coverage


def refresh_data_batch(*, offset=0, batch_size=25, finnhub=None, gateway=None):
    members = list(active_recommendation_universe().members.filter(active=True, instrument__isnull=False).select_related("instrument", "issuer").order_by("pk")[offset:offset + batch_size])
    reports = []
    for member in members:
        try:
            report = refresh_research_history(member.instrument, years=settings.RESEARCH_DAILY_LOOKBACK_YEARS,
                                              minimum_bars=settings.RESEARCH_MINIMUM_DAILY_BARS, finnhub=finnhub, gateway=gateway)
            calculate_member_eligibility(member)
            update_coverage(member)
            reports.append({"symbol": member.source_symbol, **report})
        except Exception as exc:
            reports.append({"symbol": member.source_symbol, "error": str(exc)[:500]})
            record_pipeline_failure("daily_data", member.pk, exc, symbol=member.source_symbol)
    return {"processed": len(members), "next_offset": offset + len(members), "reports": reports}
