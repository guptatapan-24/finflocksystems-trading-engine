from django.db import models
import uuid

from .modes import normalize_trading_mode

class GatewaySession(models.Model):
    state = models.CharField(max_length=32, default="DISCONNECTED")
    mode = models.CharField(
        max_length=8,
        choices=[("paper", "Paper"), ("live", "Live")],
        default="paper",
    )
    reconciled = models.BooleanField(default=False)
    connection_owner = models.CharField(max_length=128, blank=True)
    connection_generation = models.UUIDField(default=uuid.uuid4)
    last_callback_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(mode__in=["paper", "live"]),
                name="gateway_session_valid_mode",
            ),
        ]

    def save(self, *args, **kwargs):
        self.mode = normalize_trading_mode(self.mode)
        return super().save(*args, **kwargs)

class GatewayCommand(models.Model):
    STATUSES = [(value, value) for value in ["PENDING", "PROCESSING", "COMPLETED", "FAILED", "UNKNOWN"]]
    TYPES = [(x,x) for x in ["RECONNECT","SEARCH_CONTRACTS","QUALIFY","REQUEST_HISTORICAL_DATA","REQUEST_HISTORICAL_SCHEDULE","SUBSCRIBE_MARKET_DATA","CANCEL_MARKET_DATA","PLACE_ORDER","MODIFY_ORDER","CANCEL_ORDER","KILL_SWITCH","REFRESH"]]
    command_type = models.CharField(max_length=32, choices=TYPES)
    # The Backend sends "session:<uuid>:<key>" capped at 255 characters.
    idempotency_key = models.CharField(max_length=255, unique=True)
    request_hash = models.CharField(max_length=64, default="", db_index=True)
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=24, choices=STATUSES, default="PENDING")
    result = models.JSONField(default=dict)
    last_error = models.CharField(max_length=1000, blank=True)
    retryable = models.BooleanField(default=False)
    claimed_by = models.CharField(max_length=128, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    attempt_count = models.PositiveIntegerField(default=0)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["status", "lease_expires_at", "id"], name="gateway_cmd_claim_idx")]


class GatewayCommandAttempt(models.Model):
    command = models.ForeignKey(GatewayCommand, on_delete=models.PROTECT, related_name="attempt_history")
    attempt_number = models.PositiveIntegerField()
    claimed_by = models.CharField(max_length=128)
    submission_state = models.CharField(max_length=32, default="CLAIMED")
    broker_result = models.JSONField(default=dict)
    error = models.CharField(max_length=1000, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["command", "attempt_number"], name="unique_gateway_command_attempt")
        ]

class GatewayEvent(models.Model):
    event_key = models.CharField(max_length=160, unique=True)
    event_type = models.CharField(max_length=64)
    payload = models.JSONField(default=dict)
    acknowledged = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["acknowledged","created_at"],name="gateway_event_ack_idx")]

class GatewayOrderReference(models.Model):
    internal_id = models.CharField(max_length=64, unique=True)
    broker_order_id = models.CharField(max_length=64, blank=True)
    permanent_id = models.CharField(max_length=64, blank=True)
    last_status = models.CharField(max_length=32, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

class GatewayHealthSnapshot(models.Model):
    connected = models.BooleanField(default=False)
    reconciled = models.BooleanField(default=False)
    broker_time = models.DateTimeField(null=True, blank=True)
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["-created_at"],name="gateway_health_time_idx")]
