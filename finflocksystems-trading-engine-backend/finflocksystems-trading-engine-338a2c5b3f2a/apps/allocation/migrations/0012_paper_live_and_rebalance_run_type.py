from django.db import migrations, models


def migrate_rebalance_modes(apps, schema_editor):
    RebalancePolicy = apps.get_model("allocation", "RebalancePolicy")
    PortfolioTargetSnapshot = apps.get_model("allocation", "PortfolioTargetSnapshot")
    RebalanceRun = apps.get_model("allocation", "RebalanceRun")

    legacy_run_ids = list(
        RebalanceRun.objects.exclude(mode__in=["PAPER", "LIVE"]).values_list(
            "pk", flat=True
        )
    )
    RebalanceRun.objects.filter(pk__in=legacy_run_ids).update(
        run_type="PREVIEW",
    )
    RebalanceRun.objects.filter(
        pk__in=legacy_run_ids,
        phase="SHADOW_COMPLETE",
    ).update(phase="PREVIEW_COMPLETE")

    for model, field_name in (
        (RebalancePolicy, "mode"),
        (PortfolioTargetSnapshot, "execution_mode"),
        (RebalanceRun, "mode"),
    ):
        for row in model.objects.select_related("portfolio__gateway_session"):
            session = getattr(row.portfolio, "gateway_session", None)
            derived = "LIVE" if session and session.mode == "live" else "PAPER"
            updates = {field_name: derived}
            if model is RebalanceRun and row.mode != derived:
                updates.update(
                    run_type="PREVIEW",
                    phase="PREVIEW_COMPLETE",
                    status="PLANNED",
                )
            model.objects.filter(pk=row.pk).update(**updates)


class Migration(migrations.Migration):

    dependencies = [
        ("allocation", "0011_portfolio_target_coordination"),
        ("portfolios", "0004_tradingportfolio_gateway_session"),
        ("strategies", "0010_paper_live_execution_modes"),
    ]

    operations = [
        migrations.AddField(
            model_name="rebalancerun",
            name="run_type",
            field=models.CharField(
                choices=[("PREVIEW", "Preview"), ("EXECUTION", "Execution")],
                default="EXECUTION",
                max_length=16,
            ),
        ),
        migrations.RunPython(migrate_rebalance_modes, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="rebalancepolicy",
            name="mode",
            field=models.CharField(
                choices=[("PAPER", "Paper"), ("LIVE", "Live")],
                default="PAPER",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="portfoliotargetsnapshot",
            name="execution_mode",
            field=models.CharField(
                choices=[("PAPER", "Paper"), ("LIVE", "Live")],
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="rebalancerun",
            name="mode",
            field=models.CharField(
                choices=[("PAPER", "Paper"), ("LIVE", "Live")],
                default="PAPER",
                max_length=16,
            ),
        ),
        migrations.AddConstraint(
            model_name="rebalancepolicy",
            constraint=models.CheckConstraint(
                condition=models.Q(mode__in=["PAPER", "LIVE"]),
                name="rebalance_policy_valid_execution_mode",
            ),
        ),
        migrations.AddConstraint(
            model_name="portfoliotargetsnapshot",
            constraint=models.CheckConstraint(
                condition=models.Q(execution_mode__in=["PAPER", "LIVE"]),
                name="target_snapshot_valid_execution_mode",
            ),
        ),
        migrations.AddConstraint(
            model_name="rebalancerun",
            constraint=models.CheckConstraint(
                condition=models.Q(mode__in=["PAPER", "LIVE"]),
                name="rebalance_run_valid_execution_mode",
            ),
        ),
        migrations.AddConstraint(
            model_name="rebalancerun",
            constraint=models.CheckConstraint(
                condition=models.Q(run_type__in=["PREVIEW", "EXECUTION"]),
                name="rebalance_run_valid_run_type",
            ),
        ),
    ]
