import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("instruments", "0006_alter_issuer_founded")]

    operations = [
        migrations.CreateModel(
            name="OptionContract",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("underlying_conid", models.BigIntegerField(blank=True, db_index=True, null=True)),
                ("expiration", models.DateField()),
                ("strike", models.DecimalField(decimal_places=8, max_digits=24)),
                ("right", models.CharField(choices=[("C", "Call"), ("P", "Put")], max_length=1)),
                ("trading_class", models.CharField(blank=True, max_length=64)),
                ("multiplier", models.DecimalField(decimal_places=8, default=1, max_digits=20)),
                ("style", models.CharField(default="UNKNOWN", max_length=16)),
                ("settlement", models.CharField(default="UNKNOWN", max_length=16)),
                ("instrument", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="option_contract", to="instruments.instrument")),
                ("underlying", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="listed_options", to="instruments.instrument")),
            ],
        ),
        migrations.AddConstraint(
            model_name="optioncontract",
            constraint=models.UniqueConstraint(fields=("underlying_conid", "expiration", "strike", "right", "trading_class", "multiplier"), name="unique_option_contract_identity"),
        ),
        migrations.AddConstraint(
            model_name="optioncontract",
            constraint=models.CheckConstraint(condition=models.Q(("multiplier__gt", 0)), name="option_contract_positive_multiplier"),
        ),
        migrations.AddIndex(
            model_name="optioncontract",
            index=models.Index(fields=["underlying_conid", "expiration", "right", "strike"], name="option_chain_lookup_idx"),
        ),
    ]
