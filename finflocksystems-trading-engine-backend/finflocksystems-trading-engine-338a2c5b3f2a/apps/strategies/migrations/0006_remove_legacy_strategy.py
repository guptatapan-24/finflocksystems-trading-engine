import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("allocation", "0008_instance_only"),
        ("oms", "0007_instance_only"),
        ("strategies", "0005_prepare_instance_only"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="strategyrun",
            name="unique_strategy_input",
        ),
        migrations.AddConstraint(
            model_name="strategyrun",
            constraint=models.UniqueConstraint(
                fields=("strategy_instance", "input_hash"),
                name="unique_strategy_instance_input",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="strategyallocation",
            name="unique_strategy_allocation",
        ),
        migrations.RemoveField(model_name="strategyrun", name="strategy"),
        migrations.RemoveField(model_name="strategyallocation", name="strategy"),
        migrations.AlterField(
            model_name="strategyallocation",
            name="strategy_instance",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="allocations",
                to="strategies.strategyinstance",
            ),
        ),
        migrations.AddConstraint(
            model_name="strategyallocation",
            constraint=models.UniqueConstraint(
                fields=("strategy_instance", "portfolio"),
                name="unique_strategy_instance_allocation",
            ),
        ),
        migrations.RemoveField(model_name="strategyinstance", name="legacy_strategy"),
        migrations.DeleteModel(name="HistoricalBar"),
        migrations.DeleteModel(name="TradingStrategy"),
    ]
