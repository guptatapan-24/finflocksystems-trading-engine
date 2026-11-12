from decimal import Decimal
import pytest
from apps.audit.models import OperationAttempt
from apps.accounts.models import BrokerAccount
from apps.allocation.models import AllocationDecision, StrategyCapitalSnapshot
from apps.allocation.services import allocate_deposit, allocate_withdrawal, create_flow
from apps.instruments.models import Instrument
from apps.portfolios.models import TradingPortfolio
from apps.risk.models import CapitalReservation
from apps.strategies.models import StrategyAllocation, StrategyDefinition, StrategyInstance
from tests.managed_gateway import bind_gateway_mode

pytestmark=pytest.mark.django_db


def rows():
    return [{"id":1,"enabled":True,"target_share":"0.6","current":"400","maximum_share":"0.7","capacity":"1000","priority":1},
            {"id":2,"enabled":True,"target_share":"0.4","current":"500","maximum_share":"0.5","capacity":"1000","priority":2},
            {"id":3,"enabled":False,"target_share":"0.1","current":"0"}]


def allocated_strategy(portfolio, name, capital=0):
    instrument=Instrument.objects.create(symbol=f"ALLOC-{Instrument.objects.count()+1}",exchange="SMART")
    instance=StrategyInstance.objects.create(name=name,definition=StrategyDefinition.objects.get(key="FIXED_WEIGHT_REBALANCE"),
        portfolio=portfolio,instrument=instrument,timeframe="1d",parameters={"direction":"LONG"},enabled=True,
        allocated_capital=capital)
    StrategyAllocation.objects.create(portfolio=portfolio,strategy_instance=instance,weight=1)
    return instance


def test_deposit_is_deficit_weighted_capped_and_exact():
    allocated,remainder,computed=allocate_deposit("100","900",rows())
    assert allocated["1"]==Decimal("100.00") and allocated["2"]==0 and "3" not in allocated
    assert sum(allocated.values())+remainder==Decimal("100.00")


def test_zero_deficit_falls_back_to_target_shares():
    data=[{"id":1,"target_share":"0.6","current":"1000","maximum_share":"2","capacity":"2000"},{"id":2,"target_share":"0.4","current":"1000","maximum_share":"2","capacity":"2000"}]
    allocated,remainder,_=allocate_deposit("100","1000",data)
    assert allocated=={"1":Decimal("60.00"),"2":Decimal("40.00")} and remainder==0


def test_withdrawal_uses_cash_then_idle_surplus_and_liquidation():
    data=[{"id":1,"target_share":"0.5","current":"400","idle_cash":"50","priority":1},{"id":2,"target_share":"0.5","current":"300","idle_cash":"0","priority":2}]
    decisions=allocate_withdrawal("300","1000","100",data)
    assert [x["source"] for x in decisions[:3]]==["PORTFOLIO_CASH","STRATEGY_CASH","STRATEGY_SURPLUS"]
    assert any(x["source"]=="POSITION_LIQUIDATION" for x in decisions)
    assert sum(x["amount"] for x in decisions)==Decimal("300.00")


def test_flow_retry_is_idempotent_and_reserves_cash():
    account=BrokerAccount.objects.create(account_id="DU1",net_liquidation=1000,available_cash=0)
    portfolio=TradingPortfolio.objects.create(name="P",account=account,cash_buffer_pct="0.10")
    bind_gateway_mode(portfolio)
    strategy=allocated_strategy(portfolio,"S")
    first=create_flow(portfolio,"DEPOSIT",200,"flow-1"); second=create_flow(portfolio,"DEPOSIT",200,"flow-1")
    assert first.pk==second.pk and first.unallocated_amount==Decimal("120.00")
    assert AllocationDecision.objects.count()==1
    strategy.refresh_from_db()
    assert strategy.allocated_capital == Decimal("80.00")
    assert StrategyCapitalSnapshot.objects.filter(allocation_run=first).count() == 1


def test_auto_without_enabled_optimization_configuration_uses_strategy_allocation():
    account=BrokerAccount.objects.create(account_id="DU-AUTO",net_liquidation=1000,available_cash=1000)
    portfolio=TradingPortfolio.objects.create(name="Auto strategy",account=account)
    bind_gateway_mode(portfolio)
    strategy=allocated_strategy(portfolio,"Auto S",100)

    run=create_flow(portfolio,"DEPOSIT",100,"flow-auto-strategy",allocation_mode="AUTO")

    strategy.refresh_from_db()
    assert run.allocation_mode == "STRATEGY_ALLOCATION"
    assert run.optimization_run_id is None
    assert strategy.allocated_capital > Decimal("100")
    assert run.decisions.exists()


def test_pending_withdrawal_reserves_cash_for_other_operations():
    account=BrokerAccount.objects.create(account_id="DU-WITHDRAW",net_liquidation=1000,available_cash=300)
    portfolio=TradingPortfolio.objects.create(name="Withdrawal",account=account)
    bind_gateway_mode(portfolio)
    run=create_flow(portfolio,"WITHDRAWAL",200,"withdrawal-reservation")
    reservation=CapitalReservation.objects.get(reference_type="PORTFOLIO_FLOW",reference_id=str(run.flow_id))
    assert reservation.amount==200 and reservation.status=="ACTIVE"


def test_failed_flow_requires_explicit_retry_and_preserves_attempt_history(monkeypatch):
    account=BrokerAccount.objects.create(account_id="DU-RETRY",net_liquidation=1000,available_cash=1000)
    portfolio=TradingPortfolio.objects.create(name="Retry",account=account)
    bind_gateway_mode(portfolio)
    allocated_strategy(portfolio,"Retry S")
    from apps.allocation import services
    real=services.create_strategy_flow_allocation
    calls={"count":0}
    def flaky(run):
        calls["count"]+=1
        if calls["count"]==1:raise RuntimeError("temporary allocator failure")
        return real(run)
    monkeypatch.setattr(services,"create_strategy_flow_allocation",flaky)
    with pytest.raises(RuntimeError,match="temporary allocator failure"):
        services.create_flow(portfolio,"DEPOSIT",100,"retry-flow")
    stored=services.create_flow(portfolio,"DEPOSIT",100,"retry-flow")
    assert stored.status=="FAILED" and calls["count"]==1
    retried=services.create_flow(portfolio,"DEPOSIT",100,"retry-flow",retry_failed=True)
    assert retried.status=="COMPLETED" and calls["count"]==2
    attempts=list(OperationAttempt.objects.filter(operation_type="PORTFOLIO_FLOW").order_by("attempt_number"))
    assert [(item.status,item.retryable) for item in attempts]==[("FAILED",True),("COMPLETED",False)]
