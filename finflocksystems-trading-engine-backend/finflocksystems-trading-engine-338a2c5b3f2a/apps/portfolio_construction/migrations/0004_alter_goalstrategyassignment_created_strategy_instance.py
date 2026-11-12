from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("portfolio_construction", "0003_remove_portfolioconstructionrun_selection_snapshot_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="goalstrategyassignment",
            name="created_strategy_instance",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.SET_NULL,
                related_name="construction_assignments",
                to="strategies.strategyinstance",
            ),
        ),
    ]
