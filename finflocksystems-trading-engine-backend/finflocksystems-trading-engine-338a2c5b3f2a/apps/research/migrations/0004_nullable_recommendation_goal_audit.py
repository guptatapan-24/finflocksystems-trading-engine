import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("research", "0003_full_recommendation_system")]

    operations = [
        migrations.AlterField(
            model_name="goalrecommendationrun",
            name="goal_allocation",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="recommendation_runs", to="portfolio_construction.portfoliogoalallocation",
            ),
        ),
        migrations.AlterField(
            model_name="goalrecommendationacceptance",
            name="goal",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="recommendation_acceptances", to="portfolio_construction.portfoliogoalallocation",
            ),
        ),
        migrations.AlterField(
            model_name="recommendationbatchgoalresult",
            name="goal",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="recommendation_batch_results", to="portfolio_construction.portfoliogoalallocation",
            ),
        ),
    ]
