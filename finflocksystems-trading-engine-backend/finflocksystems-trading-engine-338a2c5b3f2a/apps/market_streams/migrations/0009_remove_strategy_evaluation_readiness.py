from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("market_streams", "0008_deterministic_market_identity"),
    ]

    operations = [
        migrations.DeleteModel(
            name="StrategyEvaluationReadiness",
        ),
    ]
