from django.db import migrations, models
import django.utils.timezone


def preserve_existing_occurrence_times(apps, schema_editor):
    history = apps.get_model("oms", "OrderStatusHistory")
    for item in history.objects.only("id", "created_at").iterator():
        history.objects.filter(pk=item.pk).update(occurred_at=item.created_at)


class Migration(migrations.Migration):
    dependencies = [("oms", "0003_orderintent_strategy_version_snapshot")]

    operations = [
        migrations.AddField(model_name="orderstatushistory", name="broker_status", field=models.CharField(blank=True, max_length=64)),
        migrations.AddField(model_name="orderstatushistory", name="details", field=models.JSONField(default=dict)),
        migrations.AddField(model_name="orderstatushistory", name="occurred_at", field=models.DateTimeField(default=django.utils.timezone.now)),
        migrations.AddField(model_name="orderstatushistory", name="operator_requested", field=models.BooleanField(default=False)),
        migrations.AddField(model_name="orderstatushistory", name="reason_code", field=models.CharField(blank=True, max_length=64)),
        migrations.RunPython(preserve_existing_occurrence_times, migrations.RunPython.noop),
    ]
