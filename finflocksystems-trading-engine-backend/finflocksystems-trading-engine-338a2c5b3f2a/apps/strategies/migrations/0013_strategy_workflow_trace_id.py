import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("strategies", "0012_strategy_runtime_stage_timestamps_and_warmup_audit"),
    ]

    operations = [
        migrations.AddField(
            model_name="strategyinstance",
            name="workflow_trace_id",
            field=models.UUIDField(
                db_index=True,
                default=uuid.uuid4,
                editable=False,
            ),
        ),
    ]
