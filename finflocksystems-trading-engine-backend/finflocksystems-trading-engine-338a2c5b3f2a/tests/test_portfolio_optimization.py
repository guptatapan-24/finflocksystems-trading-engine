from datetime import date, timedelta
from decimal import Decimal
import json

import numpy as np
import pytest
from django.test import Client
from django.utils import timezone

from apps.accounts.models import BrokerAccount
from apps.audit.models import OperationAttempt
from apps.broker_gateway.models import BrokerGatewaySession
from apps.execution.modes import RunType
from apps.allocation.models import AllocationDecision, RebalancePolicy, StrategyCapitalSnapshot
from apps.allocation.services import create_flow
from apps.instruments.models import BrokerContract, Instrument
from apps.market_data.models import InstrumentPriceHistory
from apps.oms.models import OrderIntent
from apps.portfolio_optimization.models import (
    PortfolioOptimizationPolicy,
    PortfolioOptimizationRun,
    PortfolioUniverse,
    PortfolioUniverseInstrument,
)
from apps.portfolio_optimization.services import (
    UniverseSizeError,
    plan_optimized_rebalance,
    run_optimization,
    solve_markowitz,
)
from apps.portfolios.models import TradingPortfolio
from apps.position_sizing.models import PositionSizingDecision
from apps.strategies.models import StrategyAllocation, StrategyDefinition, StrategyInstance


pytestmark = pytest.mark.django_db


def test_markowitz_methods_enforce_cash_bounds_and_turnover():
    covariance = np.array([[0.04, 0.006, 0.004], [0.006, 0.09, 0.01], [0.004, 0.01, 0.16]])
    expected = np.array([0.07, 0.13, 0.18])
    current = np.array([0.50, 0.25, 0.15])
    for method in ["MINIMUM_VARIANCE", "MAXIMUM_SHARPE"]:
        result = solve_markowitz(
            expected_returns=expected,
            covariance=covariance,
            current_weights=current,
            method=method,
            cash_weight=0.10,
            minimum_weight=0.05,
            maximum_weight=0.70,
            maximum_turnover=0.40,
            transaction_cost_penalty=0.01,
            risk_free_rate=0.02,
            long_only=True,
        )
        assert result["weights"].sum() == pytest.approx(0.90, abs=1e-7)
        assert min(result["weights"]) >= 0.05 - 1e-7
        assert max(result["weights"]) <= 0.70 + 1e-7
        assert result["turnover"] <= 0.40 + 1e-7
        assert result["expected_volatility"] > 0


def test_transaction_cost_penalty_reduces_turnover():
    arguments = {
        "expected_returns": [0.04, 0.20],
        "covariance": [[0.03, 0.002], [0.002, 0.05]],
        "current_weights": [0.80, 0.20],
        "method": "MAXIMUM_SHARPE",
        "cash_weight": 0,
        "minimum_weight": 0,
        "maximum_weight": 1,
        "maximum_turnover": 2,
        "risk_free_rate": 0,
        "long_only": True,
    }
    unpenalized = solve_markowitz(**arguments, transaction_cost_penalty=0)
    penalized = solve_markowitz(**arguments, transaction_cost_penalty=10)
    assert penalized["turnover"] < unpenalized["turnover"]


def _optimization_case():
    suffix = BrokerAccount.objects.count() + 1
    account = BrokerAccount.objects.create(
        account_id=f"DU-OPT-{suffix}",
        net_liquidation=10000,
        available_cash=10000,
        is_reconciled=True,
    )
    session = BrokerGatewaySession.objects.create(
        display_name=f"Optimizer Paper {suffix}",
        username_hint="optimizer",
        mode="paper",
        status=BrokerGatewaySession.Status.CONNECTED,
        child_container_name=f"optimizer-paper-{suffix}",
        encrypted_gateway_token="test",
        encrypted_novnc_password="test",
        commands_enabled=True,
        last_gateway_state={"connected": True, "reconciled": True, "mode": "paper"},
    )
    portfolio = TradingPortfolio.objects.create(
        name="Optimized", account=account, gateway_session=session,
    )
    instruments = [Instrument.objects.create(symbol=symbol) for symbol in ["AAA", "BBB"]]
    for index, instrument in enumerate(instruments, start=1):
        BrokerContract.objects.create(instrument=instrument, conid=1000 + index)
    universe = PortfolioUniverse.objects.create(portfolio=portfolio, minimum_history_observations=30)
    for instrument in instruments:
        PortfolioUniverseInstrument.objects.create(universe=universe, instrument=instrument)
    policy = PortfolioOptimizationPolicy.objects.create(
        portfolio=portfolio,
        method="MINIMUM_VARIANCE",
        lookback_days=60,
        target_cash_weight="0.10",
        maximum_weight="0.80",
        maximum_turnover="1.00",
    )
    start = timezone.now().date() - timedelta(days=69)
    for offset in range(70):
        for index, instrument in enumerate(instruments):
            price = Decimal("100") * (Decimal("1.001") + Decimal(index) * Decimal("0.0005")) ** offset
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
    return portfolio, instruments, policy


def _allocated_strategy(portfolio, instrument, name, capital):
    instance=StrategyInstance.objects.create(name=name,definition=StrategyDefinition.objects.get(key="FIXED_WEIGHT_REBALANCE"),
        portfolio=portfolio,instrument=instrument,timeframe="1d",parameters={"direction":"LONG"},enabled=True,
        allocated_capital=capital)
    StrategyAllocation.objects.create(portfolio=portfolio,strategy_instance=instance,weight=1)
    return instance


def test_optimization_targets_use_existing_rebalance_and_sizing_pipeline():
    portfolio, instruments, _ = _optimization_case()
    RebalancePolicy.objects.create(
        portfolio=portfolio,
        maximum_turnover="2",
        minimum_trade_notional="1",
        fee_buffer="0",
        mode="PAPER",
    )
    optimization = run_optimization(portfolio, "optimization-1", refresh_history=False)
    assert optimization.status == "COMPLETED"
    assert optimization.targets.count() == 2
    assert sum(target.optimized_weight for target in optimization.targets.all()) == pytest.approx(Decimal("0.90"), abs=Decimal("0.000001"))
    rebalance = plan_optimized_rebalance(
        optimization,
        "optimized-rebalance-1",
        mode="PAPER",
        run_type=RunType.EXECUTION,
        strict_market_state=True,
    )
    assert rebalance.target_source == "PORTFOLIO_OPTIMIZATION"
    assert rebalance.optimization_run_id == optimization.pk
    assert OrderIntent.objects.filter(rebalance=rebalance).exists()
    assert PositionSizingDecision.objects.filter(order_intent__rebalance=rebalance).count() == OrderIntent.objects.filter(rebalance=rebalance).count()


def test_failed_optimization_requires_explicit_retry_and_preserves_attempts(monkeypatch):
    portfolio,_,_=_optimization_case()
    from apps.portfolio_optimization import services
    real=services.solve_markowitz
    calls={"count":0}
    def flaky(**kwargs):
        calls["count"]+=1
        if calls["count"]==1:raise RuntimeError("temporary solver process failure")
        return real(**kwargs)
    monkeypatch.setattr(services,"solve_markowitz",flaky)
    with pytest.raises(Exception,match="temporary solver process failure"):
        services.run_optimization(portfolio,"optimization-retry",refresh_history=False)
    stored=services.run_optimization(portfolio,"optimization-retry",refresh_history=False)
    assert stored.status=="FAILED" and calls["count"]==1
    completed=services.run_optimization(portfolio,"optimization-retry",refresh_history=False,retry_failed=True)
    assert completed.status=="COMPLETED" and calls["count"]==2
    attempts=list(OperationAttempt.objects.filter(operation_type="PORTFOLIO_OPTIMIZATION").order_by("attempt_number"))
    assert [(item.status,item.retryable) for item in attempts]==[("FAILED",True),("COMPLETED",False)]


def test_optimization_preview_api_queues_then_task_builds_metrics_targets_and_preview_trades(client):
    portfolio, _, _ = _optimization_case()
    RebalancePolicy.objects.create(portfolio=portfolio, maximum_turnover="2", minimum_trade_notional="1", fee_buffer="0")
    result = client.post(
        "/api/v1/portfolio-optimization/preview/",
        data={"portfolio_id": portfolio.pk, "refresh_history": False},
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="api-optimization-preview",
    )
    assert result.status_code == 202
    queued=result.json()["data"]
    assert queued["status"]=="QUEUED" and queued["targets"]==[]
    from apps.portfolio_optimization.tasks import execute_optimization_run
    execute_optimization_run.run(queued["id"],False,None,True)
    body=client.get(f"/api/v1/portfolio-optimization/runs/{queued['id']}/").json()["data"]
    assert body["status"] == "COMPLETED"
    assert body["expected_volatility"] is not None
    assert len(body["targets"]) == 2
    assert body["rebalance"]["mode"] == "PAPER"
    assert body["rebalance"]["run_type"] == "PREVIEW"
    assert body["planned_trades"]
    assert not OrderIntent.objects.exists()


@pytest.mark.parametrize("flow_type,amount,expected_nav,expected_cash", [
    ("DEPOSIT", "1000", Decimal("11000"), Decimal("11000")),
    ("WITHDRAWAL", "1000", Decimal("9000"), Decimal("9000")),
])
def test_deposits_and_withdrawals_recalculate_post_flow_optimized_weights(flow_type, amount, expected_nav, expected_cash):
    portfolio, instruments, _ = _optimization_case()
    RebalancePolicy.objects.create(
        portfolio=portfolio,
        maximum_turnover="2",
        minimum_trade_notional="1",
        fee_buffer="0",
    )
    strategy = _allocated_strategy(portfolio, instruments[0], f"Flow strategy {flow_type}", 10000)
    allocation = create_flow(
        portfolio,
        flow_type,
        amount,
        f"flow-{flow_type.lower()}",
        allocation_mode="PORTFOLIO_OPTIMIZATION",
    )
    strategy.refresh_from_db()
    assert allocation.optimization_run.status == "COMPLETED"
    assert allocation.optimization_run.nav == expected_nav
    assert Decimal(allocation.snapshot["post_flow_cash"]) == expected_cash
    rebalance = allocation.optimization_run.rebalances.get()
    assert rebalance.mode == "PAPER"
    assert rebalance.run_type == "EXECUTION"
    assert rebalance.target_source == "PORTFOLIO_OPTIMIZATION"
    assert OrderIntent.objects.filter(rebalance=rebalance).exists()
    assert strategy.allocated_capital == Decimal("10000")
    assert not AllocationDecision.objects.filter(run=allocation).exists()
    assert not StrategyCapitalSnapshot.objects.filter(allocation_run=allocation).exists()
    assert allocation.allocation_mode == "PORTFOLIO_OPTIMIZATION"
    assert allocation.approved_amount + allocation.unallocated_amount == Decimal(amount)
    assert allocation.status == ("COMPLETED" if allocation.unallocated_amount == 0 else "PARTIALLY_ALLOCATED")
    assert allocation.snapshot["optimization_run_id"] == allocation.optimization_run_id
    assert allocation.snapshot["rebalance_run_id"] == rebalance.pk


def test_auto_with_enabled_universe_and_policy_resolves_to_optimization():
    portfolio, instruments, _ = _optimization_case()
    RebalancePolicy.objects.create(portfolio=portfolio, maximum_turnover="2", minimum_trade_notional="1", fee_buffer="0")
    strategy = _allocated_strategy(portfolio, instruments[0], "Auto optimization strategy", 321)

    allocation = create_flow(portfolio, "DEPOSIT", "100", "flow-auto-optimization", allocation_mode="AUTO")

    strategy.refresh_from_db()
    assert allocation.allocation_mode == "PORTFOLIO_OPTIMIZATION"
    assert allocation.optimization_run_id is not None
    assert strategy.allocated_capital == Decimal("321")
    assert not allocation.decisions.exists()


def test_optimized_flow_http_returns_queued_run_and_celery_completes_it(client):
    portfolio, _, _ = _optimization_case()
    RebalancePolicy.objects.create(portfolio=portfolio,maximum_turnover="2",minimum_trade_notional="1",fee_buffer="0")
    result=client.post("/api/v1/allocations/flows/",data={"portfolio_id":portfolio.pk,"flow_type":"DEPOSIT",
        "amount":"100","allocation_mode":"PORTFOLIO_OPTIMIZATION"},content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="async-optimized-flow")
    assert result.status_code==202 and result.json()["data"]["status"]=="QUEUED"
    from apps.allocation.tasks import execute_flow_allocation_task
    execute_flow_allocation_task.run(result.json()["data"]["id"])
    allocation=client.get(f"/api/v1/allocations/runs/{result.json()['data']['id']}/").json()["data"]
    assert allocation["status"] in {"COMPLETED","PARTIALLY_ALLOCATED"}
    assert allocation["optimization_run_id"] is not None


def test_optimization_application_is_one_time_and_identical_retry_returns_existing(client):
    portfolio, _, _ = _optimization_case()
    RebalancePolicy.objects.create(portfolio=portfolio, maximum_turnover="2", minimum_trade_notional="1", fee_buffer="0")
    preview = client.post(
        "/api/v1/portfolio-optimization/preview/",
        data={"portfolio_id": portfolio.pk, "refresh_history": False},
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="preview-once",
    ).json()["data"]
    from apps.portfolio_optimization.tasks import apply_optimization_run_task, execute_optimization_run
    execute_optimization_run.run(preview["id"],False,None,True)
    payload = json.dumps({
        "optimization_run_id": preview["id"],
        "portfolio_id": portfolio.pk,
        "policy_id": preview["policy_id"],
        "universe_id": preview["universe_id"],
    })

    first = client.post(
        "/api/v1/portfolio-optimization/run/",
        data=payload,
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="apply-once",
    )
    assert first.status_code==202
    apply_optimization_run_task.run(preview["id"],"apply-once","PAPER")
    retry = client.post(
        "/api/v1/portfolio-optimization/run/",
        data=payload,
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="apply-once",
    )
    duplicate = client.post(
        "/api/v1/portfolio-optimization/run/",
        data=payload,
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="apply-again",
    )

    assert retry.status_code == 200
    assert first.json()["data"]["application_status"]=="QUEUED"
    assert retry.json()["data"]["applied_rebalance"]["id"]
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "OPTIMIZATION_ALREADY_APPLIED"
    run = PortfolioOptimizationRun.objects.get(pk=preview["id"])
    assert run.application_status == "APPLIED"
    assert run.rebalances.count() == 2
    assert run.applied_rebalance.mode == "PAPER"
    assert run.applied_rebalance.run_type == "EXECUTION"


def test_optimizer_apply_rechecks_policy_and_universe_readiness():
    portfolio, _, policy = _optimization_case()
    run = run_optimization(
        portfolio,
        "optimizer-readiness-preview",
        refresh_history=False,
    )
    policy.enabled = False
    policy.save(update_fields=["enabled", "updated_at"])

    from apps.portfolio_optimization.services import (
        OptimizationError,
        apply_optimization_run,
    )

    with pytest.raises(OptimizationError, match="must still be enabled"):
        apply_optimization_run(run, "optimizer-readiness-apply")

    run.refresh_from_db()
    assert run.applied_rebalance_id is None


def test_universe_size_is_rejected_on_save_and_revalidated_before_optimization(client):
    portfolio, instruments, _ = _optimization_case()
    extra = Instrument.objects.create(symbol="CCC")
    response = client.post(
        "/api/v1/portfolio-universe/",
        data=json.dumps({
            "portfolio_id": portfolio.pk,
            "instrument_ids": [instrument.pk for instrument in instruments] + [extra.pk],
            "maximum_instruments": 2,
        }),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "UNIVERSE_SIZE_EXCEEDED"
    assert response.json()["error"]["details"] == {"selected_count": 3, "maximum_instruments": 2}

    universe = PortfolioUniverse.objects.get(portfolio=portfolio)
    universe.maximum_instruments = 2
    universe.save(update_fields=["maximum_instruments"])
    PortfolioUniverseInstrument.objects.create(universe=universe, instrument=extra)
    with pytest.raises(UniverseSizeError, match="3.*2"):
        run_optimization(portfolio, "oversized-external-universe", refresh_history=False)


def test_portfolio_mutations_require_csrf_and_optimization_keeps_idempotency_protection():
    portfolio, _, _ = _optimization_case()
    browser = Client(enforce_csrf_checks=True)
    browser.get("/api/v1/system/")
    token = browser.cookies["csrftoken"].value
    blocked = browser.post(
        "/api/v1/portfolio-optimization/preview/",
        data=json.dumps({"portfolio_id": portfolio.pk, "refresh_history": False}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="csrf-preview",
    )
    missing_key = browser.post(
        "/api/v1/portfolio-optimization/preview/",
        data=json.dumps({"portfolio_id": portfolio.pk, "refresh_history": False}),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )
    universe_blocked = browser.post(
        "/api/v1/portfolio-universe/",
        data=json.dumps({"portfolio_id": portfolio.pk, "instrument_ids": []}),
        content_type="application/json",
    )
    assert blocked.status_code == 403
    assert universe_blocked.status_code == 403
    assert missing_key.status_code == 400
    assert missing_key.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
