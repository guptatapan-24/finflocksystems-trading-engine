from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("broker_gateway", "0004_brokersynccursor_connection_generation"),
        ("instruments", "0006_alter_issuer_founded"),
        ("market_streams", "0010_marketbar_provider_provenance"),
    ]

    operations = [
        migrations.CreateModel(
            name="MarketDataConsumerLease",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("timeframe", models.CharField(default="1m", max_length=16)),
                ("consumer_type", models.CharField(choices=[("MANUAL", "MANUAL"), ("WATCHLIST", "WATCHLIST"), ("MONITORING", "MONITORING"), ("RISK", "RISK")], max_length=16)),
                ("lease_key", models.CharField(max_length=128)),
                ("expires_at", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("gateway_session", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="market_data_leases", to="broker_gateway.brokergatewaysession")),
                ("instrument", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="market_data_leases", to="instruments.instrument")),
            ],
        ),
        migrations.AddConstraint(
            model_name="marketdataconsumerlease",
            constraint=models.UniqueConstraint(fields=("gateway_session", "consumer_type", "lease_key"), name="unique_market_consumer_lease"),
        ),
        migrations.AddIndex(
            model_name="marketdataconsumerlease",
            index=models.Index(fields=["gateway_session", "instrument", "timeframe", "expires_at"], name="market_lease_demand_idx"),
        ),
        migrations.AddIndex(
            model_name="marketdataconsumerlease",
            index=models.Index(fields=["expires_at"], name="market_lease_expiry_idx"),
        ),
    ]
