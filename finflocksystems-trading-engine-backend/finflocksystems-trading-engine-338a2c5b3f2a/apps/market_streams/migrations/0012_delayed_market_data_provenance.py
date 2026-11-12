from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("market_streams", "0011_marketdataconsumerlease"),
    ]

    operations = [
        migrations.AddField(
            model_name="marketbar",
            name="market_data_type",
            field=models.CharField(
                choices=[("LIVE", "LIVE"), ("DELAYED", "DELAYED")],
                default="LIVE",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="marketbar",
            name="received_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="instrumentmarketstate",
            name="market_data_type",
            field=models.CharField(
                choices=[("LIVE", "LIVE"), ("DELAYED", "DELAYED")],
                default="LIVE",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="instrumentmarketstate",
            name="market_event_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
