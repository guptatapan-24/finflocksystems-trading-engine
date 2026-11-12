import django.db.models.deletion
from django.db import migrations, models


WORK_STATUSES = [
    ("QUEUED", "Queued"), ("RUNNING", "Running"), ("COMPLETED", "Completed"),
    ("FAILED", "Failed"), ("REJECTED", "Rejected"), ("BLOCKED", "Blocked"),
]
ROLES = [
    ("SELECTOR", "Selector"), ("EXECUTION", "Execution"), ("ALLOCATOR", "Allocator"),
    ("OVERLAY", "Overlay"), ("EVENT", "Event"), ("PAIR_BASKET", "Pair Basket"),
    ("INCOME", "Income"), ("RESEARCH_ONLY", "Research Only"),
]


def retire_mvp_and_normalize_roles(apps, schema_editor):
    Universe = apps.get_model("research", "ResearchUniverse")
    Member = apps.get_model("research", "ResearchUniverseMember")
    Strategy = apps.get_model("research", "ResearchStrategyDefinition")
    Implementation = apps.get_model("research", "ResearchStrategyImplementation")
    Experiment = apps.get_model("research", "ResearchExperiment")
    pilot_ids = list(Universe.objects.filter(key="RECOMMENDATION_MVP").values_list("id", flat=True))
    Universe.objects.filter(id__in=pilot_ids).update(active=False)
    Member.objects.filter(universe_id__in=pilot_ids).update(active=False, mapping_status="RETIRED")
    event_ids = list(Strategy.objects.filter(research_id__startswith="EVT_").values_list("id", flat=True))
    income_ids = list(Strategy.objects.filter(research_id__startswith="INC_").values_list("id", flat=True))
    Strategy.objects.filter(id__in=event_ids).update(role="EVENT")
    Strategy.objects.filter(id__in=income_ids).update(role="INCOME")
    Implementation.objects.filter(research_strategy_id__in=event_ids).update(role="EVENT")
    Implementation.objects.filter(research_strategy_id__in=income_ids).update(role="INCOME")
    Experiment.objects.filter(experiment_type="MVP_WALK_FORWARD").update(experiment_type="SINGLE_ASSET", role="EXECUTION")


class Migration(migrations.Migration):
    dependencies = [
        ("instruments", "0006_alter_issuer_founded"),
        ("portfolio_construction", "0005_portfoliogoalallocation_accepted_recommendation_run_and_more"),
        ("research", "0002_recommendation_mvp"),
    ]

    operations = [
        migrations.CreateModel(
            name="CrossSectionalFeatureSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("feature_key", models.CharField(max_length=128)), ("frequency", models.CharField(max_length=8)),
                ("as_of_date", models.DateField()), ("available_at", models.DateTimeField()),
                ("data_version", models.CharField(max_length=64)), ("implementation_version", models.CharField(max_length=64)),
                ("summary", models.JSONField(default=dict)), ("artifact_uri", models.CharField(max_length=1000)),
                ("universe", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="cross_sectional_features", to="research.researchuniverse")),
            ],
            options={
                "indexes": [models.Index(fields=["universe", "feature_key", "as_of_date"], name="cross_feature_date_idx")],
                "constraints": [models.UniqueConstraint(fields=("universe", "feature_key", "frequency", "as_of_date", "data_version", "implementation_version"), name="unique_cross_section_feature")],
            },
        ),
        migrations.CreateModel(
            name="EventFeatureSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("feature_key", models.CharField(max_length=128)), ("as_of_date", models.DateField()),
                ("available_at", models.DateTimeField()), ("data_version", models.CharField(max_length=64)),
                ("implementation_version", models.CharField(max_length=64)), ("value", models.JSONField(default=dict)),
                ("event", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="feature_snapshots", to="research.researchevent")),
                ("instrument", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="event_feature_snapshots", to="instruments.instrument")),
            ],
            options={"constraints": [models.UniqueConstraint(fields=("event", "feature_key", "as_of_date", "data_version", "implementation_version"), name="unique_event_feature")]},
        ),
        migrations.CreateModel(
            name="InstrumentFeatureSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("feature_key", models.CharField(max_length=128)), ("frequency", models.CharField(max_length=8)),
                ("as_of_date", models.DateField()), ("available_at", models.DateTimeField()),
                ("data_version", models.CharField(max_length=64)), ("implementation_version", models.CharField(max_length=64)),
                ("value", models.JSONField(default=dict)), ("artifact_uri", models.CharField(blank=True, max_length=1000)),
                ("instrument", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="research_feature_snapshots", to="instruments.instrument")),
            ],
            options={
                "indexes": [models.Index(fields=["feature_key", "frequency", "as_of_date"], name="instrument_feature_date_idx")],
                "constraints": [models.UniqueConstraint(fields=("instrument", "feature_key", "frequency", "as_of_date", "data_version", "implementation_version"), name="unique_instrument_feature")],
            },
        ),
        migrations.CreateModel(
            name="MarketRegimeSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("as_of_date", models.DateField()), ("available_at", models.DateTimeField()),
                ("regime", models.CharField(max_length=32)), ("features", models.JSONField(default=dict)),
                ("data_version", models.CharField(max_length=64)), ("implementation_version", models.CharField(max_length=64)),
                ("universe", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="regime_snapshots", to="research.researchuniverse")),
            ],
            options={"constraints": [models.UniqueConstraint(fields=("universe", "as_of_date", "data_version", "implementation_version"), name="unique_market_regime")]},
        ),
        migrations.CreateModel(
            name="RecommendationBatchRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("requested_plan_version", models.PositiveIntegerField()),
                ("status", models.CharField(choices=WORK_STATUSES, default="QUEUED", max_length=16)),
                ("idempotency_key", models.CharField(max_length=128, unique=True)),
                ("input_hash", models.CharField(db_index=True, max_length=64)), ("input_snapshot", models.JSONField(default=list)),
                ("metrics", models.JSONField(default=dict)), ("error", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)), ("started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("dataset", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="recommendation_batches", to="research.researchdatasetversion")),
                ("plan", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="recommendation_batches", to="portfolio_construction.portfolioconstructionplan")),
                ("protocol", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="recommendation_batches", to="research.backtestprotocolversion")),
            ],
            options={"indexes": [models.Index(fields=["plan", "status", "-created_at"], name="recommend_batch_status_idx")]},
        ),
        migrations.CreateModel(
            name="RecommendationBatchGoalResult",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=WORK_STATUSES, default="QUEUED", max_length=16)),
                ("fallback_tier", models.PositiveSmallIntegerField(default=1)), ("summary", models.JSONField(default=dict)),
                ("batch", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="goal_results", to="research.recommendationbatchrun")),
                ("goal", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="recommendation_batch_results", to="portfolio_construction.portfoliogoalallocation")),
                ("recommendation_run", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="batch_results", to="research.goalrecommendationrun")),
            ],
            options={"constraints": [models.UniqueConstraint(fields=("batch", "goal"), name="unique_batch_goal_result")]},
        ),
        migrations.CreateModel(
            name="RecommendationCacheSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("goal_timeframe", models.CharField(max_length=16)), ("risk_level", models.PositiveSmallIntegerField()),
                ("as_of_date", models.DateField()), ("input_hash", models.CharField(max_length=64)),
                ("candidate_pool", models.JSONField(default=list)), ("selected_stocks", models.JSONField(default=list)),
                ("allocator_strategy_id", models.CharField(blank=True, max_length=64)), ("overlay_strategy_ids", models.JSONField(default=list)),
                ("expected_metrics", models.JSONField(default=dict)), ("gics_exposure", models.JSONField(default=dict)),
                ("fallback_tier", models.PositiveSmallIntegerField(default=1)), ("data_freshness", models.JSONField(default=dict)),
                ("status", models.CharField(choices=WORK_STATUSES, default="COMPLETED", max_length=16)),
                ("expires_at", models.DateTimeField()), ("created_at", models.DateTimeField(auto_now_add=True)),
                ("dataset_version", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="recommendation_cache_snapshots", to="research.researchdatasetversion")),
                ("protocol_version", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="recommendation_cache_snapshots", to="research.backtestprotocolversion")),
            ],
            options={
                "indexes": [models.Index(fields=["goal_timeframe", "risk_level", "status", "-created_at"], name="recommendation_cache_key_idx")],
                "constraints": [models.UniqueConstraint(fields=("dataset_version", "protocol_version", "goal_timeframe", "risk_level", "as_of_date", "input_hash"), name="unique_recommendation_cache")],
            },
        ),
        migrations.CreateModel(
            name="ResearchAnalystFact",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("fact_type", models.CharField(max_length=64)), ("metric", models.CharField(max_length=128)),
                ("period_end", models.DateField(blank=True, null=True)), ("event_timestamp", models.DateTimeField()),
                ("public_availability_timestamp", models.DateTimeField()),
                ("value", models.DecimalField(blank=True, decimal_places=12, max_digits=36, null=True)),
                ("payload", models.JSONField(default=dict)), ("provider", models.CharField(max_length=32)),
                ("provider_timestamp", models.DateTimeField()), ("revision_timestamp", models.DateTimeField()),
                ("data_version", models.PositiveIntegerField(default=1)),
                ("instrument", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="research_analyst_facts", to="instruments.instrument")),
                ("issuer", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="research_analyst_facts", to="instruments.issuer")),
            ],
            options={
                "indexes": [models.Index(fields=["issuer", "fact_type", "public_availability_timestamp"], name="analyst_available_idx")],
                "constraints": [models.UniqueConstraint(fields=("issuer", "fact_type", "metric", "event_timestamp", "data_version"), name="unique_analyst_fact_version")],
            },
        ),
        migrations.CreateModel(
            name="ResearchDataCoverageSummary",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("as_of_date", models.DateField()), ("daily_bar_count", models.PositiveIntegerField(default=0)),
                ("daily_start_date", models.DateField(blank=True, null=True)), ("daily_end_date", models.DateField(blank=True, null=True)),
                ("intraday_bar_count", models.PositiveIntegerField(default=0)), ("intraday_start_at", models.DateTimeField(blank=True, null=True)),
                ("intraday_end_at", models.DateTimeField(blank=True, null=True)), ("corporate_action_count", models.PositiveIntegerField(default=0)),
                ("fundamental_fact_count", models.PositiveIntegerField(default=0)), ("analyst_fact_count", models.PositiveIntegerField(default=0)),
                ("event_count", models.PositiveIntegerField(default=0)), ("last_successful_update", models.DateTimeField(blank=True, null=True)),
                ("recommendation_eligible", models.BooleanField(default=False)), ("provider_status", models.JSONField(default=dict)),
                ("quality_rules", models.JSONField(default=dict)), ("updated_at", models.DateTimeField(auto_now=True)),
                ("universe_member", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="data_coverage", to="research.researchuniversemember")),
            ],
            options={"indexes": [models.Index(fields=["as_of_date", "recommendation_eligible"], name="research_coverage_ready_idx")]},
        ),
        migrations.CreateModel(
            name="ResearchIntradayBar",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("frequency", models.CharField(max_length=8)), ("window_start", models.DateTimeField()),
                ("window_end", models.DateTimeField()), ("open", models.DecimalField(decimal_places=8, max_digits=24)),
                ("high", models.DecimalField(decimal_places=8, max_digits=24)), ("low", models.DecimalField(decimal_places=8, max_digits=24)),
                ("close", models.DecimalField(decimal_places=8, max_digits=24)), ("volume", models.DecimalField(decimal_places=4, max_digits=28)),
                ("vwap", models.DecimalField(blank=True, decimal_places=8, max_digits=24, null=True)),
                ("provider", models.CharField(max_length=32)), ("provider_timestamp", models.DateTimeField()),
                ("revision_timestamp", models.DateTimeField()), ("data_version", models.PositiveIntegerField(default=1)),
                ("quality_status", models.CharField(default="PENDING", max_length=16)),
                ("instrument", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="research_intraday_bars", to="instruments.instrument")),
            ],
            options={
                "indexes": [models.Index(fields=["instrument", "frequency", "window_start", "quality_status"], name="research_intraday_date_idx")],
                "constraints": [models.UniqueConstraint(fields=("instrument", "frequency", "window_start", "data_version"), name="unique_research_intraday_version")],
            },
        ),
        migrations.CreateModel(
            name="ResearchRoleScore",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("score_type", models.CharField(choices=[(value, value) for value in ["STOCK", "EXECUTION", "ALLOCATOR", "OVERLAY", "EVENT", "PAIR_BASKET"]], max_length=24)),
                ("goal_timeframe", models.CharField(max_length=16)), ("risk_level", models.PositiveSmallIntegerField()),
                ("as_of_date", models.DateField()), ("score", models.DecimalField(decimal_places=4, max_digits=9)),
                ("components", models.JSONField(default=dict)), ("contributing_strategy_ids", models.JSONField(default=list)),
                ("expires_at", models.DateTimeField()),
                ("dataset_version", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="role_scores", to="research.researchdatasetversion")),
                ("instrument", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="research_role_scores", to="instruments.instrument")),
                ("strategy", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="role_scores", to="research.researchstrategydefinition")),
            ],
            options={
                "indexes": [models.Index(fields=["score_type", "goal_timeframe", "risk_level", "-score"], name="role_goal_score_idx")],
                "constraints": [models.UniqueConstraint(fields=("score_type", "dataset_version", "strategy", "instrument", "goal_timeframe", "risk_level", "as_of_date"), name="unique_role_score_date")],
            },
        ),
        migrations.RenameIndex(model_name="researchexperiment", new_name="research_pair_status_idx", old_name="research_mvp_pair_idx"),
        migrations.AddField(model_name="researchevent", name="data_version", field=models.PositiveIntegerField(default=1)),
        migrations.AddField(model_name="researchevent", name="provider", field=models.CharField(blank=True, max_length=32)),
        migrations.AddField(model_name="researchevent", name="provider_timestamp", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="researchevent", name="revision_timestamp", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="researchexperiment", name="feature_version", field=models.CharField(blank=True, max_length=64)),
        migrations.AddField(model_name="researchexperiment", name="provider_data_version", field=models.CharField(blank=True, max_length=64)),
        migrations.AddField(model_name="researchexperiment", name="role", field=models.CharField(choices=ROLES, default="EXECUTION", max_length=24)),
        migrations.AddField(model_name="researchfundamentalfact", name="data_version", field=models.PositiveIntegerField(default=1)),
        migrations.AddField(model_name="researchfundamentalfact", name="provider", field=models.CharField(blank=True, max_length=32)),
        migrations.AddField(model_name="researchfundamentalfact", name="provider_timestamp", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="researchfundamentalfact", name="revision_timestamp", field=models.DateTimeField(blank=True, null=True)),
        migrations.AlterField(model_name="researchstrategydefinition", name="role", field=models.CharField(choices=ROLES, max_length=24)),
        migrations.AlterField(model_name="researchstrategyimplementation", name="role", field=models.CharField(choices=ROLES, max_length=24)),
        migrations.AddIndex(model_name="researchexperiment", index=models.Index(fields=["dataset_version", "role", "strategy", "status"], name="research_role_status_idx")),
        migrations.RunPython(retire_mvp_and_normalize_roles, migrations.RunPython.noop),
    ]
