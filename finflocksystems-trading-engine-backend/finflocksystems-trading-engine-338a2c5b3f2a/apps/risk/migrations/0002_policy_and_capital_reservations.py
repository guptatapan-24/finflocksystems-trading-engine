import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
        ("oms", "0006_orderintent_idempotency_state"),
        ("portfolios", "0002_position_realized_pnl"),
        ("risk", "0001_initial"),
    ]
    operations = [
        migrations.CreateModel(
            name="PreTradeRiskPolicy",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("maximum_order_quantity", models.DecimalField(decimal_places=8, default="100000000", max_digits=24)),
                ("maximum_order_notional", models.DecimalField(decimal_places=8, default="100000", max_digits=24)),
                ("estimated_commission_rate", models.DecimalField(decimal_places=10, default="0.0005", max_digits=12)),
                ("estimated_fixed_fee", models.DecimalField(decimal_places=8, default="1", max_digits=24)),
                ("enabled", models.BooleanField(default=True)),
                ("version", models.PositiveIntegerField(default=1)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("portfolio", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="pre_trade_risk_policy", to="portfolios.tradingportfolio")),
            ],
        ),
        migrations.CreateModel(
            name="CapitalReservation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("reference_type", models.CharField(max_length=32)),
                ("reference_id", models.CharField(max_length=64)),
                ("amount", models.DecimalField(decimal_places=8, max_digits=24)),
                ("estimated_fees", models.DecimalField(decimal_places=8, default=0, max_digits=24)),
                ("status", models.CharField(choices=[("ACTIVE", "ACTIVE"), ("CONSUMED", "CONSUMED"), ("RELEASED", "RELEASED")], default="ACTIVE", max_length=16)),
                ("idempotency_key", models.CharField(max_length=128, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("released_at", models.DateTimeField(blank=True, null=True)),
                ("account", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="capital_reservations", to="accounts.brokeraccount")),
                ("order_intent", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="capital_reservation", to="oms.orderintent")),
                ("portfolio", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="capital_reservations", to="portfolios.tradingportfolio")),
            ],
        ),
        migrations.AddIndex(
            model_name="capitalreservation",
            index=models.Index(fields=["account", "status"], name="capital_res_account_idx"),
        ),
    ]
