from django.db import migrations, models


def normalize_modes(apps, schema_editor):
    GatewaySession = apps.get_model("gateway_service", "GatewaySession")
    GatewaySession.objects.exclude(mode__in=["paper", "live"]).update(
        mode="paper",
        state="DISCONNECTED",
        reconciled=False,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("gateway_service", "0005_full_research_history"),
    ]

    operations = [
        migrations.RunPython(normalize_modes, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="gatewaysession",
            name="mode",
            field=models.CharField(
                choices=[("paper", "Paper"), ("live", "Live")],
                default="paper",
                max_length=8,
            ),
        ),
        migrations.AddConstraint(
            model_name="gatewaysession",
            constraint=models.CheckConstraint(
                condition=models.Q(mode__in=["paper", "live"]),
                name="gateway_session_valid_mode",
            ),
        ),
    ]
