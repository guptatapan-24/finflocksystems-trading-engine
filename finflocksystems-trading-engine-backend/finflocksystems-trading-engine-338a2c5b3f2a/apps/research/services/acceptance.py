from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.core.idempotency import canonical_request_hash
from apps.instruments.models import BrokerContract
from apps.portfolio_construction.models import (
    GoalInstrumentSelection,
    GoalStrategyAssignment,
    PortfolioConstructionPlan,
    PortfolioGoalAllocation,
)
from apps.portfolio_construction.rules import resolved_goal_rules
from apps.portfolio_construction.services import bump_plan_version, validate_assignment

from ..models import (
    GoalRecommendationAcceptance,
    GoalRecommendationRun,
    InstrumentEligibilitySnapshot,
    ResearchStrategyImplementation,
)
from .classification import hierarchy


D = Decimal


def effective_strategy_family_cap(policy, fallback_tier):
    """Return the family cap used by generation for the recommendation tier."""
    try:
        tier = int(fallback_tier or 1)
    except (TypeError, ValueError):
        tier = 1
    return D(1) if tier >= 3 else D(policy.strategy_family_cap)


def effective_strategy_family_cap_for_run(run):
    """Prefer the run's audited constraint, with support for older fallback runs."""
    constraints = (run.optimizer_snapshot or {}).get("constraints") or {}
    stored_cap = constraints.get("strategy_family_cap")
    if stored_cap is not None:
        return D(str(stored_cap))
    return effective_strategy_family_cap(
        run.policy,
        (run.metrics or {}).get("fallback_tier", 1),
    )


def validate_recommendation_for_construction(run, *, check_expiry=True):
    if run.status != "COMPLETED":
        raise ValueError("Only a completed recommendation can be used for construction")
    if check_expiry and run.expires_at <= timezone.now():
        raise ValueError("Accepted recommendation has expired; regenerate before preview or apply")
    sleeves = list(run.sleeves.select_related(
        "instrument__issuer", "research_strategy", "execution_strategy_definition", "universe_member"
    ).order_by("instrument_id", "rank"))
    if not sleeves and run.goal_allocation.timeframe_bucket != "NOW":
        raise ValueError("A non-NOW recommendation must contain at least one actionable sleeve")
    rules = resolved_goal_rules(run.goal_allocation.timeframe_bucket, run.goal_allocation.risk_level)
    stock_weights = {}
    shares = defaultdict(Decimal)
    group_weights = {"sector": defaultdict(Decimal), "industry": defaultdict(Decimal), "sub_industry": defaultdict(Decimal), "family": defaultdict(Decimal)}
    for sleeve in sleeves:
        if not sleeve.instrument.active or not sleeve.instrument.tradable:
            raise ValueError(f"Instrument {sleeve.instrument.symbol} is no longer active and tradable")
        if not BrokerContract.objects.filter(instrument=sleeve.instrument, qualified_at__isnull=False).exists():
            raise ValueError(f"Instrument {sleeve.instrument.symbol} is not exactly broker-qualified")
        if not InstrumentEligibilitySnapshot.objects.filter(
            universe_member=sleeve.universe_member,
            as_of_date__range=(
                timezone.localdate() - timedelta(days=run.policy.maximum_candidate_age_days),
                timezone.localdate(),
            ),
            builder_eligible=True,
        ).order_by("-as_of_date").exists():
            raise ValueError(f"Instrument {sleeve.instrument.symbol} is no longer builder-eligible")
        approved = ResearchStrategyImplementation.objects.filter(
            research_strategy=sleeve.research_strategy,
            executable_strategy_definition=sleeve.execution_strategy_definition,
            status__in=["VALIDATED", "BACKTESTED", "SCORED", "APPROVED_FOR_RECOMMENDATION",
                        "PAPER_VALIDATED", "BUILDER_READY", "APPROVED"],
            exact_semantic_match=True,
        ).exists()
        if not approved:
            raise ValueError(f"Strategy {sleeve.research_strategy.research_id} is no longer approved")
        if sleeve.instrument_id in stock_weights and stock_weights[sleeve.instrument_id] != D(sleeve.stock_weight):
            raise ValueError("Recommendation contains inconsistent stock weights")
        stock_weights[sleeve.instrument_id] = D(sleeve.stock_weight)
        shares[sleeve.instrument_id] += D(sleeve.strategy_share)
        classification = sleeve.instrument.classifications.filter(
            effective_from__lte=run.as_of_date,
            taxonomy_version=run.dataset_version,
        ).select_related("sub_industry_node__parent__parent__parent").order_by("-effective_from").first()
        gics = hierarchy(classification)
        if not gics:
            raise ValueError(f"Instrument {sleeve.instrument.symbol} has no point-in-time GICS classification")
        group_weights["family"][sleeve.research_strategy.family] += D(sleeve.sleeve_weight)
    for instrument_id, share in shares.items():
        if share != D(1):
            raise ValueError(f"Strategy shares for instrument {instrument_id} do not total exactly 100%")
    for sleeve in sleeves:
        if sleeve.rank != min(item.rank for item in sleeves if item.instrument_id == sleeve.instrument_id):
            continue
        classification = sleeve.instrument.classifications.filter(
            effective_from__lte=run.as_of_date,
            taxonomy_version=run.dataset_version,
        ).select_related("sub_industry_node__parent__parent__parent").order_by("-effective_from").first()
        gics = hierarchy(classification)
        weight = D(sleeve.stock_weight)
        group_weights["sector"][gics["sector"]["code"]] += weight
        group_weights["industry"][gics["industry"]["code"]] += weight
        group_weights["sub_industry"][gics["sub_industry"]["code"]] += weight
        if weight > min(D(run.policy.per_stock_cap), D(rules["maximum_stock_weight"])):
            raise ValueError(f"Stock weight exceeds current live cap for {sleeve.instrument.symbol}")
    caps = {
        "sector": D(run.policy.sector_cap),
        "industry": D(run.policy.industry_cap),
        "sub_industry": D(run.policy.sub_industry_cap),
        "family": effective_strategy_family_cap_for_run(run),
    }
    for group, values in group_weights.items():
        if any(value > caps[group] for value in values.values()):
            raise ValueError(f"Recommendation violates its {group} cap")
    stock_total = sum(stock_weights.values(), D(0))
    cash = D(1) - stock_total
    minimum_cash = max(D(run.policy.minimum_cash), D(rules["minimum_cash_weight"]))
    if cash < minimum_cash or stock_total + cash != D(1):
        raise ValueError("Recommendation violates the current live cash floor or total-weight invariant")
    return {
        "cash_weight": cash,
        "stock_weights": stock_weights,
        "sleeves": sleeves,
        "group_weights": {key: {name: str(value) for name, value in rows.items()} for key, rows in group_weights.items()},
    }


@transaction.atomic
def accept_recommendation(run_or_id, *, actor="operator"):
    run_id = run_or_id.pk if isinstance(run_or_id, GoalRecommendationRun) else run_or_id
    run = GoalRecommendationRun.objects.select_for_update().select_related(
        "goal_allocation__plan", "policy"
    ).get(pk=run_id)
    existing = GoalRecommendationAcceptance.objects.filter(recommendation_run=run).first()
    if existing:
        return existing, False
    goal = PortfolioGoalAllocation.objects.select_for_update().get(pk=run.goal_allocation_id)
    plan = PortfolioConstructionPlan.objects.select_for_update().get(pk=goal.plan_id)
    if plan.version != run.requested_plan_version:
        raise ValueError("Portfolio Builder plan changed after recommendation generation")
    validated = validate_recommendation_for_construction(run)
    sleeves_by_instrument = defaultdict(list)
    for sleeve in validated["sleeves"]:
        sleeves_by_instrument[sleeve.instrument_id].append(sleeve)
    selection_ids = []
    assignment_ids = []
    for instrument_id, sleeves in sleeves_by_instrument.items():
        selection, _ = GoalInstrumentSelection.objects.update_or_create(
            goal_allocation=goal,
            instrument_id=instrument_id,
            defaults={"enabled": True, "minimum_weight": None, "maximum_weight": None, "display_order": min(item.rank for item in sleeves)},
        )
        selection_ids.append(selection.pk)
        keep = []
        for sleeve in sleeves:
            parameters = validate_assignment(
                goal_instrument_selection=selection,
                definition=sleeve.execution_strategy_definition,
                execution_timeframe=sleeve.execution_timeframe,
                parameter_overrides=sleeve.parameters,
                strategy_share=sleeve.strategy_share,
                system_generated=True,
            )
            parameter_hash = canonical_request_hash("parameters", parameters)
            assignment, _ = GoalStrategyAssignment.objects.update_or_create(
                goal_instrument_selection=selection,
                strategy_definition=sleeve.execution_strategy_definition,
                execution_timeframe=sleeve.execution_timeframe,
                parameter_hash=parameter_hash,
                defaults={
                    "parameter_overrides": parameters,
                    "strategy_share": sleeve.strategy_share,
                    "create_instance": True,
                    "enabled": True,
                },
            )
            keep.append(assignment.pk)
            assignment_ids.append(assignment.pk)
        selection.assignments.exclude(pk__in=keep).update(enabled=False)
    goal.instrument_selections.exclude(pk__in=selection_ids).update(enabled=False)
    goal.construction_source = "ACCEPTED_RECOMMENDATION"
    goal.accepted_recommendation_run = run
    goal.save(update_fields=["construction_source", "accepted_recommendation_run", "updated_at"])
    accepted_version = plan.version
    bump_plan_version(plan)
    run.accepted_at = timezone.now()
    run.save(update_fields=["accepted_at"])
    acceptance = GoalRecommendationAcceptance.objects.create(
        recommendation_run=run,
        goal=goal,
        accepted_plan_version=accepted_version,
        created_updated_instrument_selections=selection_ids,
        created_updated_strategy_assignments=assignment_ids,
        accepted_by=actor,
        change_summary={
            "construction_source": "ACCEPTED_RECOMMENDATION",
            "stock_weights": {str(key): str(value) for key, value in validated["stock_weights"].items()},
            "cash_weight": str(validated["cash_weight"]),
            "new_plan_version": plan.version,
            "created_strategy_instances": 0,
            "created_rebalances": 0,
        },
    )
    AuditEvent.objects.create(
        event_type="research.recommendation.accepted",
        actor=actor,
        aggregate_type="portfolio_goal",
        aggregate_id=str(goal.pk),
        data={"recommendation_run_id": run.pk, **acceptance.change_summary},
        idempotency_key=f"recommendation-acceptance:{run.pk}",
    )
    return acceptance, True


@transaction.atomic
def detach_recommendation(goal_or_id, *, actor="operator"):
    goal_id = goal_or_id.pk if isinstance(goal_or_id, PortfolioGoalAllocation) else goal_or_id
    goal = PortfolioGoalAllocation.objects.select_for_update().select_related("plan").get(pk=goal_id)
    plan = PortfolioConstructionPlan.objects.select_for_update().get(pk=goal.plan_id)
    previous = goal.accepted_recommendation_run_id
    goal.construction_source = "MANUAL_OPTIMIZER"
    goal.accepted_recommendation_run = None
    goal.save(update_fields=["construction_source", "accepted_recommendation_run", "updated_at"])
    bump_plan_version(plan)
    AuditEvent.objects.create(
        event_type="research.recommendation.detached",
        actor=actor,
        aggregate_type="portfolio_goal",
        aggregate_id=str(goal.pk),
        data={"recommendation_run_id": previous, "plan_version": plan.version},
        idempotency_key=f"recommendation-detach:{goal.pk}:{plan.version}",
    )
    return goal


def require_manual_edit_allowed(goal):
    if goal.construction_source == "ACCEPTED_RECOMMENDATION":
        raise ValueError("Detach the accepted recommendation before making manual edits")
