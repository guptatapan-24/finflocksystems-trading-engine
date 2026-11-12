import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("strategies", "0003_warmup_runtime_timestamps")]
    operations = [
        migrations.CreateModel(
            name="StrategyAction",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("action", models.CharField(max_length=24)),
                ("idempotency_key", models.CharField(max_length=128, unique=True)),
                ("request_hash", models.CharField(db_index=True, max_length=64)),
                ("status", models.CharField(default="PROCESSING", max_length=24)),
                ("result", models.JSONField(default=dict)),
                ("last_error", models.CharField(blank=True, max_length=1000)),
                ("retryable", models.BooleanField(default=False)),
                ("attempt_count", models.PositiveIntegerField(default=1)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("strategy_instance", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="actions", to="strategies.strategyinstance")),
            ],
        )
    ]
