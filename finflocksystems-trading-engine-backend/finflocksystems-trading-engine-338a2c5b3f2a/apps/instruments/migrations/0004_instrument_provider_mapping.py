import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("instruments", "0003_exact_contract_identity")]

    operations = [
        migrations.CreateModel(
            name="InstrumentProviderMapping",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("provider", models.CharField(default="FINNHUB", max_length=32)),
                ("provider_symbol", models.CharField(blank=True, max_length=96)),
                ("exchange_mic", models.CharField(blank=True, max_length=16)),
                ("provider_exchange", models.CharField(blank=True, max_length=128)),
                ("currency", models.CharField(blank=True, max_length=8)),
                ("isin", models.CharField(blank=True, max_length=32)),
                ("figi", models.CharField(blank=True, max_length=32)),
                ("status", models.CharField(choices=[("PENDING", "PENDING"), ("VERIFIED", "VERIFIED"),
                                                    ("AMBIGUOUS", "AMBIGUOUS"), ("UNSUPPORTED", "UNSUPPORTED"),
                                                    ("ERROR", "ERROR")], default="PENDING", max_length=16)),
                ("verification_method", models.CharField(blank=True, choices=[("AUTOMATIC", "AUTOMATIC"),
                                                                              ("MANUAL", "MANUAL")], max_length=16)),
                ("metadata", models.JSONField(default=dict)),
                ("verified_at", models.DateTimeField(blank=True, null=True)),
                ("last_error", models.CharField(blank=True, max_length=1000)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("instrument", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                                 related_name="provider_mappings", to="instruments.instrument")),
            ],
        ),
        migrations.AddConstraint(
            model_name="instrumentprovidermapping",
            constraint=models.UniqueConstraint(fields=("instrument", "provider"), name="unique_instrument_provider_mapping"),
        ),
        migrations.AddConstraint(
            model_name="instrumentprovidermapping",
            constraint=models.UniqueConstraint(condition=models.Q(("status", "VERIFIED")),
                                                fields=("provider", "provider_symbol"),
                                                name="unique_verified_provider_symbol"),
        ),
        migrations.AddIndex(
            model_name="instrumentprovidermapping",
            index=models.Index(fields=["provider", "status", "updated_at"], name="provider_mapping_status_idx"),
        ),
    ]
