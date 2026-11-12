from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("allocation", "0006_allocationdecision_strategy_snapshot_and_more")]
    operations = [
        migrations.AddField(model_name="portfolioflow", name="request_hash", field=models.CharField(db_index=True, default="", max_length=64)),
        migrations.AddField(model_name="portfolioflow", name="retryable", field=models.BooleanField(default=False)),
        migrations.AddField(model_name="portfolioflow", name="last_error", field=models.CharField(blank=True, max_length=1000)),
        migrations.AddField(model_name="portfolioflow", name="attempt_count", field=models.PositiveIntegerField(default=1)),
        migrations.AddField(model_name="rebalancerun", name="request_hash", field=models.CharField(db_index=True, default="", max_length=64)),
        migrations.AddField(model_name="rebalancerun", name="retryable", field=models.BooleanField(default=False)),
        migrations.AddField(model_name="rebalancerun", name="last_error", field=models.CharField(blank=True, max_length=1000)),
        migrations.AddField(model_name="rebalancerun", name="attempt_count", field=models.PositiveIntegerField(default=1)),
    ]
