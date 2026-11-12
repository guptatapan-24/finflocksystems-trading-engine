import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
        ("broker_gateway", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="BrokerPositionSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("snapshot_key", models.CharField(max_length=160, unique=True)),
                ("complete", models.BooleanField(default=False)),
                ("status", models.CharField(default="RECEIVED", max_length=24)),
                ("row_count", models.PositiveIntegerField(default=0)),
                ("positions", models.JSONField(default=list)),
                ("attempt_count", models.PositiveIntegerField(default=0)),
                ("last_error", models.CharField(blank=True, max_length=1000)),
                ("received_at", models.DateTimeField(auto_now_add=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("broker_account", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="position_snapshots", to="accounts.brokeraccount")),
            ],
        ),
        migrations.AddIndex(
            model_name="brokerpositionsnapshot",
            index=models.Index(fields=["broker_account", "-received_at"], name="broker_pos_acct_received_idx"),
        ),
        migrations.AddIndex(
            model_name="brokerpositionsnapshot",
            index=models.Index(fields=["status", "received_at"], name="broker_pos_status_idx"),
        ),
    ]
