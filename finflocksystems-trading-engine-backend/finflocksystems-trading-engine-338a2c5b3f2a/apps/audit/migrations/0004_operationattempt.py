from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("audit", "0003_remove_legacy_attempts_column")]
    operations = [
        migrations.CreateModel(
            name="OperationAttempt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("operation_type", models.CharField(max_length=40)),
                ("operation_id", models.CharField(max_length=64)),
                ("attempt_number", models.PositiveIntegerField()),
                ("request_hash", models.CharField(max_length=64)),
                ("status", models.CharField(default="PROCESSING", max_length=24)),
                ("retryable", models.BooleanField(default=False)),
                ("error", models.CharField(blank=True, max_length=1000)),
                ("result", models.JSONField(default=dict)),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
            ],
        ),
        migrations.AddConstraint(
            model_name="operationattempt",
            constraint=models.UniqueConstraint(fields=("operation_type", "operation_id", "attempt_number"), name="unique_operation_attempt"),
        ),
        migrations.AddIndex(
            model_name="operationattempt",
            index=models.Index(fields=["operation_type", "status"], name="operation_attempt_status_idx"),
        ),
    ]
