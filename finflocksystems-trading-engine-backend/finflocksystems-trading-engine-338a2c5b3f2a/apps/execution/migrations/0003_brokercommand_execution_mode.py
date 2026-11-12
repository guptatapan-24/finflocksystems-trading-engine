from django.db import migrations, models


ACTIVE_COMMAND_STATUSES = [
    "PENDING",
    "CLAIMED",
    "SENDING",
    "RETRY",
    "UNCERTAIN",
]


def migrate_command_modes(apps, schema_editor):
    BrokerCommand = apps.get_model("execution", "BrokerCommand")
    for command in BrokerCommand.objects.select_related("order__intent"):
        intent = command.order.intent
        updates = {"mode": intent.mode}
        if (
            intent.operation_status == "FAILED"
            and command.status in ACTIVE_COMMAND_STATUSES
        ):
            updates.update(
                status="FAILED",
                next_attempt_at=None,
                last_error=(
                    "Legacy command cancelled by PAPER/LIVE execution-mode migration"
                ),
            )
        BrokerCommand.objects.filter(pk=command.pk).update(**updates)


class Migration(migrations.Migration):

    dependencies = [
        ("execution", "0002_brokercommand"),
        ("oms", "0010_paper_live_intents"),
    ]

    operations = [
        migrations.AddField(
            model_name="brokercommand",
            name="mode",
            field=models.CharField(
                choices=[("PAPER", "Paper"), ("LIVE", "Live")],
                default="PAPER",
                max_length=16,
            ),
        ),
        migrations.RunPython(migrate_command_modes, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="brokercommand",
            constraint=models.CheckConstraint(
                condition=models.Q(mode__in=["PAPER", "LIVE"]),
                name="broker_command_valid_execution_mode",
            ),
        ),
    ]
