import hashlib
import json

from django.db import migrations, models


def full_identity_hash(requirement):
    parameters = requirement.parameters or {}
    role = str(parameters.get("role") or "")
    payload = {
        "implementation_version": 1,
        "indicator_name": str(requirement.name),
        "indicator_role": role,
        "input_type": str(requirement.input_type).upper(),
        "instrument_id": str(requirement.instrument_id),
        "parameters": parameters,
        "timeframe": str(requirement.timeframe),
    }
    return role, hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def migrate_requirement_identities(apps, schema_editor):
    Requirement = apps.get_model("strategies", "StrategyInputRequirement")
    for requirement in Requirement.objects.all().order_by("pk"):
        role, identity_hash = full_identity_hash(requirement)
        requirement.role = role
        requirement.implementation_version = 1
        requirement.identity_hash = identity_hash
        requirement.save(update_fields=["role", "implementation_version", "identity_hash"])


def publish_replacement_registrations(apps, schema_editor):
    Instance = apps.get_model("strategies", "StrategyInstance")
    Binding = apps.get_model("strategies", "StrategyInputBinding")
    Outbox = apps.get_model("audit", "OutboxEvent")
    for instance in Instance.objects.filter(enabled=True).order_by("pk"):
        bindings = Binding.objects.filter(
            strategy_instance_id=instance.pk,
            strategy_version__version=instance.version,
            active=True,
        ).select_related("requirement")
        requirements = [
            {
                "identity_hash": binding.requirement.identity_hash,
                "input_type": binding.requirement.input_type,
                "name": binding.requirement.name,
                "role": binding.requirement.role,
                "parameters": binding.requirement.parameters,
                "implementation_version": binding.requirement.implementation_version,
                "warmup_bars": binding.requirement.warmup_bars,
            }
            for binding in bindings
        ]
        Outbox.objects.get_or_create(
            idempotency_key=f"strategy-inputs:{instance.pk}:v{instance.version}:identity-v2",
            defaults={
                "topic": "strategy.inputs.v1",
                "event_type": "strategy.inputs.changed",
                "aggregate_type": "strategy_instance",
                "aggregate_id": str(instance.pk),
                "partition_key": str(instance.instrument_id),
                "payload": {
                    "strategy_instance_id": instance.pk,
                    "strategy_version": instance.version,
                    "instrument_id": instance.instrument_id,
                    "timeframe": instance.timeframe,
                    "requirements": requirements,
                    "removed_requirement_hashes": [],
                },
            },
        )


class Migration(migrations.Migration):

    dependencies = [
        ("audit", "0005_auditevent_audit_aggregate_time_idx_and_more"),
        ("strategies", "0007_strategyallocation_strategy_alloc_priority_idx_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="strategyinstance",
            name="last_market_bar_id",
            field=models.CharField(blank=True, max_length=160),
        ),
        migrations.AddField(
            model_name="strategyinstance",
            name="last_market_bar_version",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="strategyinstance",
            name="last_market_event_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="strategyinputrequirement",
            name="implementation_version",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="strategyinputrequirement",
            name="role",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.RunPython(migrate_requirement_identities, migrations.RunPython.noop),
        migrations.RunPython(publish_replacement_registrations, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="strategyinputrequirement",
            name="parameters_hash",
        ),
    ]
