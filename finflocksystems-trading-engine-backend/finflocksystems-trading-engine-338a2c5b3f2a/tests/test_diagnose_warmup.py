from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.accounts.models import BrokerAccount
from apps.event_bus.models import StreamHealthMetric
from apps.instruments.models import BrokerContract, Instrument
from apps.market_streams.models import MarketDataSubscription
from apps.portfolios.models import TradingPortfolio
from apps.strategies.framework import create_instance
from apps.strategies.models import StrategyInstance
from apps.strategies.option_templates import AUTO_OPTION_STRATEGY_KEY
from tests.managed_gateway import bind_gateway_mode

pytestmark = pytest.mark.django_db


@pytest.fixture
def instance():
    account = BrokerAccount.objects.create(account_id="DU-DIAG", net_liquidation=1, available_cash=1, buying_power=1)
    portfolio = TradingPortfolio.objects.create(name="Diag", account=account, minimum_notional=1)
    bind_gateway_mode(portfolio)
    item = Instrument.objects.create(symbol="NIFTY50", exchange="NSE", currency="INR", asset_class="IND")
    BrokerContract.objects.create(instrument=item, conid=99, primary_exchange="NSE", local_symbol="NIFTY50")
    created, _ = create_instance(name="DIAG", definition_key=AUTO_OPTION_STRATEGY_KEY, portfolio=portfolio, instrument_id=item.pk,
                                 timeframe="5m", parameters={}, target_configuration={"target_weight": 0}, qualify=False)
    return created


def run(*args):
    out = StringIO()
    call_command("diagnose_warmup", *args, stdout=out)
    return out.getvalue()


def test_reports_a_missing_consumer_and_a_missing_subscription(instance):
    text = run(str(instance.pk))
    assert "no fresh heartbeat" in text
    assert "VERDICT: no subscription was created" in text


def test_a_fresh_consumer_heartbeat_is_not_flagged(instance):
    StreamHealthMetric.objects.create(component="backend-market-consumer", metric="heartbeat", status="HEALTHY", value={})
    assert "no fresh heartbeat" not in run(str(instance.pk))


def test_a_stale_consumer_heartbeat_is_flagged(instance):
    metric = StreamHealthMetric.objects.create(component="backend-market-consumer", metric="heartbeat", status="HEALTHY", value={})
    StreamHealthMetric.objects.filter(pk=metric.pk).update(observed_at=timezone.now() - timedelta(minutes=10))
    assert "no fresh heartbeat" in run(str(instance.pk))


def test_a_subscription_that_never_received_an_event_is_named(instance):
    MarketDataSubscription.objects.create(
        gateway_session=instance.portfolio.gateway_session, instrument=instance.instrument, timeframe="5m",
        conid=99, state="ACTIVE", requested_at=timezone.now(),
    )
    text = run(str(instance.pk))
    assert "never delivered an event" in text


def test_it_writes_nothing(instance):
    before = (StrategyInstance.objects.get(pk=instance.pk).state, StreamHealthMetric.objects.count())
    run()
    assert (StrategyInstance.objects.get(pk=instance.pk).state, StreamHealthMetric.objects.count()) == before
