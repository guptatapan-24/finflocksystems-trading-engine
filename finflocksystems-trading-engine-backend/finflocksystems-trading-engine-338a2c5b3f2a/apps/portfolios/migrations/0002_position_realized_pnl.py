from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("portfolios", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="portfolioposition",
            name="realized_pnl",
            field=models.DecimalField(decimal_places=8, default=0, max_digits=24),
        ),
        migrations.AddField(
            model_name="positionledgerentry",
            name="realized_pnl",
            field=models.DecimalField(decimal_places=8, default=0, max_digits=24),
        ),
    ]
