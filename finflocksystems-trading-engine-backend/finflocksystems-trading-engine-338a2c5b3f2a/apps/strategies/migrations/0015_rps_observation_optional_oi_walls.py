from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("strategies", "0014_option_strategy_execution")]

    operations = [
        migrations.AlterField(
            model_name="optionselectionsnapshot",
            name="oi_support",
            field=models.DecimalField(blank=True, decimal_places=8, max_digits=24, null=True),
        ),
        migrations.AlterField(
            model_name="optionselectionsnapshot",
            name="oi_resistance",
            field=models.DecimalField(blank=True, decimal_places=8, max_digits=24, null=True),
        ),
        migrations.AlterField(
            model_name="optionselectionsnapshot",
            name="model_version",
            field=models.CharField(default="rps-v2-framework-2026-09-normalized100", max_length=64),
        ),
    ]
