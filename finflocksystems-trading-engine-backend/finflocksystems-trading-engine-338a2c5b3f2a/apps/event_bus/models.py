from django.db import models


class ConsumedEvent(models.Model):
    consumer_name = models.CharField(max_length=128)
    event_id = models.UUIDField()
    processed_at = models.DateTimeField(auto_now_add=True)
    result = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["consumer_name", "event_id"], name="unique_consumed_event")]


class DeadLetterEvent(models.Model):
    event_id = models.UUIDField(null=True, blank=True)
    source_topic = models.CharField(max_length=128)
    consumer_name = models.CharField(max_length=128, blank=True)
    reason = models.CharField(max_length=255)
    envelope = models.JSONField(default=dict)
    replayed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["replayed_at","created_at"],name="dead_letter_replay_idx")]


class ReplayRequest(models.Model):
    STATUSES = [(x, x) for x in ["REQUESTED", "RUNNING", "COMPLETED", "FAILED"]]
    topic = models.CharField(max_length=128)
    consumer_name = models.CharField(max_length=128)
    from_timestamp = models.DateTimeField(null=True, blank=True)
    to_timestamp = models.DateTimeField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=128, unique=True)
    status = models.CharField(max_length=16, choices=STATUSES, default="REQUESTED")
    processed_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)


class StreamHealthMetric(models.Model):
    component = models.CharField(max_length=128)
    metric = models.CharField(max_length=128)
    status = models.CharField(max_length=24, default="UNKNOWN")
    value = models.JSONField(default=dict)
    observed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["component", "metric"], name="unique_stream_health_metric")]
