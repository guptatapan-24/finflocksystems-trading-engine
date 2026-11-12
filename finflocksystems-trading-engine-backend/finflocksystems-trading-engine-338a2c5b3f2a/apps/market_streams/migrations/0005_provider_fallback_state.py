import uuid
import django.db.models.deletion
from django.db import migrations, models


def copy_primary_event_time(apps, schema_editor):
    Subscription = apps.get_model("market_streams", "MarketDataSubscription")
    for item in Subscription.objects.exclude(last_event_at=None).iterator():
        item.last_primary_event_at = item.last_event_at
        item.save(update_fields=["last_primary_event_at"])


def clear_primary_event_time(apps, schema_editor):
    apps.get_model("market_streams", "MarketDataSubscription").objects.update(last_primary_event_at=None)


class Migration(migrations.Migration):
    dependencies = [
        ("instruments", "0004_instrument_provider_mapping"),
        ("market_streams", "0004_strategyevaluationreadiness_and_more"),
    ]

    operations = [
        migrations.AddField(model_name="marketdatasubscription", name="primary_provider",
                            field=models.CharField(default="IBKR", max_length=16)),
        migrations.AddField(model_name="marketdatasubscription", name="active_provider",
                            field=models.CharField(default="IBKR", max_length=16)),
        migrations.AddField(model_name="marketdatasubscription", name="fallback_state",
                            field=models.CharField(default="PRIMARY", max_length=24)),
        migrations.AddField(model_name="marketdatasubscription", name="fallback_reason",
                            field=models.CharField(blank=True, max_length=64)),
        migrations.AddField(model_name="marketdatasubscription", name="provider_generation",
                            field=models.UUIDField(default=uuid.uuid4)),
        migrations.AddField(model_name="marketdatasubscription", name="last_primary_event_at",
                            field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="marketdatasubscription", name="last_fallback_event_at",
                            field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="marketdatasubscription", name="failed_over_at",
                            field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="marketdatasubscription", name="recovered_at",
                            field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="marketdatasubscription", name="primary_probe_generation",
                            field=models.UUIDField(blank=True, null=True)),
        migrations.AddField(model_name="marketdatasubscription", name="primary_probe_started_at",
                            field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="marketdatasubscription", name="primary_probe_event_count",
                            field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="marketdatasubscription", name="last_published_window_end",
                            field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="instrumentmarketstate", name="reference_price_provider",
                            field=models.CharField(blank=True, max_length=16)),
        migrations.AddField(model_name="instrumentmarketstate", name="reference_price_source",
                            field=models.CharField(blank=True, max_length=64)),
        migrations.AddField(model_name="instrumentmarketstate", name="provider_generation",
                            field=models.UUIDField(blank=True, null=True)),
        migrations.RunPython(copy_primary_event_time, clear_primary_event_time),
        migrations.CreateModel(
            name="MarketDataProviderTransition",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("timeframe", models.CharField(max_length=16)),
                ("previous_provider", models.CharField(max_length=16)),
                ("new_provider", models.CharField(max_length=16)),
                ("reason", models.CharField(max_length=64)),
                ("previous_generation", models.UUIDField(blank=True, null=True)),
                ("generation", models.UUIDField()),
                ("metadata", models.JSONField(default=dict)),
                ("occurred_at", models.DateTimeField(auto_now_add=True)),
                ("instrument", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                                 to="instruments.instrument")),
                ("subscription", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                                   related_name="provider_transitions",
                                                   to="market_streams.marketdatasubscription")),
            ],
        ),
        migrations.AddIndex(
            model_name="marketdataprovidertransition",
            index=models.Index(fields=["instrument", "-occurred_at"], name="provider_transition_time_idx"),
        ),
    ]
