from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("oms", "0005_orderintent_strategy_snapshot_and_more")]
    operations = [
        migrations.AddField(model_name="orderintent", name="request_hash", field=models.CharField(db_index=True, default="", max_length=64)),
        migrations.AddField(model_name="orderintent", name="operation_status", field=models.CharField(default="PENDING", max_length=24)),
        migrations.AddField(model_name="orderintent", name="operation_error", field=models.CharField(blank=True, max_length=1000)),
        migrations.AddField(model_name="orderintent", name="retryable", field=models.BooleanField(default=False)),
        migrations.AddField(model_name="orderintent", name="attempt_count", field=models.PositiveIntegerField(default=1)),
    ]
