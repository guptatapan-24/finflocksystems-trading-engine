from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("allocation", "0013_target_coordination_retry_queue")]

    operations = [
        migrations.AddField(
            model_name="portfoliotargetsnapshot",
            name="net_target_quantities",
            field=models.JSONField(default=dict),
        ),
    ]
