from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("strategies", "0008_full_input_identity_and_market_cursor"),
    ]

    operations = [
        migrations.AlterField(
            model_name="strategyinstance",
            name="state",
            field=models.CharField(
                choices=[
                    ("FLAT", "FLAT"),
                    ("ENTRY_PENDING", "ENTRY_PENDING"),
                    ("PARTIALLY_LONG", "PARTIALLY_LONG"),
                    ("LONG", "LONG"),
                    ("EXIT_PENDING", "EXIT_PENDING"),
                    ("PARTIALLY_SHORT", "PARTIALLY_SHORT"),
                    ("SHORT", "SHORT"),
                    ("PAUSED", "PAUSED"),
                    ("DISABLED", "DISABLED"),
                    ("FLATTEN_REQUESTED", "FLATTEN_REQUESTED"),
                    ("KILLED", "KILLED"),
                    ("BLOCKED", "BLOCKED"),
                    ("WARMING_UP", "WARMING_UP"),
                    ("ERROR", "ERROR"),
                ],
                default="WARMING_UP",
                max_length=24,
            ),
        ),
    ]
