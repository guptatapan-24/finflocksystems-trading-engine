from django.db import models

from apps.execution.modes import ExecutionMode


class PortfolioUniverse(models.Model):
    portfolio = models.OneToOneField("portfolios.TradingPortfolio", on_delete=models.PROTECT, related_name="optimization_universe")
    name = models.CharField(max_length=128, default="Default universe")
    include_strategy_instruments = models.BooleanField(default=False)
    minimum_history_observations = models.PositiveIntegerField(default=60)
    maximum_instruments = models.PositiveIntegerField(default=50)
    enabled = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)


class PortfolioUniverseInstrument(models.Model):
    universe = models.ForeignKey(PortfolioUniverse, on_delete=models.CASCADE, related_name="memberships")
    instrument = models.ForeignKey("instruments.Instrument", on_delete=models.PROTECT)
    enabled = models.BooleanField(default=True)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["universe", "instrument"], name="unique_portfolio_universe_instrument")]


class PortfolioOptimizationPolicy(models.Model):
    METHODS = [(value, value) for value in ["MINIMUM_VARIANCE", "MAXIMUM_SHARPE"]]
    portfolio = models.OneToOneField("portfolios.TradingPortfolio", on_delete=models.PROTECT, related_name="optimization_policy")
    name = models.CharField(max_length=128, default="Default Markowitz policy")
    method = models.CharField(max_length=32, choices=METHODS, default="MINIMUM_VARIANCE")
    lookback_days = models.PositiveIntegerField(default=252)
    return_estimation = models.CharField(max_length=32, default="HISTORICAL_MEAN")
    covariance_estimation = models.CharField(max_length=32, default="SAMPLE")
    risk_free_rate = models.DecimalField(max_digits=12, decimal_places=8, default="0")
    target_cash_weight = models.DecimalField(max_digits=10, decimal_places=8, default="0.02")
    minimum_weight = models.DecimalField(max_digits=10, decimal_places=8, default="0")
    maximum_weight = models.DecimalField(max_digits=10, decimal_places=8, default="1")
    maximum_turnover = models.DecimalField(max_digits=10, decimal_places=8, default="1")
    transaction_cost_penalty = models.DecimalField(max_digits=16, decimal_places=8, default="0")
    long_only = models.BooleanField(default=True)
    enabled = models.BooleanField(default=True)
    execution_mode = models.CharField(
        max_length=16, choices=ExecutionMode.choices, default=ExecutionMode.PAPER
    )
    version = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(execution_mode__in=ExecutionMode.values),
                name="optimization_policy_valid_execution_mode",
            ),
        ]


class PortfolioOptimizationRun(models.Model):
    APPLICATION_STATUSES = [(value, value) for value in ["NOT_APPLIED", "QUEUED", "APPLYING", "APPLIED", "FAILED"]]
    portfolio = models.ForeignKey("portfolios.TradingPortfolio", on_delete=models.PROTECT, related_name="optimization_runs")
    policy = models.ForeignKey(PortfolioOptimizationPolicy, on_delete=models.PROTECT)
    universe = models.ForeignKey(PortfolioUniverse, on_delete=models.PROTECT)
    idempotency_key = models.CharField(max_length=128, unique=True)
    request_hash = models.CharField(max_length=64, default="", db_index=True)
    retryable = models.BooleanField(default=False)
    last_error = models.CharField(max_length=1000, blank=True)
    attempt_count = models.PositiveIntegerField(default=1)
    trigger = models.CharField(max_length=40, default="MANUAL")
    status = models.CharField(max_length=24, default="CALCULATING")
    input_start_date = models.DateField(null=True, blank=True)
    input_end_date = models.DateField(null=True, blank=True)
    nav = models.DecimalField(max_digits=24, decimal_places=8)
    current_weights = models.JSONField(default=dict)
    expected_returns = models.JSONField(default=dict)
    covariance_snapshot = models.JSONField(default=dict)
    constraints_snapshot = models.JSONField(default=dict)
    policy_snapshot = models.JSONField(default=dict)
    solver_status = models.CharField(max_length=64, blank=True)
    objective_value = models.DecimalField(max_digits=28, decimal_places=12, null=True, blank=True)
    expected_return = models.DecimalField(max_digits=20, decimal_places=10, null=True, blank=True)
    expected_volatility = models.DecimalField(max_digits=20, decimal_places=10, null=True, blank=True)
    sharpe_ratio = models.DecimalField(max_digits=20, decimal_places=10, null=True, blank=True)
    turnover = models.DecimalField(max_digits=20, decimal_places=10, null=True, blank=True)
    cash_weight = models.DecimalField(max_digits=10, decimal_places=8, default=0)
    warnings = models.JSONField(default=list)
    error_details = models.JSONField(default=dict)
    flow_reference = models.CharField(max_length=128, blank=True)
    application_status = models.CharField(max_length=24, choices=APPLICATION_STATUSES, default="NOT_APPLIED")
    application_idempotency_key = models.CharField(max_length=128, blank=True)
    applied_rebalance = models.OneToOneField(
        "allocation.RebalanceRun",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="applied_optimization",
    )
    applied_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)


class OptimizedPortfolioTarget(models.Model):
    optimization_run = models.ForeignKey(PortfolioOptimizationRun, on_delete=models.PROTECT, related_name="targets")
    instrument = models.ForeignKey("instruments.Instrument", on_delete=models.PROTECT)
    current_weight = models.DecimalField(max_digits=12, decimal_places=8)
    optimized_weight = models.DecimalField(max_digits=12, decimal_places=8)
    weight_change = models.DecimalField(max_digits=12, decimal_places=8)
    target_value = models.DecimalField(max_digits=24, decimal_places=8)
    expected_return_contribution = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    risk_contribution = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    constraint_status = models.CharField(max_length=64, blank=True)
    rank = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["optimization_run", "instrument"], name="unique_optimized_run_instrument")]
