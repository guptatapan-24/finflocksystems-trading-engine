from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from .enums import (
    DatasetStatus,
    ImplementationStatus,
    MappingStatus,
    ReadinessStatus,
    StrategyRole,
    TaxonomyLevel,
    WorkStatus,
)


class ResearchDatasetVersion(models.Model):
    bundle_name = models.CharField(max_length=255)
    version = models.CharField(max_length=64)
    snapshot_date = models.DateField()
    source_path = models.CharField(max_length=1000)
    status = models.CharField(max_length=16, choices=DatasetStatus.choices, default=DatasetStatus.STAGED)
    manifest_hash = models.CharField(max_length=64)
    file_hashes = models.JSONField(default=dict)
    source_metadata = models.JSONField(default=dict)
    validation_report = models.JSONField(default=dict)
    imported_at = models.DateTimeField(null=True, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    retired_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["bundle_name", "version"], name="unique_research_bundle_version"),
            models.UniqueConstraint(
                fields=["bundle_name"], condition=Q(status=DatasetStatus.ACTIVE),
                name="one_active_research_bundle",
            ),
        ]


class BacktestProtocolVersion(models.Model):
    protocol_id = models.CharField(max_length=128)
    dataset_version = models.ForeignKey(ResearchDatasetVersion, on_delete=models.PROTECT, related_name="protocols")
    configuration = models.JSONField(default=dict)
    configuration_hash = models.CharField(max_length=64)
    active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["dataset_version", "protocol_id"], name="unique_dataset_protocol")]


class CompatibilityRuleSet(models.Model):
    dataset_version = models.OneToOneField(
        ResearchDatasetVersion, on_delete=models.PROTECT, related_name="compatibility_rules"
    )
    configuration = models.JSONField(default=dict)
    configuration_hash = models.CharField(max_length=64)
    active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class GICSTaxonomyNode(models.Model):
    dataset_version = models.ForeignKey(ResearchDatasetVersion, on_delete=models.PROTECT, related_name="gics_nodes")
    level = models.CharField(max_length=24, choices=TaxonomyLevel.choices)
    code = models.CharField(max_length=8)
    name = models.CharField(max_length=255)
    parent = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="children")
    path = models.JSONField(default=list)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["dataset_version", "code"], name="unique_dataset_gics_code")]
        indexes = [models.Index(fields=["dataset_version", "level", "active"], name="gics_dataset_level_idx")]

    def clean(self):
        levels = list(TaxonomyLevel.values)
        if self.level == TaxonomyLevel.SECTOR and self.parent_id:
            raise ValidationError("A sector cannot have a parent")
        if self.level != TaxonomyLevel.SECTOR:
            if not self.parent_id:
                raise ValidationError("Non-sector GICS nodes require a parent")
            if levels.index(self.parent.level) + 1 != levels.index(self.level):
                raise ValidationError("GICS parent must be the immediately preceding level")


class InstrumentClassification(models.Model):
    instrument = models.ForeignKey(
        "instruments.Instrument", on_delete=models.PROTECT, null=True, blank=True, related_name="classifications"
    )
    issuer = models.ForeignKey("instruments.Issuer", on_delete=models.PROTECT, related_name="classifications")
    taxonomy_version = models.ForeignKey(ResearchDatasetVersion, on_delete=models.PROTECT, related_name="classifications")
    sub_industry_node = models.ForeignKey(GICSTaxonomyNode, on_delete=models.PROTECT, related_name="classifications")
    effective_from = models.DateField(null=True, blank=True)
    effective_to = models.DateField(null=True, blank=True)
    is_current = models.BooleanField(default=True)
    source_dataset_version = models.ForeignKey(
        ResearchDatasetVersion, on_delete=models.PROTECT, related_name="source_classifications"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["issuer", "taxonomy_version", "sub_industry_node", "effective_from"],
                name="unique_issuer_gics_period",
            )
        ]
        indexes = [models.Index(fields=["issuer", "effective_from", "effective_to"], name="issuer_gics_period_idx")]


class ResearchUniverse(models.Model):
    MEMBERSHIP_TYPES = [(value, value) for value in ["CURRENT_SNAPSHOT", "POINT_IN_TIME"]]
    key = models.CharField(max_length=128)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    dataset_version = models.ForeignKey(ResearchDatasetVersion, on_delete=models.PROTECT, related_name="universes")
    membership_type = models.CharField(max_length=24, choices=MEMBERSHIP_TYPES, default="CURRENT_SNAPSHOT")
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["dataset_version", "key"], name="unique_dataset_universe")]


class ResearchUniverseMember(models.Model):
    universe = models.ForeignKey(ResearchUniverse, on_delete=models.PROTECT, related_name="members")
    issuer = models.ForeignKey("instruments.Issuer", on_delete=models.PROTECT, related_name="research_memberships")
    instrument = models.ForeignKey(
        "instruments.Instrument", on_delete=models.PROTECT, null=True, blank=True, related_name="research_memberships"
    )
    source_symbol = models.CharField(max_length=32)
    security_name = models.CharField(max_length=255)
    currency = models.CharField(max_length=8)
    exchange_hint = models.CharField(max_length=32, blank=True)
    membership_start = models.DateField(null=True, blank=True)
    membership_end = models.DateField(null=True, blank=True)
    membership_status = models.CharField(max_length=24, default="CURRENT")
    research_eligibility_configuration = models.JSONField(default=dict)
    risk_timeframe_profile = models.JSONField(default=dict)
    mapping_status = models.CharField(max_length=32, choices=MappingStatus.choices, default=MappingStatus.METADATA_ONLY)
    mapping_notes = models.TextField(blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["universe", "issuer"], name="unique_universe_issuer")]
        indexes = [models.Index(fields=["universe", "mapping_status", "active"], name="universe_mapping_idx")]


class InstrumentEligibilitySnapshot(models.Model):
    universe_member = models.ForeignKey(ResearchUniverseMember, on_delete=models.PROTECT, related_name="eligibility_snapshots")
    as_of_date = models.DateField()
    price = models.DecimalField(max_digits=24, decimal_places=8, null=True, blank=True)
    median_dollar_volume_20d = models.DecimalField(max_digits=28, decimal_places=4, null=True, blank=True)
    history_days = models.PositiveIntegerField(default=0)
    trading_days_252d = models.PositiveIntegerField(default=0)
    realized_volatility = models.DecimalField(max_digits=16, decimal_places=8, null=True, blank=True)
    maximum_drawdown = models.DecimalField(max_digits=16, decimal_places=8, null=True, blank=True)
    data_quality_status = models.CharField(max_length=24, default="PENDING")
    research_eligible = models.BooleanField(default=False)
    builder_eligible = models.BooleanField(default=False)
    rejection_reasons = models.JSONField(default=list)
    metrics = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["universe_member", "as_of_date"], name="unique_member_eligibility_date")]
        indexes = [models.Index(fields=["as_of_date", "research_eligible", "builder_eligible"], name="eligibility_date_idx")]


class ResearchDataCoverageSummary(models.Model):
    universe_member = models.OneToOneField(
        ResearchUniverseMember, on_delete=models.PROTECT, related_name="data_coverage"
    )
    as_of_date = models.DateField()
    daily_bar_count = models.PositiveIntegerField(default=0)
    daily_start_date = models.DateField(null=True, blank=True)
    daily_end_date = models.DateField(null=True, blank=True)
    intraday_bar_count = models.PositiveIntegerField(default=0)
    intraday_start_at = models.DateTimeField(null=True, blank=True)
    intraday_end_at = models.DateTimeField(null=True, blank=True)
    corporate_action_count = models.PositiveIntegerField(default=0)
    fundamental_fact_count = models.PositiveIntegerField(default=0)
    analyst_fact_count = models.PositiveIntegerField(default=0)
    event_count = models.PositiveIntegerField(default=0)
    last_successful_update = models.DateTimeField(null=True, blank=True)
    recommendation_eligible = models.BooleanField(default=False)
    provider_status = models.JSONField(default=dict)
    quality_rules = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["as_of_date", "recommendation_eligible"], name="research_coverage_ready_idx")]


class ResearchStrategyDefinition(models.Model):
    research_id = models.CharField(max_length=64)
    dataset_version = models.ForeignKey(ResearchDatasetVersion, on_delete=models.PROTECT, related_name="strategies")
    name = models.CharField(max_length=255)
    family = models.CharField(max_length=64)
    scope = models.CharField(max_length=64)
    role = models.CharField(max_length=24, choices=StrategyRole.choices)
    description = models.TextField(blank=True)
    research_hypothesis = models.TextField(blank=True)
    production_status = models.CharField(max_length=32)
    engine_compatibility = models.JSONField(default=dict)
    supported_directions = models.JSONField(default=list)
    supported_frequencies = models.JSONField(default=list)
    typical_holding_period = models.CharField(max_length=255, blank=True)
    required_data = models.JSONField(default=list)
    features = models.JSONField(default=list)
    signal_logic = models.JSONField(default=dict)
    parameter_grid = models.JSONField(default=dict)
    eligibility_filters = models.JSONField(default=list)
    portfolio_construction = models.JSONField(default=dict)
    risk_controls = models.JSONField(default=list)
    recommended_risk_levels = models.JSONField(default=list)
    recommended_goal_timeframes = models.JSONField(default=list)
    required_metrics = models.JSONField(default=list)
    known_failure_modes = models.JSONField(default=list)
    configuration_hash = models.CharField(max_length=64)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["dataset_version", "research_id"], name="unique_dataset_research_strategy")]
        indexes = [models.Index(fields=["role", "family", "active"], name="research_strategy_role_idx")]


class ResearchFeatureDefinition(models.Model):
    key = models.CharField(max_length=128, unique=True)
    category = models.CharField(max_length=64)
    description = models.TextField(blank=True)
    formula = models.TextField(blank=True)
    batch_implementation_path = models.CharField(max_length=500, blank=True)
    stream_input_name = models.CharField(max_length=128, blank=True)
    supported_frequencies = models.JSONField(default=list)
    required_datasets = models.JSONField(default=list)
    availability_lag = models.CharField(max_length=64, default="1 bar")
    status = models.CharField(max_length=16, choices=ReadinessStatus.choices, default=ReadinessStatus.DECLARED)
    implementation_version = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class ResearchStrategyFeatureRequirement(models.Model):
    research_strategy = models.ForeignKey(ResearchStrategyDefinition, on_delete=models.PROTECT, related_name="feature_requirements")
    feature = models.ForeignKey(ResearchFeatureDefinition, on_delete=models.PROTECT, related_name="strategy_requirements")
    required = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["research_strategy", "feature"], name="unique_strategy_feature_requirement")]


class ResearchStrategyImplementation(models.Model):
    research_strategy = models.ForeignKey(ResearchStrategyDefinition, on_delete=models.PROTECT, related_name="implementations")
    implementation_path = models.CharField(max_length=500)
    implementation_version = models.CharField(max_length=64)
    implementation_hash = models.CharField(max_length=64)
    role = models.CharField(max_length=24, choices=StrategyRole.choices)
    exact_semantic_match = models.BooleanField(default=False)
    supported_frequency = models.CharField(max_length=16)
    supported_direction = models.CharField(max_length=16)
    status = models.CharField(max_length=32, choices=ImplementationStatus.choices, default=ImplementationStatus.DRAFT)
    executable_strategy_definition = models.ForeignKey(
        "strategies.StrategyDefinition", on_delete=models.PROTECT, null=True, blank=True,
        related_name="research_implementations",
    )
    default_parameters = models.JSONField(default=dict)
    approval_record = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["research_strategy", "implementation_path", "implementation_version"],
                name="unique_research_implementation",
            ),
            models.CheckConstraint(
                condition=Q(exact_semantic_match=True) | Q(executable_strategy_definition__isnull=True),
                name="executable_requires_exact_match",
            ),
        ]


class ResearchStrategyReadiness(models.Model):
    research_strategy = models.ForeignKey(ResearchStrategyDefinition, on_delete=models.PROTECT, related_name="readiness_snapshots")
    as_of_date = models.DateField()
    data_ready = models.BooleanField(default=False)
    features_ready = models.BooleanField(default=False)
    implementation_ready = models.BooleanField(default=False)
    backtest_ready = models.BooleanField(default=False)
    approved = models.BooleanField(default=False)
    builder_ready = models.BooleanField(default=False)
    blocking_reasons = models.JSONField(default=list)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["research_strategy", "as_of_date"], name="unique_strategy_readiness_date")]


class ResearchDailyBar(models.Model):
    QUALITY = [(value, value) for value in ["PENDING", "VALID", "SUSPECT", "REJECTED"]]
    instrument = models.ForeignKey("instruments.Instrument", on_delete=models.PROTECT, related_name="research_daily_bars")
    trading_date = models.DateField()
    raw_open = models.DecimalField(max_digits=24, decimal_places=8)
    raw_high = models.DecimalField(max_digits=24, decimal_places=8)
    raw_low = models.DecimalField(max_digits=24, decimal_places=8)
    raw_close = models.DecimalField(max_digits=24, decimal_places=8)
    adjusted_open = models.DecimalField(max_digits=24, decimal_places=8)
    adjusted_high = models.DecimalField(max_digits=24, decimal_places=8)
    adjusted_low = models.DecimalField(max_digits=24, decimal_places=8)
    adjusted_close = models.DecimalField(max_digits=24, decimal_places=8)
    total_return_close = models.DecimalField(max_digits=24, decimal_places=8)
    volume = models.DecimalField(max_digits=28, decimal_places=4)
    cash_dividend = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    split_factor = models.DecimalField(max_digits=20, decimal_places=10, default=1)
    adjustment_factor = models.DecimalField(max_digits=20, decimal_places=10, default=1)
    provider = models.CharField(max_length=32)
    provider_timestamp = models.DateTimeField()
    revision_timestamp = models.DateTimeField()
    data_version = models.PositiveIntegerField(default=1)
    quality_status = models.CharField(max_length=16, choices=QUALITY, default="PENDING")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["instrument", "trading_date", "data_version"], name="unique_research_bar_version")]
        indexes = [models.Index(fields=["instrument", "trading_date", "quality_status"], name="research_bar_date_idx")]


class ResearchIntradayBar(models.Model):
    instrument = models.ForeignKey("instruments.Instrument", on_delete=models.PROTECT, related_name="research_intraday_bars")
    frequency = models.CharField(max_length=8)
    window_start = models.DateTimeField()
    window_end = models.DateTimeField()
    open = models.DecimalField(max_digits=24, decimal_places=8)
    high = models.DecimalField(max_digits=24, decimal_places=8)
    low = models.DecimalField(max_digits=24, decimal_places=8)
    close = models.DecimalField(max_digits=24, decimal_places=8)
    volume = models.DecimalField(max_digits=28, decimal_places=4)
    vwap = models.DecimalField(max_digits=24, decimal_places=8, null=True, blank=True)
    provider = models.CharField(max_length=32)
    provider_timestamp = models.DateTimeField()
    revision_timestamp = models.DateTimeField()
    data_version = models.PositiveIntegerField(default=1)
    quality_status = models.CharField(max_length=16, default="PENDING")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["instrument", "frequency", "window_start", "data_version"], name="unique_research_intraday_version")]
        indexes = [models.Index(fields=["instrument", "frequency", "window_start", "quality_status"], name="research_intraday_date_idx")]


class ResearchCorporateAction(models.Model):
    ACTIONS = [(value, value) for value in ["DIVIDEND", "SPLIT", "SYMBOL_CHANGE", "MERGER", "SPIN_OFF", "DELISTING", "CASH_PROCEEDS"]]
    instrument = models.ForeignKey("instruments.Instrument", on_delete=models.PROTECT, related_name="research_corporate_actions")
    action_type = models.CharField(max_length=24, choices=ACTIONS)
    announced_at = models.DateTimeField(null=True, blank=True)
    effective_at = models.DateTimeField()
    payload = models.JSONField(default=dict)
    provider = models.CharField(max_length=32)
    revision_timestamp = models.DateTimeField()
    data_version = models.PositiveIntegerField(default=1)
    quality_status = models.CharField(max_length=16, default="PENDING")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["instrument", "action_type", "effective_at", "data_version"], name="unique_corporate_action_version")]


class ResearchFundamentalFact(models.Model):
    issuer = models.ForeignKey("instruments.Issuer", on_delete=models.PROTECT, related_name="research_fundamentals")
    metric = models.CharField(max_length=128)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField()
    filing_timestamp = models.DateTimeField()
    public_availability_timestamp = models.DateTimeField()
    value = models.DecimalField(max_digits=36, decimal_places=12)
    units = models.CharField(max_length=32)
    original_value = models.CharField(max_length=255)
    revision_version = models.PositiveIntegerField(default=1)
    provider = models.CharField(max_length=32, blank=True)
    provider_timestamp = models.DateTimeField(null=True, blank=True)
    revision_timestamp = models.DateTimeField(null=True, blank=True)
    data_version = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["issuer", "metric", "period_end", "revision_version"], name="unique_fundamental_revision")]
        indexes = [models.Index(fields=["issuer", "metric", "public_availability_timestamp"], name="fundamental_available_idx")]


class ResearchAnalystFact(models.Model):
    issuer = models.ForeignKey("instruments.Issuer", on_delete=models.PROTECT, related_name="research_analyst_facts")
    instrument = models.ForeignKey("instruments.Instrument", on_delete=models.PROTECT, null=True, blank=True, related_name="research_analyst_facts")
    fact_type = models.CharField(max_length=64)
    metric = models.CharField(max_length=128)
    period_end = models.DateField(null=True, blank=True)
    event_timestamp = models.DateTimeField()
    public_availability_timestamp = models.DateTimeField()
    value = models.DecimalField(max_digits=36, decimal_places=12, null=True, blank=True)
    payload = models.JSONField(default=dict)
    provider = models.CharField(max_length=32)
    provider_timestamp = models.DateTimeField()
    revision_timestamp = models.DateTimeField()
    data_version = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["issuer", "fact_type", "metric", "event_timestamp", "data_version"], name="unique_analyst_fact_version")]
        indexes = [models.Index(fields=["issuer", "fact_type", "public_availability_timestamp"], name="analyst_available_idx")]


class ResearchEvent(models.Model):
    issuer = models.ForeignKey("instruments.Issuer", on_delete=models.PROTECT, null=True, blank=True, related_name="research_events")
    instrument = models.ForeignKey("instruments.Instrument", on_delete=models.PROTECT, null=True, blank=True, related_name="research_events")
    event_type = models.CharField(max_length=64)
    announced_timestamp = models.DateTimeField(null=True, blank=True)
    effective_timestamp = models.DateTimeField()
    available_timestamp = models.DateTimeField()
    timezone = models.CharField(max_length=64, default="UTC")
    payload = models.JSONField(default=dict)
    quality_status = models.CharField(max_length=16, default="PENDING")
    provider = models.CharField(max_length=32, blank=True)
    provider_timestamp = models.DateTimeField(null=True, blank=True)
    revision_timestamp = models.DateTimeField(null=True, blank=True)
    data_version = models.PositiveIntegerField(default=1)

    class Meta:
        indexes = [models.Index(fields=["event_type", "available_timestamp"], name="research_event_available_idx")]


class ResearchExperiment(models.Model):
    strategy = models.ForeignKey(ResearchStrategyDefinition, on_delete=models.PROTECT, related_name="experiments")
    universe = models.ForeignKey(ResearchUniverse, on_delete=models.PROTECT, related_name="experiments")
    protocol = models.ForeignKey(BacktestProtocolVersion, on_delete=models.PROTECT, related_name="experiments")
    dataset_version = models.ForeignKey(ResearchDatasetVersion, on_delete=models.PROTECT, related_name="experiments")
    instrument = models.ForeignKey(
        "instruments.Instrument", on_delete=models.PROTECT, null=True, blank=True,
        related_name="research_experiments",
    )
    implementation_hash = models.CharField(max_length=64, blank=True)
    data_version = models.CharField(max_length=64, blank=True)
    provider_data_version = models.CharField(max_length=64, blank=True)
    feature_version = models.CharField(max_length=64, blank=True)
    role = models.CharField(max_length=24, choices=StrategyRole.choices, default=StrategyRole.EXECUTION)
    parameter_space_hash = models.CharField(max_length=64, blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    experiment_type = models.CharField(max_length=32, default="WALK_FORWARD")
    parameter_budget = models.PositiveIntegerField(default=100)
    request_hash = models.CharField(max_length=64, db_index=True)
    idempotency_key = models.CharField(max_length=128, unique=True)
    status = models.CharField(max_length=16, choices=WorkStatus.choices, default=WorkStatus.QUEUED)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["dataset_version", "role", "strategy", "status"], name="research_role_status_idx"),
            models.Index(fields=["dataset_version", "instrument", "strategy", "status"], name="research_pair_status_idx"),
        ]


class ResearchTrial(models.Model):
    experiment = models.ForeignKey(ResearchExperiment, on_delete=models.PROTECT, related_name="trials")
    instrument = models.ForeignKey("instruments.Instrument", on_delete=models.PROTECT, null=True, blank=True, related_name="research_trials")
    peer_group_reference = models.CharField(max_length=255, blank=True)
    parameters = models.JSONField(default=dict)
    parameter_hash = models.CharField(max_length=64)
    window_configuration = models.JSONField(default=dict)
    status = models.CharField(max_length=16, choices=WorkStatus.choices, default=WorkStatus.QUEUED)
    summary_metrics = models.JSONField(default=dict)
    validation_metrics = models.JSONField(default=dict)
    rejection_reasons = models.JSONField(default=list)
    artifact_uri = models.CharField(max_length=1000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["experiment", "instrument", "parameter_hash"], name="unique_experiment_trial")]


class ResearchCandidateScore(models.Model):
    strategy = models.ForeignKey(ResearchStrategyDefinition, on_delete=models.PROTECT, related_name="candidate_scores")
    instrument = models.ForeignKey("instruments.Instrument", on_delete=models.PROTECT, null=True, blank=True, related_name="research_candidate_scores")
    candidate_type = models.CharField(max_length=32)
    goal_timeframe = models.CharField(max_length=16)
    risk_level = models.PositiveSmallIntegerField()
    as_of_date = models.DateField()
    score = models.DecimalField(max_digits=7, decimal_places=3)
    eligible = models.BooleanField(default=False)
    hard_rejection_reasons = models.JSONField(default=list)
    best_parameters = models.JSONField(default=dict)
    metrics = models.JSONField(default=dict)
    regime_metrics = models.JSONField(default=dict)
    cost_metrics = models.JSONField(default=dict)
    stability_metrics = models.JSONField(default=dict)
    capacity_metrics = models.JSONField(default=dict)
    protocol_version = models.ForeignKey(BacktestProtocolVersion, on_delete=models.PROTECT, related_name="candidate_scores")
    dataset_version = models.ForeignKey(ResearchDatasetVersion, on_delete=models.PROTECT, related_name="candidate_scores")
    expires_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["strategy", "instrument", "goal_timeframe", "risk_level", "as_of_date"], name="unique_candidate_score_date")]
        indexes = [
            models.Index(fields=["goal_timeframe", "risk_level", "eligible", "-score"], name="candidate_goal_score_idx"),
            models.Index(fields=["instrument", "goal_timeframe", "risk_level"], name="candidate_instrument_idx"),
            models.Index(fields=["strategy", "as_of_date"], name="candidate_strategy_date_idx"),
        ]


class ResearchRoleScore(models.Model):
    SCORE_TYPES = [(value, value) for value in ["STOCK", "EXECUTION", "ALLOCATOR", "OVERLAY", "EVENT", "PAIR_BASKET"]]
    score_type = models.CharField(max_length=24, choices=SCORE_TYPES)
    dataset_version = models.ForeignKey(ResearchDatasetVersion, on_delete=models.PROTECT, related_name="role_scores")
    strategy = models.ForeignKey(ResearchStrategyDefinition, on_delete=models.PROTECT, null=True, blank=True, related_name="role_scores")
    instrument = models.ForeignKey("instruments.Instrument", on_delete=models.PROTECT, null=True, blank=True, related_name="research_role_scores")
    goal_timeframe = models.CharField(max_length=16)
    risk_level = models.PositiveSmallIntegerField()
    as_of_date = models.DateField()
    score = models.DecimalField(max_digits=9, decimal_places=4)
    components = models.JSONField(default=dict)
    contributing_strategy_ids = models.JSONField(default=list)
    expires_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["score_type", "dataset_version", "strategy", "instrument", "goal_timeframe", "risk_level", "as_of_date"], name="unique_role_score_date")]
        indexes = [models.Index(fields=["score_type", "goal_timeframe", "risk_level", "-score"], name="role_goal_score_idx")]


class InstrumentFeatureSnapshot(models.Model):
    instrument = models.ForeignKey("instruments.Instrument", on_delete=models.PROTECT, related_name="research_feature_snapshots")
    feature_key = models.CharField(max_length=128)
    frequency = models.CharField(max_length=8)
    as_of_date = models.DateField()
    available_at = models.DateTimeField()
    data_version = models.CharField(max_length=64)
    implementation_version = models.CharField(max_length=64)
    value = models.JSONField(default=dict)
    artifact_uri = models.CharField(max_length=1000, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["instrument", "feature_key", "frequency", "as_of_date", "data_version", "implementation_version"], name="unique_instrument_feature")]
        indexes = [models.Index(fields=["feature_key", "frequency", "as_of_date"], name="instrument_feature_date_idx")]


class CrossSectionalFeatureSnapshot(models.Model):
    universe = models.ForeignKey(ResearchUniverse, on_delete=models.PROTECT, related_name="cross_sectional_features")
    feature_key = models.CharField(max_length=128)
    frequency = models.CharField(max_length=8)
    as_of_date = models.DateField()
    available_at = models.DateTimeField()
    data_version = models.CharField(max_length=64)
    implementation_version = models.CharField(max_length=64)
    summary = models.JSONField(default=dict)
    artifact_uri = models.CharField(max_length=1000)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["universe", "feature_key", "frequency", "as_of_date", "data_version", "implementation_version"], name="unique_cross_section_feature")]
        indexes = [models.Index(fields=["universe", "feature_key", "as_of_date"], name="cross_feature_date_idx")]


class MarketRegimeSnapshot(models.Model):
    universe = models.ForeignKey(ResearchUniverse, on_delete=models.PROTECT, related_name="regime_snapshots")
    as_of_date = models.DateField()
    available_at = models.DateTimeField()
    regime = models.CharField(max_length=32)
    features = models.JSONField(default=dict)
    data_version = models.CharField(max_length=64)
    implementation_version = models.CharField(max_length=64)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["universe", "as_of_date", "data_version", "implementation_version"], name="unique_market_regime")]


class EventFeatureSnapshot(models.Model):
    event = models.ForeignKey(ResearchEvent, on_delete=models.PROTECT, related_name="feature_snapshots")
    instrument = models.ForeignKey("instruments.Instrument", on_delete=models.PROTECT, null=True, blank=True, related_name="event_feature_snapshots")
    feature_key = models.CharField(max_length=128)
    as_of_date = models.DateField()
    available_at = models.DateTimeField()
    data_version = models.CharField(max_length=64)
    implementation_version = models.CharField(max_length=64)
    value = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["event", "feature_key", "as_of_date", "data_version", "implementation_version"], name="unique_event_feature")]


class GoalRecommendationPolicy(models.Model):
    name = models.CharField(max_length=128, unique=True)
    minimum_candidate_score = models.DecimalField(max_digits=6, decimal_places=2, default=65)
    maximum_candidate_age_days = models.PositiveIntegerField(default=7)
    number_of_stocks = models.PositiveIntegerField(default=20)
    minimum_sectors = models.PositiveIntegerField(default=3)
    sector_cap = models.DecimalField(max_digits=8, decimal_places=6, default="0.35")
    industry_cap = models.DecimalField(max_digits=8, decimal_places=6, default="0.25")
    sub_industry_cap = models.DecimalField(max_digits=8, decimal_places=6, default="0.20")
    per_stock_cap = models.DecimalField(max_digits=8, decimal_places=6, default="0.20")
    strategy_family_cap = models.DecimalField(max_digits=8, decimal_places=6, default="0.40")
    maximum_turnover = models.DecimalField(max_digits=8, decimal_places=6, default="1.00")
    minimum_cash = models.DecimalField(max_digits=8, decimal_places=6, default="0.10")
    target_volatility = models.DecimalField(max_digits=8, decimal_places=6, default="0.15")
    maximum_expected_drawdown = models.DecimalField(max_digits=8, decimal_places=6, default="0.30")
    minimum_liquidity = models.DecimalField(max_digits=28, decimal_places=4, default=25000000)
    approved_candidate_roles = models.JSONField(default=list)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class GoalRecommendationRun(models.Model):
    goal_allocation = models.ForeignKey(
        "portfolio_construction.PortfolioGoalAllocation", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="recommendation_runs",
    )
    requested_plan_version = models.PositiveIntegerField()
    policy = models.ForeignKey(GoalRecommendationPolicy, on_delete=models.PROTECT, related_name="recommendation_runs")
    dataset_version = models.ForeignKey(ResearchDatasetVersion, on_delete=models.PROTECT, related_name="recommendation_runs")
    protocol_version = models.ForeignKey(BacktestProtocolVersion, on_delete=models.PROTECT, related_name="recommendation_runs")
    as_of_date = models.DateField()
    status = models.CharField(max_length=16, choices=WorkStatus.choices, default=WorkStatus.QUEUED)
    idempotency_key = models.CharField(max_length=128, unique=True)
    request_hash = models.CharField(max_length=64, db_index=True)
    input_snapshot = models.JSONField(default=dict)
    candidate_snapshot = models.JSONField(default=list)
    optimizer_snapshot = models.JSONField(default=dict)
    stress_test_snapshot = models.JSONField(default=dict)
    metrics = models.JSONField(default=dict)
    warnings = models.JSONField(default=list)
    error = models.TextField(blank=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["goal_allocation", "status", "-created_at"], name="goal_recommendation_idx")]


class GoalRecommendationSleeve(models.Model):
    recommendation_run = models.ForeignKey(GoalRecommendationRun, on_delete=models.PROTECT, related_name="sleeves")
    instrument = models.ForeignKey("instruments.Instrument", on_delete=models.PROTECT, related_name="recommendation_sleeves")
    universe_member = models.ForeignKey(ResearchUniverseMember, on_delete=models.PROTECT, related_name="recommendation_sleeves")
    research_strategy = models.ForeignKey(ResearchStrategyDefinition, on_delete=models.PROTECT, related_name="recommendation_sleeves")
    execution_strategy_definition = models.ForeignKey("strategies.StrategyDefinition", on_delete=models.PROTECT, related_name="recommendation_sleeves")
    execution_timeframe = models.CharField(max_length=16)
    parameters = models.JSONField(default=dict)
    sleeve_weight = models.DecimalField(max_digits=12, decimal_places=8)
    stock_weight = models.DecimalField(max_digits=12, decimal_places=8)
    strategy_share = models.DecimalField(max_digits=12, decimal_places=8)
    candidate_score = models.DecimalField(max_digits=7, decimal_places=3)
    expected_return = models.DecimalField(max_digits=16, decimal_places=8, default=0)
    expected_volatility = models.DecimalField(max_digits=16, decimal_places=8, default=0)
    expected_drawdown = models.DecimalField(max_digits=16, decimal_places=8, default=0)
    cost_metrics = models.JSONField(default=dict)
    rationale = models.TextField(blank=True)
    rank = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["recommendation_run", "instrument", "research_strategy"], name="unique_recommendation_sleeve")]
        ordering = ["rank", "id"]


class GoalRecommendationAcceptance(models.Model):
    recommendation_run = models.OneToOneField(GoalRecommendationRun, on_delete=models.PROTECT, related_name="acceptance")
    goal = models.ForeignKey(
        "portfolio_construction.PortfolioGoalAllocation", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="recommendation_acceptances",
    )
    accepted_plan_version = models.PositiveIntegerField()
    created_updated_instrument_selections = models.JSONField(default=list)
    created_updated_strategy_assignments = models.JSONField(default=list)
    accepted_by = models.CharField(max_length=255)
    accepted_at = models.DateTimeField(auto_now_add=True)
    change_summary = models.JSONField(default=dict)


class RecommendationCacheSnapshot(models.Model):
    dataset_version = models.ForeignKey(ResearchDatasetVersion, on_delete=models.PROTECT, related_name="recommendation_cache_snapshots")
    protocol_version = models.ForeignKey(BacktestProtocolVersion, on_delete=models.PROTECT, related_name="recommendation_cache_snapshots")
    goal_timeframe = models.CharField(max_length=16)
    risk_level = models.PositiveSmallIntegerField()
    as_of_date = models.DateField()
    input_hash = models.CharField(max_length=64)
    candidate_pool = models.JSONField(default=list)
    selected_stocks = models.JSONField(default=list)
    allocator_strategy_id = models.CharField(max_length=64, blank=True)
    overlay_strategy_ids = models.JSONField(default=list)
    expected_metrics = models.JSONField(default=dict)
    gics_exposure = models.JSONField(default=dict)
    fallback_tier = models.PositiveSmallIntegerField(default=1)
    data_freshness = models.JSONField(default=dict)
    status = models.CharField(max_length=16, choices=WorkStatus.choices, default=WorkStatus.COMPLETED)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["dataset_version", "protocol_version", "goal_timeframe", "risk_level", "as_of_date", "input_hash"], name="unique_recommendation_cache")]
        indexes = [models.Index(fields=["goal_timeframe", "risk_level", "status", "-created_at"], name="recommendation_cache_key_idx")]


class RecommendationBatchRun(models.Model):
    plan = models.ForeignKey("portfolio_construction.PortfolioConstructionPlan", on_delete=models.PROTECT, related_name="recommendation_batches")
    requested_plan_version = models.PositiveIntegerField()
    status = models.CharField(max_length=16, choices=WorkStatus.choices, default=WorkStatus.QUEUED)
    idempotency_key = models.CharField(max_length=128, unique=True)
    input_hash = models.CharField(max_length=64, db_index=True)
    dataset = models.ForeignKey(ResearchDatasetVersion, on_delete=models.PROTECT, related_name="recommendation_batches")
    protocol = models.ForeignKey(BacktestProtocolVersion, on_delete=models.PROTECT, related_name="recommendation_batches")
    input_snapshot = models.JSONField(default=list)
    metrics = models.JSONField(default=dict)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["plan", "status", "-created_at"], name="recommend_batch_status_idx")]


class RecommendationBatchGoalResult(models.Model):
    batch = models.ForeignKey(RecommendationBatchRun, on_delete=models.PROTECT, related_name="goal_results")
    goal = models.ForeignKey(
        "portfolio_construction.PortfolioGoalAllocation", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="recommendation_batch_results",
    )
    recommendation_run = models.ForeignKey(GoalRecommendationRun, on_delete=models.PROTECT, null=True, blank=True, related_name="batch_results")
    status = models.CharField(max_length=16, choices=WorkStatus.choices, default=WorkStatus.QUEUED)
    fallback_tier = models.PositiveSmallIntegerField(default=1)
    summary = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["batch", "goal"], name="unique_batch_goal_result")]
