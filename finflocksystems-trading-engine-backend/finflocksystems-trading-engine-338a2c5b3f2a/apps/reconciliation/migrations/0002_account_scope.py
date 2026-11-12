import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
        ("reconciliation", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="reconciliationrun",
            name="broker_account",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="reconciliation_runs", to="accounts.brokeraccount"),
        ),
        migrations.AddIndex(
            model_name="reconciliationbreak",
            index=models.Index(fields=["run", "category", "material"], name="recon_break_run_cat_idx"),
        ),
        migrations.AddIndex(
            model_name="reconciliationbreak",
            index=models.Index(fields=["resolved", "material", "category"], name="recon_break_open_idx"),
        ),
    ]
