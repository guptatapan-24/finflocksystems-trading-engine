import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.execution.modes import normalize_gateway_mode


def normalize_broker_mode(value):
    try:
        return normalize_gateway_mode(value)
    except ValueError as exc:
        raise ValidationError("IBKR mode must be exactly paper or live") from exc


class BrokerGatewaySession(models.Model):
    class Mode(models.TextChoices):
        PAPER = "paper", "Paper"
        LIVE = "live", "Live"

    class MarketDataType(models.TextChoices):
        LIVE = "LIVE", "Live"
        DELAYED = "DELAYED", "Delayed"

    class Status(models.TextChoices):
        CREATING = "CREATING", "Creating"
        STARTING = "STARTING", "Starting"
        WAITING_FOR_LOGIN = "WAITING_FOR_LOGIN", "Waiting for login"
        WAITING_FOR_2FA = "WAITING_FOR_2FA", "Waiting for 2FA"
        CONNECTED = "CONNECTED", "Connected"
        DISCONNECTED = "DISCONNECTED", "Disconnected"
        LOGIN_FAILED = "LOGIN_FAILED", "Login failed"
        ERROR = "ERROR", "Error"
        STOPPING = "STOPPING", "Stopping"
        DELETED = "DELETED", "Deleted"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    display_name = models.CharField(max_length=128)
    username_hint = models.CharField(max_length=128)
    mode = models.CharField(max_length=8, choices=Mode.choices)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.CREATING)
    # What the IBKR login is entitled to, as its user declared it. The child
    # requests this type, and quotes are accepted only with the matching tick
    # type: an account with real-time subscriptions is served real time even
    # when it asks for delayed, so the declaration must match the entitlement.
    market_data_type = models.CharField(
        max_length=8, choices=MarketDataType.choices, default=MarketDataType.DELAYED, db_default=MarketDataType.DELAYED)
    child_container_id = models.CharField(max_length=160, blank=True)
    child_container_name = models.CharField(max_length=128, unique=True)
    internal_base_url = models.URLField(max_length=255, blank=True)
    encrypted_gateway_token = models.TextField()
    encrypted_novnc_password = models.TextField()
    commands_enabled = models.BooleanField(default=False)
    last_gateway_state = models.JSONField(default=dict, blank=True)
    last_qch_state = models.JSONField(default=dict, blank=True)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    provisioned_at = models.DateTimeField(null=True, blank=True)
    connected_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    lifecycle_version = models.PositiveBigIntegerField(default=1)

    class Meta:
        indexes = [
            models.Index(fields=["status", "deleted_at"], name="broker_session_status_idx"),
            models.Index(fields=["mode", "created_at"], name="broker_session_mode_idx"),
        ]

    def clean(self):
        self.mode = normalize_broker_mode(self.mode)
        if self.internal_base_url and self.child_container_name not in self.internal_base_url:
            raise ValidationError({"internal_base_url": "Gateway URL must use the recorded child container name"})

    def save(self, *args, **kwargs):
        self.mode = normalize_broker_mode(self.mode)
        if self.mode == self.Mode.LIVE:
            # The child refuses to start a live Gateway on delayed data.
            self.market_data_type = self.MarketDataType.LIVE
        return super().save(*args, **kwargs)

    @property
    def is_active(self):
        return self.deleted_at is None and self.status not in {self.Status.STOPPING, self.Status.DELETED}

    def mark_checked(self, *, status=None, gateway_state=None, qch_state=None, error=""):
        if status:
            self.status = status
        if gateway_state is not None:
            self.last_gateway_state = gateway_state
        if qch_state is not None:
            self.last_qch_state = qch_state
        self.last_error = str(error or "")[:4000]
        self.last_checked_at = timezone.now()


class BrokerGatewaySessionSecret(models.Model):
    session = models.OneToOneField(BrokerGatewaySession, on_delete=models.CASCADE, related_name="temporary_secret")
    encrypted_username = models.TextField()
    encrypted_password = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        indexes = [models.Index(fields=["expires_at"], name="broker_secret_expiry_idx")]


class BrokerSessionAccount(models.Model):
    session = models.ForeignKey(BrokerGatewaySession, on_delete=models.PROTECT, related_name="session_accounts")
    broker_account = models.ForeignKey(
        "accounts.BrokerAccount", on_delete=models.PROTECT, related_name="gateway_sessions"
    )
    broker_alias = models.CharField(max_length=128, blank=True)
    available = models.BooleanField(default=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["session", "broker_account"], name="unique_session_broker_account")
        ]
        indexes = [models.Index(fields=["session", "available"], name="broker_session_account_idx")]

class BrokerSyncCursor(models.Model):
    session = models.ForeignKey(
        BrokerGatewaySession, on_delete=models.PROTECT, related_name="sync_cursors", null=True, blank=True
    )
    name = models.CharField(max_length=64, default="gateway-events")
    connection_generation = models.CharField(max_length=64, blank=True)
    last_sequence = models.BigIntegerField(default=0)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=1000, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["session", "name"], name="unique_session_sync_cursor")]


class BrokerPositionSnapshot(models.Model):
    session = models.ForeignKey(
        BrokerGatewaySession, on_delete=models.PROTECT, related_name="position_snapshots", null=True, blank=True
    )
    broker_account = models.ForeignKey(
        "accounts.BrokerAccount",
        on_delete=models.PROTECT,
        related_name="position_snapshots",
    )
    snapshot_key = models.CharField(max_length=160, unique=True)
    complete = models.BooleanField(default=False)
    status = models.CharField(max_length=24, default="RECEIVED")
    row_count = models.PositiveIntegerField(default=0)
    positions = models.JSONField(default=list)
    attempt_count = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=1000, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["broker_account", "-received_at"], name="broker_pos_acct_received_idx"),
            models.Index(fields=["status", "received_at"], name="broker_pos_status_idx"),
        ]
