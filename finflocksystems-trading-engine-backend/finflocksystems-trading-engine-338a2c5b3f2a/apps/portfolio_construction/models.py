from django.core.exceptions import ValidationError
from django.db import models

from .rules import RISK_OPTIONS, TIMEFRAME_OPTIONS, validate_timeframe_risk


class PortfolioConstructionPlan(models.Model):
    STATUSES = [(value, value) for value in ["DRAFT", "ACTIVE", "PAUSED"]]
    portfolio = models.OneToOneField(
        "portfolios.TradingPortfolio", on_delete=models.PROTECT, related_name="construction_plan"
    )
    name = models.CharField(max_length=128, default="Portfolio Builder")
    status = models.CharField(max_length=16, choices=STATUSES, default="DRAFT")
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class PortfolioGoalAllocation(models.Model):
    CONSTRUCTION_SOURCES = [
        ("MANUAL_OPTIMIZER", "Manual optimizer"),
        ("ACCEPTED_RECOMMENDATION", "Accepted recommendation"),
    ]
    plan = models.ForeignKey(PortfolioConstructionPlan, on_delete=models.CASCADE, related_name="goals")
    name = models.CharField(max_length=128)
    allocation_weight = models.DecimalField(max_digits=10, decimal_places=8)
    timeframe_bucket = models.CharField(max_length=16, choices=TIMEFRAME_OPTIONS)
    risk_level = models.PositiveSmallIntegerField(choices=[(level, label) for level, _, label in RISK_OPTIONS])
    enabled = models.BooleanField(default=True)
    display_order = models.PositiveSmallIntegerField(default=0)
    construction_source = models.CharField(
        max_length=32, choices=CONSTRUCTION_SOURCES, default="MANUAL_OPTIMIZER"
    )
    accepted_recommendation_run = models.ForeignKey(
        "research.GoalRecommendationRun",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="accepted_goals",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_order", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(allocation_weight__gte=0) & models.Q(allocation_weight__lte=1),
                name="goal_allocation_weight_between_zero_one",
            ),
            models.CheckConstraint(
                condition=models.Q(risk_level__gte=1) & models.Q(risk_level__lte=5),
                name="goal_risk_level_between_one_five",
            ),
        ]

    def clean(self):
        try:
            validate_timeframe_risk(self.timeframe_bucket, self.risk_level)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc


class StrategyConstructionProfile(models.Model):
    strategy_definition = models.OneToOneField(
        "strategies.StrategyDefinition", on_delete=models.PROTECT, related_name="construction_profile"
    )
    supported_goal_timeframes = models.JSONField(default=list)
    minimum_risk = models.PositiveSmallIntegerField(default=1)
    maximum_risk = models.PositiveSmallIntegerField(default=5)
    construction_enabled = models.BooleanField(default=True)
    user_selectable = models.BooleanField(default=True)
    summary = models.TextField(blank=True)
    limitations = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(minimum_risk__gte=1) & models.Q(minimum_risk__lte=5),
                name="construction_profile_minimum_risk",
            ),
            models.CheckConstraint(
                condition=models.Q(maximum_risk__gte=1) & models.Q(maximum_risk__lte=5),
                name="construction_profile_maximum_risk",
            ),
            models.CheckConstraint(
                condition=models.Q(minimum_risk__lte=models.F("maximum_risk")),
                name="construction_profile_risk_order",
            ),
        ]


class GoalInstrumentSelection(models.Model):
    goal_allocation = models.ForeignKey(
        PortfolioGoalAllocation, on_delete=models.CASCADE, related_name="instrument_selections"
    )
    instrument = models.ForeignKey("instruments.Instrument", on_delete=models.PROTECT)
    enabled = models.BooleanField(default=True)
    minimum_weight = models.DecimalField(max_digits=10, decimal_places=8, null=True, blank=True)
    maximum_weight = models.DecimalField(max_digits=10, decimal_places=8, null=True, blank=True)
    display_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["goal_allocation", "instrument"],
                name="unique_goal_instrument_selection",
            ),
            models.CheckConstraint(
                condition=(models.Q(minimum_weight__isnull=True) |
                           (models.Q(minimum_weight__gte=0) & models.Q(minimum_weight__lte=1))),
                name="goal_instrument_minimum_weight_range",
            ),
            models.CheckConstraint(
                condition=(models.Q(maximum_weight__isnull=True) |
                           (models.Q(maximum_weight__gte=0) & models.Q(maximum_weight__lte=1))),
                name="goal_instrument_maximum_weight_range",
            ),
            models.CheckConstraint(
                condition=(models.Q(minimum_weight__isnull=True) |
                           models.Q(maximum_weight__isnull=True) |
                           models.Q(minimum_weight__lte=models.F("maximum_weight"))),
                name="goal_instrument_weight_order",
            ),
        ]

    def clean(self):
        if self.instrument_id and (
            not self.instrument.active or not self.instrument.tradable or self.instrument.asset_class != "STK"
        ):
            raise ValidationError("Instrument must be an active, tradable stock")
        if (
            self.minimum_weight is not None
            and self.maximum_weight is not None
            and self.minimum_weight > self.maximum_weight
        ):
            raise ValidationError("minimum_weight must not exceed maximum_weight")


class GoalStrategyAssignment(models.Model):
    goal_instrument_selection = models.ForeignKey(
        GoalInstrumentSelection, on_delete=models.CASCADE, related_name="assignments"
    )
    strategy_definition = models.ForeignKey("strategies.StrategyDefinition", on_delete=models.PROTECT)
    execution_timeframe = models.CharField(max_length=16)
    parameter_overrides = models.JSONField(default=dict)
    parameter_hash = models.CharField(max_length=64)
    strategy_share = models.DecimalField(max_digits=10, decimal_places=8, default=1)
    risk_policy = models.ForeignKey(
        "strategies.StrategyRiskPolicy", on_delete=models.PROTECT, null=True, blank=True
    )
    order_policy = models.ForeignKey(
        "strategies.OrderPolicy", on_delete=models.PROTECT, null=True, blank=True
    )
    create_instance = models.BooleanField(default=True)
    enabled = models.BooleanField(default=True)
    created_strategy_instance = models.ForeignKey(
        "strategies.StrategyInstance", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="construction_assignments",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "goal_instrument_selection", "strategy_definition", "execution_timeframe", "parameter_hash"
                ],
                name="unique_goal_strategy_assignment_identity",
            ),
            models.CheckConstraint(
                condition=models.Q(strategy_share__gte=0) & models.Q(strategy_share__lte=1),
                name="goal_strategy_share_between_zero_one",
            ),
        ]


class PortfolioConstructionRun(models.Model):
    APPLICATION_STATUSES = [(value, value) for value in [
        "NOT_APPLIED", "QUEUED", "APPLYING", "ACTIVATING", "APPLIED",
        "PARTIALLY_APPLIED", "FAILED",
    ]]
    plan = models.ForeignKey(PortfolioConstructionPlan, on_delete=models.PROTECT, related_name="runs")
    idempotency_key = models.CharField(max_length=128, unique=True)
    request_hash = models.CharField(max_length=64, db_index=True)
    status = models.CharField(max_length=24, default="CALCULATING")
    application_status = models.CharField(max_length=24, choices=APPLICATION_STATUSES, default="NOT_APPLIED")
    application_idempotency_key = models.CharField(max_length=128, blank=True)
    retryable = models.BooleanField(default=False)
    last_error = models.CharField(max_length=1000, blank=True)
    attempt_count = models.PositiveIntegerField(default=1)
    nav = models.DecimalField(max_digits=24, decimal_places=8)
    plan_snapshot = models.JSONField(default=dict)
    goal_snapshot = models.JSONField(default=list)
    instrument_snapshot = models.JSONField(default=list)
    assignment_snapshot = models.JSONField(default=list)
    policy_snapshot = models.JSONField(default=dict)
    goal_results = models.JSONField(default=list)
    final_target_weights = models.JSONField(default=dict)
    metrics = models.JSONField(default=dict)
    warnings = models.JSONField(default=list)
    applied_rebalance = models.OneToOneField(
        "allocation.RebalanceRun", on_delete=models.PROTECT, null=True, blank=True,
        related_name="applied_construction",
    )
    applied_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["plan", "status", "-created_at"], name="construction_plan_status_idx")]


class PortfolioConstructionTarget(models.Model):
    construction_run = models.ForeignKey(PortfolioConstructionRun, on_delete=models.PROTECT, related_name="targets")
    instrument = models.ForeignKey("instruments.Instrument", on_delete=models.PROTECT)
    current_weight = models.DecimalField(max_digits=12, decimal_places=8, default=0)
    target_weight = models.DecimalField(max_digits=12, decimal_places=8)
    target_value = models.DecimalField(max_digits=24, decimal_places=8)
    expected_return_contribution = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    risk_contribution = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    goal_contributions = models.JSONField(default=list)
    rank = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["construction_run", "instrument"], name="unique_construction_run_instrument")
        ]
