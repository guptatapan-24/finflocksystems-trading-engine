from django.db import migrations, models


def rename_validation_evidence(apps, schema_editor):
    implementation_model = apps.get_model("research", "ResearchStrategyImplementation")
    readiness_model = apps.get_model("research", "ResearchStrategyReadiness")

    implementation_model.objects.filter(status="SHADOW_VALIDATED").update(status="PAPER_VALIDATED")
    for implementation in implementation_model.objects.exclude(approval_record={}):
        evidence = dict(implementation.approval_record or {})
        if "shadow_validated" not in evidence:
            continue
        evidence.setdefault("paper_validated", evidence["shadow_validated"])
        evidence.pop("shadow_validated", None)
        implementation.approval_record = evidence
        implementation.save(update_fields=["approval_record"])

    for readiness in readiness_model.objects.exclude(blocking_reasons=[]):
        reasons = [
            "PAPER_VALIDATION_REQUIRED" if reason == "SHADOW_VALIDATION_REQUIRED" else reason
            for reason in (readiness.blocking_reasons or [])
        ]
        if reasons != readiness.blocking_reasons:
            readiness.blocking_reasons = reasons
            readiness.save(update_fields=["blocking_reasons"])


def restore_legacy_validation_evidence(apps, schema_editor):
    implementation_model = apps.get_model("research", "ResearchStrategyImplementation")
    readiness_model = apps.get_model("research", "ResearchStrategyReadiness")

    implementation_model.objects.filter(status="PAPER_VALIDATED").update(status="SHADOW_VALIDATED")
    for implementation in implementation_model.objects.exclude(approval_record={}):
        evidence = dict(implementation.approval_record or {})
        if "paper_validated" not in evidence:
            continue
        evidence.setdefault("shadow_validated", evidence["paper_validated"])
        evidence.pop("paper_validated", None)
        implementation.approval_record = evidence
        implementation.save(update_fields=["approval_record"])

    for readiness in readiness_model.objects.exclude(blocking_reasons=[]):
        reasons = [
            "SHADOW_VALIDATION_REQUIRED" if reason == "PAPER_VALIDATION_REQUIRED" else reason
            for reason in (readiness.blocking_reasons or [])
        ]
        if reasons != readiness.blocking_reasons:
            readiness.blocking_reasons = reasons
            readiness.save(update_fields=["blocking_reasons"])


class Migration(migrations.Migration):
    dependencies = [
        ("research", "0004_nullable_recommendation_goal_audit"),
    ]

    operations = [
        migrations.RunPython(
            rename_validation_evidence,
            restore_legacy_validation_evidence,
        ),
        migrations.AlterField(
            model_name="researchstrategyimplementation",
            name="status",
            field=models.CharField(
                choices=[
                    ("DRAFT", "Draft"),
                    ("VALIDATED", "Validated"),
                    ("BACKTESTED", "Backtested"),
                    ("SCORED", "Scored"),
                    ("APPROVED_FOR_RECOMMENDATION", "Approved For Recommendation"),
                    ("PAPER_VALIDATED", "Paper Validated"),
                    ("BUILDER_READY", "Builder Ready"),
                    ("APPROVED", "Approved"),
                    ("RETIRED", "Retired"),
                ],
                default="DRAFT",
                max_length=32,
            ),
        ),
    ]
