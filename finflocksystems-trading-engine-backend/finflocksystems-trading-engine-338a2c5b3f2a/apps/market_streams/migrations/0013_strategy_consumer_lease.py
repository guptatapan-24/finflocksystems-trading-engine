from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("market_streams", "0012_delayed_market_data_provenance")]

    operations = [
        migrations.AlterField(
            model_name="marketdataconsumerlease",
            name="consumer_type",
            field=models.CharField(
                choices=[
                    ("MANUAL", "MANUAL"), ("WATCHLIST", "WATCHLIST"),
                    ("MONITORING", "MONITORING"), ("RISK", "RISK"),
                    ("STRATEGY", "STRATEGY"),
                ],
                max_length=16,
            ),
        ),
    ]
