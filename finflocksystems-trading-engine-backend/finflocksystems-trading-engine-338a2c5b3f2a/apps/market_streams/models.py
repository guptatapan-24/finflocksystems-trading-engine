from django.db import models
from django.utils import timezone
import uuid


class MarketDataSubscription(models.Model):
    STATES=[(x,x) for x in ["PENDING","SUBSCRIBING","ACTIVE","DEGRADED","ERROR","CANCELLING","INACTIVE"]]
    instrument=models.ForeignKey("instruments.Instrument",on_delete=models.PROTECT,related_name="market_subscriptions")
    gateway_session=models.ForeignKey(
        "broker_gateway.BrokerGatewaySession",on_delete=models.PROTECT,related_name="market_subscriptions",
        null=True,blank=True)
    conid=models.BigIntegerField()
    timeframe=models.CharField(max_length=16)
    state=models.CharField(max_length=24,choices=STATES,default="PENDING")
    consumer_count=models.PositiveIntegerField(default=0)
    required_history_bars=models.PositiveIntegerField(default=0)
    request_id=models.UUIDField(default=uuid.uuid4)
    gateway_command_id=models.BigIntegerField(null=True,blank=True)
    gateway_connection_generation=models.CharField(max_length=64,blank=True)
    requested_at=models.DateTimeField(null=True,blank=True)
    last_event_at=models.DateTimeField(null=True,blank=True)
    last_error=models.TextField(blank=True)
    primary_provider=models.CharField(max_length=16,default="IBKR")
    active_provider=models.CharField(max_length=16,default="IBKR")
    fallback_state=models.CharField(max_length=24,default="PRIMARY")
    fallback_reason=models.CharField(max_length=64,blank=True)
    provider_generation=models.UUIDField(default=uuid.uuid4)
    last_primary_event_at=models.DateTimeField(null=True,blank=True)
    last_fallback_event_at=models.DateTimeField(null=True,blank=True)
    failed_over_at=models.DateTimeField(null=True,blank=True)
    recovered_at=models.DateTimeField(null=True,blank=True)
    primary_probe_generation=models.UUIDField(null=True,blank=True)
    primary_probe_started_at=models.DateTimeField(null=True,blank=True)
    primary_probe_event_count=models.PositiveIntegerField(default=0)
    last_published_window_end=models.DateTimeField(null=True,blank=True)
    created_at=models.DateTimeField(auto_now_add=True)
    updated_at=models.DateTimeField(auto_now=True)

    class Meta:
        constraints=[models.UniqueConstraint(fields=["gateway_session","instrument","timeframe"],name="unique_session_market_subscription")]
        indexes=[models.Index(fields=["state","updated_at"],name="market_sub_state_idx")]


class MarketDataConsumerLease(models.Model):
    """Temporary, persisted demand for a routed market-data subscription."""

    CONSUMER_TYPES = [(value, value) for value in ["MANUAL", "WATCHLIST", "MONITORING", "RISK", "STRATEGY"]]
    gateway_session = models.ForeignKey(
        "broker_gateway.BrokerGatewaySession", on_delete=models.CASCADE, related_name="market_data_leases"
    )
    instrument = models.ForeignKey(
        "instruments.Instrument", on_delete=models.PROTECT, related_name="market_data_leases"
    )
    timeframe = models.CharField(max_length=16, default="1m")
    consumer_type = models.CharField(max_length=16, choices=CONSUMER_TYPES)
    lease_key = models.CharField(max_length=128)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["gateway_session", "consumer_type", "lease_key"],
            name="unique_market_consumer_lease",
        )]
        indexes = [
            models.Index(fields=["gateway_session", "instrument", "timeframe", "expires_at"],
                         name="market_lease_demand_idx"),
            models.Index(fields=["expires_at"], name="market_lease_expiry_idx"),
        ]


class MarketDataProviderTransition(models.Model):
    subscription = models.ForeignKey(MarketDataSubscription, on_delete=models.PROTECT, related_name="provider_transitions")
    instrument = models.ForeignKey("instruments.Instrument", on_delete=models.PROTECT)
    timeframe = models.CharField(max_length=16)
    previous_provider = models.CharField(max_length=16)
    new_provider = models.CharField(max_length=16)
    reason = models.CharField(max_length=64)
    previous_generation = models.UUIDField(null=True, blank=True)
    generation = models.UUIDField()
    metadata = models.JSONField(default=dict)
    occurred_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["instrument", "-occurred_at"], name="provider_transition_time_idx")]


class MarketBar(models.Model):
    PROCESSING_MODES = [(value, value) for value in ["LIVE", "WARMUP", "REPLAY", "BACKFILL"]]
    MARKET_DATA_TYPES = [(value, value) for value in ["LIVE", "DELAYED"]]
    instrument = models.ForeignKey("instruments.Instrument", on_delete=models.PROTECT, related_name="stream_bars")
    bar_id = models.CharField(max_length=160)
    interval = models.CharField(max_length=16)
    window_start = models.DateTimeField()
    window_end = models.DateTimeField()
    open = models.DecimalField(max_digits=24, decimal_places=8)
    high = models.DecimalField(max_digits=24, decimal_places=8)
    low = models.DecimalField(max_digits=24, decimal_places=8)
    close = models.DecimalField(max_digits=24, decimal_places=8)
    volume = models.DecimalField(max_digits=28, decimal_places=8, default=0)
    version = models.PositiveIntegerField(default=1)
    is_final = models.BooleanField(default=False)
    source_event_count = models.PositiveIntegerField(default=0)
    provider = models.CharField(max_length=16, blank=True)
    provider_generation = models.CharField(max_length=64, blank=True)
    source = models.CharField(max_length=64, blank=True)
    market_data_type = models.CharField(max_length=16, choices=MARKET_DATA_TYPES, default="LIVE")
    received_at = models.DateTimeField(null=True, blank=True)
    processing_mode = models.CharField(max_length=16, choices=PROCESSING_MODES, default="LIVE")
    produced_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["bar_id", "version"], name="unique_market_bar_version")]
        indexes = [models.Index(fields=["instrument","interval","is_final","-window_end"],name="market_bar_latest_idx")]


class IndicatorValue(models.Model):
    PROCESSING_MODES = [(value, value) for value in ["LIVE", "WARMUP", "REPLAY", "BACKFILL"]]
    instrument = models.ForeignKey("instruments.Instrument", on_delete=models.PROTECT, related_name="indicators")
    bar = models.ForeignKey(MarketBar, on_delete=models.PROTECT, null=True, blank=True)
    indicator = models.CharField(max_length=64)
    indicator_name = models.CharField(max_length=64)
    indicator_role = models.CharField(max_length=64, blank=True)
    implementation_version = models.PositiveIntegerField(default=1)
    requirement_identity_hash = models.CharField(max_length=64)
    value = models.DecimalField(max_digits=28, decimal_places=10, null=True, blank=True)
    parameters = models.JSONField(default=dict)
    previous_value = models.DecimalField(max_digits=28, decimal_places=10, null=True, blank=True)
    timeframe = models.CharField(max_length=16, blank=True)
    source_bar_id = models.CharField(max_length=160, blank=True)
    source_bar_version = models.PositiveIntegerField(default=1)
    is_final = models.BooleanField(default=True)
    processing_mode = models.CharField(max_length=16, choices=PROCESSING_MODES, default="LIVE")
    event_time = models.DateTimeField()
    source_key = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["source_key"], name="unique_indicator_source")]
        indexes = [
            models.Index(fields=["instrument","timeframe","is_final","-event_time"],name="indicator_latest_idx"),
            models.Index(fields=["source_bar_id","source_bar_version","requirement_identity_hash","is_final"],
                name="indicator_identity_ready_idx"),
        ]


class StrategyEvaluationJob(models.Model):
    STATUSES = [(value, value) for value in [
        "WAITING_FOR_INPUT", "PENDING", "CLAIMED", "RUNNING", "RETRY", "COMPLETED", "FAILED",
    ]]
    ERROR_CODES = [(value, value) for value in [
        "INFRASTRUCTURE_RETRYABLE", "MISSING_INPUT", "STALE_INPUT", "INVALID_CONFIGURATION",
        "PLUGIN_LOGIC_ERROR", "DATA_INTEGRITY_ERROR",
    ]]
    strategy_instance = models.ForeignKey(
        "strategies.StrategyInstance", on_delete=models.CASCADE, related_name="evaluation_jobs",
    )
    strategy_version = models.ForeignKey(
        "strategies.StrategyVersion", on_delete=models.CASCADE, related_name="evaluation_jobs",
    )
    bar = models.ForeignKey(MarketBar, on_delete=models.CASCADE, related_name="evaluation_jobs")
    strategy_run = models.ForeignKey(
        "strategies.StrategyRun", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="evaluation_jobs",
    )
    market_bar_id = models.CharField(max_length=160)
    bar_version = models.PositiveIntegerField()
    event_id = models.CharField(max_length=160)
    event_time = models.DateTimeField()
    source_data_version = models.PositiveIntegerField(default=1)
    processing_mode = models.CharField(max_length=16, default="LIVE")
    expected_input_identity_hashes = models.JSONField(default=list)
    status = models.CharField(max_length=24, choices=STATUSES, default="WAITING_FOR_INPUT")
    attempt_count = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    claimed_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=40, choices=ERROR_CODES, blank=True)
    error_details = models.JSONField(default=dict)
    idempotency_key = models.CharField(max_length=128, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["strategy_instance", "strategy_version", "market_bar_id", "bar_version"],
            name="unique_strategy_eval_job",
        )]
        indexes = [
            models.Index(fields=["status", "next_attempt_at", "created_at"], name="strategy_eval_job_queue_idx"),
            models.Index(fields=["status", "claimed_at"], name="strategy_eval_job_claim_idx"),
        ]


class InstrumentMarketState(models.Model):
    STATES = [(x, x) for x in ["FRESH", "STALE", "UNAVAILABLE"]]
    instrument = models.OneToOneField("instruments.Instrument", on_delete=models.PROTECT, related_name="market_state")
    status = models.CharField(max_length=16, choices=STATES, default="UNAVAILABLE")
    reference_price = models.DecimalField(max_digits=24, decimal_places=8, null=True, blank=True)
    latest_event_at = models.DateTimeField(null=True, blank=True)
    market_event_at = models.DateTimeField(null=True, blank=True)
    watermark_at = models.DateTimeField(null=True, blank=True)
    stale_after_seconds = models.PositiveIntegerField(default=300)
    source_event_id = models.UUIDField(null=True, blank=True)
    reference_price_provider = models.CharField(max_length=16, blank=True)
    reference_price_source = models.CharField(max_length=64, blank=True)
    market_data_type = models.CharField(max_length=16, choices=[("LIVE", "LIVE"), ("DELAYED", "DELAYED")], default="LIVE")
    provider_generation = models.UUIDField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def is_usable(self, at=None):
        from django.utils import timezone
        at = at or timezone.now()
        if self.status != "FRESH" or not self.latest_event_at or self.reference_price is None:
            return False
        return (at - self.latest_event_at).total_seconds() <= self.stale_after_seconds

    def is_execution_usable(self, at=None, execution_mode=None):
        """Authorize delayed prices only for an explicitly PAPER execution path."""
        if not self.is_usable(at):
            return False
        source = str(self.reference_price_source or "").strip().lower()
        if source in {
            "ibkr_historical",
            "finnhub_historical",
            "historical",
            "warmup",
            "backfill",
            "replay",
        }:
            return False
        data_type = str(self.market_data_type or "LIVE").strip().upper()
        if data_type == "DELAYED":
            return str(execution_mode or "").strip().upper() == "PAPER" and source == "ibkr_delayed"
        return data_type == "LIVE"
