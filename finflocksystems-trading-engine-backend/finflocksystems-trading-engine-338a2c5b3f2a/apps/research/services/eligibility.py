from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.core.lazy_imports import lazy_module
from apps.instruments.models import BrokerContract, InstrumentProviderMapping

from ..enums import MappingStatus
from ..models import InstrumentEligibilitySnapshot, ResearchDailyBar


# Deferred: reached from config.urls via portfolio_construction.views.
# See apps/core/lazy_imports.py.
np = lazy_module("numpy")

D = Decimal


def _metrics(rows):
    closes = np.asarray([float(row.raw_close) for row in rows], dtype=float)
    volumes = np.asarray([float(row.volume) for row in rows], dtype=float)
    if len(closes) == 0:
        return {}
    returns = np.diff(np.log(closes)) if len(closes) > 1 else np.array([], dtype=float)
    volatility = float(np.std(returns[-252:], ddof=1) * np.sqrt(252)) if len(returns) > 1 else 0.0
    running_max = np.maximum.accumulate(closes)
    drawdown = closes / running_max - 1
    dollar_volumes = closes * volumes
    return {
        "price": closes[-1],
        "median_dollar_volume_20d": float(np.median(dollar_volumes[-20:])),
        "history_days": len(closes),
        "trading_days_252d": min(len(closes), 252),
        "realized_volatility": volatility,
        "maximum_drawdown": abs(float(np.min(drawdown))),
    }


def calculate_member_eligibility(member, *, as_of_date=None):
    as_of_date = as_of_date or timezone.localdate()
    reasons = []
    if not member.active or not member.instrument_id:
        reasons.append("UNMAPPED_OR_INACTIVE")
        metrics = {}
    else:
        rows = list(
            ResearchDailyBar.objects.filter(
                instrument_id=member.instrument_id,
                trading_date__lte=as_of_date,
                quality_status="VALID",
            ).order_by("trading_date", "-data_version")
        )
        latest_by_date = {}
        for row in rows:
            latest_by_date.setdefault(row.trading_date, row)
        rows = [latest_by_date[key] for key in sorted(latest_by_date)]
        metrics = _metrics(rows)
        if not metrics:
            reasons.append("INSUFFICIENT_VALID_HISTORY")
    config = member.research_eligibility_configuration or {}
    if metrics:
        if metrics["price"] < float(config.get("minimum_unadjusted_price_usd", 5)):
            reasons.append("PRICE_BELOW_MINIMUM")
        if metrics["median_dollar_volume_20d"] < float(config.get("minimum_median_dollar_volume_20d_usd", 25_000_000)):
            reasons.append("LIQUIDITY_BELOW_MINIMUM")
        if metrics["history_days"] < int(config.get("minimum_adjusted_history_days", 756)):
            reasons.append("INSUFFICIENT_VALID_HISTORY")
        required_sessions = int(config.get("minimum_trading_days_252d", 240))
        if metrics["history_days"] >= 252 and metrics["trading_days_252d"] < required_sessions:
            reasons.append("INSUFFICIENT_RECENT_SESSIONS")
        latest = max((row.trading_date for row in rows), default=None)
        if latest is None or latest < as_of_date - timedelta(days=7):
            reasons.append("STALE_DATA")
    data_ready = not reasons
    provider_ready = bool(member.instrument_id and InstrumentProviderMapping.objects.filter(
        instrument_id=member.instrument_id, provider="FINNHUB", status="VERIFIED"
    ).exists())
    broker_ready = bool(member.instrument_id and BrokerContract.objects.filter(
        instrument_id=member.instrument_id, qualified_at__isnull=False
    ).exists())
    builder_eligible = data_ready and provider_ready and broker_ready
    if data_ready:
        if broker_ready:
            member.mapping_status = MappingStatus.BROKER_QUALIFIED
        elif provider_ready:
            member.mapping_status = MappingStatus.RESEARCH_DATA_READY
        member.save(update_fields=["mapping_status"])
    snapshot, _ = InstrumentEligibilitySnapshot.objects.update_or_create(
        universe_member=member,
        as_of_date=as_of_date,
        defaults={
            "price": D(str(metrics["price"])) if metrics else None,
            "median_dollar_volume_20d": D(str(metrics["median_dollar_volume_20d"])) if metrics else None,
            "history_days": int(metrics.get("history_days", 0)),
            "trading_days_252d": int(metrics.get("trading_days_252d", 0)),
            "realized_volatility": D(str(metrics["realized_volatility"])) if metrics else None,
            "maximum_drawdown": D(str(metrics["maximum_drawdown"])) if metrics else None,
            "data_quality_status": "VALID" if data_ready else "BLOCKED",
            "research_eligible": data_ready and provider_ready,
            "builder_eligible": builder_eligible,
            "rejection_reasons": list(dict.fromkeys(reasons + ([] if provider_ready else ["FINNHUB_MAPPING_MISSING"]) + ([] if broker_ready else ["IBKR_CONTRACT_NOT_QUALIFIED"]))),
            "metrics": {**metrics, "provider_ready": provider_ready, "broker_ready": broker_ready,
                        "latest_data_date": str(latest) if metrics and latest else None,
                        "provider": rows[-1].provider if metrics and rows else None},
        },
    )
    return snapshot


def calculate_universe_eligibility(universe, *, as_of_date=None):
    snapshots = [
        calculate_member_eligibility(member, as_of_date=as_of_date)
        for member in universe.members.filter(active=True).select_related("instrument")
    ]
    return {
        "total": len(snapshots),
        "research_eligible": sum(item.research_eligible for item in snapshots),
        "builder_eligible": sum(item.builder_eligible for item in snapshots),
    }
