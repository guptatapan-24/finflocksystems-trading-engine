from django.db import models
from django.utils import timezone

from apps.execution.modes import ExecutionMode

ORDER_STATES = [x for x in "CREATED RISK_APPROVED QUEUED BROKER_BLOCKED SUBMITTED ACKNOWLEDGED PARTIALLY_FILLED FILLED CANCEL_PENDING CANCELLED REJECTED EXPIRED UNKNOWN".split()]

class OrderIntent(models.Model):
    class Origin(models.TextChoices):
        MANUAL = "MANUAL", "Manual"
        STRATEGY = "STRATEGY", "Strategy"
        REBALANCE = "REBALANCE", "Rebalance"
        BROKER_IMPORT = "BROKER_IMPORT", "Broker import"

    rebalance = models.ForeignKey("allocation.RebalanceRun", on_delete=models.PROTECT, null=True, blank=True)
    portfolio = models.ForeignKey("portfolios.TradingPortfolio", on_delete=models.PROTECT)
    strategy_instance = models.ForeignKey("strategies.StrategyInstance", on_delete=models.SET_NULL, null=True, blank=True)
    strategy_version = models.ForeignKey("strategies.StrategyVersion", on_delete=models.SET_NULL, null=True, blank=True)
    strategy_snapshot = models.JSONField(default=dict)
    strategy_version_snapshot = models.JSONField(default=list)
    instrument = models.ForeignKey("instruments.Instrument", on_delete=models.PROTECT)
    side = models.CharField(max_length=4)
    quantity = models.DecimalField(max_digits=24, decimal_places=8)
    order_type = models.CharField(max_length=16, default="MKT")
    limit_price = models.DecimalField(max_digits=24, decimal_places=8, null=True, blank=True)
    stop_price = models.DecimalField(max_digits=24, decimal_places=8, null=True, blank=True)
    reference_price = models.DecimalField(max_digits=24, decimal_places=8, null=True, blank=True)
    time_in_force = models.CharField(max_length=8, default="DAY")
    idempotency_key = models.CharField(max_length=128, unique=True)
    request_hash = models.CharField(max_length=64, default="", db_index=True)
    operation_status = models.CharField(max_length=24, default="PENDING")
    operation_error = models.CharField(max_length=1000, blank=True)
    retryable = models.BooleanField(default=False)
    attempt_count = models.PositiveIntegerField(default=1)
    source = models.CharField(max_length=32, default="MANUAL")
    origin = models.CharField(max_length=16, choices=Origin.choices, default=Origin.STRATEGY)
    mode = models.CharField(
        max_length=16, choices=ExecutionMode.choices, default=ExecutionMode.PAPER
    )
    requires_fresh_price = models.BooleanField(default=False)
    execution_priority = models.PositiveIntegerField(default=100)
    eligible = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(mode__in=ExecutionMode.values),
                name="order_intent_valid_execution_mode",
            ),
            models.CheckConstraint(
                condition=models.Q(origin__in=["MANUAL", "STRATEGY", "REBALANCE", "BROKER_IMPORT"]),
                name="order_intent_valid_origin",
            ),
        ]
        indexes = [
            models.Index(fields=["portfolio", "-created_at"], name="intent_portfolio_created_idx"),
            models.Index(fields=["operation_status", "eligible", "created_at"], name="intent_operation_queue_idx"),
            models.Index(fields=["rebalance", "side"], name="intent_rebalance_side_idx"),
            models.Index(fields=["strategy_instance", "-created_at"], name="intent_strategy_created_idx"),
        ]

class Order(models.Model):
    intent = models.OneToOneField(OrderIntent, on_delete=models.PROTECT, related_name="order")
    internal_id = models.CharField(max_length=64, unique=True)
    broker_order_id = models.CharField(max_length=64, blank=True)
    broker_permanent_id = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=24, choices=[(x, x) for x in ORDER_STATES], default="CREATED")
    quantity = models.DecimalField(max_digits=24, decimal_places=8)
    filled_quantity = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    average_fill_price = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "updated_at"], name="order_status_updated_idx"),
            models.Index(fields=["broker_order_id"], name="order_broker_id_idx"),
            models.Index(fields=["broker_permanent_id"], name="order_permanent_id_idx"),
        ]

class OrderStatusHistory(models.Model):
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="status_history")
    from_status = models.CharField(max_length=24, blank=True)
    to_status = models.CharField(max_length=24)
    source = models.CharField(max_length=32)
    broker_status = models.CharField(max_length=64, blank=True)
    reason_code = models.CharField(max_length=64, blank=True)
    reason = models.CharField(max_length=255, blank=True)
    details = models.JSONField(default=dict)
    occurred_at = models.DateTimeField(default=timezone.now)
    operator_requested = models.BooleanField(default=False)
    event_key = models.CharField(max_length=128, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["order", "occurred_at"], name="order_history_time_idx")]
