from datetime import timedelta
from decimal import Decimal, ROUND_DOWN

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.idempotency import canonical_request_hash, require_matching_request
from apps.portfolio_construction.rules import resolved_goal_rules
from apps.portfolios.models import PortfolioPosition

from ..models import (
    BacktestProtocolVersion,
    GoalRecommendationPolicy,
    GoalRecommendationRun,
    GoalRecommendationSleeve,
    InstrumentClassification,
    InstrumentEligibilitySnapshot,
    ResearchCandidateScore,
    ResearchDatasetVersion,
    ResearchStrategyImplementation,
)
from ..enums import ImplementationStatus
from .classification import hierarchy
from .optimizer import RecommendationOptimizationError, optimize_sleeves
from .recommendation_cache import target_stock_count


D = Decimal
Q8 = D("0.00000001")


def _policy_for_goal(goal):
    rules = resolved_goal_rules(goal.timeframe_bucket, goal.risk_level)
    name = f"LIVE-{goal.timeframe_bucket}-R{goal.risk_level}"
    defaults = {
        "minimum_candidate_score":65,"maximum_candidate_age_days":settings.RESEARCH_SCORE_MAX_AGE_DAYS,
        "number_of_stocks":target_stock_count(goal.timeframe_bucket, goal.risk_level),"minimum_sectors":3,"sector_cap":"0.35","industry_cap":"0.25",
        "sub_industry_cap":"0.20","per_stock_cap":rules["maximum_stock_weight"],
        "strategy_family_cap":"0.40","maximum_turnover":"1.00","minimum_cash":rules["minimum_cash_weight"],
        "target_volatility":D("0.05")+D(goal.risk_level)*D("0.03"),
        "maximum_expected_drawdown":D("0.10")+D(goal.risk_level)*D("0.05"),
        "minimum_liquidity":25_000_000,"approved_candidate_roles":["EXECUTION"],"active":True,
    }
    policy, _ = GoalRecommendationPolicy.objects.update_or_create(
        name=name,
        defaults=defaults,
    )
    return policy, rules


def create_recommendation_run(goal, idempotency_key, *, policy=None, defer=False):
    if not idempotency_key:
        raise ValueError("Idempotency key is required")
    policy, rules = (policy, resolved_goal_rules(goal.timeframe_bucket, goal.risk_level)) if policy else _policy_for_goal(goal)
    dataset = ResearchDatasetVersion.objects.filter(status="ACTIVE").order_by("-snapshot_date").first()
    if not dataset:
        raise ValueError("No active research dataset")
    protocol = BacktestProtocolVersion.objects.filter(dataset_version=dataset, active=True).first()
    if not protocol:
        raise ValueError("No active backtest protocol")
    request_hash = canonical_request_hash("goal_recommendation", {
        "goal_id": goal.pk,
        "plan_version": goal.plan.version,
        "timeframe": goal.timeframe_bucket,
        "risk_level": goal.risk_level,
        "policy_id": policy.pk,
        "dataset_id": dataset.pk,
        "protocol_id": protocol.pk,
    })
    existing = GoalRecommendationRun.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        require_matching_request(existing.request_hash, request_hash)
        return existing
    now = timezone.now()
    run = GoalRecommendationRun.objects.create(
        goal_allocation=goal,
        requested_plan_version=goal.plan.version,
        policy=policy,
        dataset_version=dataset,
        protocol_version=protocol,
        as_of_date=timezone.localdate(),
        status="QUEUED" if defer else "RUNNING",
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        input_snapshot={"goal_id": goal.pk, "plan_version": goal.plan.version, "rules": {
            key: str(value) if isinstance(value, D) else value for key, value in rules.items()
        }},
        expires_at=now + timedelta(days=settings.RESEARCH_RECOMMENDATION_MAX_AGE_DAYS),
    )
    return run


def _latest_builder_eligibility(instrument_id, as_of_date):
    return InstrumentEligibilitySnapshot.objects.filter(
        universe_member__instrument_id=instrument_id,
        as_of_date__lte=as_of_date,
        research_eligible=True,
    ).select_related("universe_member").order_by("-as_of_date").first()


def _candidate_rows(run):
    cutoff = timezone.now() - timedelta(days=run.policy.maximum_candidate_age_days)
    scores = ResearchCandidateScore.objects.filter(
        goal_timeframe=run.goal_allocation.timeframe_bucket,
        risk_level=run.goal_allocation.risk_level,
        eligible=True,
        score__gte=run.policy.minimum_candidate_score,
        expires_at__gt=timezone.now(),
        as_of_date__gte=cutoff.date(),
        dataset_version=run.dataset_version,
        instrument__isnull=False,
        strategy__role="EXECUTION",
    ).select_related("strategy", "instrument__issuer").order_by("-score").distinct()
    rows = []
    for score in scores:
        readiness = _latest_builder_eligibility(score.instrument_id, run.as_of_date)
        if not readiness:
            continue
        implementation = ResearchStrategyImplementation.objects.filter(
            research_strategy=score.strategy,
            status__in=[ImplementationStatus.BUILDER_READY,ImplementationStatus.APPROVED,
                        ImplementationStatus.PAPER_VALIDATED,ImplementationStatus.APPROVED_FOR_RECOMMENDATION],
            exact_semantic_match=True,
            executable_strategy_definition__enabled=True,
        ).select_related("executable_strategy_definition").first()
        if not implementation:
            continue
        classification = InstrumentClassification.objects.filter(
            instrument=score.instrument,
            effective_from__lte=run.as_of_date,
        ).filter(Q(effective_to__isnull=True) | Q(effective_to__gte=run.as_of_date)).select_related(
            "sub_industry_node__parent__parent__parent"
        ).order_by("-effective_from").first()
        gics = hierarchy(classification)
        if not gics:
            continue
        metrics = score.metrics or {}
        rows.append({
            "identity": f"{score.instrument_id}:{score.strategy_id}",
            "candidate_score_id": score.pk,
            "instrument_id": score.instrument_id,
            "symbol": score.instrument.symbol,
            "universe_member_id": readiness.universe_member_id,
            "research_strategy_id": score.strategy_id,
            "strategy_family": score.strategy.family,
            "execution_strategy_definition_id": implementation.executable_strategy_definition_id,
            "execution_timeframe": implementation.supported_frequency,
            "parameters": score.best_parameters or implementation.default_parameters,
            "candidate_score": float(score.score),
            "expected_return": float(metrics.get("expected_return", metrics.get("cagr", 0))),
            "expected_volatility": max(float(metrics.get("expected_volatility", metrics.get("annualized_volatility", 0.20))), 0.0001),
            "expected_drawdown": float(metrics.get("expected_drawdown", metrics.get("max_drawdown", 0))),
            "cost_penalty": float((score.cost_metrics or {}).get("expected_cost", 0)),
            "instability_penalty": float((score.stability_metrics or {}).get("penalty", 0)),
            "capacity_weight": float((score.capacity_metrics or {}).get("maximum_weight", 1)),
            "sector": gics["sector"]["code"],
            "industry": gics["industry"]["code"],
            "sub_industry": gics["sub_industry"]["code"],
            "gics": gics,
            "cost_metrics": score.cost_metrics,
            "data_source":(readiness.metrics or {}).get("provider"),
            "latest_data_date":(readiness.metrics or {}).get("latest_data_date"),
        })
    allowed_instruments=[];selected=[]
    for row in rows:
        if row["instrument_id"] in allowed_instruments:continue
        if len(allowed_instruments)>=min(settings.RECOMMENDATION_CANDIDATE_POOL_SIZE,run.policy.number_of_stocks):continue
        allowed_instruments.append(row["instrument_id"]);selected.append(row)
    return selected


def run_recommendation(run_or_id):
    run_id = run_or_id.pk if isinstance(run_or_id, GoalRecommendationRun) else run_or_id
    run = GoalRecommendationRun.objects.select_related("goal_allocation__plan__portfolio", "policy").get(pk=run_id)
    if run.status == "COMPLETED":
        return run
    if run.status not in {"QUEUED", "RUNNING"}:
        return run
    run.status = "RUNNING"
    run.started_at = timezone.now()
    run.save(update_fields=["status", "started_at"])
    try:
        if run.goal_allocation.timeframe_bucket == "NOW":
            candidates = []
            result = {"weights": [], "cash_weight": 1.0, "expected_return": 0.0, "expected_volatility": 0.0}
            warnings = [{"code": "NOW_CASH_ONLY", "message": "NOW goals remain intentionally cash-only"}]
        else:
            candidates = _candidate_rows(run)
            if not candidates:
                raise ValueError("No current full-model candidates; use the plan recommendation batch fallback tiers")
            else:
                live_rules=resolved_goal_rules(run.goal_allocation.timeframe_bucket,run.goal_allocation.risk_level)
                constraints = {
                    "minimum_cash": float(max(D(run.policy.minimum_cash),D(live_rules["minimum_cash_weight"]))),
                    "per_stock_cap": float(min(D(run.policy.per_stock_cap),D(live_rules["maximum_stock_weight"]))),
                    "sector_cap": float(run.policy.sector_cap),
                    "industry_cap": float(run.policy.industry_cap),
                    "sub_industry_cap": float(run.policy.sub_industry_cap),
                    "strategy_family_cap": float(run.policy.strategy_family_cap),
                    "minimum_sectors": min(3,len({row["instrument_id"] for row in candidates})),
                    "maximum_turnover": float(run.policy.maximum_turnover),
                    "risk_aversion": 6 - run.goal_allocation.risk_level,
                }
                nav = D(run.goal_allocation.plan.portfolio.account.net_liquidation)
                positions = {
                    str(item.instrument_id): float(
                        D(item.quantity) * D(item.market_price)
                        * D(item.instrument.multiplier or 1) / nav
                    )
                    for item in PortfolioPosition.objects.filter(
                        portfolio=run.goal_allocation.plan.portfolio
                    ).select_related("instrument")
                    if nav > 0 and D(item.market_price) > 0
                }
                current = {row["identity"]: positions.get(str(row["instrument_id"]), 0) for row in candidates}
                result = optimize_sleeves(candidates, constraints=constraints, current_weights=current)
                warnings = []
        with transaction.atomic():
            run.sleeves.all().delete()
            raw_weights = result.get("weights", [])
            quantized = [D(str(value)).quantize(Q8, rounding=ROUND_DOWN) for value in raw_weights]
            stock_totals = {}
            for row, weight in zip(candidates, quantized):
                if weight > 0:
                    stock_totals[row["instrument_id"]] = stock_totals.get(row["instrument_id"], D(0)) + weight
            share_values = {}
            for instrument_id, stock_weight in stock_totals.items():
                indices = [index for index, (row, weight) in enumerate(zip(candidates, quantized)) if row["instrument_id"] == instrument_id and weight > 0]
                assigned = D(0)
                for offset, index in enumerate(indices):
                    if offset == len(indices) - 1:
                        share = D(1) - assigned
                    else:
                        share = (quantized[index] / stock_weight).quantize(Q8, rounding=ROUND_DOWN)
                        assigned += share
                    share_values[index] = share
            rank = 0
            for index, (row, weight) in enumerate(zip(candidates, quantized)):
                if weight <= 0:
                    continue
                stock_weight = stock_totals[row["instrument_id"]]
                GoalRecommendationSleeve.objects.create(
                    recommendation_run=run,
                    instrument_id=row["instrument_id"],
                    universe_member_id=row["universe_member_id"],
                    research_strategy_id=row["research_strategy_id"],
                    execution_strategy_definition_id=row["execution_strategy_definition_id"],
                    execution_timeframe=row["execution_timeframe"],
                    parameters=row["parameters"],
                    sleeve_weight=weight,
                    stock_weight=stock_weight,
                    strategy_share=share_values[index],
                    candidate_score=D(str(row["candidate_score"])),
                    expected_return=D(str(row["expected_return"])),
                    expected_volatility=D(str(row["expected_volatility"])),
                    expected_drawdown=D(str(row["expected_drawdown"])),
                    cost_metrics=row["cost_metrics"],
                    rationale=f"Approved {row['strategy_family']} candidate with score {row['candidate_score']}",
                    rank=rank,
                )
                rank += 1
            cash = D(1) - sum(quantized, D(0))
            run.candidate_snapshot = candidates
            run.optimizer_snapshot = {**result, "weights": [str(value) for value in quantized], "cash_weight": str(cash)}
            run.stress_test_snapshot = {
                "high_cost_expected_return": float(result.get("expected_return", 0)) - sum(
                    float(row.get("cost_penalty", 0)) * float(weight) for row, weight in zip(candidates, raw_weights)
                ),
                "gics_constraints_checked": True,
            }
            run.metrics = {
                "expected_return": result.get("expected_return", 0),
                "expected_volatility": result.get("expected_volatility", 0),
                "cash_weight": str(cash),
                "sleeve_count": rank,
                "rejected_close_alternatives": [
                    {"symbol": row["symbol"], "research_strategy_id": row["research_strategy_id"], "candidate_score": row["candidate_score"]}
                    for row, weight in zip(candidates, quantized) if weight <= 0
                ][:10],
            }
            run.warnings = warnings
            run.status = "COMPLETED"
            run.completed_at = timezone.now()
            run.error = ""
            run.save()
        return run
    except Exception as exc:
        run.status = "FAILED"
        run.error = str(exc)[:2000]
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "error", "completed_at"])
        if isinstance(exc, RecommendationOptimizationError):
            raise
        raise
