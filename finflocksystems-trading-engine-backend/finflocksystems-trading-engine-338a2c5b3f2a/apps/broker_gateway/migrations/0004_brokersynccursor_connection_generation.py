from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("broker_gateway", "0003_brokergatewaysessionsecret_brokersessionaccount_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="brokersynccursor",
            name="connection_generation",
            field=models.CharField(blank=True, max_length=64),
        ),
    ]
