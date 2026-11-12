from datetime import timedelta

import pytest
import responses
from django.utils import timezone

from apps.accounts.models import BrokerAccount
from apps.instruments.models import BrokerContract, Instrument
from apps.market_streams.models import IndicatorValue, MarketBar
from apps.oms.models import Order, OrderIntent
from apps.portfolios.models import PortfolioPosition, TradingPortfolio
from apps.strategies.framework import create_instance, evaluate_instance
from tests.managed_gateway import bind_managed_gateway
from tests.strategy_activation import activate_strategy

pytestmark = pytest.mark.django_db


@pytest.fixture
def portfolio(settings):
    account = BrokerAccount.objects.create(
        account_id="DU-ANALYTICS", net_liquidation=1100, available_cash=100,
        buying_power=2000, daily_pnl=25, is_reconciled=True,
    )
    portfolio = TradingPortfolio.objects.create(name="Analytics", account=account)
    bind_managed_gateway(portfolio, settings)
    return portfolio


@pytest.fixture
def instrument(portfolio):
    item = Instrument.objects.create(symbol="PORTABLE", exchange="SMART")
    BrokerContract.objects.create(instrument=item, conid=991, primary_exchange="NASDAQ", local_symbol="PORTABLE")
    PortfolioPosition.objects.create(portfolio=portfolio, instrument=item, quantity=10, average_cost=80, market_price=100)
    return item


def make_bar(instrument, bar_id, when, close):
    return MarketBar.objects.create(
        instrument=instrument, bar_id=bar_id, interval="5m", window_start=when-timedelta(minutes=5),
        window_end=when, open=close-1, high=close+1, low=close-2, close=close, volume=1000,
        version=1, is_final=True, source_event_count=1, produced_at=when,
    )


@responses.activate
def test_dashboard_summary_is_portfolio_scoped_and_partial_gateway_safe(client, portfolio, instrument):
    responses.get(f"{portfolio.gateway_session.internal_base_url}/health/", json={
        "ok": True, "data": {"connected": True, "reconciled": True, "mode": "paper"}, "error": None, "meta": {},
    })
    body = client.get(f"/api/v1/dashboard/summary/?portfolio={portfolio.pk}").json()
    assert body["ok"]
    assert body["data"]["portfolio"]["id"] == portfolio.pk
    assert body["data"]["gross_exposure"] == "1000.0000000000000000"
    assert body["data"]["gateway"]["connected"] is True
    assert body["data"]["mode"] == "PAPER"


def test_portfolio_series_uses_persisted_bars_and_current_holdings(client, portfolio, instrument):
    now = timezone.now()
    make_bar(instrument, "portable-1", now-timedelta(minutes=5), 90)
    make_bar(instrument, "portable-2", now, 100)
    body = client.get(f"/api/v1/portfolios/series/?portfolio={portfolio.pk}").json()
    assert body["ok"] and body["data"]["source"] == "POSTGRES_MARKET_BARS_WITH_CURRENT_HOLDINGS"
    assert [point["value"] for point in body["data"]["nav"]] == [1000.0, 1100.0]
    assert body["data"]["allocation_by_instrument"][0]["symbol"] == "PORTABLE"


def test_strategy_chart_maps_persisted_market_and_strategy_facts(client, portfolio, instrument):
    now = timezone.now()
    bar = make_bar(instrument, "chart-bar", now, 100)
    instance, _ = create_instance(
        name="PORTABLE_FIXED", definition_key="FIXED_WEIGHT_REBALANCE", portfolio=portfolio,
        instrument_id=instrument.pk, timeframe="5m", parameters={"direction": "LONG"},
        target_configuration={"target_weight": "0.10"}, qualify=False,
    )
    activate_strategy(instance)
    IndicatorValue.objects.create(
        instrument=instrument, bar=bar, indicator="sma", indicator_name="sma", indicator_role="",
        implementation_version=1, requirement_identity_hash="c"*64, value=99, parameters={"window": 2},
        timeframe="5m", source_bar_id=bar.bar_id, source_bar_version=1,
        event_time=now, source_key="chart-indicator",
    )
    evaluate_instance(instance, bar={"bar_id": bar.bar_id, "close": "100", "is_final": True}, indicators={}, event_id="chart-evaluation", event_time=now)
    body = client.get(f"/api/v1/strategy-instances/{instance.pk}/chart/").json()
    assert body["ok"] and body["data"]["source"] == "POSTGRES_MARKET_AND_EXECUTION_FACTS"
    assert body["data"]["bars"][0]["close"] == "100.00000000"
    assert body["data"]["indicators"][0]["name"] == "sma"
    assert {marker["type"] for marker in body["data"]["markers"]} >= {"SIGNAL", "TARGET"}


def test_order_filters_keep_array_envelope_and_add_pagination_meta(client, portfolio, instrument):
    for index, status in enumerate(["QUEUED", "FILLED"]):
        intent = OrderIntent.objects.create(
            portfolio=portfolio, instrument=instrument, side="BUY", quantity=1,
            idempotency_key=f"filter-{index}", reference_price=100,
        )
        Order.objects.create(intent=intent, internal_id=f"filtered-{index}", status=status, quantity=1)
    body = client.get(f"/api/v1/orders/?portfolio={portfolio.pk}&status=FILLED&limit=1").json()
    assert body["ok"] and isinstance(body["data"], list) and len(body["data"]) == 1
    assert body["data"][0]["status"] == "FILLED"
    assert body["meta"] == {"count": 1, "limit": 1, "offset": 0}


def test_slashless_portfolio_series_and_orders_are_supported(client, portfolio, instrument):
    intent = OrderIntent.objects.create(
        portfolio=portfolio, instrument=instrument, side="BUY", quantity=1,
        idempotency_key="slashless-order", reference_price=100,
    )
    Order.objects.create(intent=intent, internal_id="slashless-order", status="QUEUED", quantity=1)

    series = client.get(f"/api/v1/portfolios/series?portfolio={portfolio.pk}")
    orders = client.get("/api/v1/orders?limit=250")

    assert series.status_code == 200, series.content
    assert series.json()["ok"]
    assert orders.status_code == 200, orders.content
    assert orders.json()["data"][0]["internal_id"] == "slashless-order"
