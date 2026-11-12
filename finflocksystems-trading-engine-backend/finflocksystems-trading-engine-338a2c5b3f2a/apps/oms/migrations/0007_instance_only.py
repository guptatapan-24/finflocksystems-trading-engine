from django.db import migrations


def backfill_order_intents(apps, schema_editor):
    OrderIntent = apps.get_model("oms", "OrderIntent")
    StrategyInstance = apps.get_model("strategies", "StrategyInstance")
    mappings = dict(
        StrategyInstance.objects.exclude(legacy_strategy_id=None).values_list(
            "legacy_strategy_id", "id"
        )
    )
    for legacy_id, instance_id in mappings.items():
        OrderIntent.objects.filter(
            strategy_id=legacy_id,
            strategy_instance_id=None,
        ).update(strategy_instance_id=instance_id)
    if OrderIntent.objects.filter(strategy_id__isnull=False, strategy_instance_id=None).exists():
        raise RuntimeError(
            "Cannot migrate an order intent whose legacy strategy has no StrategyInstance."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("oms", "0006_orderintent_idempotency_state"),
        ("strategies", "0005_prepare_instance_only"),
    ]

    operations = [
        migrations.RunPython(backfill_order_intents, migrations.RunPython.noop),
        migrations.RemoveField(model_name="orderintent", name="strategy"),
    ]
