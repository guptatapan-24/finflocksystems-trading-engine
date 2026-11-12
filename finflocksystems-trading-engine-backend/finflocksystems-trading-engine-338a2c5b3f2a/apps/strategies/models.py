import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.execution.modes import ExecutionMode


class StrategyDefinition(models.Model):
    key = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=128)
    description = models.TextField(blank=True)
    plugin_path = models.CharField(max_length=255)
    input_requirements = models.JSONField(default=list)
    parameter_schema = models.JSONField(default=dict)
    supported_asset_types = models.JSONField(default=list)
    supported_directions = models.JSONField(default=list)
    supported_timeframes = models.JSONField(default=list)
    version = models.PositiveIntegerField(default=1)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        self.key = self.key.upper()
        return super().save(*args, **kwargs)


class OrderPolicy(models.Model):
    name = models.CharField(max_length=128, unique=True)
    order_type = models.CharField(max_length=16, default="LMT")
    time_in_force = models.CharField(max_length=8, default="DAY")
    limit_offset_bps = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    price_collar_bps = models.DecimalField(max_digits=10, decimal_places=4, default=50)
    allow_market_order = models.BooleanField(default=False)
    replace_after_seconds = models.PositiveIntegerField(default=60)
    maximum_replacements = models.PositiveIntegerField(default=2)
    cancel_at_session_end = models.BooleanField(default=True)
    outside_regular_hours = models.BooleanField(default=False)
    enabled = models.BooleanField(default=True)


class StrategyRiskPolicy(models.Model):
    name = models.CharField(max_length=128, unique=True)
    maximum_weight = models.DecimalField(max_digits=10, decimal_places=8, default="0.10")
    maximum_notional = models.DecimalField(max_digits=24, decimal_places=8, default="100000")
    maximum_quantity = models.DecimalField(max_digits=24, decimal_places=8, default="100000")
    allow_short = models.BooleanField(default=False)
    configuration = models.JSONField(default=dict)
    enabled = models.BooleanField(default=True)


class StrategyInstance(models.Model):
    MODES = ExecutionMode.choices
    STATES = [(x, x) for x in ["FLAT", "ENTRY_PENDING", "PARTIALLY_LONG", "LONG", "EXIT_PENDING",
        "PARTIALLY_SHORT", "SHORT", "PAUSED", "DISABLED", "FLATTEN_REQUESTED", "KILLED",
        "ACTIVATING", "SUBSCRIBING", "WARMING_UP", "READY_WAITING_FOR_LIVE_BAR",
        "BLOCKED", "ERROR"]]
    name = models.CharField(max_length=128)
    definition = models.ForeignKey(StrategyDefinition, on_delete=models.PROTECT, related_name="instances")
    portfolio = models.ForeignKey("portfolios.TradingPortfolio", on_delete=models.PROTECT, related_name="strategy_instances")
    instrument = models.ForeignKey("instruments.Instrument", on_delete=models.PROTECT, related_name="strategy_instances")
    universe = models.JSONField(default=list, blank=True)
    timeframe = models.CharField(max_length=16)
    parameters = models.JSONField(default=dict)
    target_configuration = models.JSONField(default=dict)
    risk_policy = models.ForeignKey(StrategyRiskPolicy, on_delete=models.PROTECT, null=True, blank=True)
    order_policy = models.ForeignKey(OrderPolicy, on_delete=models.PROTECT, null=True, blank=True)
    workflow_trace_id = models.UUIDField(default=uuid.uuid4, editable=False, db_index=True)
    execution_mode = models.CharField(
        max_length=16, choices=MODES, default=ExecutionMode.PAPER
    )
    state = models.CharField(max_length=32, choices=STATES, default="DISABLED")
    enabled = models.BooleanField(default=False)
    allocated_capital = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    kill_switch = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)
    effective_from = models.DateTimeField(null=True, blank=True)
    effective_to = models.DateTimeField(null=True, blank=True)
    state_data = models.JSONField(default=dict)
    warmup_progress = models.PositiveIntegerField(default=0)
    warmup_started_at = models.DateTimeField(null=True, blank=True)
    warmup_last_progress_at = models.DateTimeField(null=True, blank=True)
    subscription_ready_at = models.DateTimeField(null=True, blank=True)
    warmup_completed_at = models.DateTimeField(null=True, blank=True)
    ready_waiting_since = models.DateTimeField(null=True, blank=True)
    first_evaluation_completed_at = models.DateTimeField(null=True, blank=True)
    execution_active_at = models.DateTimeField(null=True, blank=True)
    block_reason = models.CharField(max_length=255, blank=True)
    last_market_event_at = models.DateTimeField(null=True, blank=True)
    last_market_bar_id = models.CharField(max_length=160, blank=True)
    last_market_bar_version = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["portfolio", "name"], name="unique_strategy_instance_name"),
            models.CheckConstraint(
                condition=models.Q(execution_mode__in=ExecutionMode.values),
                name="strategy_instance_valid_execution_mode",
            ),
        ]
        indexes = [models.Index(fields=["enabled","state","instrument","timeframe"],name="strategy_active_input_idx")]

    def clean(self):
        from apps.execution.modes import normalize_execution_mode

        self.execution_mode = normalize_execution_mode(self.execution_mode)


class StrategyVersion(models.Model):
    strategy_instance = models.ForeignKey(StrategyInstance, on_delete=models.PROTECT, related_name="versions")
    version = models.PositiveIntegerField()
    configuration_snapshot = models.JSONField(default=dict)
    parameter_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    retired_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["strategy_instance", "version"], name="unique_strategy_version")]

    def save(self, *args, **kwargs):
        if self.pk and StrategyVersion.objects.filter(pk=self.pk).exists():
            raise ValidationError("StrategyVersion records are immutable")
        return super().save(*args, **kwargs)


class StrategyInputRequirement(models.Model):
    TYPES = [(x, x) for x in ["BAR", "INDICATOR"]]
    identity_hash = models.CharField(max_length=64, unique=True)
    instrument = models.ForeignKey("instruments.Instrument", on_delete=models.PROTECT, related_name="strategy_input_requirements")
    timeframe = models.CharField(max_length=16)
    input_type = models.CharField(max_length=16, choices=TYPES)
    name = models.CharField(max_length=64)
    role = models.CharField(max_length=64, blank=True)
    parameters = models.JSONField(default=dict)
    implementation_version = models.PositiveIntegerField(default=1)
    required_bar_fields = models.JSONField(default=list)
    warmup_bars = models.PositiveIntegerField(default=0)
    active_ref_count = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)


class StrategyInputBinding(models.Model):
    strategy_instance = models.ForeignKey(StrategyInstance, on_delete=models.PROTECT, related_name="input_bindings")
    strategy_version = models.ForeignKey(StrategyVersion, on_delete=models.PROTECT, related_name="input_bindings")
    requirement = models.ForeignKey(StrategyInputRequirement, on_delete=models.PROTECT, related_name="bindings")
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["strategy_version", "requirement"], name="unique_version_input_requirement")]


class StrategyWarmupReadiness(models.Model):
    strategy_instance = models.ForeignKey(
        StrategyInstance, on_delete=models.PROTECT, related_name="warmup_readiness_records"
    )
    strategy_version = models.ForeignKey(
        StrategyVersion, on_delete=models.PROTECT, related_name="warmup_readiness_records"
    )
    provider = models.CharField(max_length=16)
    provider_generation = models.CharField(max_length=64)
    requirement_hashes = models.JSONField(default=list)
    requirement_snapshot_hash = models.CharField(max_length=64)
    bar_ids = models.JSONField(default=list)
    bar_timestamps = models.JSONField(default=list)
    evidence_hash = models.CharField(max_length=64, unique=True)
    is_current = models.BooleanField(default=True)
    completed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [
            models.Index(
                fields=["strategy_instance", "strategy_version", "-completed_at"],
                name="strategy_warmup_audit_idx",
            ),
        ]


class StrategyRun(models.Model):
    strategy_instance = models.ForeignKey(StrategyInstance, on_delete=models.PROTECT, related_name="runs", null=True, blank=True)
    strategy_version = models.ForeignKey(StrategyVersion, on_delete=models.PROTECT, related_name="runs", null=True, blank=True)
    input_hash = models.CharField(max_length=64)
    idempotency_key = models.CharField(max_length=255, null=True, blank=True, unique=True)
    triggering_event_id = models.CharField(max_length=160, blank=True)
    source_data_version = models.PositiveIntegerField(default=1)
    configuration_snapshot = models.JSONField(default=dict)
    context_snapshot = models.JSONField(default=dict)
    status = models.CharField(max_length=24, default="RUNNING")
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["strategy_instance", "input_hash"], name="unique_strategy_instance_input")]
        indexes = [
            models.Index(fields=["strategy_instance","-started_at"],name="strategy_run_latest_idx"),
            models.Index(fields=["status","started_at"],name="strategy_run_status_idx"),
        ]


class StrategySignal(models.Model):
    run = models.OneToOneField(StrategyRun, on_delete=models.PROTECT, related_name="signal")
    strategy_instance = models.ForeignKey(StrategyInstance, on_delete=models.PROTECT, related_name="signals")
    strategy_version = models.ForeignKey(StrategyVersion, on_delete=models.PROTECT, related_name="signals")
    signal_type = models.CharField(max_length=32)
    signal_time = models.DateTimeField()
    reason = models.CharField(max_length=255, blank=True)
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class StrategyTarget(models.Model):
    TARGET_TYPES = [(x, x) for x in ["WEIGHT", "VALUE", "QUANTITY", "FLAT"]]
    run = models.ForeignKey(StrategyRun, on_delete=models.PROTECT, related_name="targets")
    strategy_instance = models.ForeignKey(StrategyInstance, on_delete=models.PROTECT, related_name="targets", null=True, blank=True)
    strategy_version = models.ForeignKey(StrategyVersion, on_delete=models.PROTECT, related_name="targets", null=True, blank=True)
    portfolio = models.ForeignKey("portfolios.TradingPortfolio", on_delete=models.PROTECT, null=True, blank=True)
    instrument = models.ForeignKey("instruments.Instrument", on_delete=models.PROTECT)
    target_type = models.CharField(max_length=16, choices=TARGET_TYPES, default="WEIGHT")
    target_weight = models.DecimalField(max_digits=12, decimal_places=8, default=0)
    target_value = models.DecimalField(max_digits=24, decimal_places=8, null=True, blank=True)
    target_quantity = models.DecimalField(max_digits=24, decimal_places=8, null=True, blank=True)
    direction = models.CharField(max_length=16, default="FLAT")
    signal_type = models.CharField(max_length=32, default="SET_TARGET")
    signal_time = models.DateTimeField(null=True, blank=True)
    source_event_id = models.CharField(max_length=160, blank=True)
    execution_mode = models.CharField(
        max_length=16, choices=ExecutionMode.choices, default=ExecutionMode.PAPER
    )
    reason = models.CharField(max_length=255, blank=True)
    rationale = models.CharField(max_length=255, blank=True)
    confidence = models.DecimalField(max_digits=8, decimal_places=6, null=True, blank=True)
    metadata = models.JSONField(default=dict)
    status = models.CharField(max_length=24, default="ACTIVE")
    created_at = models.DateTimeField(default=timezone.now)
    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["run", "instrument"], name="unique_run_target"),
            models.CheckConstraint(
                condition=models.Q(execution_mode__in=ExecutionMode.values),
                name="strategy_target_valid_execution_mode",
            ),
        ]
        indexes = [models.Index(fields=["strategy_instance","status","-created_at"],name="strategy_target_latest_idx")]


class OptionSelectionSnapshot(models.Model):
    """Immutable audit evidence for one ATR/OI option entry decision."""

    strategy_instance = models.ForeignKey(
        StrategyInstance, on_delete=models.PROTECT, related_name="option_selections"
    )
    strategy_version = models.ForeignKey(
        StrategyVersion, on_delete=models.PROTECT, related_name="option_selections"
    )
    market_bar_id = models.CharField(max_length=160)
    market_bar_version = models.PositiveIntegerField(default=1)
    observed_at = models.DateTimeField()
    provider = models.CharField(max_length=32, default="DHAN")
    model_version = models.CharField(max_length=64, default="rps-v2-framework-2026-09-normalized100")
    atr_reference = models.DecimalField(max_digits=24, decimal_places=8)
    atr_value = models.DecimalField(max_digits=24, decimal_places=8)
    atr_support = models.DecimalField(max_digits=24, decimal_places=8)
    atr_resistance = models.DecimalField(max_digits=24, decimal_places=8)
    # Null when the top-five OI walls did not form a valid range at the decision
    # timestamp. That excludes the `oi_structure` factor from RPS and
    # renormalises the remaining frozen weights; it is not an entry failure, so
    # it must be recordable rather than unrepresentable.
    oi_support = models.DecimalField(max_digits=24, decimal_places=8, null=True, blank=True)
    oi_resistance = models.DecimalField(max_digits=24, decimal_places=8, null=True, blank=True)
    spot = models.DecimalField(max_digits=24, decimal_places=8)
    entry_score = models.DecimalField(max_digits=8, decimal_places=4)
    selected_strategy = models.CharField(max_length=64)
    expiration = models.DateField()
    candidate_rankings = models.JSONField(default=list)
    selected_legs = models.JSONField(default=list)
    source_snapshot = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "strategy_instance", "strategy_version", "market_bar_id",
                    "market_bar_version",
                ],
                name="unique_option_selection_input",
            ),
        ]
        indexes = [
            models.Index(
                fields=["strategy_instance", "-observed_at"],
                name="option_selection_time_idx",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk and OptionSelectionSnapshot.objects.filter(pk=self.pk).exists():
            raise ValidationError("OptionSelectionSnapshot records are immutable")
        return super().save(*args, **kwargs)


class StrategyAttributedPosition(models.Model):
    strategy_instance = models.ForeignKey(StrategyInstance, on_delete=models.PROTECT, related_name="attributed_positions")
    instrument = models.ForeignKey("instruments.Instrument", on_delete=models.PROTECT)
    portfolio = models.ForeignKey("portfolios.TradingPortfolio", on_delete=models.PROTECT)
    quantity = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    average_cost = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["strategy_instance", "instrument", "portfolio"], name="unique_strategy_attributed_position")]


class StrategyAllocation(models.Model):
    strategy_instance = models.ForeignKey(StrategyInstance, on_delete=models.PROTECT, related_name="allocations")
    portfolio = models.ForeignKey("portfolios.TradingPortfolio", on_delete=models.PROTECT)
    weight = models.DecimalField(max_digits=12, decimal_places=8)
    minimum_share = models.DecimalField(max_digits=12, decimal_places=8, default=0)
    maximum_share = models.DecimalField(max_digits=12, decimal_places=8, default=1)
    capacity = models.DecimalField(max_digits=24, decimal_places=8, null=True, blank=True)
    minimum_allocation = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    priority = models.PositiveIntegerField(default=100)
    idle_cash = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["strategy_instance", "portfolio"], name="unique_strategy_instance_allocation")]
        indexes = [models.Index(fields=["portfolio","priority"],name="strategy_alloc_priority_idx")]


class StrategyAction(models.Model):
    strategy_instance = models.ForeignKey(StrategyInstance, on_delete=models.PROTECT, related_name="actions")
    action = models.CharField(max_length=24)
    idempotency_key = models.CharField(max_length=128, unique=True)
    request_hash = models.CharField(max_length=64, db_index=True)
    status = models.CharField(max_length=24, default="PROCESSING")
    result = models.JSONField(default=dict)
    last_error = models.CharField(max_length=1000, blank=True)
    retryable = models.BooleanField(default=False)
    attempt_count = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
