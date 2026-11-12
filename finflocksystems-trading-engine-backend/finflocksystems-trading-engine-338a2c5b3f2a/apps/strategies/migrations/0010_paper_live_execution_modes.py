from django.db import migrations, models


def migrate_strategy_modes(apps, schema_editor):
    StrategyInstance = apps.get_model("strategies", "StrategyInstance")
    StrategyTarget = apps.get_model("strategies", "StrategyTarget")

    legacy_ids = list(
        StrategyInstance.objects.filter(
            execution_mode__in=["SHADOW", "OBSERVE"]
        ).values_list("pk", flat=True)
    )
    StrategyInstance.objects.filter(pk__in=legacy_ids).update(
        execution_mode="PAPER",
        enabled=False,
        state="PAUSED",
        block_reason="Paused by PAPER/LIVE execution-mode migration",
    )

    invalid_ids = list(
        StrategyInstance.objects.exclude(execution_mode__in=["PAPER", "LIVE"])
        .values_list("pk", flat=True)
    )
    StrategyInstance.objects.filter(pk__in=invalid_ids).update(
        execution_mode="PAPER",
        enabled=False,
        state="PAUSED",
        block_reason="Paused by PAPER/LIVE execution-mode migration",
    )

    for instance in StrategyInstance.objects.exclude(pk__in=legacy_ids).select_related(
        "portfolio__gateway_session"
    ):
        session = getattr(instance.portfolio, "gateway_session", None)
        derived = "LIVE" if session and session.mode == "live" else "PAPER"
        updates = {}
        if instance.execution_mode != derived:
            updates["execution_mode"] = derived
            updates["enabled"] = False
            updates["state"] = "PAUSED"
            updates["block_reason"] = (
                "Paused because execution mode changed to match the Gateway session"
            )
        if updates:
            StrategyInstance.objects.filter(pk=instance.pk).update(**updates)

    instance_modes = dict(
        StrategyInstance.objects.values_list("pk", "execution_mode")
    )
    for target in StrategyTarget.objects.all().only("pk", "strategy_instance_id"):
        StrategyTarget.objects.filter(pk=target.pk).update(
            execution_mode=instance_modes.get(target.strategy_instance_id, "PAPER")
        )


class Migration(migrations.Migration):

    dependencies = [
        ("portfolios", "0004_tradingportfolio_gateway_session"),
        ("strategies", "0009_strategy_lifecycle_states"),
    ]

    operations = [
        migrations.AddField(
            model_name="strategytarget",
            name="execution_mode",
            field=models.CharField(
                choices=[("PAPER", "Paper"), ("LIVE", "Live")],
                default="PAPER",
                max_length=16,
            ),
        ),
        migrations.RunPython(migrate_strategy_modes, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="strategyinstance",
            name="execution_mode",
            field=models.CharField(
                choices=[("PAPER", "Paper"), ("LIVE", "Live")],
                default="PAPER",
                max_length=16,
            ),
        ),
        migrations.AddConstraint(
            model_name="strategyinstance",
            constraint=models.CheckConstraint(
                condition=models.Q(execution_mode__in=["PAPER", "LIVE"]),
                name="strategy_instance_valid_execution_mode",
            ),
        ),
        migrations.AddConstraint(
            model_name="strategytarget",
            constraint=models.CheckConstraint(
                condition=models.Q(execution_mode__in=["PAPER", "LIVE"]),
                name="strategy_target_valid_execution_mode",
            ),
        ),
    ]
