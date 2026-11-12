from django.db import migrations, models


def disable_inactive_warmups(apps, schema_editor):
    StrategyInstance=apps.get_model("strategies","StrategyInstance")
    StrategyInstance.objects.filter(
        enabled=False,
        state="WARMING_UP",
    ).update(state="DISABLED")


class Migration(migrations.Migration):

    dependencies=[
        ("strategies","0010_paper_live_execution_modes"),
    ]

    operations=[
        migrations.RunPython(disable_inactive_warmups,migrations.RunPython.noop),
        migrations.AlterField(
            model_name="strategyinstance",
            name="state",
            field=models.CharField(
                choices=[
                    ("FLAT","FLAT"),
                    ("ENTRY_PENDING","ENTRY_PENDING"),
                    ("PARTIALLY_LONG","PARTIALLY_LONG"),
                    ("LONG","LONG"),
                    ("EXIT_PENDING","EXIT_PENDING"),
                    ("PARTIALLY_SHORT","PARTIALLY_SHORT"),
                    ("SHORT","SHORT"),
                    ("PAUSED","PAUSED"),
                    ("DISABLED","DISABLED"),
                    ("FLATTEN_REQUESTED","FLATTEN_REQUESTED"),
                    ("KILLED","KILLED"),
                    ("ACTIVATING","ACTIVATING"),
                    ("SUBSCRIBING","SUBSCRIBING"),
                    ("WARMING_UP","WARMING_UP"),
                    ("READY_WAITING_FOR_LIVE_BAR","READY_WAITING_FOR_LIVE_BAR"),
                    ("BLOCKED","BLOCKED"),
                    ("ERROR","ERROR"),
                ],
                default="DISABLED",
                max_length=32,
            ),
        ),
    ]
