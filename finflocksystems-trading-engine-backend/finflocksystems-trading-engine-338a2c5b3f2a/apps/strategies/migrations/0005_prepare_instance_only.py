import django.db.models.deletion
from django.db import migrations, models


def backfill_instance_identity(apps, schema_editor):
    StrategyAllocation = apps.get_model("strategies", "StrategyAllocation")
    StrategyInstance = apps.get_model("strategies", "StrategyInstance")
    StrategyRun = apps.get_model("strategies", "StrategyRun")

    for instance in StrategyInstance.objects.exclude(legacy_strategy_id=None).iterator():
        legacy = instance.legacy_strategy
        StrategyInstance.objects.filter(pk=instance.pk).update(
            allocated_capital=legacy.allocated_capital,
            kill_switch=legacy.kill_switch,
        )
        StrategyAllocation.objects.filter(
            strategy_id=instance.legacy_strategy_id,
            strategy_instance_id=None,
        ).update(strategy_instance_id=instance.pk)
        StrategyRun.objects.filter(
            strategy_id=instance.legacy_strategy_id,
            strategy_instance_id=None,
        ).update(strategy_instance_id=instance.pk)

    if StrategyAllocation.objects.filter(strategy_instance_id=None).exists():
        raise RuntimeError(
            "Cannot migrate legacy strategy allocations without a StrategyInstance; "
            "create the missing instance mapping before retrying the migration."
        )
    if StrategyRun.objects.filter(strategy_id__isnull=False, strategy_instance_id=None).exists():
        raise RuntimeError(
            "Cannot migrate legacy strategy runs without a StrategyInstance; "
            "create the missing instance mapping before retrying the migration."
        )


class Migration(migrations.Migration):
    dependencies = [("strategies", "0004_strategyaction")]

    operations = [
        migrations.AddField(
            model_name="strategyinstance",
            name="allocated_capital",
            field=models.DecimalField(decimal_places=8, default=0, max_digits=24),
        ),
        migrations.AddField(
            model_name="strategyinstance",
            name="kill_switch",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="strategyallocation",
            name="strategy_instance",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="allocations",
                to="strategies.strategyinstance",
            ),
        ),
        migrations.RunPython(backfill_instance_identity, migrations.RunPython.noop),
    ]
