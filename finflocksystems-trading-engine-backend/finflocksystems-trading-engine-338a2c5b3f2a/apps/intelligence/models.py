"""Phase 1 tables. Every table is intel_* and only references other intel_ rows with PROTECT (R12)."""
from django.db import models


class PromptTemplate(models.Model):
    name = models.CharField(max_length=64)
    version = models.PositiveIntegerField()
    system_prompt = models.TextField()
    output_schema = models.JSONField(default=dict)
    active = models.BooleanField(default=False)
    notes = models.CharField(max_length=500, blank=True)
    # Result of `run_intelligence_evals`; a template may only replace the active one after beating it (Phase 6).
    eval_scores = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "intel_prompt_template"
        constraints = [
            models.UniqueConstraint(fields=["name", "version"], name="intel_prompt_name_version_uniq"),
            models.UniqueConstraint(
                fields=["name"], condition=models.Q(active=True), name="intel_prompt_one_active_per_name"
            ),
        ]

    def __str__(self):
        return f"{self.name} v{self.version}"


class LlmRunLog(models.Model):
    class Status(models.TextChoices):
        OK = "OK"
        SCHEMA_INVALID = "SCHEMA_INVALID"
        GROUNDING_FAILED = "GROUNDING_FAILED"
        ERROR = "ERROR"
        TIMEOUT = "TIMEOUT"
        BUDGET = "BUDGET"
        CIRCUIT_OPEN = "CIRCUIT_OPEN"

    scope = models.CharField(max_length=32)
    provider = models.CharField(max_length=16)
    model = models.CharField(max_length=96, blank=True)
    prompt_template = models.ForeignKey(
        PromptTemplate, on_delete=models.PROTECT, null=True, blank=True, related_name="runs"
    )
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    thinking_tokens = models.PositiveIntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=12, decimal_places=6, default=0)
    latency_ms = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, choices=Status.choices)
    raw_response = models.TextField(blank=True)
    parsed_output = models.JSONField(null=True, blank=True)
    validation_errors = models.JSONField(default=list)
    evidence_hash = models.CharField(max_length=64, blank=True)
    idempotency_key = models.CharField(max_length=190, unique=True)
    episode_id = models.BigIntegerField(null=True, blank=True)  # soft reference (Phase 2+)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "intel_llm_run_log"
        indexes = [
            models.Index(fields=["scope", "created_at"], name="intel_llm_scope_time_idx"),
            models.Index(fields=["status", "created_at"], name="intel_llm_status_time_idx"),
        ]


class IntelCursor(models.Model):
    """High-water mark for sweeps (fill sweep in Phase 2, others later)."""

    name = models.CharField(max_length=64, unique=True)
    last_id = models.BigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "intel_cursor"


# ---------------------------------------------------------------------------------------------------
# Phase 2: trade episodes. References into the engine are soft (R12): plain ids or DO_NOTHING FKs with
# db_constraint=False, plus denormalised identity columns so a row stays readable after a strategy delete
# or a database flush resets engine ids.
# ---------------------------------------------------------------------------------------------------
def _soft_fk(model):
    return models.ForeignKey(model, on_delete=models.DO_NOTHING, db_constraint=False, null=True,
                             blank=True, related_name="+")


class TradeEpisode(models.Model):
    class Status(models.TextChoices):
        OPEN = "OPEN"
        CLOSED = "CLOSED"
        EXPIRED = "EXPIRED"

    # identity (denormalised)
    strategy_key = models.CharField(max_length=64)
    instance_id = models.BigIntegerField(db_index=True)
    instance_name = models.CharField(max_length=128)
    strategy_version = models.PositiveIntegerField(default=0)
    portfolio_id = models.BigIntegerField(null=True, blank=True)
    instrument_id = models.BigIntegerField(null=True, blank=True)
    instrument_symbol = models.CharField(max_length=64)
    asset_family = models.CharField(max_length=8)  # STOCK | OPTION
    execution_mode = models.CharField(max_length=16)  # PAPER | LIVE (engine style)
    currency = models.CharField(max_length=8)
    direction = models.CharField(max_length=16)  # LONG | SHORT | NET_CREDIT | NET_DEBIT
    # soft references
    strategy_instance = _soft_fk("strategies.StrategyInstance")
    strategy_version_ref = _soft_fk("strategies.StrategyVersion")
    entry_run = _soft_fk("strategies.StrategyRun")
    option_selection_snapshot = _soft_fk("strategies.OptionSelectionSnapshot")
    run_config_snapshot_id = models.BigIntegerField(null=True, blank=True)  # Phase 4
    # lifecycle
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OPEN)
    opened_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True, blank=True)
    # money (realised to date while OPEN)
    gross_pnl = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    commissions = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    net_pnl = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    pnl_basis = models.CharField(max_length=24, default="FILLS")  # FILLS | ESTIMATED_AT_EXPIRY
    return_on_risk = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    hold_seconds = models.BigIntegerField(null=True, blank=True)
    exit_reason = models.CharField(max_length=24, blank=True)
    exit_subtype = models.CharField(max_length=64, blank=True)
    data_quality_flags = models.JSONField(default=list)
    first_fill_execution_id = models.CharField(max_length=140)
    last_fill_execution_id = models.CharField(max_length=140, blank=True)
    fill_count = models.PositiveIntegerField(default=0)
    fills_fingerprint = models.CharField(max_length=64, blank=True)
    metrics_version_built = models.CharField(max_length=16, blank=True)
    idempotency_key = models.CharField(max_length=255, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "intel_trade_episode"
        indexes = [
            models.Index(fields=["instance_id", "opened_at"], name="intel_ep_instance_open_idx"),
            models.Index(fields=["status", "closed_at"], name="intel_ep_status_close_idx"),
            models.Index(fields=["currency", "closed_at"], name="intel_ep_currency_close_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=~models.Q(currency=""), name="intel_ep_currency_nonempty"),
            models.CheckConstraint(
                condition=models.Q(closed_at__isnull=True) | models.Q(closed_at__gte=models.F("opened_at")),
                name="intel_ep_closed_after_open"),
        ]


class TradeEpisodeLeg(models.Model):
    episode = models.ForeignKey(TradeEpisode, on_delete=models.PROTECT, related_name="legs")
    instrument_id = models.BigIntegerField()
    symbol = models.CharField(max_length=64)
    role = models.CharField(max_length=32, blank=True)
    right = models.CharField(max_length=1, blank=True)
    strike = models.DecimalField(max_digits=24, decimal_places=8, null=True, blank=True)
    expiration = models.DateField(null=True, blank=True)
    multiplier = models.DecimalField(max_digits=20, decimal_places=8, default=1)
    entry_side = models.CharField(max_length=4)
    entry_quantity = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    entry_avg_price = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    exit_quantity = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    exit_avg_price = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    realized_pnl = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    commission = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    entry_reference_price = models.DecimalField(max_digits=24, decimal_places=8, null=True, blank=True)
    exit_reference_price = models.DecimalField(max_digits=24, decimal_places=8, null=True, blank=True)
    entry_slippage_bps = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    exit_slippage_bps = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    first_fill_at = models.DateTimeField(null=True, blank=True)
    last_fill_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "intel_trade_episode_leg"
        constraints = [models.UniqueConstraint(fields=["episode", "instrument_id"], name="intel_leg_episode_instr_uniq")]


class EpisodeMetrics(models.Model):
    episode = models.OneToOneField(TradeEpisode, on_delete=models.PROTECT, related_name="metrics_row")
    metrics = models.JSONField(default=dict)
    metrics_version = models.CharField(max_length=16)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "intel_episode_metrics"
        indexes = [models.Index(fields=["metrics_version"], name="intel_metrics_version_idx")]


class EpisodeEvidenceSnapshot(models.Model):
    """Frozen at episode close; immutable (the engine's own evidence may be compacted or flushed)."""

    episode = models.OneToOneField(TradeEpisode, on_delete=models.PROTECT, related_name="evidence_snapshot")
    pack = models.JSONField(default=dict)
    evidence_hash = models.CharField(max_length=64)
    builder_version = models.CharField(max_length=16)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "intel_episode_evidence_snapshot"

    def save(self, *args, **kwargs):
        if self.pk and EpisodeEvidenceSnapshot.objects.filter(pk=self.pk).exists():
            from django.core.exceptions import ValidationError

            raise ValidationError("EpisodeEvidenceSnapshot records are immutable")
        return super().save(*args, **kwargs)


# ---------------------------------------------------------------------------------------------------
# Phase 3: reviews, mistakes, suggestions. Only intel_ -> intel_ references use PROTECT.
# ---------------------------------------------------------------------------------------------------
MISTAKE_CATEGORIES = [
    "ENTRY_TIMING", "EXIT_TIMING", "SIZING", "PARAMETER_MISFIT", "REGIME_MISMATCH", "EXECUTION_SLIPPAGE",
    "COST_DRAG", "RULE_VIOLATION", "MODEL_MISCALIBRATION", "DATA_QUALITY", "MANUAL_INTERVENTION", "NONE",
]


class TradeReview(models.Model):
    class Scope(models.TextChoices):
        TRADE = "TRADE"
        WEEKLY = "WEEKLY"
        STRATEGY = "STRATEGY"

    scope = models.CharField(max_length=10, choices=Scope.choices, default=Scope.TRADE)
    episode = models.ForeignKey(TradeEpisode, on_delete=models.PROTECT, null=True, blank=True, related_name="reviews")
    # identity (denormalised)
    strategy_key = models.CharField(max_length=64)
    instance_id = models.BigIntegerField(db_index=True)
    instance_name = models.CharField(max_length=128)
    instrument_symbol = models.CharField(max_length=64)
    currency = models.CharField(max_length=8)
    execution_mode = models.CharField(max_length=16)
    verdict = models.CharField(max_length=400, blank=True)
    process_grade = models.CharField(max_length=1, blank=True)  # A-F, blank when not graded (fallback)
    outcome_class = models.CharField(max_length=32, default="INCONCLUSIVE")
    summary = models.TextField(blank=True)
    confidence = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True)
    is_fallback = models.BooleanField(default=False)
    strengths = models.JSONField(default=list)
    open_questions = models.JSONField(default=list)
    lesson_proposals = models.JSONField(default=list)  # stored raw; ignored until Phase 5
    rejected_items = models.JSONField(default=list)  # LLM items dropped by grounding, with reasons
    llm_run = models.ForeignKey(LlmRunLog, on_delete=models.PROTECT, null=True, blank=True, related_name="reviews")
    prompt_version = models.CharField(max_length=32, blank=True)
    model = models.CharField(max_length=96, blank=True)
    evidence_hash = models.CharField(max_length=64, blank=True)
    net_pnl_at_review = models.DecimalField(max_digits=24, decimal_places=8, null=True, blank=True)
    sample_n = models.PositiveIntegerField(default=0)  # closed episodes of this instance and currency
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "intel_trade_review"
        constraints = [
            models.UniqueConstraint(
                fields=["episode", "evidence_hash", "prompt_version", "model"], name="intel_review_once_per_input"),
        ]
        indexes = [models.Index(fields=["instance_id", "-created_at"], name="intel_review_instance_idx")]


class MistakeFinding(models.Model):
    class Source(models.TextChoices):
        RULE = "RULE"
        LLM = "LLM"

    review = models.ForeignKey(TradeReview, on_delete=models.PROTECT, related_name="mistakes")
    category = models.CharField(max_length=24)
    severity = models.CharField(max_length=8)
    description = models.TextField()
    evidence_refs = models.JSONField(default=list)
    source = models.CharField(max_length=4, choices=Source.choices)
    rule_id = models.CharField(max_length=48, blank=True)
    is_defect = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "intel_mistake_finding"
        indexes = [models.Index(fields=["category", "created_at"], name="intel_mistake_cat_idx")]


class Suggestion(models.Model):
    class Status(models.TextChoices):
        PROPOSED = "PROPOSED"
        ACKNOWLEDGED = "ACKNOWLEDGED"
        ACCEPTED = "ACCEPTED"
        REJECTED = "REJECTED"
        APPLIED = "APPLIED"
        EXPIRED = "EXPIRED"
        SUPERSEDED = "SUPERSEDED"

    review = models.ForeignKey(TradeReview, on_delete=models.PROTECT, related_name="suggestions")
    strategy_key = models.CharField(max_length=64)
    instance_id = models.BigIntegerField(db_index=True)
    instance_name = models.CharField(max_length=128)
    kind = models.CharField(max_length=10)  # PARAMETER SIZING RISK PROCESS DATA CODE
    parameter_key = models.CharField(max_length=96, blank=True)
    current_value = models.JSONField(null=True, blank=True)
    proposed_value = models.JSONField(null=True, blank=True)
    rationale = models.TextField(blank=True)
    expected_effect = models.TextField(blank=True)
    risk_of_change = models.TextField(blank=True)
    confidence = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True)
    evidence_needed = models.TextField(blank=True)
    evidence_refs = models.JSONField(default=list)
    is_hypothesis = models.BooleanField(default=True)
    # Phase 11 playbook item (kind EXPERIMENT): predicate, metric to watch, min n, stop rule, history check, tracking state
    experiment = models.JSONField(null=True, blank=True)
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.PROPOSED)
    decided_by = models.CharField(max_length=150, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_reason = models.CharField(max_length=500, blank=True)
    applied_version_id = models.BigIntegerField(null=True, blank=True)  # soft ref to StrategyVersion
    idempotency_key = models.CharField(max_length=190, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "intel_suggestion"
        indexes = [models.Index(fields=["instance_id", "status"], name="intel_sugg_instance_status_idx")]


class ReviewFeedback(models.Model):
    review = models.ForeignKey(TradeReview, on_delete=models.PROTECT, related_name="feedback")
    rating = models.SmallIntegerField()  # -1 wrong, 0 neutral, 1 useful
    comment = models.CharField(max_length=500, blank=True)
    created_by = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "intel_review_feedback"


class IdempotentRequest(models.Model):
    """Replay record for state-changing intelligence endpoints (R4, R7): same key + same request replays
    the stored response; same key + different request is a 409."""

    key = models.CharField(max_length=190, unique=True)
    request_hash = models.CharField(max_length=64)
    status_code = models.PositiveSmallIntegerField(default=0)
    response = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "intel_idempotent_request"


# ---------------------------------------------------------------------------------------------------
# Phase 4: parameter capture, live monitor, alerts
# ---------------------------------------------------------------------------------------------------
class RunConfigSnapshot(models.Model):
    """What a strategy version was run with, beyond StrategyVersion.configuration_snapshot (which already holds the
    parameters): data-source flags, regime at activation, advisory shown, operator intent note."""

    strategy_key = models.CharField(max_length=64)
    instance_id = models.BigIntegerField(db_index=True)
    instance_name = models.CharField(max_length=128)
    strategy_version = models.PositiveIntegerField()
    strategy_version_ref = _soft_fk("strategies.StrategyVersion")
    parameter_hash = models.CharField(max_length=64, blank=True)
    execution_mode = models.CharField(max_length=16, blank=True)
    data_source_flags = models.JSONField(default=dict)
    regime_at_activation = models.JSONField(default=dict)
    advisory_shown = models.JSONField(null=True, blank=True)  # filled in Phase 6
    intent_note = models.CharField(max_length=500, blank=True)  # untrusted operator text (R8)
    # Phase 8: what the operator chose for this version (ticker, strategy, timeframe, risk profile and what it sets, ...)
    selection = models.JSONField(default=dict, blank=True)
    activation_log = models.JSONField(default=list)
    idempotency_key = models.CharField(max_length=190, unique=True)
    captured_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "intel_run_config_snapshot"
        indexes = [models.Index(fields=["instance_id", "strategy_version"], name="intel_runcfg_instance_idx")]


class EpisodeLiveSnapshot(models.Model):
    episode = models.ForeignKey(TradeEpisode, on_delete=models.PROTECT, related_name="live_snapshots")
    observed_at = models.DateTimeField()
    quote_freshness = models.CharField(max_length=12)  # FRESH | STALE | UNAVAILABLE
    marks = models.JSONField(default=dict)
    mark_pnl = models.DecimalField(max_digits=24, decimal_places=8, null=True, blank=True)
    pnl_bps = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    pct_of_max_loss = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    pct_of_max_profit = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    mae_bps_so_far = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    mfe_bps_so_far = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    distances = models.JSONField(default=dict)
    flags = models.JSONField(default=list)
    material = models.BooleanField(default=False)

    class Meta:
        db_table = "intel_episode_live_snapshot"
        indexes = [models.Index(fields=["episode", "-observed_at"], name="intel_live_episode_time_idx")]


class LiveAlert(models.Model):
    """Templated text only: no model writes an alert."""

    episode = models.ForeignKey(TradeEpisode, on_delete=models.PROTECT, related_name="alerts")
    rule_id = models.CharField(max_length=48)
    severity = models.CharField(max_length=8)  # LOW | MEDIUM | HIGH
    text = models.CharField(max_length=400)
    dedupe_key = models.CharField(max_length=120)
    details = models.JSONField(default=dict)
    occurrences = models.PositiveIntegerField(default=1)
    first_seen = models.DateTimeField()
    last_seen = models.DateTimeField()
    acknowledged_by = models.CharField(max_length=150, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "intel_live_alert"
        constraints = [models.UniqueConstraint(fields=["episode", "dedupe_key"], name="intel_alert_episode_dedupe_uniq")]
        indexes = [models.Index(fields=["severity", "last_seen"], name="intel_alert_severity_idx")]


# ---------------------------------------------------------------------------------------------------
# Phase 5: learning loop. "Learning" = validated lessons and statistics stored here and fed into prompts.
# There is no model training anywhere (R14).
# ---------------------------------------------------------------------------------------------------
class Lesson(models.Model):
    class Status(models.TextChoices):
        HYPOTHESIS = "HYPOTHESIS"
        SUPPORTED = "SUPPORTED"
        ESTABLISHED = "ESTABLISHED"
        REFUTED = "REFUTED"
        RETIRED = "RETIRED"

    scope = models.CharField(max_length=16, default="STRATEGY")
    strategy_key = models.CharField(max_length=64)
    statement = models.CharField(max_length=400)
    predicate = models.JSONField(default=dict)  # DSL, normalised
    predicate_hash = models.CharField(max_length=64)
    expected_effect = models.JSONField(default=dict)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.HYPOTHESIS)
    support_n = models.PositiveIntegerField(default=0)       # episodes that supported it (origin excluded)
    contradict_n = models.PositiveIntegerField(default=0)
    support_weight = models.FloatField(default=0.0)          # PAPER / delayed-data episodes count less
    contradict_weight = models.FloatField(default=0.0)
    beta_alpha = models.FloatField(default=1.0)
    beta_beta = models.FloatField(default=1.0)
    n_forward = models.PositiveIntegerField(default=0)
    forward_alpha = models.FloatField(default=1.0)
    forward_beta = models.FloatField(default=1.0)
    p_value = models.FloatField(null=True, blank=True)       # one-sided permutation p-value in the expected direction
    q_value = models.FloatField(null=True, blank=True)       # Benjamini-Hochberg adjusted p across live lessons (Phase 11)
    effect_size = models.FloatField(null=True, blank=True)   # mean outcome (matching) minus mean outcome (others)
    effect_ci_low = models.FloatField(null=True, blank=True)
    effect_ci_high = models.FloatField(null=True, blank=True)
    supported_at = models.DateTimeField(null=True, blank=True)
    established_at = models.DateTimeField(null=True, blank=True)
    last_matched_at = models.DateTimeField(null=True, blank=True)
    last_evaluated_at = models.DateTimeField(null=True, blank=True)
    created_from_review = models.ForeignKey(TradeReview, on_delete=models.PROTECT, null=True, blank=True, related_name="lessons")
    origin_episode_id = models.BigIntegerField(null=True, blank=True)  # soft
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "intel_lesson"
        constraints = [models.UniqueConstraint(fields=["strategy_key", "predicate_hash"], name="intel_lesson_predicate_uniq")]
        indexes = [models.Index(fields=["strategy_key", "status"], name="intel_lesson_status_idx")]


class LessonEvidence(models.Model):
    class Verdict(models.TextChoices):
        SUPPORTS = "SUPPORTS"
        CONTRADICTS = "CONTRADICTS"

    lesson = models.ForeignKey(Lesson, on_delete=models.PROTECT, related_name="evidence")
    episode = models.ForeignKey(TradeEpisode, on_delete=models.PROTECT, related_name="lesson_evidence")
    verdict = models.CharField(max_length=12, choices=Verdict.choices)
    outcome_value = models.FloatField(null=True, blank=True)
    weight = models.FloatField(default=1.0)
    forward_only = models.BooleanField(default=False)
    is_origin = models.BooleanField(default=False)  # the trade that proposed the lesson never counts for it
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "intel_lesson_evidence"
        constraints = [models.UniqueConstraint(fields=["lesson", "episode"], name="intel_lessonev_uniq")]


class PerformanceStat(models.Model):
    """Per-currency statistics (R5): money is never summed across currencies; every row carries n and a CI."""

    strategy_key = models.CharField(max_length=64, blank=True)
    strategy_version = models.PositiveIntegerField(default=0)   # 0 = all versions
    instrument = models.CharField(max_length=64, blank=True)
    regime = models.CharField(max_length=24, blank=True)
    structure = models.CharField(max_length=48, blank=True)
    mode = models.CharField(max_length=16, blank=True)
    risk_profile = models.CharField(max_length=24, blank=True)  # Phase 11: CONSERVATIVE / NEUTRAL / AGGRESSIVE, "" = all
    context = models.CharField(max_length=24, blank=True)       # Phase 11: EVENT_IN_HOLD / NO_EVENT / NO_CONTEXT, "" = all
    currency = models.CharField(max_length=8)
    n = models.PositiveIntegerField(default=0)
    wins = models.PositiveIntegerField(default=0)
    shrunk_mean_bps = models.FloatField(null=True, blank=True)  # empirical-Bayes partial pooling across slices of the same kind
    shrunk_sd_bps = models.FloatField(null=True, blank=True)
    shrinkage = models.FloatField(null=True, blank=True)        # weight kept on this slice's own mean (1 = no pooling)
    mean_return_bps = models.FloatField(null=True, blank=True)
    ci_low = models.FloatField(null=True, blank=True)
    ci_high = models.FloatField(null=True, blank=True)
    sum_net_pnl = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    as_of = models.DateTimeField()

    class Meta:
        db_table = "intel_performance_stat"
        constraints = [models.UniqueConstraint(
            fields=["strategy_key", "strategy_version", "instrument", "regime", "structure", "mode", "risk_profile", "context", "currency"],
            name="intel_perf_dims_uniq")]


class CalibrationStat(models.Model):
    metric = models.CharField(max_length=24)           # POP
    strategy_key = models.CharField(max_length=64, blank=True)
    bin = models.CharField(max_length=16)              # "0.6-0.7" or "ALL"
    n = models.PositiveIntegerField(default=0)
    predicted_mean = models.FloatField(null=True, blank=True)
    observed_rate = models.FloatField(null=True, blank=True)
    brier = models.FloatField(null=True, blank=True)   # only on the ALL row
    as_of = models.DateTimeField()

    class Meta:
        db_table = "intel_calibration_stat"
        constraints = [models.UniqueConstraint(fields=["metric", "strategy_key", "bin"], name="intel_calib_uniq")]


class LearningEvent(models.Model):
    class Type(models.TextChoices):
        LESSON_CREATED = "LESSON_CREATED"
        LESSON_REINFORCED = "LESSON_REINFORCED"
        LESSON_CONTRADICTED = "LESSON_CONTRADICTED"
        LESSON_PROMOTED = "LESSON_PROMOTED"
        LESSON_RETIRED = "LESSON_RETIRED"
        STAT_SHIFT = "STAT_SHIFT"
        CALIBRATION_SHIFT = "CALIBRATION_SHIFT"
        SUGGESTION_RESOLVED = "SUGGESTION_RESOLVED"
        CHANGE_POINT = "CHANGE_POINT"

    type = models.CharField(max_length=24, choices=Type.choices)
    episode = models.ForeignKey(TradeEpisode, on_delete=models.PROTECT, null=True, blank=True, related_name="learning_events")
    lesson = models.ForeignKey(Lesson, on_delete=models.PROTECT, null=True, blank=True, related_name="events")
    payload = models.JSONField(default=dict)
    dedupe_key = models.CharField(max_length=190, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)  # the feed cursor is the primary key

    class Meta:
        db_table = "intel_learning_event"
        indexes = [models.Index(fields=["type", "created_at"], name="intel_event_type_idx")]


class SuggestionOutcome(models.Model):
    class Result(models.TextChoices):
        HELPED = "HELPED"
        HURT = "HURT"
        INCONCLUSIVE = "INCONCLUSIVE"

    suggestion = models.OneToOneField(Suggestion, on_delete=models.PROTECT, related_name="outcome")
    result = models.CharField(max_length=14, choices=Result.choices)
    n_before = models.PositiveIntegerField(default=0)
    n_after = models.PositiveIntegerField(default=0)
    delta_bps = models.FloatField(null=True, blank=True)
    ci_low = models.FloatField(null=True, blank=True)
    ci_high = models.FloatField(null=True, blank=True)
    evaluated_at = models.DateTimeField()

    class Meta:
        db_table = "intel_suggestion_outcome"


# ---------------------------------------------------------------------------------------------------
# Phase 6: pre-trade advisory (never blocks anything)
# ---------------------------------------------------------------------------------------------------
class PreTradeAdvisory(models.Model):
    class Trigger(models.TextChoices):
        CREATE = "CREATE"
        CHANGE = "CHANGE"
        ACTIVATION = "ACTIVATION"
        PREVIEW = "PREVIEW"

    class Severity(models.TextChoices):
        INFO = "INFO"
        WARNING = "WARNING"
        HIGH = "HIGH"

    strategy_key = models.CharField(max_length=64)
    instance_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    instance_name = models.CharField(max_length=128, blank=True)
    instrument_symbol = models.CharField(max_length=64, blank=True)
    currency = models.CharField(max_length=8, blank=True)
    execution_mode = models.CharField(max_length=16, blank=True)
    trigger = models.CharField(max_length=12, choices=Trigger.choices)
    parameters = models.JSONField(default=dict)
    lessons = models.JSONField(default=list)
    stats = models.JSONField(default=list)
    open_suggestion_ids = models.JSONField(default=list)
    calibration_warnings = models.JSONField(default=list)
    compared_snapshot_ids = models.JSONField(default=list)
    comparisons = models.JSONField(default=list)
    severity = models.CharField(max_length=8, choices=Severity.choices, default=Severity.INFO)
    summary = models.TextField(blank=True)
    requires_ack = models.BooleanField(default=False)   # informational: an advisory never blocks activation
    acknowledged_by = models.CharField(max_length=150, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=190, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "intel_pre_trade_advisory"
        indexes = [models.Index(fields=["instance_id", "-created_at"], name="intel_adv_instance_idx")]


# ---------------------------------------------------------------------------------------------------
# Phase 8: operator journal and frozen report artifacts.
# ---------------------------------------------------------------------------------------------------
class OperatorEvent(models.Model):
    """Append-only journal of what the operator chose and did (R17). Engine references are soft (R12). `actor` is
    "UNKNOWN" when the engine endpoint carried no identity: the engine has no app-layer auth, and we never invent one."""

    class Type(models.TextChoices):
        INSTANCE_CREATED = "INSTANCE_CREATED"
        INSTANCE_UPDATED = "INSTANCE_UPDATED"
        INSTANCE_ACTION = "INSTANCE_ACTION"  # enable / disable / flatten / ... (StrategyAction)
        REQUEST = "REQUEST"  # what the create / change request contained
        NOTE = "NOTE"
        MANUAL_ORDER = "MANUAL_ORDER"
        SUGGESTION_DECISION = "SUGGESTION_DECISION"
        ADVISORY_ACK = "ADVISORY_ACK"
        REVIEW_FEEDBACK = "REVIEW_FEEDBACK"

    occurred_at = models.DateTimeField(db_index=True)
    event_type = models.CharField(max_length=24, choices=Type.choices)
    actor = models.CharField(max_length=150, default="UNKNOWN")
    instance_id = models.BigIntegerField(null=True, blank=True)  # soft reference
    strategy_key = models.CharField(max_length=64, blank=True)
    instance_name = models.CharField(max_length=128, blank=True)
    instrument_symbol = models.CharField(max_length=32, blank=True)
    execution_mode = models.CharField(max_length=16, blank=True)
    payload = models.JSONField(default=dict)  # sanitised (R8)
    idempotency_key = models.CharField(max_length=190, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "intel_operator_event"
        indexes = [
            models.Index(fields=["instance_id", "occurred_at"], name="intel_opevent_instance_idx"),
            models.Index(fields=["event_type", "occurred_at"], name="intel_opevent_type_idx"),
        ]

    def save(self, *args, **kwargs):
        if self.pk and OperatorEvent.objects.filter(pk=self.pk).exists():
            from django.core.exceptions import ValidationError

            raise ValidationError("OperatorEvent records are append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        from django.core.exceptions import ValidationError

        raise ValidationError("OperatorEvent records are append-only")


class ReportArtifact(models.Model):
    """A report exactly as it was generated (R21): the structured content, its hash and the versions that produced it.
    PDF / HTML / Markdown are rendered from `content`; a changed report is a NEW row, an old one is never edited."""

    class Kind(models.TextChoices):
        TRADE = "TRADE"
        DAILY = "DAILY"

    kind = models.CharField(max_length=8, choices=Kind.choices)
    scope_key = models.CharField(max_length=120)  # "episode:<id>" or "daily:<zone>:<date>"
    report_version = models.CharField(max_length=16)
    content = models.JSONField()
    content_hash = models.CharField(max_length=64)
    provenance = models.JSONField(default=dict)  # metrics_version, prompt_version, model, builder versions
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "intel_report_artifact"
        constraints = [models.UniqueConstraint(fields=["kind", "scope_key", "content_hash"], name="intel_report_unique_content")]
        indexes = [models.Index(fields=["kind", "scope_key", "-generated_at"], name="intel_report_scope_idx")]

    def save(self, *args, **kwargs):
        if self.pk and ReportArtifact.objects.filter(pk=self.pk).exists():
            from django.core.exceptions import ValidationError

            raise ValidationError("ReportArtifact records are immutable")
        return super().save(*args, **kwargs)


# ---------------------------------------------------------------------------------------------------
# Phase 9: one record per market session (zone + local date), built only from stored rows.
# ---------------------------------------------------------------------------------------------------
class SessionRecord(models.Model):
    """What happened in one market session, per exchange zone (never mixed across currencies, R5). Updated while OPEN,
    frozen at finalisation: a FINAL row is immutable. `content` is everything the report is rendered from."""

    class Status(models.TextChoices):
        OPEN = "OPEN"
        FINAL = "FINAL"

    zone = models.CharField(max_length=8)  # XNSE | XNYS
    session_date = models.DateField()  # the exchange-local calendar date
    currency = models.CharField(max_length=8)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OPEN)
    record_version = models.CharField(max_length=8)
    content = models.JSONField(default=dict)
    content_hash = models.CharField(max_length=64, blank=True)
    narrative = models.JSONField(default=dict, blank=True)  # model text + how it was produced, or why there is none
    built_at = models.DateTimeField()
    finalized_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "intel_session_record"
        constraints = [models.UniqueConstraint(fields=["zone", "session_date"], name="intel_session_zone_date_uniq")]
        indexes = [models.Index(fields=["-session_date", "zone"], name="intel_session_date_idx")]

    def save(self, *args, **kwargs):
        if self.pk and SessionRecord.objects.filter(pk=self.pk, status=self.Status.FINAL).exists():
            from django.core.exceptions import ValidationError

            raise ValidationError("A FINAL SessionRecord is immutable")
        return super().save(*args, **kwargs)



# ---------------------------------------------------------------------------------------------------
# Phase 10: outside context (calendars, news, announcements), stored point in time (R18).
# ---------------------------------------------------------------------------------------------------
class ContextSource(models.Model):
    """Health and budget of one context source (finnhub, marketaux, rbi, fed). One row each."""

    name = models.CharField(max_length=32, unique=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True)
    consecutive_failures = models.PositiveIntegerField(default=0)
    calls_day = models.DateField(null=True, blank=True)
    calls_today = models.PositiveIntegerField(default=0)
    remaining_quota = models.IntegerField(null=True, blank=True)  # as reported by the provider, when it reports one
    items_total = models.PositiveIntegerField(default=0)
    jobs = models.JSONField(default=dict)  # job name -> {"last_run": iso, "last_ok": iso, "items": n}
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "intel_context_source"


class ContextItem(models.Model):
    """One headline, scheduled event, earnings date, holiday or official announcement. `published_at` is when it
    became public (news, announcements); `event_time` is when a scheduled event happens. `first_seen_at` is when WE
    first stored it. Text is untrusted: sanitised on the way in (R8, R17)."""

    class Kind(models.TextChoices):
        NEWS = "NEWS"
        EVENT = "EVENT"  # scheduled macro calendar entry
        EARNINGS = "EARNINGS"
        HOLIDAY = "HOLIDAY"
        ANNOUNCEMENT = "ANNOUNCEMENT"  # official release (central bank press release)

    source = models.CharField(max_length=32)
    external_id = models.CharField(max_length=190)
    kind = models.CharField(max_length=14, choices=Kind.choices)
    published_at = models.DateTimeField(null=True, blank=True)
    event_time = models.DateTimeField(null=True, blank=True)
    first_seen_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)
    title = models.CharField(max_length=300)
    summary = models.CharField(max_length=500, blank=True)
    url = models.CharField(max_length=500, blank=True)
    symbols = models.JSONField(default=list)
    symbol_index = models.CharField(max_length=240, blank=True)  # "|AAPL|MSFT|": portable symbol lookup (JSON contains is not on SQLite)
    region = models.CharField(max_length=8, blank=True)  # IN, US, EU ... or GLOBAL
    event_type = models.CharField(max_length=20, blank=True)  # CENTRAL_BANK, MACRO_RELEASE, EARNINGS, HOLIDAY, GENERAL
    impact = models.CharField(max_length=8, blank=True)  # low | medium | high (calendar items)
    story_key = models.CharField(max_length=40, blank=True, db_index=True)  # near-duplicate headlines share one key
    attrs = models.JSONField(default=dict)  # estimate / previous / sentiment / publisher ...
    raw_hash = models.CharField(max_length=64)

    class Meta:
        db_table = "intel_context_item"
        constraints = [models.UniqueConstraint(fields=["source", "external_id"], name="intel_ctx_source_external_uniq")]
        indexes = [
            models.Index(fields=["kind", "published_at"], name="intel_ctx_kind_pub_idx"),
            models.Index(fields=["kind", "event_time"], name="intel_ctx_kind_event_idx"),
            models.Index(fields=["region", "published_at"], name="intel_ctx_region_pub_idx"),
        ]


class EpisodeContext(models.Model):
    """Frozen link between a closed trade and the context that surrounded it (R18)."""

    class Relation(models.TextChoices):
        KNOWN_AT_ENTRY = "KNOWN_AT_ENTRY"
        SCHEDULED_AFTER_ENTRY = "SCHEDULED_AFTER_ENTRY"
        DURING_HOLD = "DURING_HOLD"

    episode = models.ForeignKey(TradeEpisode, on_delete=models.PROTECT, related_name="context_links")
    item = models.ForeignKey(ContextItem, on_delete=models.PROTECT, related_name="episode_links")
    relation = models.CharField(max_length=24, choices=Relation.choices)
    linked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "intel_episode_context"
        constraints = [models.UniqueConstraint(fields=["episode", "item", "relation"], name="intel_epctx_unique")]


class DailyBrief(models.Model):
    """The pre-open brief for one exchange and date: deterministic facts from stored context. Rebuilt until the exchange
    opens, then frozen."""

    zone = models.CharField(max_length=8)
    brief_date = models.DateField()
    content = models.JSONField(default=dict)
    content_hash = models.CharField(max_length=64)
    final = models.BooleanField(default=False)
    built_at = models.DateTimeField()

    class Meta:
        db_table = "intel_daily_brief"
        constraints = [models.UniqueConstraint(fields=["zone", "brief_date"], name="intel_brief_zone_date_uniq")]

    def save(self, *args, **kwargs):
        if self.pk and DailyBrief.objects.filter(pk=self.pk, final=True).exists():
            from django.core.exceptions import ValidationError

            raise ValidationError("A final DailyBrief is immutable")
        return super().save(*args, **kwargs)


class ChangePointState(models.Model):
    """Edge-decay monitor (Phase 11): a CUSUM over a strategy's closed trades in time order. One row per
    (strategy, currency, metric), recomputed from the trades each day; a row only says what the data show, with n."""

    class Status(models.TextChoices):
        INSUFFICIENT = "INSUFFICIENT"
        STABLE = "STABLE"
        EDGE_DECAY = "EDGE_DECAY"
        EDGE_IMPROVEMENT = "EDGE_IMPROVEMENT"

    strategy_key = models.CharField(max_length=64)
    currency = models.CharField(max_length=8, blank=True)
    metric = models.CharField(max_length=24)  # RETURN | POP_CALIBRATION
    n = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=18, choices=Status.choices, default=Status.INSUFFICIENT)
    statistic = models.FloatField(null=True, blank=True)    # the CUSUM at its peak, in standard deviations
    threshold = models.FloatField(null=True, blank=True)
    reference_n = models.PositiveIntegerField(default=0)    # trades used as the baseline period
    baseline_mean = models.FloatField(null=True, blank=True)
    baseline_sd = models.FloatField(null=True, blank=True)
    change_index = models.PositiveIntegerField(null=True, blank=True)  # estimated first trade of the changed regime (1-based)
    change_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=300, blank=True)
    as_of = models.DateTimeField()

    class Meta:
        db_table = "intel_change_point"
        constraints = [models.UniqueConstraint(fields=["strategy_key", "currency", "metric"], name="intel_changepoint_uniq")]

