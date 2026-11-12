from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("portfolio_optimization", "0002_portfoliooptimizationrun_application_idempotency_key_and_more")]
    operations = [
        migrations.AddField(model_name="portfoliooptimizationrun", name="request_hash", field=models.CharField(db_index=True, default="", max_length=64)),
        migrations.AddField(model_name="portfoliooptimizationrun", name="retryable", field=models.BooleanField(default=False)),
        migrations.AddField(model_name="portfoliooptimizationrun", name="last_error", field=models.CharField(blank=True, max_length=1000)),
        migrations.AddField(model_name="portfoliooptimizationrun", name="attempt_count", field=models.PositiveIntegerField(default=1)),
    ]
