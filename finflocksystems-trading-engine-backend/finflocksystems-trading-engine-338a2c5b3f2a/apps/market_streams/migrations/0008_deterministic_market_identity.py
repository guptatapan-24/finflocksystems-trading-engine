import hashlib
import json

from django.db import migrations, models


def output_name(requirement):
    role = requirement.role
    if requirement.name == "donchian":
        return "donchian_upper" if role == "entry" else "donchian_lower"
    return f"{requirement.name}_{role}" if role else requirement.name


def fallback_identity(value):
    parameters = value.parameters or {}
    role = str(parameters.get("role") or "")
    name = value.indicator
    if role and name.endswith(f"_{role}"):
        name = name[:-(len(role) + 1)]
    if value.indicator in {"donchian_upper", "donchian_lower"}:
        name = "donchian"
        role = "entry" if value.indicator == "donchian_upper" else "exit"
    payload = {
        "implementation_version": 1,
        "indicator_name": name,
        "indicator_role": role,
        "input_type": "INDICATOR",
        "instrument_id": str(value.instrument_id),
        "parameters": parameters,
        "timeframe": str(value.timeframe),
    }
    return name, role, hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def migrate_indicator_identities(apps, schema_editor):
    Indicator = apps.get_model("market_streams", "IndicatorValue")
    Requirement = apps.get_model("strategies", "StrategyInputRequirement")
    requirements = {}
    for requirement in Requirement.objects.filter(input_type="INDICATOR").order_by("pk"):
        key = (
            requirement.instrument_id,
            requirement.timeframe,
            json.dumps(requirement.parameters or {}, sort_keys=True, separators=(",", ":")),
            output_name(requirement),
        )
        requirements[key] = requirement
    for value in Indicator.objects.all().order_by("pk"):
        key = (
            value.instrument_id,
            value.timeframe,
            json.dumps(value.parameters or {}, sort_keys=True, separators=(",", ":")),
            value.indicator,
        )
        requirement = requirements.get(key)
        if requirement:
            value.indicator_name = requirement.name
            value.indicator_role = requirement.role
            value.implementation_version = requirement.implementation_version
            value.requirement_identity_hash = requirement.identity_hash
        else:
            name, role, identity_hash = fallback_identity(value)
            value.indicator_name = name
            value.indicator_role = role
            value.implementation_version = 1
            value.requirement_identity_hash = identity_hash
        value.save(update_fields=[
            "indicator_name",
            "indicator_role",
            "implementation_version",
            "requirement_identity_hash",
        ])


class Migration(migrations.Migration):

    dependencies = [
        ("strategies", "0008_full_input_identity_and_market_cursor"),
        ("market_streams", "0007_strategyevaluationjob"),
    ]

    operations = [
        migrations.AddField(
            model_name="marketbar",
            name="processing_mode",
            field=models.CharField(
                choices=[
                    ("LIVE", "LIVE"),
                    ("WARMUP", "WARMUP"),
                    ("REPLAY", "REPLAY"),
                    ("BACKFILL", "BACKFILL"),
                ],
                default="LIVE",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="indicatorvalue",
            name="implementation_version",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="indicatorvalue",
            name="indicator_name",
            field=models.CharField(default="", max_length=64),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="indicatorvalue",
            name="indicator_role",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="indicatorvalue",
            name="processing_mode",
            field=models.CharField(
                choices=[
                    ("LIVE", "LIVE"),
                    ("WARMUP", "WARMUP"),
                    ("REPLAY", "REPLAY"),
                    ("BACKFILL", "BACKFILL"),
                ],
                default="LIVE",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="indicatorvalue",
            name="requirement_identity_hash",
            field=models.CharField(default="", max_length=64),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="strategyevaluationjob",
            name="processing_mode",
            field=models.CharField(default="LIVE", max_length=16),
        ),
        migrations.RunPython(migrate_indicator_identities, migrations.RunPython.noop),
        migrations.RemoveIndex(
            model_name="indicatorvalue",
            name="indicator_ready_idx",
        ),
        migrations.RemoveConstraint(
            model_name="indicatorvalue",
            name="unique_indicator_source_version",
        ),
        migrations.RemoveField(
            model_name="indicatorvalue",
            name="parameter_version",
        ),
        migrations.RemoveField(
            model_name="indicatorvalue",
            name="parameters_hash",
        ),
        migrations.RemoveField(
            model_name="strategyevaluationreadiness",
            name="expected_input_count",
        ),
        migrations.RemoveField(
            model_name="strategyevaluationreadiness",
            name="received_input_hashes",
        ),
        migrations.AddConstraint(
            model_name="indicatorvalue",
            constraint=models.UniqueConstraint(
                fields=("source_key",),
                name="unique_indicator_source",
            ),
        ),
        migrations.AddIndex(
            model_name="indicatorvalue",
            index=models.Index(
                fields=[
                    "source_bar_id",
                    "source_bar_version",
                    "requirement_identity_hash",
                    "is_final",
                ],
                name="indicator_identity_ready_idx",
            ),
        ),
    ]
