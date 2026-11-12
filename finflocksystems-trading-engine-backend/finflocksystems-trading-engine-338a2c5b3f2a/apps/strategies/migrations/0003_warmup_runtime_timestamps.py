from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies=[("strategies","0002_orderpolicy_strategydefinition_strategyriskpolicy_and_more")]
    operations=[
        migrations.AddField(model_name="strategyinstance",name="warmup_started_at",field=models.DateTimeField(blank=True,null=True)),
        migrations.AddField(model_name="strategyinstance",name="warmup_last_progress_at",field=models.DateTimeField(blank=True,null=True)),
    ]
