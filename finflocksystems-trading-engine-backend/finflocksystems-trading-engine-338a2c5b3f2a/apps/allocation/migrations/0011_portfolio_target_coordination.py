# Generated manually for the durable portfolio target coordination workflow.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("allocation", "0010_rebalancerun_construction_run"),
    ]

    operations = [
        migrations.CreateModel(
            name="PortfolioTargetSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("logical_evaluation_time", models.DateTimeField()),
                ("source_strategy_runs", models.JSONField(default=list)),
                ("strategy_versions", models.JSONField(default=dict)),
                ("target_contributions", models.JSONField(default=list)),
                ("target_ages", models.JSONField(default=list)),
                ("net_targets", models.JSONField(default=dict)),
                ("account_nav", models.DecimalField(decimal_places=8, max_digits=24)),
                ("portfolio_nav", models.DecimalField(decimal_places=8, max_digits=24)),
                ("available_cash", models.DecimalField(decimal_places=8, max_digits=24)),
                ("current_positions", models.JSONField(default=dict)),
                ("open_orders", models.JSONField(default=list)),
                ("exposure_reservations", models.JSONField(default=list)),
                ("reference_prices", models.JSONField(default=dict)),
                ("broker_reconciliation_generation", models.CharField(blank=True, max_length=160)),
                ("execution_mode", models.CharField(choices=[("SHADOW", "SHADOW"), ("PAPER", "PAPER")], max_length=16)),
                ("portfolio_order_policy", models.JSONField(default=dict)),
                ("portfolio_risk_limits", models.JSONField(default=dict)),
                ("rejected_targets", models.JSONField(default=list)),
                ("idempotency_key", models.CharField(max_length=128, unique=True)),
                ("status", models.CharField(choices=[("READY", "READY"), ("REJECTED", "REJECTED")], default="READY", max_length=16)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "portfolio",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="target_snapshots",
                        to="portfolios.tradingportfolio",
                    ),
                ),
            ],
        ),
        migrations.AddField(
            model_name="rebalancerun",
            name="automatic",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="rebalancerun",
            name="target_snapshot",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="rebalance_run",
                to="allocation.portfoliotargetsnapshot",
            ),
        ),
        migrations.CreateModel(
            name="PortfolioTargetCoordination",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("needs_coordination", models.BooleanField(default=False)),
                ("pending_recalculation", models.BooleanField(default=False)),
                ("status", models.CharField(choices=[("IDLE", "IDLE"), ("PENDING", "PENDING"), ("CLAIMED", "CLAIMED"), ("ACTIVE", "ACTIVE"), ("ERROR", "ERROR")], default="IDLE", max_length=16)),
                ("requested_at", models.DateTimeField(blank=True, null=True)),
                ("debounce_until", models.DateTimeField(blank=True, null=True)),
                ("logical_event_time", models.DateTimeField(blank=True, null=True)),
                ("last_error", models.CharField(blank=True, max_length=1000)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "active_rebalance",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="allocation.rebalancerun",
                    ),
                ),
                (
                    "last_snapshot",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="allocation.portfoliotargetsnapshot",
                    ),
                ),
                (
                    "portfolio",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="target_coordination",
                        to="portfolios.tradingportfolio",
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="portfoliotargetsnapshot",
            index=models.Index(fields=["portfolio", "-logical_evaluation_time"], name="target_snapshot_port_time_idx"),
        ),
        migrations.AddIndex(
            model_name="portfoliotargetcoordination",
            index=models.Index(fields=["needs_coordination", "debounce_until", "status"], name="target_coordination_queue_idx"),
        ),
        migrations.AddConstraint(
            model_name="rebalancerun",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("automatic", True),
                    ("status__in", ["QUEUED", "CALCULATING", "INTENTS_CREATED", "EXECUTING"]),
                ),
                fields=("portfolio",),
                name="unique_active_auto_rebalance",
            ),
        ),
    ]
