from datetime import timedelta
from decimal import Decimal
import json

import pytest
from django.utils import timezone

from apps.accounts.models import BrokerAccount
from apps.allocation.models import RebalancePolicy
from apps.audit.models import AuditEvent, OperationAttempt
from apps.broker_gateway.models import BrokerGatewaySession
from apps.core.idempotency import canonical_request_hash
from apps.instruments.models import BrokerContract, Instrument
from apps.market_data.models import InstrumentPriceHistory
from apps.portfolio_construction.models import (
    GoalInstrumentSelection,
    GoalStrategyAssignment,
    PortfolioConstructionPlan,
    PortfolioConstructionRun,
    PortfolioGoalAllocation,
    StrategyConstructionProfile,
)
from apps.portfolio_construction.rules import MAXIMUM_RISK, resolved_goal_rules, validate_timeframe_risk
from apps.portfolio_construction.services import (
    ConstructionAlreadyApplied,
    ConstructionError,
    apply_construction_run,
    create_construction_run,
    eligible_strategies,
    plan_construction_rebalance,
    plan_validation,
    run_construction,
    validate_assignment,
    validate_instrument_selection,
)
from apps.portfolios.models import TradingPortfolio
from apps.strategies.models import OrderPolicy, StrategyDefinition, StrategyInstance, StrategyRiskPolicy
from apps.strategies.plugins import get_plugin


pytestmark = pytest.mark.django_db
D = Decimal


def construction_case(symbols=("AAA", "BBB", "CCC")):
    suffix = BrokerAccount.objects.count() + 1
    account = BrokerAccount.objects.create(
        account_id=f"DU-BUILDER-{suffix}", net_liquidation=10000, available_cash=10000,
        is_reconciled=True,
    )
    session = BrokerGatewaySession.objects.create(
        display_name=f"Builder Paper {suffix}",
        username_hint="builder",
        mode="paper",
        status=BrokerGatewaySession.Status.CONNECTED,
        child_container_name=f"builder-paper-{suffix}",
        encrypted_gateway_token="test",
        encrypted_novnc_password="test",
        commands_enabled=True,
        last_gateway_state={"connected": True, "reconciled": True, "mode": "paper"},
    )
    portfolio = TradingPortfolio.objects.create(
        name="Builder portfolio", account=account, gateway_session=session,
    )
    instruments = []
    start = timezone.now().date() - timedelta(days=79)
    for index, symbol in enumerate(symbols, start=1):
        instrument = Instrument.objects.create(symbol=symbol)
        BrokerContract.objects.create(instrument=instrument, conid=8000 + index)
        instruments.append(instrument)
        for offset in range(80):
            drift = D("0.0007") + D(index) * D("0.00015")
            cycle = D((offset % (4 + index)) - 2) * D("0.0002")
            price = D("100") * (D(1) + drift) ** offset * (D(1) + cycle)
            InstrumentPriceHistory.objects.create(
                instrument=instrument,
                trading_date=start + timedelta(days=offset),
                open=price,
                high=price,
                low=price,
                close=price,
                adjusted_close=price,
                volume=100000,
                fetched_at=timezone.now(),
            )
    RebalancePolicy.objects.create(
        portfolio=portfolio,
        maximum_turnover="2",
        minimum_trade_notional="1",
        fee_buffer="0",
        cash_buffer_percent="0",
    )
    return portfolio, instruments


def add_goal(plan, name, weight, timeframe, risk, order=0):
    return PortfolioGoalAllocation.objects.create(
        plan=plan,
        name=name,
        allocation_weight=weight,
        timeframe_bucket=timeframe,
        risk_level=risk,
        display_order=order,
    )


def add_instrument(goal, instrument, **overrides):
    return GoalInstrumentSelection.objects.create(
        goal_allocation=goal,
        instrument=instrument,
        **overrides,
    )


def add_assignment(selection, definition=None, *, share="1", parameters=None, timeframe="1d", **overrides):
    definition = definition or StrategyDefinition.objects.get(key="FIXED_WEIGHT_REBALANCE")
    parameters = parameters or {"direction": "LONG"}
    return GoalStrategyAssignment.objects.create(
        goal_instrument_selection=selection,
        strategy_definition=definition,
        execution_timeframe=timeframe,
        parameter_overrides=parameters,
        parameter_hash=canonical_request_hash("parameters", parameters),
        strategy_share=share,
        **overrides,
    )


def add_stock(goal, instrument, definition=None, **assignment_overrides):
    selection = add_instrument(goal, instrument)
    add_assignment(selection, definition, **assignment_overrides)
    return selection


@pytest.mark.parametrize("timeframe", ["NOW", "HURRY", "FAST", "BUILD", "GROW", "COMPOUND"])
def test_all_timeframe_rules_are_fixed_and_valid(timeframe):
    rules = resolved_goal_rules(timeframe, MAXIMUM_RISK[timeframe])
    assert rules["timeframe_bucket"] == timeframe
    assert rules["lookback_days"] == 252
    assert rules["minimum_history_observations"] == 60
    assert rules["long_only"] is True


@pytest.mark.parametrize("risk", [1, 2, 3, 4, 5])
def test_all_risk_rules_have_fixed_caps_and_optimizer_methods(risk):
    rules = resolved_goal_rules("GROW", risk)
    assert rules["risk_level"] == risk
    assert rules["maximum_stock_weight"] > 0
    assert rules["optimizer_method"] == ("MINIMUM_VARIANCE" if risk <= 3 else "MAXIMUM_SHARPE")


@pytest.mark.parametrize("timeframe,risk", [("NOW", 2), ("HURRY", 3), ("FAST", 4), ("BUILD", 5)])
def test_invalid_timeframe_risk_combinations_are_rejected(timeframe, risk):
    with pytest.raises(ValueError, match="exceeds the maximum"):
        validate_timeframe_risk(timeframe, risk)


def test_drafts_allow_incomplete_or_excess_allocations_but_preview_requires_exact_total():
    portfolio, _ = construction_case(())
    plan = PortfolioConstructionPlan.objects.create(portfolio=portfolio)
    add_goal(plan, "Incomplete", "0.40", "BUILD", 3)
    assert plan_validation(plan)["ready_to_preview"] is False
    add_goal(plan, "Excess", "0.70", "GROW", 4)
    validation = plan_validation(plan)
    assert validation["allocated_weight"] == D("1.10")
    assert validation["validation_errors"][0]["code"] == "ALLOCATION_TOTAL"
    with pytest.raises(ConstructionError, match="exactly 100%"):
        create_construction_run(plan, "not-ready", refresh_history=False)


def test_plan_goal_api_exposes_totals_rules_and_audits_mutations(client):
    portfolio, _ = construction_case(())
    created = client.post(
        "/api/v1/portfolio-construction/plans/",
        json.dumps({"portfolio_id": portfolio.pk, "name": "Goals"}),
        content_type="application/json",
    )
    assert created.status_code == 201
    plan = created.json()["data"]
    goal = client.post(
        f"/api/v1/portfolio-construction/plans/{plan['id']}/goals/",
        json.dumps({
            "name": "Growth",
            "allocation_percentage": "50",
            "timeframe_bucket": "GROW",
            "risk_level": 4,
        }),
        content_type="application/json",
    )
    assert goal.status_code == 201
    detail = client.get(f"/api/v1/portfolio-construction/plans/{plan['id']}/").json()["data"]
    assert D(detail["allocated_percentage"]) == D(50)
    assert detail["ready_to_preview"] is False
    assert detail["goals"][0]["resolved_rules"]["optimizer_method"] == "MAXIMUM_SHARPE"
    changed = client.patch(
        f"/api/v1/portfolio-construction/goals/{goal.json()['data']['id']}/",
        json.dumps({"allocation_percentage": "100"}),
        content_type="application/json",
    )
    assert changed.status_code == 200
    assert client.get(f"/api/v1/portfolio-construction/plans/{plan['id']}/").json()["data"]["ready_to_preview"] is True
    assert AuditEvent.objects.filter(event_type="portfolio.construction_goal.created").exists()
    assert AuditEvent.objects.filter(event_type="portfolio.construction_goal.changed").exists()


def test_strategy_eligibility_returns_eligible_and_rejected_reasons(client):
    portfolio, instruments = construction_case(("AAA",))
    plan = PortfolioConstructionPlan.objects.create(portfolio=portfolio)
    goal = add_goal(plan, "Conservative", "1", "HURRY", 1)
    result = eligible_strategies(goal)
    assert any(item["key"] == "FIXED_WEIGHT_REBALANCE" for item in result["eligible"])
    assert result["rejected"]
    assert all(item["reason"] for item in result["rejected"])
    stock = client.post(
        f"/api/v1/portfolio-construction/goals/{goal.pk}/instruments/",
        json.dumps({"instrument_id": instruments[0].pk}),
        content_type="application/json",
    )
    assert stock.status_code == 201
    post = client.post(
        f"/api/v1/portfolio-construction/instruments/{stock.json()['data']['id']}/assignments/",
        json.dumps({
            "strategy_key": "FIXED_WEIGHT_REBALANCE",
            "execution_timeframe": "1d",
            "parameter_overrides": {"direction": "LONG"},
        }),
        content_type="application/json",
    )
    assert post.status_code == 201
    assert post.json()["data"]["symbol"] == "AAA"
    assert D(post.json()["data"]["strategy_share"]) == D(1)
    assert StrategyConstructionProfile.objects.count() == 5


def test_cash_only_and_one_stock_goal_edges_are_explainable():
    portfolio, _ = construction_case(())
    cash_plan = PortfolioConstructionPlan.objects.create(portfolio=portfolio)
    cash_goal = add_goal(cash_plan, "Immediate", "1", "NOW", 1)
    cash = run_construction(create_construction_run(cash_plan, "cash-only", refresh_history=False), refresh_history=False)
    assert cash.final_target_weights == {"cash": "1", "stocks": {}}
    assert cash.goal_results[0]["intentionally_cash_only"] is True

    stock_portfolio, instruments = construction_case(("AAA",))
    stock_plan = PortfolioConstructionPlan.objects.create(portfolio=stock_portfolio)
    stock_goal = add_goal(stock_plan, "Aggressive", "1", "GROW", 5)
    add_stock(stock_goal, instruments[0])
    stock = run_construction(create_construction_run(stock_plan, "one-stock", refresh_history=False), refresh_history=False)
    assert stock.final_target_weights["stocks"][str(instruments[0].pk)] == "0.25000000"
    assert stock.final_target_weights["cash"] == "0.75000000"
    assert stock.goal_results[0]["warnings"][0]["code"] == "SINGLE_STOCK_LIMIT"


def test_no_stock_non_cash_goal_previews_cash_but_blocks_apply():
    portfolio, _ = construction_case(())
    plan = PortfolioConstructionPlan.objects.create(portfolio=portfolio)
    add_goal(plan, "Missing choices", "1", "GROW", 5)
    run = run_construction(create_construction_run(plan, "empty-goal", refresh_history=False), refresh_history=False)
    assert run.status == "COMPLETED"
    assert run.goal_results[0]["apply_blocked"] is True
    with pytest.raises(ConstructionError, match="must include at least one"):
        apply_construction_run(run, "empty-apply")


def test_two_goal_preview_merges_duplicate_stocks_and_preserves_existing_optimizer_records():
    portfolio, instruments = construction_case()
    plan = PortfolioConstructionPlan.objects.create(portfolio=portfolio)
    first = add_goal(plan, "Growth", "0.50", "GROW", 4, 1)
    second = add_goal(plan, "Aggressive", "0.50", "GROW", 5, 2)
    for instrument in instruments[:2]:
        add_stock(first, instrument)
    for instrument in (instruments[0], instruments[2]):
        add_stock(second, instrument)
    run = run_construction(create_construction_run(plan, "fifty-fifty", refresh_history=False), refresh_history=False)
    weights = run.final_target_weights
    assert run.status == "COMPLETED"
    assert set(weights["stocks"]) == {str(item.pk) for item in instruments}
    assert weights["stocks"][str(instruments[0].pk)] == "0.22500000"
    assert sum(D(value) for value in weights["stocks"].values()) + D(weights["cash"]) == D(1)
    shared = run.targets.get(instrument=instruments[0])
    assert len(shared.goal_contributions) == 2
    assert run.metrics["expected_volatility"] is not None
    assert not hasattr(portfolio, "optimization_universe")


def test_async_preview_retry_records_attempts_and_is_idempotent(client, monkeypatch):
    portfolio, instruments = construction_case(("AAA", "BBB"))
    plan = PortfolioConstructionPlan.objects.create(portfolio=portfolio)
    goal = add_goal(plan, "Retry", "1", "GROW", 5)
    for instrument in instruments:
        add_stock(goal, instrument)
    payload = json.dumps({"plan_id": plan.pk, "refresh_history": False})
    queued = client.post(
        "/api/v1/portfolio-construction/preview/",
        payload,
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="builder-retry",
    )
    assert queued.status_code == 202
    run_id = queued.json()["data"]["id"]
    from apps.portfolio_construction import services
    from apps.portfolio_construction.tasks import execute_construction_run

    real = services.optimize_explicit_universe
    calls = {"count": 0}

    def flaky(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("temporary construction worker failure")
        return real(*args, **kwargs)

    monkeypatch.setattr(services, "optimize_explicit_universe", flaky)
    with pytest.raises(ConstructionError, match="temporary construction worker failure"):
        execute_construction_run.run(run_id, False, False)
    failed = PortfolioConstructionRun.objects.get(pk=run_id)
    assert failed.status == "FAILED" and failed.retryable is True
    retried = client.post(
        "/api/v1/portfolio-construction/preview/",
        payload,
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="builder-retry",
        HTTP_IDEMPOTENCY_RETRY="true",
    )
    assert retried.status_code == 202
    execute_construction_run.run(run_id, False, False)
    assert PortfolioConstructionRun.objects.get(pk=run_id).status == "COMPLETED"
    assert list(OperationAttempt.objects.filter(
        operation_type="PORTFOLIO_CONSTRUCTION", operation_id=str(run_id)
    ).values_list("status", flat=True)) == ["FAILED", "COMPLETED"]


def test_apply_creates_one_net_construction_rebalance_and_reuses_disabled_paper_instances():
    portfolio, instruments = construction_case()
    plan = PortfolioConstructionPlan.objects.create(portfolio=portfolio)
    first = add_goal(plan, "Growth", "0.50", "GROW", 4, 1)
    second = add_goal(plan, "Aggressive", "0.50", "GROW", 5, 2)
    first_stock = add_stock(first, instruments[0])
    add_stock(first, instruments[1])
    second_stock = add_stock(second, instruments[0])
    add_stock(second, instruments[2])
    first_a = first_stock.assignments.get()
    second_a = second_stock.assignments.get()
    run = run_construction(create_construction_run(plan, "apply-preview", refresh_history=False), refresh_history=False)
    preview = plan_construction_rebalance(run, "apply-preview:rebalance", mode="PAPER")
    assert preview.target_source == "GOAL_CONSTRUCTION"
    applied, rebalance, created = apply_construction_run(run, "apply-once", mode="PAPER")
    assert created is True
    assert rebalance.target_source == "GOAL_CONSTRUCTION"
    assert rebalance.construction_run_id == run.pk
    assert rebalance.targets.count() == 3
    assert applied.applied_rebalance_id == rebalance.pk
    assert StrategyInstance.objects.filter(portfolio=portfolio).count() == 3
    assert not StrategyInstance.objects.filter(portfolio=portfolio, enabled=True).exists()
    assert not StrategyInstance.objects.filter(portfolio=portfolio).exclude(execution_mode="PAPER").exists()
    first_a.refresh_from_db()
    second_a.refresh_from_db()
    assert first_a.created_strategy_instance_id == second_a.created_strategy_instance_id
    same, same_rebalance, duplicate_created = apply_construction_run(run, "apply-once", mode="PAPER")
    assert duplicate_created is False and same_rebalance.pk == rebalance.pk
    with pytest.raises(ConstructionAlreadyApplied):
        apply_construction_run(run, "apply-again", mode="PAPER")


def test_apply_rechecks_builder_plan_readiness_after_preview():
    portfolio, instruments = construction_case(("AAA",))
    plan = PortfolioConstructionPlan.objects.create(portfolio=portfolio)
    goal = add_goal(plan, "Readiness changed", "1", "GROW", 4)
    add_stock(goal, instruments[0])
    run = run_construction(
        create_construction_run(
            plan,
            "builder-readiness-preview",
            refresh_history=False,
        ),
        refresh_history=False,
    )
    goal.allocation_weight = Decimal("0.50")
    goal.save(update_fields=["allocation_weight", "updated_at"])

    with pytest.raises(ConstructionError, match="total exactly 100%"):
        apply_construction_run(run, "builder-readiness-apply")

    run.refresh_from_db()
    assert run.applied_rebalance_id is None


def test_apply_api_queues_polls_and_returns_identical_one_time_result(client):
    portfolio, instruments = construction_case(("AAA",))
    plan = PortfolioConstructionPlan.objects.create(portfolio=portfolio)
    goal = add_goal(plan, "API apply", "1", "GROW", 5)
    add_stock(goal, instruments[0])
    run = run_construction(
        create_construction_run(plan, "api-apply-preview", refresh_history=False),
        refresh_history=False,
    )
    payload = json.dumps({"plan_id": plan.pk, "portfolio_id": portfolio.pk})
    queued = client.post(
        f"/api/v1/portfolio-construction/runs/{run.pk}/apply/",
        payload,
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="api-apply-once",
    )
    assert queued.status_code == 202
    assert queued.json()["data"]["application_status"] == "QUEUED"
    from apps.portfolio_construction.tasks import apply_construction_run_task

    apply_construction_run_task.run(run.pk, "api-apply-once", "PAPER")
    polled = client.get(f"/api/v1/portfolio-construction/runs/{run.pk}/").json()["data"]
    assert polled["application_status"] == "ACTIVATING"
    assert polled["metrics"]["application"]["strategy_activation"] == "QUEUED"
    assert polled["applied_rebalance"]["id"]
    assert polled["strategy_instances"][0]["strategy_instance_id"]
    same = client.post(
        f"/api/v1/portfolio-construction/runs/{run.pk}/apply/",
        payload,
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="api-apply-once",
    )
    duplicate = client.post(
        f"/api/v1/portfolio-construction/runs/{run.pk}/apply/",
        payload,
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="api-apply-again",
    )
    assert same.status_code == 200
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "CONSTRUCTION_ALREADY_APPLIED"


def test_stock_selection_validation_enforces_tradable_stocks_and_local_bounds():
    _, instruments = construction_case(("AAA",))
    instrument = instruments[0]
    assert validate_instrument_selection(instrument=instrument, minimum_weight="0.1", maximum_weight="0.2") == (
        D("0.1"), D("0.2")
    )
    with pytest.raises(ConstructionError, match="must not exceed"):
        validate_instrument_selection(instrument=instrument, minimum_weight="0.3", maximum_weight="0.2")
    instrument.tradable = False
    instrument.save(update_fields=["tradable"])
    with pytest.raises(ConstructionError, match="active, tradable stock"):
        validate_instrument_selection(instrument=instrument)


def test_local_stock_bounds_are_applied_without_weakening_goal_caps():
    portfolio, instruments = construction_case(("AAA", "BBB"))
    plan = PortfolioConstructionPlan.objects.create(portfolio=portfolio)
    goal = add_goal(plan, "Growth", "1", "GROW", 5)
    first = add_instrument(goal, instruments[0], minimum_weight="0.1", maximum_weight="0.1")
    second = add_instrument(goal, instruments[1], maximum_weight="0.25")
    add_assignment(first)
    add_assignment(second)
    run = run_construction(
        create_construction_run(plan, "local-stock-bounds", refresh_history=False),
        refresh_history=False,
    )
    local = {item["instrument_id"]: D(item["local_weight"]) for item in run.goal_results[0]["stocks"]}
    assert local[instruments[0].pk] == D("0.1")
    assert D("0.24999999") <= local[instruments[1].pk] <= D("0.25")
    assert D("0.65") <= D(run.goal_results[0]["cash_weight"]) <= D("0.65000001")


def test_assignment_validation_enforces_eligibility_plugin_schema_and_long_only():
    portfolio, instruments = construction_case(("AAA",))
    plan = PortfolioConstructionPlan.objects.create(portfolio=portfolio)
    goal = add_goal(plan, "Growth", "1", "GROW", 5)
    stock = add_instrument(goal, instruments[0])
    definition = StrategyDefinition.objects.get(key="SMA_CROSSOVER")
    parameters = get_plugin(definition).default_parameters
    assert validate_assignment(
        goal_instrument_selection=stock,
        definition=definition,
        execution_timeframe="1d",
        parameter_overrides=parameters,
        strategy_share="1",
    )["direction"] == "LONG"
    with pytest.raises(ConstructionError, match="fast_window must be less"):
        validate_assignment(
            goal_instrument_selection=stock,
            definition=definition,
            execution_timeframe="1d",
            parameter_overrides={**parameters, "fast_window": 60, "slow_window": 50},
            strategy_share="1",
        )
    with pytest.raises(ConstructionError, match="long-only"):
        validate_assignment(
            goal_instrument_selection=stock,
            definition=definition,
            execution_timeframe="1d",
            parameter_overrides={**parameters, "direction": "SHORT"},
            strategy_share="1",
        )


def test_multiple_assignments_split_stock_ownership_without_changing_stock_weight():
    portfolio, instruments = construction_case(("AAA",))
    plan = PortfolioConstructionPlan.objects.create(portfolio=portfolio)
    goal = add_goal(plan, "Growth", "1", "GROW", 5)
    stock = add_instrument(goal, instruments[0])
    fixed = StrategyDefinition.objects.get(key="FIXED_WEIGHT_REBALANCE")
    sma = StrategyDefinition.objects.get(key="SMA_CROSSOVER")
    add_assignment(stock, fixed, share="0.4")
    add_assignment(stock, sma, share="0.6", parameters=get_plugin(sma).default_parameters)
    run = run_construction(
        create_construction_run(plan, "split-ownership", refresh_history=False),
        refresh_history=False,
    )
    assert run.final_target_weights["stocks"][str(instruments[0].pk)] == "0.25000000"
    preview_stock = run.goal_results[0]["stocks"][0]
    assert preview_stock["strategy_share_valid"] is True
    assert [D(item["portfolio_weight"]) for item in preview_stock["strategies"]] == [D("0.1"), D("0.15")]
    assert sorted(D(item["target_weight"]) for item in run.metrics["strategy_targets"]) == [D("0.1"), D("0.15")]


def test_incomplete_strategy_shares_preview_with_attribution_but_block_apply():
    portfolio, instruments = construction_case(("AAA",))
    plan = PortfolioConstructionPlan.objects.create(portfolio=portfolio)
    goal = add_goal(plan, "Growth", "1", "GROW", 5)
    stock = add_instrument(goal, instruments[0])
    fixed = StrategyDefinition.objects.get(key="FIXED_WEIGHT_REBALANCE")
    sma = StrategyDefinition.objects.get(key="SMA_CROSSOVER")
    add_assignment(stock, fixed, share="0.6")
    add_assignment(stock, sma, share="0.3", parameters=get_plugin(sma).default_parameters)
    run = run_construction(
        create_construction_run(plan, "invalid-shares", refresh_history=False),
        refresh_history=False,
    )
    assert run.goal_results[0]["apply_blocked"] is True
    assert D(run.goal_results[0]["stocks"][0]["strategy_share_total"]) == D("0.9")
    assert any(item["code"] == "INVALID_STRATEGY_SHARES" for item in run.goal_results[0]["warnings"])
    with pytest.raises(ConstructionError, match="shares must total exactly 100%"):
        apply_construction_run(run, "invalid-shares-apply")


def test_apply_aggregates_same_identity_across_goals_into_nonzero_target_configuration():
    portfolio, instruments = construction_case(("AAA",))
    plan = PortfolioConstructionPlan.objects.create(portfolio=portfolio)
    first = add_goal(plan, "First", "0.4", "GROW", 5)
    second = add_goal(plan, "Second", "0.6", "GROW", 5)
    first_assignment = add_assignment(add_instrument(first, instruments[0]))
    second_assignment = add_assignment(add_instrument(second, instruments[0]))
    run = run_construction(
        create_construction_run(plan, "aggregate-identity", refresh_history=False),
        refresh_history=False,
    )
    _, _, created = apply_construction_run(run, "aggregate-identity-apply")
    assert created is True
    assert StrategyInstance.objects.filter(portfolio=portfolio).count() == 1
    instance = StrategyInstance.objects.get(portfolio=portfolio)
    assert instance.target_configuration == {
        "target_weight": "0.25000000",
        "capital_share": "1",
        "priority": 100,
        "construction_run_id": str(run.pk),
    }
    assert instance.enabled is False and instance.execution_mode == "PAPER"
    first_assignment.refresh_from_db()
    second_assignment.refresh_from_db()
    assert first_assignment.created_strategy_instance_id == instance.pk
    assert second_assignment.created_strategy_instance_id == instance.pk


@pytest.mark.parametrize("identity_dimension", ["parameters", "timeframe"])
def test_different_strategy_identity_dimensions_create_different_instances(identity_dimension):
    portfolio, instruments = construction_case(("AAA",))
    plan = PortfolioConstructionPlan.objects.create(portfolio=portfolio)
    goal = add_goal(plan, "Growth", "1", "GROW", 5)
    stock = add_instrument(goal, instruments[0])
    if identity_dimension == "parameters":
        definition = StrategyDefinition.objects.get(key="SMA_CROSSOVER")
        first_parameters = get_plugin(definition).default_parameters
        second_parameters = {**first_parameters, "fast_window": 10}
        add_assignment(stock, definition, share="0.5", parameters=first_parameters)
        add_assignment(stock, definition, share="0.5", parameters=second_parameters)
    else:
        definition = StrategyDefinition.objects.get(key="FIXED_WEIGHT_REBALANCE")
        add_assignment(stock, definition, share="0.5", timeframe="1d")
        add_assignment(stock, definition, share="0.5", timeframe="1h")
    run = run_construction(
        create_construction_run(plan, f"different-{identity_dimension}", refresh_history=False),
        refresh_history=False,
    )
    apply_construction_run(run, f"different-{identity_dimension}-apply")
    instances = list(StrategyInstance.objects.filter(portfolio=portfolio).order_by("pk"))
    assert len(instances) == 2
    assert {D(item.target_configuration["target_weight"]) for item in instances} == {D("0.125")}


def test_outdated_disabled_paper_instance_is_versioned_via_update_workflow():
    from apps.strategies.framework import create_instance

    portfolio, instruments = construction_case(("AAA",))
    definition = StrategyDefinition.objects.get(key="FIXED_WEIGHT_REBALANCE")
    existing, _ = create_instance(
        name="Reusable builder instance",
        definition_key=definition.key,
        portfolio=portfolio,
        instrument_id=instruments[0].pk,
        timeframe="1d",
        parameters={"direction": "LONG"},
        target_configuration={"target_weight": "0.01", "capital_share": "0.01", "priority": 100},
        execution_mode="PAPER",
        qualify=False,
    )
    plan = PortfolioConstructionPlan.objects.create(portfolio=portfolio)
    goal = add_goal(plan, "Growth", "1", "GROW", 5)
    add_stock(goal, instruments[0])
    run = run_construction(
        create_construction_run(plan, "version-target", refresh_history=False),
        refresh_history=False,
    )
    apply_construction_run(run, "version-target-apply")
    existing.refresh_from_db()
    assert existing.version == 2
    assert existing.versions.count() == 2
    assert existing.target_configuration["target_weight"] == "0.25000000"
    assert existing.enabled is False and existing.execution_mode == "PAPER"


def test_enabled_instances_are_never_reused_or_modified():
    from apps.strategies.framework import create_instance

    portfolio, instruments = construction_case(("AAA",))
    definition = StrategyDefinition.objects.get(key="FIXED_WEIGHT_REBALANCE")
    existing, _ = create_instance(
        name="Incompatible enabled",
        definition_key=definition.key,
        portfolio=portfolio,
        instrument_id=instruments[0].pk,
        timeframe="1d",
        parameters={"direction": "LONG"},
        target_configuration={"target_weight": "0.01", "capital_share": "0.01", "priority": 100},
        execution_mode="PAPER",
        qualify=False,
    )
    existing.enabled = True
    existing.state = "LONG"
    existing.save(update_fields=["enabled", "state"])
    original_configuration = dict(existing.target_configuration)
    plan = PortfolioConstructionPlan.objects.create(portfolio=portfolio)
    goal = add_goal(plan, "Growth", "1", "GROW", 5)
    add_stock(goal, instruments[0])
    run = run_construction(
        create_construction_run(plan, "incompatible-enabled", refresh_history=False),
        refresh_history=False,
    )
    apply_construction_run(run, "incompatible-enabled-apply")
    existing.refresh_from_db()
    assert existing.target_configuration == original_configuration and existing.version == 1
    created = StrategyInstance.objects.exclude(pk=existing.pk).get(portfolio=portfolio)
    assert created.execution_mode == "PAPER" and created.enabled is False
    assert created.target_configuration["target_weight"] == "0.25000000"


def test_instrument_and_assignment_api_crud_rejects_unknown_fields_bumps_versions_and_audits(client):
    portfolio, instruments = construction_case(("AAA",))
    plan = PortfolioConstructionPlan.objects.create(portfolio=portfolio)
    goal = add_goal(plan, "Growth", "1", "GROW", 5)
    start_version = plan.version
    stock = client.post(
        f"/api/v1/portfolio-construction/goals/{goal.pk}/instruments/",
        json.dumps({"instrument_id": instruments[0].pk}),
        content_type="application/json",
    )
    assert stock.status_code == 201
    stock_id = stock.json()["data"]["id"]
    duplicate = client.post(
        f"/api/v1/portfolio-construction/goals/{goal.pk}/instruments/",
        json.dumps({"instrument_id": instruments[0].pk}),
        content_type="application/json",
    )
    assert duplicate.status_code == 400
    assignment = client.post(
        f"/api/v1/portfolio-construction/instruments/{stock_id}/assignments/",
        json.dumps({
            "strategy_key": "FIXED_WEIGHT_REBALANCE",
            "execution_timeframe": "1d",
            "parameter_overrides": {"direction": "LONG"},
        }),
        content_type="application/json",
    )
    assert assignment.status_code == 201
    assignment_id = assignment.json()["data"]["id"]
    changed = client.patch(
        f"/api/v1/portfolio-construction/assignments/{assignment_id}/",
        json.dumps({"strategy_share": "0.75"}),
        content_type="application/json",
    )
    assert changed.status_code == 200 and D(changed.json()["data"]["strategy_share"]) == D("0.75")
    unknown = client.patch(
        f"/api/v1/portfolio-construction/assignments/{assignment_id}/",
        json.dumps({"legacy_selection": True}),
        content_type="application/json",
    )
    assert unknown.status_code == 400
    deleted = client.delete(f"/api/v1/portfolio-construction/assignments/{assignment_id}/")
    assert deleted.status_code == 200
    plan.refresh_from_db()
    assert plan.version == start_version + 4
    assert AuditEvent.objects.filter(event_type="portfolio.construction_instrument.created").exists()
    assert AuditEvent.objects.filter(event_type="portfolio.construction_assignment.created").exists()
    assert AuditEvent.objects.filter(event_type="portfolio.construction_assignment.changed").exists()
    assert AuditEvent.objects.filter(event_type="portfolio.construction_assignment.deleted").exists()


def test_old_construction_routes_and_model_are_removed(client):
    portfolio, _ = construction_case(())
    plan = PortfolioConstructionPlan.objects.create(portfolio=portfolio)
    goal = add_goal(plan, "Growth", "1", "GROW", 5)
    assert client.get(f"/api/v1/portfolio-construction/goals/{goal.pk}/selections/").status_code == 404
    with pytest.raises(LookupError):
        from django.apps import apps
        apps.get_model("portfolio_construction", "GoalStrategySelection")
