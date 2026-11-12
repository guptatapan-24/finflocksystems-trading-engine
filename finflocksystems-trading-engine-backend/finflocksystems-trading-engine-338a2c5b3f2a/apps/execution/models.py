from django.db import models

from .modes import ExecutionMode


class BrokerCommand(models.Model):
    class CommandType(models.TextChoices):
        PLACE = "PLACE", "Place"
        MODIFY = "MODIFY", "Modify"
        CANCEL = "CANCEL", "Cancel"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        CLAIMED = "CLAIMED", "Claimed"
        SENDING = "SENDING", "Sending"
        RETRY = "RETRY", "Retry"
        UNCERTAIN = "UNCERTAIN", "Uncertain"
        ACKNOWLEDGED = "ACKNOWLEDGED", "Acknowledged"
        FAILED = "FAILED", "Failed"

    order = models.ForeignKey(
        "oms.Order", on_delete=models.PROTECT, related_name="broker_commands"
    )
    internal_order_id = models.CharField(max_length=64, db_index=True)
    gateway_session = models.ForeignKey(
        "broker_gateway.BrokerGatewaySession",
        on_delete=models.PROTECT,
        related_name="broker_commands",
    )
    command_type = models.CharField(max_length=8, choices=CommandType.choices)
    idempotency_key = models.CharField(max_length=128, unique=True)
    request_payload = models.JSONField(default=dict)
    request_hash = models.CharField(max_length=64, db_index=True)
    mode = models.CharField(
        max_length=16, choices=ExecutionMode.choices, default=ExecutionMode.PAPER
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING
    )
    attempt_count = models.PositiveIntegerField(default=0)
    claimed_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    response_payload = models.JSONField(default=dict)
    last_error = models.CharField(max_length=1000, blank=True)
    uncertainty_reason = models.CharField(max_length=1000, blank=True)
    broker_order_id = models.CharField(max_length=64, blank=True)
    broker_permanent_id = models.CharField(max_length=64, blank=True)
    gateway_command_id = models.PositiveBigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["status", "next_attempt_at", "created_at"],
                name="broker_cmd_dispatch_idx",
            ),
            models.Index(
                fields=["gateway_session", "internal_order_id"],
                name="broker_cmd_session_order_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(mode__in=ExecutionMode.values),
                name="broker_command_valid_execution_mode",
            ),
        ]


class Fill(models.Model):
    order = models.ForeignKey("oms.Order", on_delete=models.PROTECT, related_name="fills")
    execution_id = models.CharField(max_length=128, unique=True)
    quantity = models.DecimalField(max_digits=24, decimal_places=8)
    price = models.DecimalField(max_digits=24, decimal_places=8)
    commission = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    currency = models.CharField(max_length=8, default="USD")
    executed_at = models.DateTimeField()
    raw_event = models.JSONField(default=dict)
