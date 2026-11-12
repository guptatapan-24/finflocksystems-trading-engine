from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("instruments", "0002_instrument_active_instrument_fractional_support_and_more")]

    operations = [
        migrations.AddField(model_name="instrument", name="primary_exchange", field=models.CharField(blank=True, max_length=32)),
        migrations.AddField(model_name="brokercontract", name="description", field=models.CharField(blank=True, max_length=255)),
        migrations.RemoveConstraint(model_name="instrument", name="unique_instrument"),
        migrations.AddConstraint(model_name="instrument", constraint=models.UniqueConstraint(
            fields=("symbol", "asset_class", "exchange", "primary_exchange", "currency"), name="unique_instrument")),
    ]
