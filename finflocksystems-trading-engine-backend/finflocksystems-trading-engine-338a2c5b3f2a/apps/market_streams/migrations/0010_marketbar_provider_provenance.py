from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("market_streams", "0009_remove_strategy_evaluation_readiness"),
    ]

    operations = [
        migrations.AddField(
            model_name="marketbar",
            name="provider",
            field=models.CharField(blank=True, max_length=16),
        ),
        migrations.AddField(
            model_name="marketbar",
            name="provider_generation",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="marketbar",
            name="source",
            field=models.CharField(blank=True, max_length=64),
        ),
    ]
