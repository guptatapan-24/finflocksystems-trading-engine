from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("instruments", "0006_alter_issuer_founded"),
        ("research", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="goalrecommendationrun", name="status",
            field=models.CharField(choices=[(value, label) for value, label in [
                ("QUEUED", "Queued"), ("RUNNING", "Running"), ("COMPLETED", "Completed"),
                ("FAILED", "Failed"), ("REJECTED", "Rejected"), ("BLOCKED", "Blocked"),
            ]], default="QUEUED", max_length=16),
        ),
        migrations.AlterField(
            model_name="researchexperiment", name="status",
            field=models.CharField(choices=[(value, label) for value, label in [
                ("QUEUED", "Queued"), ("RUNNING", "Running"), ("COMPLETED", "Completed"),
                ("FAILED", "Failed"), ("REJECTED", "Rejected"), ("BLOCKED", "Blocked"),
            ]], default="QUEUED", max_length=16),
        ),
        migrations.AlterField(
            model_name="researchtrial", name="status",
            field=models.CharField(choices=[(value, label) for value, label in [
                ("QUEUED", "Queued"), ("RUNNING", "Running"), ("COMPLETED", "Completed"),
                ("FAILED", "Failed"), ("REJECTED", "Rejected"), ("BLOCKED", "Blocked"),
            ]], default="QUEUED", max_length=16),
        ),
        migrations.AlterField(
            model_name="researchstrategyimplementation",
            name="status",
            field=models.CharField(
                choices=[
                    ("DRAFT", "Draft"), ("VALIDATED", "Validated"),
                    ("BACKTESTED", "Backtested"), ("SCORED", "Scored"),
                    ("APPROVED_FOR_RECOMMENDATION", "Approved For Recommendation"),
                    ("SHADOW_VALIDATED", "Shadow Validated"), ("BUILDER_READY", "Builder Ready"),
                    ("APPROVED", "Approved"), ("RETIRED", "Retired"),
                ],
                default="DRAFT", max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="researchexperiment", name="instrument",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                                    related_name="research_experiments", to="instruments.instrument"),
        ),
        migrations.AddField(model_name="researchexperiment", name="implementation_hash", field=models.CharField(blank=True, max_length=64)),
        migrations.AddField(model_name="researchexperiment", name="data_version", field=models.CharField(blank=True, max_length=64)),
        migrations.AddField(model_name="researchexperiment", name="parameter_space_hash", field=models.CharField(blank=True, max_length=64)),
        migrations.AddField(model_name="researchexperiment", name="start_date", field=models.DateField(blank=True, null=True)),
        migrations.AddField(model_name="researchexperiment", name="end_date", field=models.DateField(blank=True, null=True)),
        migrations.AddIndex(
            model_name="researchexperiment",
            index=models.Index(fields=["dataset_version", "instrument", "strategy", "status"], name="research_mvp_pair_idx"),
        ),
    ]
