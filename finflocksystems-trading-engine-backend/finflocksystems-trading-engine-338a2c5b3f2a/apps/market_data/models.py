from django.db import models


class MarketDataProviderConfiguration(models.Model):
    provider = models.CharField(max_length=32, unique=True, default="FINNHUB")
    encrypted_api_key = models.TextField(blank=True)
    api_key_last_four = models.CharField(max_length=4, blank=True)
    override_environment = models.BooleanField(default=False)
    enabled = models.BooleanField(default=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_tested_at = models.DateTimeField(null=True, blank=True)
    last_test_success_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    rate_limit_state = models.JSONField(default=dict)
    updated_by = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class InstrumentPriceHistory(models.Model):
    instrument = models.ForeignKey("instruments.Instrument", on_delete=models.PROTECT, related_name="price_history")
    trading_date = models.DateField()
    open = models.DecimalField(max_digits=24, decimal_places=8)
    high = models.DecimalField(max_digits=24, decimal_places=8)
    low = models.DecimalField(max_digits=24, decimal_places=8)
    close = models.DecimalField(max_digits=24, decimal_places=8)
    adjusted_close = models.DecimalField(max_digits=24, decimal_places=8, null=True, blank=True)
    volume = models.DecimalField(max_digits=28, decimal_places=8, default=0)
    provider = models.CharField(max_length=32, default="FINNHUB")
    data_version = models.PositiveIntegerField(default=1)
    quality_status = models.CharField(max_length=24, default="COMPLETE")
    fetched_at = models.DateTimeField()

    class Meta:
        ordering = ["trading_date"]
        constraints = [
            models.UniqueConstraint(fields=["instrument", "trading_date", "provider"], name="unique_instrument_daily_price_provider")
        ]
        indexes = [models.Index(fields=["instrument", "provider", "trading_date"])]


class MarketDataFetchRun(models.Model):
    provider = models.CharField(max_length=32, default="FINNHUB")
    instrument = models.ForeignKey("instruments.Instrument", on_delete=models.PROTECT, null=True, blank=True)
    purpose = models.CharField(max_length=32, default="HISTORY")
    status = models.CharField(max_length=24, default="STARTED")
    requested_start = models.DateField(null=True, blank=True)
    requested_end = models.DateField(null=True, blank=True)
    records_received = models.PositiveIntegerField(default=0)
    records_written = models.PositiveIntegerField(default=0)
    response_metadata = models.JSONField(default=dict)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
