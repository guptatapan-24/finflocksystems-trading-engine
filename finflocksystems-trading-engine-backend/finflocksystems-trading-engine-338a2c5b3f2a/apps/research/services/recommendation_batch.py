from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.core.idempotency import canonical_request_hash, require_matching_request
from apps.portfolio_construction.models import GoalInstrumentSelection, GoalStrategyAssignment, PortfolioConstructionPlan
from apps.portfolio_construction.services import bump_plan_version, validate_assignment
from apps.portfolios.models import PortfolioPosition

from ..models import (
    BacktestProtocolVersion,
    GoalRecommendationAcceptance,
    GoalRecommendationRun,
    GoalRecommendationSleeve,
    RecommendationBatchGoalResult,
    RecommendationBatchRun,
    ResearchDatasetVersion,
)
from .eligibility import calculate_member_eligibility
from .acceptance import effective_strategy_family_cap
from .recommendation_cache import best_cached_recommendation
from .recommendations import _policy_for_goal
from .optimizer import optimize_sleeves
from .universe_pipeline import active_recommendation_universe, qualify_and_substitute_finalists


D = Decimal


def _goal_snapshot(goal):
    return {
        "goal_id": goal.pk, "name": goal.name, "allocation_weight": str(goal.allocation_weight),
        "timeframe": goal.timeframe_bucket, "risk_level": goal.risk_level, "enabled": goal.enabled,
    }


@transaction.atomic
def create_recommendation_batch(plan_or_id, idempotency_key):
    if not settings.RECOMMENDATION_SYSTEM_ENABLED:
        raise ValueError("The recommendation system is disabled")
    if not idempotency_key:
        raise ValueError("Idempotency key is required")
    plan_id = plan_or_id.pk if isinstance(plan_or_id, PortfolioConstructionPlan) else plan_or_id
    plan = PortfolioConstructionPlan.objects.select_for_update().get(pk=plan_id)
    goals = list(plan.goals.filter(enabled=True).order_by("display_order", "pk"))
    if not goals:
        raise ValueError("At least one enabled goal is required")
    dataset = ResearchDatasetVersion.objects.get(status="ACTIVE")
    protocol = BacktestProtocolVersion.objects.get(dataset_version=dataset, active=True)
    snapshot = [_goal_snapshot(goal) for goal in goals]
    input_hash = canonical_request_hash("plan_recommendations", {
        "plan_id": plan.pk, "plan_version": plan.version, "goals": snapshot,
        "dataset_id": dataset.pk, "protocol_id": protocol.pk,
    })
    existing = RecommendationBatchRun.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        require_matching_request(existing.input_hash, input_hash)
        return existing, False
    batch = RecommendationBatchRun.objects.create(
        plan=plan, requested_plan_version=plan.version, status="QUEUED", idempotency_key=idempotency_key,
        input_hash=input_hash, dataset=dataset, protocol=protocol, input_snapshot=snapshot,
    )
    for goal in goals:
        RecommendationBatchGoalResult.objects.create(batch=batch, goal=goal, status="QUEUED")
    return batch, True


def _ranked_members(cache):
    universe = active_recommendation_universe()
    member_ids = []
    for row in [*(cache.selected_stocks or []), *(cache.candidate_pool or [])]:
        member_id = row.get("universe_member_id")
        if member_id and member_id not in member_ids:
            member_ids.append(member_id)
    members = {item.pk: item for item in universe.members.filter(pk__in=member_ids).select_related("instrument", "issuer")}
    return [members[pk] for pk in member_ids if pk in members]


def _row_lookup(cache):
    return {row["universe_member_id"]: row for row in [*(cache.selected_stocks or []), *(cache.candidate_pool or [])] if row.get("universe_member_id")}


def _goal_weights(rows, goal, policy, rules, *, strategy_family_cap, recommended_cash=None):
    if not rows:
        return [], 1.0, {"expected_return": 0.0, "expected_volatility": 0.0, "iterations": 0}
    minimum_cash = max(
        D(policy.minimum_cash), D(rules["minimum_cash_weight"]), D(str(recommended_cash or 0)),
    )
    investable = float(1 - minimum_cash)
    recommended = [max(0.0, float(row.get("weight", 0))) for row in rows]
    recommended_total = sum(recommended)
    recommended = [value / recommended_total * investable for value in recommended] if recommended_total else [investable / len(rows)] * len(rows)
    candidates = []
    for row, recommended_weight in zip(rows, recommended):
        gics = row.get("gics") or {}
        candidates.append({
            **row, "identity": str(row["instrument_id"]),
            "strategy_family": str(row["research_strategy_id"]).split("_", 1)[0],
            "sector": (gics.get("sector") or {}).get("code"),
            "industry": (gics.get("industry") or {}).get("code"),
            "sub_industry": (gics.get("sub_industry") or {}).get("code"),
            "capacity_weight": min(
                float(rules["maximum_stock_weight"]), float(row.get("capacity_weight", rules["maximum_stock_weight"])),
            ),
            "recommended_weight": recommended_weight,
        })
    nav = D(goal.plan.portfolio.account.net_liquidation or 0)
    allocation = D(goal.allocation_weight or 0)
    current = {}
    if nav > 0 and allocation > 0:
        for position in PortfolioPosition.objects.filter(
            portfolio=goal.plan.portfolio, instrument_id__in=[row["instrument_id"] for row in rows],
        ).select_related("instrument"):
            current[str(position.instrument_id)] = float(
                (
                    D(position.quantity) * D(position.market_price)
                    * D(position.instrument.multiplier or 1) / nav
                ) / allocation
            ) if D(position.market_price) > 0 else 0.0
    sectors = {row["sector"] for row in candidates if row.get("sector")}
    result = optimize_sleeves(candidates, constraints={
        "minimum_cash": float(minimum_cash),
        "per_stock_cap": float(min(D(policy.per_stock_cap), D(rules["maximum_stock_weight"]))),
        "sector_cap": float(policy.sector_cap), "industry_cap": float(policy.industry_cap),
        "sub_industry_cap": float(policy.sub_industry_cap), "strategy_family_cap": float(strategy_family_cap),
        "minimum_sectors": min(3, len(sectors)), "maximum_turnover": float(policy.maximum_turnover),
        "risk_aversion": 6 - goal.risk_level, "recommendation_penalty": 2.0,
    }, current_weights=current)
    return result["weights"], result["cash_weight"], result


def _create_goal_run(goal, batch, cache, selected_members, *, substitutions, failures):
    policy, rules = _policy_for_goal(goal)
    strategy_family_cap = effective_strategy_family_cap(policy, cache.fallback_tier)
    run = GoalRecommendationRun.objects.create(
        goal_allocation=goal, requested_plan_version=batch.requested_plan_version, policy=policy,
        dataset_version=batch.dataset, protocol_version=batch.protocol, as_of_date=cache.as_of_date,
        status="RUNNING", idempotency_key=f"batch:{batch.pk}:goal:{goal.pk}",
        request_hash=canonical_request_hash("batch_goal_recommendation", {"batch": batch.pk, "goal": goal.pk, "cache": cache.pk}),
        input_snapshot={"goal": _goal_snapshot(goal), "cache_snapshot_id": cache.pk},
        candidate_snapshot=cache.candidate_pool, optimizer_snapshot={
            "allocator": cache.allocator_strategy_id,
            "overlays": cache.overlay_strategy_ids,
            "constraints": {"strategy_family_cap": str(strategy_family_cap)},
        },
        stress_test_snapshot={"fallback_tier": cache.fallback_tier}, expires_at=timezone.now() + timedelta(hours=settings.RECOMMENDATION_SNAPSHOT_MAX_AGE_HOURS),
        started_at=timezone.now(),
    )
    if goal.timeframe_bucket == "NOW":
        rows=[];weights=[];cash=1.0;optimizer={"expected_return":0.0,"expected_volatility":0.0,"iterations":0}
    else:
        lookup=_row_lookup(cache);rows=[lookup[member.pk] for member in selected_members if member.pk in lookup]
        weights,cash,optimizer=_goal_weights(
            rows,goal,policy,rules,strategy_family_cap=strategy_family_cap,
            recommended_cash=(cache.expected_metrics or {}).get("cash_weight"),
        )
    weighted_rows = [(row, weight) for row, weight in zip(rows, weights) if float(weight) > 1e-10]
    rows = [row for row, _ in weighted_rows]; weights = [weight for _, weight in weighted_rows]
    if goal.timeframe_bucket != "NOW" and not weighted_rows:
        raise ValueError(
            f"Operational deployment failure: {goal.name} produced no positive stock/strategy sleeves; "
            "non-NOW goals cannot be treated as intentional cash-only recommendations"
        )
    strategy_ids = {
        item.research_id: item.pk for item in batch.dataset.strategies.filter(
            research_id__in={row["research_strategy_id"] for row in rows}
        )
    }
    for rank,(row,weight) in enumerate(weighted_rows):
        research_strategy_pk = strategy_ids.get(row["research_strategy_id"])
        if not research_strategy_pk:
            raise ValueError(f"Recommendation cache references unknown strategy {row['research_strategy_id']}")
        GoalRecommendationSleeve.objects.create(
            recommendation_run=run,instrument_id=row["instrument_id"],universe_member_id=row["universe_member_id"],
            research_strategy_id=research_strategy_pk,execution_strategy_definition_id=row["execution_strategy_definition_id"],
            execution_timeframe=row.get("execution_timeframe") or "1d",parameters=row.get("parameters") or {},
            sleeve_weight=D(str(weight)),stock_weight=D(str(weight)),strategy_share=D(1),
            candidate_score=D(str(row.get("candidate_score") or 0)),expected_return=D(str(row.get("expected_return") or 0)),
            expected_volatility=D(str(row.get("expected_volatility") or 0)),expected_drawdown=D(str(row.get("expected_drawdown") or 0)),
            cost_metrics={},rationale=row.get("reason") or "Recommendation cache selection",rank=rank,
        )
    run.metrics={**(cache.expected_metrics or {}),**{key:value for key,value in optimizer.items() if key in {"expected_return","expected_volatility","iterations","objective"}},
                 "cash_weight":cash,"sleeve_count":len(rows),"fallback_tier":cache.fallback_tier,
                 "qualification_substitutions":list(substitutions),"qualification_failures":list(failures)}
    run.warnings=[{"code":"FALLBACK_TIER","message":f"Recommendation availability tier {cache.fallback_tier}"}] if cache.fallback_tier>1 else []
    if goal.timeframe_bucket != "NOW" and strategy_family_cap > D(policy.strategy_family_cap):
        run.warnings.append({
            "code": "FALLBACK_FAMILY_CAP_RELAXED",
            "message": (
                f"Fallback tier {cache.fallback_tier} permits strategy-family concentration up to "
                f"{strategy_family_cap}."
            ),
        })
    run.status="COMPLETED";run.completed_at=timezone.now();run.save()
    return run,rows,weights,cash


def _attach_without_version_bump(run, goal, plan, *, actor):
    sleeves=list(run.sleeves.select_related("execution_strategy_definition").order_by("rank"))
    selection_ids=[];assignment_ids=[]
    for sleeve in sleeves:
        selection,_=GoalInstrumentSelection.objects.update_or_create(
            goal_allocation=goal,instrument=sleeve.instrument,
            defaults={"enabled":True,"minimum_weight":None,"maximum_weight":None,"display_order":sleeve.rank},
        );selection_ids.append(selection.pk)
        parameters=validate_assignment(goal_instrument_selection=selection,definition=sleeve.execution_strategy_definition,
            execution_timeframe=sleeve.execution_timeframe,parameter_overrides=sleeve.parameters,strategy_share=D(1),
            system_generated=True)
        parameter_hash=canonical_request_hash("parameters",parameters)
        assignment,_=GoalStrategyAssignment.objects.update_or_create(
            goal_instrument_selection=selection,strategy_definition=sleeve.execution_strategy_definition,
            execution_timeframe=sleeve.execution_timeframe,parameter_hash=parameter_hash,
            defaults={"parameter_overrides":parameters,"strategy_share":D(1),"create_instance":True,"enabled":True},
        );assignment_ids.append(assignment.pk)
        selection.assignments.exclude(pk=assignment.pk).update(enabled=False)
    goal.instrument_selections.exclude(pk__in=selection_ids).update(enabled=False)
    goal.construction_source="ACCEPTED_RECOMMENDATION";goal.accepted_recommendation_run=run
    goal.save(update_fields=["construction_source","accepted_recommendation_run","updated_at"])
    run.accepted_at=timezone.now();run.save(update_fields=["accepted_at"])
    GoalRecommendationAcceptance.objects.create(
        recommendation_run=run,goal=goal,accepted_plan_version=plan.version,
        created_updated_instrument_selections=selection_ids,created_updated_strategy_assignments=assignment_ids,
        accepted_by=actor,change_summary={"automatic_batch_attachment":True,"created_strategy_instances":0,"created_rebalances":0},
    )


def _locked_goal_results(batch):
    """Lock batch result rows without trying to lock their nullable audit relation."""
    return (
        batch.goal_results.select_for_update(of=("self",))
        .select_related("goal")
        .order_by("goal__display_order", "goal_id")
    )


def run_recommendation_batch(batch_or_id, *, gateway=None, actor="recommendation-system"):
    batch_id=batch_or_id.pk if isinstance(batch_or_id,RecommendationBatchRun) else batch_or_id
    batch=RecommendationBatchRun.objects.select_related(
        "plan__portfolio__account", "plan__portfolio__gateway_session"
    ).get(pk=batch_id)
    with transaction.atomic():
        locked = RecommendationBatchRun.objects.select_for_update().get(pk=batch_id)
        if locked.status != "QUEUED":
            return batch
        locked.status="RUNNING";locked.started_at=timezone.now()
        locked.save(update_fields=["status","started_at"])
        batch.status=locked.status;batch.started_at=locked.started_at
    try:
        if gateway is None and batch.plan.portfolio.gateway_session_id:
            from apps.broker_gateway.client import GatewayClient
            gateway=GatewayClient.for_portfolio(batch.plan.portfolio,require_commands=True)
        with transaction.atomic():
            batch=RecommendationBatchRun.objects.select_for_update().select_related("plan").get(pk=batch_id)
            plan=PortfolioConstructionPlan.objects.select_for_update().get(pk=batch.plan_id)
            if plan.version!=batch.requested_plan_version:raise ValueError("Portfolio Builder plan changed after recommendation batch creation")
            goal_results=list(_locked_goal_results(batch))
            total_substitutions=0
            for result in goal_results:
                goal=result.goal;cache=best_cached_recommendation(goal.timeframe_bucket,goal.risk_level)
                if goal.timeframe_bucket=="NOW":qualification=type("CashQualification",(),{"selected":(),"substitutions":(),"failures":()})()
                else:
                    required=len(cache.selected_stocks)
                    qualification=qualify_and_substitute_finalists(_ranked_members(cache),required,gateway=gateway)
                    if len(qualification.selected)<min(settings.RECOMMENDATION_MIN_STOCKS,required):
                        raise ValueError(f"Operational deployment failure: only {len(qualification.selected)} exact contracts available for {goal.name}")
                    for member in qualification.selected:calculate_member_eligibility(member)
                run,rows,weights,cash=_create_goal_run(goal,batch,cache,qualification.selected,
                    substitutions=qualification.substitutions,failures=qualification.failures)
                _attach_without_version_bump(run,goal,plan,actor=actor)
                result.recommendation_run=run;result.status="COMPLETED";result.fallback_tier=cache.fallback_tier
                result.summary={"goal_id":goal.pk,"goal_name":goal.name,
                                "stocks":[{**row,"weight":weight} for row,weight in zip(rows,weights)],
                                "cash_weight":cash,"timeframe":goal.timeframe_bucket,"risk_level":goal.risk_level,
                                "metrics":run.metrics,"freshness":cache.data_freshness}
                result.save();total_substitutions+=len(qualification.substitutions)
            bump_plan_version(plan)
            batch.status="COMPLETED";batch.completed_at=timezone.now();batch.metrics={
                "goal_count":len(goal_results),"qualification_substitutions":total_substitutions,
                "orders_created":0,"rebalances_created":0,"strategy_instances_created":0,"plan_version":plan.version,
                "latency_seconds":max(0.0,(batch.completed_at-batch.started_at).total_seconds()) if batch.started_at else None,
            };batch.error="";batch.save()
            AuditEvent.objects.create(event_type="research.recommendation.batch.completed",actor=actor,
                aggregate_type="portfolio_construction_plan",aggregate_id=str(plan.pk),data={"batch_id":batch.pk,**batch.metrics},
                idempotency_key=f"recommendation-batch:{batch.pk}:completed")
        return batch
    except Exception as exc:
        RecommendationBatchRun.objects.filter(pk=batch_id).update(status="FAILED",error=str(exc)[:2000],completed_at=timezone.now())
        RecommendationBatchGoalResult.objects.filter(batch_id=batch_id,status__in=["QUEUED","RUNNING"]).update(status="FAILED",summary={"error":str(exc)[:500]})
        raise
