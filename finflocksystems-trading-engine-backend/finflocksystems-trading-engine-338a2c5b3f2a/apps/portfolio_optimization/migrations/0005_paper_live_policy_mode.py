from django.db import migrations, models


def migrate_policy_modes(apps, schema_editor):
    Policy = apps.get_model(
        "portfolio_optimization",
        "PortfolioOptimizationPolicy",
    )
    for policy in Policy.objects.select_related("portfolio__gateway_session"):
        session = getattr(policy.portfolio, "gateway_session", None)
        derived = "LIVE" if session and session.mode == "live" else "PAPER"
        Policy.objects.filter(pk=policy.pk).update(execution_mode=derived)


class Migration(migrations.Migration):

    dependencies = [
        ("allocation", "0012_paper_live_and_rebalance_run_type"),
        ("portfolio_optimization", "0004_alter_portfoliooptimizationrun_application_status"),
    ]

    operations = [
        migrations.RunPython(migrate_policy_modes, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="portfoliooptimizationpolicy",
            name="execution_mode",
            field=models.CharField(
                choices=[("PAPER", "Paper"), ("LIVE", "Live")],
                default="PAPER",
                max_length=16,
            ),
        ),
        migrations.AddConstraint(
            model_name="portfoliooptimizationpolicy",
            constraint=models.CheckConstraint(
                condition=models.Q(execution_mode__in=["PAPER", "LIVE"]),
                name="optimization_policy_valid_execution_mode",
            ),
        ),
    ]
