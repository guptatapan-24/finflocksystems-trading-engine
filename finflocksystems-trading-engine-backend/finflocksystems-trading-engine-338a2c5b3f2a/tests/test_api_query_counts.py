import pytest

from apps.accounts.models import BrokerAccount
from apps.allocation.models import RebalanceRun
from apps.instruments.models import Instrument
from apps.oms.models import Order, OrderIntent
from apps.portfolios.models import PortfolioPosition, TradingPortfolio
from apps.strategies.models import StrategyAllocation, StrategyDefinition, StrategyInstance, StrategyVersion


pytestmark=pytest.mark.django_db


@pytest.fixture
def api_rows():
    account=BrokerAccount.objects.create(account_id="DU-QUERY",net_liquidation=1000,available_cash=1000)
    portfolio=TradingPortfolio.objects.create(name="Query portfolio",account=account)
    definition=StrategyDefinition.objects.get(key="FIXED_WEIGHT_REBALANCE")
    for index in range(5):
        instrument=Instrument.objects.create(symbol=f"QUERY{index}",exchange="SMART")
        instance=StrategyInstance.objects.create(name=f"Query strategy {index}",definition=definition,portfolio=portfolio,
            instrument=instrument,timeframe="1d",parameters={"direction":"LONG"})
        StrategyVersion.objects.create(strategy_instance=instance,version=1,configuration_snapshot={},parameter_hash=f"query-{index}")
        StrategyAllocation.objects.create(strategy_instance=instance,portfolio=portfolio,weight="0.20",priority=index)
        PortfolioPosition.objects.create(portfolio=portfolio,instrument=instrument,quantity=1,market_price=10)
        intent=OrderIntent.objects.create(portfolio=portfolio,instrument=instrument,side="BUY",quantity=1,
            idempotency_key=f"query-intent-{index}")
        Order.objects.create(intent=intent,internal_id=f"query-order-{index}",quantity=1)
        RebalanceRun.objects.create(portfolio=portfolio,trigger="QUERY",idempotency_key=f"query-rebalance-{index}")
    return portfolio


def test_strategy_list_query_count_does_not_scale_with_rows(client,api_rows,django_assert_max_num_queries):
    with django_assert_max_num_queries(1):
        result=client.get(f"/api/v1/strategy-instances/?portfolio={api_rows.pk}")
    assert result.status_code==200 and len(result.json()["data"])==5


def test_order_list_query_count_is_count_plus_page(client,api_rows,django_assert_max_num_queries):
    with django_assert_max_num_queries(2):
        result=client.get(f"/api/v1/orders/?portfolio={api_rows.pk}")
    assert result.status_code==200 and len(result.json()["data"])==5


@pytest.mark.parametrize("path",["positions/","allocations/policies/","rebalances/"])
def test_portfolio_allocation_and_rebalance_lists_are_single_query(client,api_rows,path,django_assert_max_num_queries):
    with django_assert_max_num_queries(1):
        result=client.get(f"/api/v1/{path}")
    assert result.status_code==200 and len(result.json()["data"])==5


def test_strategy_detail_has_a_fixed_query_budget(client,api_rows,django_assert_max_num_queries):
    instance=StrategyInstance.objects.filter(portfolio=api_rows).first()
    with django_assert_max_num_queries(3):
        result=client.get(f"/api/v1/strategy-instances/{instance.pk}/")
    assert result.status_code==200 and result.json()["data"]["id"]==instance.pk
