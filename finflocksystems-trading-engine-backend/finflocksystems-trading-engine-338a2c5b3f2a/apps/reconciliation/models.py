from django.db import models

class ReconciliationRun(models.Model):
    gateway_session = models.ForeignKey(
        "broker_gateway.BrokerGatewaySession",on_delete=models.PROTECT,related_name="reconciliation_runs",
        null=True,blank=True)
    broker_account = models.ForeignKey(
        "accounts.BrokerAccount",
        on_delete=models.PROTECT,
        related_name="reconciliation_runs",
        null=True,
        blank=True,
    )
    trigger = models.CharField(max_length=40)
    status = models.CharField(max_length=24, default="RUNNING")
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

class ReconciliationBreak(models.Model):
    run = models.ForeignKey(ReconciliationRun, on_delete=models.PROTECT, related_name="breaks")
    category = models.CharField(max_length=32)
    severity = models.CharField(max_length=16)
    internal_value = models.JSONField(default=dict)
    broker_value = models.JSONField(default=dict)
    material = models.BooleanField(default=False)
    resolved = models.BooleanField(default=False)
    resolution = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["run", "category", "material"], name="recon_break_run_cat_idx"),
            models.Index(fields=["resolved", "material", "category"], name="recon_break_open_idx"),
        ]
