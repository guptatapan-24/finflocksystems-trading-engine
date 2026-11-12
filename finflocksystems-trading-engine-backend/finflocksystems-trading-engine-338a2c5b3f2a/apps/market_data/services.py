from django.db import transaction
from django.utils import timezone
from apps.instruments.models import InstrumentProviderMapping

from .models import InstrumentPriceHistory, MarketDataFetchRun
from .providers.finnhub import (
    FinnhubClient,
    FinnhubError,
    decrypt_api_key,
    effective_api_key,
    encrypt_api_key,
    provider_status,
)


def fetch_daily_history(instrument, start_date, end_date, *, purpose="HISTORY", client=None,
                        require_verified_mapping=False):
    """Preserve portfolio-optimization history behavior independently of fallback mappings."""
    run = MarketDataFetchRun.objects.create(
        instrument=instrument, purpose=purpose, requested_start=start_date, requested_end=end_date
    )
    active_client = None
    rows = []
    try:
        active_client = client or FinnhubClient()
        mapping=InstrumentProviderMapping.objects.filter(
            instrument=instrument,provider="FINNHUB",status="VERIFIED"
        ).first()
        if require_verified_mapping and not mapping:
            raise FinnhubError("Verified Finnhub mapping is required",code="FINNHUB_MAPPING_MISSING",status_code=400)
        provider_symbol=mapping.provider_symbol if mapping else instrument.symbol
        rows = active_client.daily_candles(provider_symbol, start_date, end_date)
        now = timezone.now()
        rows_by_date = {row["trading_date"]: row for row in rows}
        dates = list(rows_by_date)
        existing_dates = set(InstrumentPriceHistory.objects.filter(
            instrument=instrument, provider="FINNHUB", trading_date__in=dates,
        ).values_list("trading_date", flat=True))
        price_fields = ("trading_date", "open", "high", "low", "close", "adjusted_close", "volume")
        records = [InstrumentPriceHistory(
            instrument=instrument,
            provider="FINNHUB",
            quality_status="COMPLETE",
            fetched_at=now,
            **{field: row[field] for field in price_fields},
        ) for row in rows_by_date.values()]
        with transaction.atomic():
            InstrumentPriceHistory.objects.bulk_create(
                records, update_conflicts=True,
                update_fields=["open", "high", "low", "close", "adjusted_close", "volume",
                               "quality_status", "fetched_at"],
                unique_fields=["instrument", "trading_date", "provider"],
            )
        run.status = "COMPLETED"
        run.records_received = len(rows)
        run.records_written = len(set(dates) - existing_dates)
        run.response_metadata = {**getattr(active_client, "last_response_metadata", {}),
                                 "provider_symbol":provider_symbol,"mapping_verified":bool(mapping)}
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "records_received", "records_written", "response_metadata", "completed_at"])
        return run
    except Exception as exc:
        run.status = "FAILED"
        run.records_received = len(rows)
        run.response_metadata = getattr(active_client, "last_response_metadata", {})
        run.error = str(exc)[:2000]
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "records_received", "response_metadata", "error", "completed_at"])
        raise
