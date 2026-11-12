from django.core.exceptions import ValidationError
from django.db import models

from apps.execution.modes import ExecutionMode, RunType

class PortfolioFlow(models.Model):
    TYPES = [(x, x) for x in ["DEPOSIT", "WITHDRAWAL", "INTERNAL_TRANSFER_IN", "INTERNAL_TRANSFER_OUT"]]
    portfolio = models.ForeignKey("portfolios.TradingPortfolio", on_delete=models.PROTECT, related_name="flows")
    flow_type = models.CharField(max_length=32, choices=TYPES)
    amount = models.DecimalField(max_digits=24, decimal_places=8)
    currency = models.CharField(max_length=8, default="USD")
    effective_at = models.DateTimeField()
    idempotency_key = models.CharField(max_length=128, unique=True)
    request_hash = models.CharField(max_length=64, default="", db_index=True)
    retryable = models.BooleanField(default=False)
    last_error = models.CharField(max_length=1000, blank=True)
    attempt_count = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=24, default="REQUESTED")
    created_at = models.DateTimeField(auto_now_add=True)


class AllocationRun(models.Model):
    POLICIES = [(x, x) for x in ["PROPORTIONAL", "LOWEST_CONVICTION_FIRST", "MOST_LIQUID_FIRST", "LOWEST_COST_FIRST", "PRIORITY_ORDER"]]
    flow = models.OneToOneField(PortfolioFlow, on_delete=models.PROTECT, related_name="allocation_run")
    portfolio_nav_before = models.DecimalField(max_digits=24, decimal_places=8)
    portfolio_cash_before = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    approved_amount = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    unallocated_amount = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    liquidation_policy = models.CharField(max_length=40, choices=POLICIES, default="PROPORTIONAL")
    allocation_mode = models.CharField(max_length=40, default="STRATEGY_ALLOCATION")
    optimization_run = models.ForeignKey("portfolio_optimization.PortfolioOptimizationRun", on_delete=models.PROTECT, null=True, blank=True)
    status = models.CharField(max_length=24, default="CALCULATING")
    calculation_version = models.PositiveIntegerField(default=1)
    snapshot = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)


class StrategyCapitalSnapshot(models.Model):
    allocation_run = models.ForeignKey(AllocationRun, on_delete=models.PROTECT, related_name="capital_snapshots")
    strategy_instance = models.ForeignKey("strategies.StrategyInstance", on_delete=models.SET_NULL, null=True, blank=True)
    strategy_snapshot = models.JSONField(default=dict)
    capital_before = models.DecimalField(max_digits=24, decimal_places=8)
    target_capital = models.DecimalField(max_digits=24, decimal_places=8)
    deficit = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    surplus = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    idle_cash = models.DecimalField(max_digits=24, decimal_places=8, default=0)


class AllocationDecision(models.Model):
    run = models.ForeignKey(AllocationRun, on_delete=models.PROTECT, related_name="decisions")
    strategy_instance = models.ForeignKey("strategies.StrategyInstance", on_delete=models.SET_NULL, null=True, blank=True)
    strategy_snapshot = models.JSONField(default=dict)
    source = models.CharField(max_length=40)
    requested_amount = models.DecimalField(max_digits=24, decimal_places=8)
    approved_amount = models.DecimalField(max_digits=24, decimal_places=8)
    binding_constraint = models.CharField(max_length=64, blank=True)
    liquidation_required = models.BooleanField(default=False)
    rank = models.PositiveIntegerField(default=0)
    details = models.JSONField(default=dict)


class RebalancePolicy(models.Model):
    portfolio = models.OneToOneField("portfolios.TradingPortfolio", on_delete=models.PROTECT, related_name="rebalance_policy")
    instrument_drift_threshold = models.DecimalField(max_digits=10, decimal_places=8, default="0.01")
    portfolio_drift_threshold = models.DecimalField(max_digits=10, decimal_places=8, default="0.05")
    minimum_trade_notional = models.DecimalField(max_digits=24, decimal_places=8, default="10")
    minimum_trade_quantity = models.DecimalField(max_digits=24, decimal_places=8, default="0.00000001")
    cash_buffer_percent = models.DecimalField(max_digits=10, decimal_places=8, default="0.02")
    fee_buffer = models.DecimalField(max_digits=24, decimal_places=8, default="1")
    maximum_turnover = models.DecimalField(max_digits=10, decimal_places=8, default="0.25")
    sell_before_buy = models.BooleanField(default=True)
    price_staleness_limit = models.PositiveIntegerField(default=300)
    partial_fill_threshold = models.DecimalField(max_digits=8, decimal_places=6, default="0.95")
    mode = models.CharField(
        max_length=16, choices=ExecutionMode.choices, default=ExecutionMode.PAPER
    )
    enabled = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(mode__in=ExecutionMode.values),
                name="rebalance_policy_valid_execution_mode",
            ),
        ]


class PortfolioTargetSnapshot(models.Model):
    STATUSES = [(value, value) for value in ["READY", "REJECTED"]]
    MODES = ExecutionMode.choices

    portfolio = models.ForeignKey(
        "portfolios.TradingPortfolio",
        on_delete=models.PROTECT,
        related_name="target_snapshots",
    )
    logical_evaluation_time = models.DateTimeField()
    source_strategy_runs = models.JSONField(default=list)
    strategy_versions = models.JSONField(default=dict)
    target_contributions = models.JSONField(default=list)
    target_ages = models.JSONField(default=list)
    net_targets = models.JSONField(default=dict)
    net_target_quantities = models.JSONField(default=dict)
    account_nav = models.DecimalField(max_digits=24, decimal_places=8)
    portfolio_nav = models.DecimalField(max_digits=24, decimal_places=8)
    available_cash = models.DecimalField(max_digits=24, decimal_places=8)
    current_positions = models.JSONField(default=dict)
    open_orders = models.JSONField(default=list)
    exposure_reservations = models.JSONField(default=list)
    reference_prices = models.JSONField(default=dict)
    broker_reconciliation_generation = models.CharField(max_length=160, blank=True)
    execution_mode = models.CharField(max_length=16, choices=MODES)
    portfolio_order_policy = models.JSONField(default=dict)
    portfolio_risk_limits = models.JSONField(default=dict)
    rejected_targets = models.JSONField(default=list)
    idempotency_key = models.CharField(max_length=128, unique=True)
    status = models.CharField(max_length=16, choices=STATUSES, default="READY")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["portfolio", "-logical_evaluation_time"],
                name="target_snapshot_port_time_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(execution_mode__in=ExecutionMode.values),
                name="target_snapshot_valid_execution_mode",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk and PortfolioTargetSnapshot.objects.filter(pk=self.pk).exists():
            raise ValidationError("PortfolioTargetSnapshot records are immutable")
        return super().save(*args, **kwargs)


class RebalanceRun(models.Model):
    portfolio = models.ForeignKey("portfolios.TradingPortfolio", on_delete=models.PROTECT)
    policy = models.ForeignKey(RebalancePolicy, on_delete=models.PROTECT, null=True, blank=True)
    target_snapshot = models.OneToOneField(
        PortfolioTargetSnapshot,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="rebalance_run",
    )
    optimization_run = models.ForeignKey("portfolio_optimization.PortfolioOptimizationRun", on_delete=models.PROTECT, null=True, blank=True, related_name="rebalances")
    construction_run = models.ForeignKey("portfolio_construction.PortfolioConstructionRun", on_delete=models.PROTECT, null=True, blank=True, related_name="rebalances")
    target_source = models.CharField(max_length=40, default="STRATEGY_AGGREGATION")
    trigger = models.CharField(max_length=40)
    idempotency_key = models.CharField(max_length=128, unique=True)
    request_hash = models.CharField(max_length=64, default="", db_index=True)
    retryable = models.BooleanField(default=False)
    last_error = models.CharField(max_length=1000, blank=True)
    attempt_count = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=24, default="CALCULATING")
    phase = models.CharField(max_length=24, default="PLANNING")
    mode = models.CharField(
        max_length=16, choices=ExecutionMode.choices, default=ExecutionMode.PAPER
    )
    run_type = models.CharField(
        max_length=16, choices=RunType.choices, default=RunType.EXECUTION
    )
    nav = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    snapshot = models.JSONField(default=dict)
    total_drift = models.DecimalField(max_digits=18, decimal_places=10, default=0)
    planned_turnover = models.DecimalField(max_digits=18, decimal_places=10, default=0)
    last_recalculated_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    automatic = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=["portfolio","status","-created_at"],name="rebalance_port_status_idx"),
            models.Index(fields=["status","mode","phase"],name="rebalance_recovery_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(mode__in=ExecutionMode.values),
                name="rebalance_run_valid_execution_mode",
            ),
            models.CheckConstraint(
                condition=models.Q(run_type__in=RunType.values),
                name="rebalance_run_valid_run_type",
            ),
            models.UniqueConstraint(
                fields=["portfolio"],
                condition=models.Q(
                    automatic=True,
                    status__in=["QUEUED", "CALCULATING", "INTENTS_CREATED", "EXECUTING"],
                ),
                name="unique_active_auto_rebalance",
            ),
        ]


class PortfolioTargetCoordination(models.Model):
    STATUSES = [(value, value) for value in ["IDLE", "PENDING", "CLAIMED", "ACTIVE", "ERROR"]]

    portfolio = models.OneToOneField(
        "portfolios.TradingPortfolio",
        on_delete=models.PROTECT,
        related_name="target_coordination",
    )
    needs_coordination = models.BooleanField(default=False)
    pending_recalculation = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=STATUSES, default="IDLE")
    requested_at = models.DateTimeField(null=True, blank=True)
    debounce_until = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    attempt_count = models.PositiveIntegerField(default=0)
    logical_event_time = models.DateTimeField(null=True, blank=True)
    active_rebalance = models.ForeignKey(
        RebalanceRun,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    last_snapshot = models.ForeignKey(
        PortfolioTargetSnapshot,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    last_error = models.CharField(max_length=1000, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["needs_coordination", "debounce_until", "status"],
                name="target_coordination_queue_idx",
            ),
        ]

class TargetPortfolioPosition(models.Model):
    rebalance = models.ForeignKey(RebalanceRun, on_delete=models.PROTECT, related_name="targets")
    instrument = models.ForeignKey("instruments.Instrument", on_delete=models.PROTECT)
    target_weight = models.DecimalField(max_digits=12, decimal_places=8)
    target_quantity = models.DecimalField(max_digits=24, decimal_places=8)
    trade_quantity = models.DecimalField(max_digits=24, decimal_places=8)
    reference_price = models.DecimalField(max_digits=24, decimal_places=8)
    current_quantity = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    current_weight = models.DecimalField(max_digits=12, decimal_places=8, default=0)
    drift = models.DecimalField(max_digits=12, decimal_places=8, default=0)
    lot_size = models.DecimalField(max_digits=24, decimal_places=8, default=1)
    estimated_cost = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    suppressed = models.BooleanField(default=False)
    suppression_reason = models.CharField(max_length=128, blank=True)
    rank = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["rebalance", "instrument"], name="unique_rebalance_target")]


class OrderIntentAttribution(models.Model):
    order_intent = models.ForeignKey("oms.OrderIntent", on_delete=models.PROTECT, related_name="attributions")
    strategy_instance = models.ForeignKey("strategies.StrategyInstance", on_delete=models.SET_NULL, null=True, blank=True)
    strategy_version = models.ForeignKey("strategies.StrategyVersion", on_delete=models.SET_NULL, null=True, blank=True)
    strategy_snapshot = models.JSONField(default=dict)
    target_delta = models.DecimalField(max_digits=24, decimal_places=8)
    allocated_quantity = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    allocated_value = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    allocated_cost = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    realized_pnl = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    method = models.CharField(max_length=40, default="PRO_RATA_TARGET_DELTA")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["order_intent", "strategy_instance"], name="unique_intent_strategy_instance_attribution")]
