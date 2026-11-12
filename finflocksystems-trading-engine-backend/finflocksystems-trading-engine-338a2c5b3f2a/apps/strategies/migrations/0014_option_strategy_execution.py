import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("strategies", "0013_strategy_workflow_trace_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="strategytarget",
            name="metadata",
            field=models.JSONField(default=dict),
        ),
        migrations.CreateModel(
            name="OptionSelectionSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("market_bar_id", models.CharField(max_length=160)),
                ("market_bar_version", models.PositiveIntegerField(default=1)),
                ("observed_at", models.DateTimeField()),
                ("provider", models.CharField(default="DHAN", max_length=32)),
                ("model_version", models.CharField(default="rps-atr075-oi-v1", max_length=64)),
                ("atr_reference", models.DecimalField(decimal_places=8, max_digits=24)),
                ("atr_value", models.DecimalField(decimal_places=8, max_digits=24)),
                ("atr_support", models.DecimalField(decimal_places=8, max_digits=24)),
                ("atr_resistance", models.DecimalField(decimal_places=8, max_digits=24)),
                ("oi_support", models.DecimalField(decimal_places=8, max_digits=24)),
                ("oi_resistance", models.DecimalField(decimal_places=8, max_digits=24)),
                ("spot", models.DecimalField(decimal_places=8, max_digits=24)),
                ("entry_score", models.DecimalField(decimal_places=4, max_digits=8)),
                ("selected_strategy", models.CharField(max_length=64)),
                ("expiration", models.DateField()),
                ("candidate_rankings", models.JSONField(default=list)),
                ("selected_legs", models.JSONField(default=list)),
                ("source_snapshot", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("strategy_instance", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="option_selections", to="strategies.strategyinstance")),
                ("strategy_version", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="option_selections", to="strategies.strategyversion")),
            ],
            options={
                "indexes": [models.Index(fields=["strategy_instance", "-observed_at"], name="option_selection_time_idx")],
                "constraints": [models.UniqueConstraint(fields=("strategy_instance", "strategy_version", "market_bar_id", "market_bar_version"), name="unique_option_selection_input")],
            },
        ),
        migrations.RunPython(
            __import__("apps.strategies.seeding", fromlist=["seed_all_strategy_definitions"]).seed_all_strategy_definitions,
            migrations.RunPython.noop,
        ),
    ]
