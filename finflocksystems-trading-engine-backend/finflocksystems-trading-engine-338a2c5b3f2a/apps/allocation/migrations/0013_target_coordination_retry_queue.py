from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("allocation", "0012_paper_live_and_rebalance_run_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="portfoliotargetcoordination",
            name="attempt_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="portfoliotargetcoordination",
            name="next_attempt_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
