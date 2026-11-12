from django.db import migrations, models


def backfill_order_origins(apps, schema_editor):
    OrderIntent = apps.get_model("oms", "OrderIntent")
    OrderIntent.objects.filter(
        models.Q(rebalance_id__isnull=False) | models.Q(source__iexact="REBALANCE")
    ).update(origin="REBALANCE")
    OrderIntent.objects.filter(idempotency_key__startswith="broker-import:").update(
        origin="BROKER_IMPORT"
    )
    OrderIntent.objects.filter(
        source__iexact="MANUAL",
        rebalance_id__isnull=True,
        strategy_instance_id__isnull=True,
        strategy_version_id__isnull=True,
    ).exclude(origin="BROKER_IMPORT").update(origin="MANUAL")


def reverse_order_origins(apps, schema_editor):
    OrderIntent = apps.get_model("oms", "OrderIntent")
    OrderIntent.objects.all().update(origin="STRATEGY")


class Migration(migrations.Migration):

    dependencies = [
        ("oms", "0008_order_order_status_updated_idx_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="orderintent",
            name="origin",
            field=models.CharField(
                choices=[
                    ("MANUAL", "Manual"),
                    ("STRATEGY", "Strategy"),
                    ("REBALANCE", "Rebalance"),
                    ("BROKER_IMPORT", "Broker import"),
                ],
                default="STRATEGY",
                max_length=16,
            ),
        ),
        migrations.RunPython(backfill_order_origins, reverse_order_origins),
        migrations.AddConstraint(
            model_name="orderintent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("origin__in", ["MANUAL", "STRATEGY", "REBALANCE", "BROKER_IMPORT"])
                ),
                name="order_intent_valid_origin",
            ),
        ),
    ]
