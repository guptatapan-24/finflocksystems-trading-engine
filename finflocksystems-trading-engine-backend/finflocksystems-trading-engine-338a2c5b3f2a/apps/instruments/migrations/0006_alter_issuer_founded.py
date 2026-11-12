from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("instruments", "0005_issuer_instrument_issuer"),
    ]

    operations = [
        migrations.AlterField(
            model_name="issuer",
            name="founded",
            field=models.CharField(blank=True, max_length=64),
        ),
    ]
