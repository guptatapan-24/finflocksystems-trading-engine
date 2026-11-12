from django.db import migrations, models


OLD_DISABLED_LIMITATIONS={
    "Long-only construction; strategy signals remain disabled after apply.":
        "Long-only construction; generated strategies are activated after apply.",
    "The created instance starts disabled and must be reviewed before activation.":
        "The generated strategy is activated after the construction application commits.",
}


def update_activation_limitations(apps, schema_editor):
    Profile=apps.get_model("portfolio_construction","StrategyConstructionProfile")
    for old, new in OLD_DISABLED_LIMITATIONS.items():
        Profile.objects.filter(limitations=old).update(limitations=new)


def restore_activation_limitations(apps, schema_editor):
    Profile=apps.get_model("portfolio_construction","StrategyConstructionProfile")
    for old, new in OLD_DISABLED_LIMITATIONS.items():
        Profile.objects.filter(limitations=new).update(limitations=old)


class Migration(migrations.Migration):

    dependencies=[
        ("portfolio_construction","0005_portfoliogoalallocation_accepted_recommendation_run_and_more"),
    ]

    operations=[
        migrations.AlterField(
            model_name="portfolioconstructionrun",
            name="application_status",
            field=models.CharField(
                choices=[
                    ("NOT_APPLIED","NOT_APPLIED"),
                    ("QUEUED","QUEUED"),
                    ("APPLYING","APPLYING"),
                    ("ACTIVATING","ACTIVATING"),
                    ("APPLIED","APPLIED"),
                    ("PARTIALLY_APPLIED","PARTIALLY_APPLIED"),
                    ("FAILED","FAILED"),
                ],
                default="NOT_APPLIED",
                max_length=24,
            ),
        ),
        migrations.RunPython(update_activation_limitations,restore_activation_limitations),
    ]
