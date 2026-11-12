import django.db.models.deletion
from django.db import migrations, models


def backfill_historical_identity(apps, schema_editor):
    AllocationDecision = apps.get_model("allocation", "AllocationDecision")
    OrderIntentAttribution = apps.get_model("allocation", "OrderIntentAttribution")
    StrategyCapitalSnapshot = apps.get_model("allocation", "StrategyCapitalSnapshot")
    StrategyInstance = apps.get_model("strategies", "StrategyInstance")
    TradingStrategy = apps.get_model("strategies", "TradingStrategy")

    mappings = dict(
        StrategyInstance.objects.exclude(legacy_strategy_id=None).values_list(
            "legacy_strategy_id", "id"
        )
    )
    legacy_names = dict(TradingStrategy.objects.values_list("id", "name"))
    for model in (StrategyCapitalSnapshot, AllocationDecision, OrderIntentAttribution):
        for record in model.objects.exclude(strategy_id=None).iterator():
            updates = {}
            instance_id = mappings.get(record.strategy_id)
            if instance_id and not record.strategy_instance_id:
                updates["strategy_instance_id"] = instance_id
            if not record.strategy_snapshot:
                updates["strategy_snapshot"] = {
                    "legacy_strategy_id": record.strategy_id,
                    "strategy_name": legacy_names.get(record.strategy_id, ""),
                    "strategy_instance_id": instance_id,
                }
            if updates:
                model.objects.filter(pk=record.pk).update(**updates)


class Migration(migrations.Migration):
    dependencies = [
        ("allocation", "0007_operation_idempotency_state"),
        ("oms", "0007_instance_only"),
        ("strategies", "0005_prepare_instance_only"),
    ]

    operations = [
        migrations.AddField(
            model_name="strategycapitalsnapshot",
            name="strategy_instance",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to="strategies.strategyinstance",
            ),
        ),
        migrations.AddField(
            model_name="allocationdecision",
            name="strategy_instance",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to="strategies.strategyinstance",
            ),
        ),
        migrations.RunPython(backfill_historical_identity, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="orderintentattribution",
            name="unique_intent_strategy_attribution",
        ),
        migrations.RemoveField(model_name="strategycapitalsnapshot", name="strategy"),
        migrations.RemoveField(model_name="allocationdecision", name="strategy"),
        migrations.RemoveField(model_name="orderintentattribution", name="strategy"),
        migrations.AddConstraint(
            model_name="orderintentattribution",
            constraint=models.UniqueConstraint(
                fields=("order_intent", "strategy_instance"),
                name="unique_intent_strategy_instance_attribution",
            ),
        ),
    ]
