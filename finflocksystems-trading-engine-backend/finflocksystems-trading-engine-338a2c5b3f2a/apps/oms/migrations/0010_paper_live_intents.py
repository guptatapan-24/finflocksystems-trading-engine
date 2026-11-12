from django.db import migrations, models


PENDING_LEGACY_STATUSES = [
    "PENDING",
    "CLAIMED",
    "RISK_APPROVED",
    "SUBMITTING",
    "QUEUED",
    "BROKER_BLOCKED",
    "UNKNOWN",
]


def migrate_intent_modes(apps, schema_editor):
    OrderIntent = apps.get_model("oms", "OrderIntent")
    legacy_ids = list(
        OrderIntent.objects.exclude(mode__in=["PAPER", "LIVE"]).values_list(
            "pk", flat=True
        )
    )
    OrderIntent.objects.filter(
        pk__in=legacy_ids,
        operation_status__in=PENDING_LEGACY_STATUSES,
    ).update(
        operation_status="FAILED",
        operation_error=(
            "Legacy SHADOW/OBSERVE intent cancelled by PAPER/LIVE migration"
        ),
        retryable=False,
        eligible=False,
    )
    OrderIntent.objects.filter(pk__in=legacy_ids).update(eligible=False)

    for intent in OrderIntent.objects.select_related("portfolio__gateway_session"):
        session = getattr(intent.portfolio, "gateway_session", None)
        derived = "LIVE" if session and session.mode == "live" else "PAPER"
        updates = {"mode": derived}
        if intent.mode != derived:
            updates["eligible"] = False
            if intent.operation_status in PENDING_LEGACY_STATUSES:
                updates.update(
                    operation_status="FAILED",
                    operation_error=(
                        "Intent cancelled because its stored mode did not match "
                        "the portfolio Gateway session during PAPER/LIVE migration"
                    ),
                    retryable=False,
                )
        OrderIntent.objects.filter(pk=intent.pk).update(**updates)


class Migration(migrations.Migration):

    dependencies = [
        ("allocation", "0012_paper_live_and_rebalance_run_type"),
        ("oms", "0009_orderintent_origin"),
    ]

    operations = [
        migrations.RunPython(migrate_intent_modes, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="orderintent",
            name="mode",
            field=models.CharField(
                choices=[("PAPER", "Paper"), ("LIVE", "Live")],
                default="PAPER",
                max_length=16,
            ),
        ),
        migrations.AddConstraint(
            model_name="orderintent",
            constraint=models.CheckConstraint(
                condition=models.Q(mode__in=["PAPER", "LIVE"]),
                name="order_intent_valid_execution_mode",
            ),
        ),
    ]
