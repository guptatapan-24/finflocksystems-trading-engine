import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("gateway_service", "0001_initial")]

    operations = [
        migrations.RenameField(model_name="gatewaycommand", old_name="attempts", new_name="attempt_count"),
        migrations.RenameField(model_name="gatewaycommand", old_name="error", new_name="last_error"),
        migrations.AddField(model_name="gatewaycommand", name="request_hash", field=models.CharField(db_index=True, default="", max_length=64)),
        migrations.AddField(model_name="gatewaycommand", name="retryable", field=models.BooleanField(default=False)),
        migrations.AddField(model_name="gatewaycommand", name="claimed_by", field=models.CharField(blank=True, max_length=128)),
        migrations.AddField(model_name="gatewaycommand", name="claimed_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="gatewaycommand", name="lease_expires_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="gatewaycommand", name="completed_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AlterField(
            model_name="gatewaycommand", name="status",
            field=models.CharField(choices=[("PENDING", "PENDING"), ("PROCESSING", "PROCESSING"), ("COMPLETED", "COMPLETED"), ("FAILED", "FAILED"), ("UNKNOWN", "UNKNOWN")], default="PENDING", max_length=24),
        ),
        migrations.AddIndex(
            model_name="gatewaycommand",
            index=models.Index(fields=["status", "lease_expires_at", "id"], name="gateway_cmd_claim_idx"),
        ),
        migrations.CreateModel(
            name="GatewayCommandAttempt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("attempt_number", models.PositiveIntegerField()),
                ("claimed_by", models.CharField(max_length=128)),
                ("submission_state", models.CharField(default="CLAIMED", max_length=24)),
                ("broker_result", models.JSONField(default=dict)),
                ("error", models.CharField(blank=True, max_length=1000)),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("command", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="attempt_history", to="gateway_service.gatewaycommand")),
            ],
        ),
        migrations.AddConstraint(
            model_name="gatewaycommandattempt",
            constraint=models.UniqueConstraint(fields=("command", "attempt_number"), name="unique_gateway_command_attempt"),
        ),
    ]
