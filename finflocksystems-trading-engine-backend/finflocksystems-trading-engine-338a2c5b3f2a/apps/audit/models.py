import uuid
from django.db import models
from django.utils import timezone

class AuditEvent(models.Model):
    event_type = models.CharField(max_length=64)
    actor = models.CharField(max_length=128)
    aggregate_type = models.CharField(max_length=64)
    aggregate_id = models.CharField(max_length=128)
    data = models.JSONField(default=dict)
    idempotency_key = models.CharField(max_length=128, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["aggregate_type","aggregate_id","-created_at"],name="audit_aggregate_time_idx")]

class OutboxEvent(models.Model):
    STATUSES = [(x, x) for x in ["PENDING", "PUBLISHING", "PUBLISHED", "FAILED"]]
    event_id = models.UUIDField(unique=True, editable=False, default=uuid.uuid4)
    topic = models.CharField(max_length=128)
    partition_key = models.CharField(max_length=128, default="")
    event_type = models.CharField(max_length=128, default="")
    schema_version = models.PositiveIntegerField(default=1)
    aggregate_type = models.CharField(max_length=64, default="system")
    aggregate_id = models.CharField(max_length=128)
    payload = models.JSONField(default=dict)
    correlation_id = models.UUIDField(null=True, blank=True)
    causation_id = models.UUIDField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=128, unique=True)
    status = models.CharField(max_length=16, choices=STATUSES, default="PENDING")
    attempt_count = models.PositiveIntegerField(default=0)
    available_at = models.DateTimeField(default=timezone.now)
    published_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["status","available_at","created_at"],name="outbox_publish_queue_idx")]

    def save(self, *args, **kwargs):
        import uuid
        from django.utils import timezone
        if not self.event_id:
            self.event_id = uuid.uuid4()
        if not self.partition_key:
            self.partition_key = str(self.aggregate_id)
        if not self.event_type:
            self.event_type = self.topic
        if not self.available_at:
            self.available_at = timezone.now()
        super().save(*args, **kwargs)

    @property
    def attempts(self):
        return self.attempt_count


class OperationAttempt(models.Model):
    operation_type = models.CharField(max_length=40)
    operation_id = models.CharField(max_length=64)
    attempt_number = models.PositiveIntegerField()
    request_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=24, default="PROCESSING")
    retryable = models.BooleanField(default=False)
    error = models.CharField(max_length=1000, blank=True)
    result = models.JSONField(default=dict)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["operation_type", "operation_id", "attempt_number"],
            name="unique_operation_attempt",
        )]
        indexes = [models.Index(fields=["operation_type", "status"], name="operation_attempt_status_idx")]
