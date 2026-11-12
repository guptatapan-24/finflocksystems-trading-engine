from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("strategies", "0011_activation_lifecycle"),
    ]

    operations = [
        migrations.AddField(
            model_name="strategyinstance",
            name="subscription_ready_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="strategyinstance",
            name="warmup_completed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="strategyinstance",
            name="ready_waiting_since",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="strategyinstance",
            name="first_evaluation_completed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="strategyinstance",
            name="execution_active_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="StrategyWarmupReadiness",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("provider", models.CharField(max_length=16)),
                ("provider_generation", models.CharField(max_length=64)),
                ("requirement_hashes", models.JSONField(default=list)),
                ("requirement_snapshot_hash", models.CharField(max_length=64)),
                ("bar_ids", models.JSONField(default=list)),
                ("bar_timestamps", models.JSONField(default=list)),
                ("evidence_hash", models.CharField(max_length=64, unique=True)),
                ("is_current", models.BooleanField(default=True)),
                ("completed_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("strategy_instance", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="warmup_readiness_records", to="strategies.strategyinstance")),
                ("strategy_version", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="warmup_readiness_records", to="strategies.strategyversion")),
            ],
            options={
                "indexes": [
                    models.Index(fields=["strategy_instance", "strategy_version", "-completed_at"], name="strategy_warmup_audit_idx"),
                ],
            },
        ),
    ]
