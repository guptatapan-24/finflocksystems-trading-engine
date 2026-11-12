import pytest

from apps.accounts.models import BrokerAccount
from apps.instruments.models import BrokerContract, Instrument
from apps.portfolios.models import PortfolioPosition, TradingPortfolio
from apps.reconciliation.services import reconcile
from tests.managed_gateway import bind_gateway_mode

pytestmark=pytest.mark.django_db


class FakeGateway:
    def __init__(self, positions=None, executions=None, *, connected=False, reconciled=False):
        self._positions=positions or []
        self._executions=executions or []
        self._health={"connected":connected,"reconciled":reconciled}
    def health(self): return self._health
    def positions(self): return self._positions
    def executions(self): return self._executions


def account_position(account_id, conid, quantity):
    return {"account":account_id,"conid":conid,"quantity":str(quantity)}


def create_account(account_id, quantity=0):
    account=BrokerAccount.objects.create(account_id=account_id)
    portfolio=TradingPortfolio.objects.create(account=account,name=account_id)
    return account,portfolio


def test_disconnected_gateway_creates_material_break_for_identified_account():
    account,_=create_account("DU-A")
    run=reconcile("test",FakeGateway(),broker_account=account)
    assert run.broker_account==account
    assert run.status=="BLOCKED" and run.breaks.filter(material=True).exists()


def test_clean_run_resolves_only_prior_breaks_for_the_same_account():
    account_a,_=create_account("DU-A")
    account_b,_=create_account("DU-B")
    first_a=reconcile("disconnect-a",FakeGateway(),broker_account=account_a)
    first_b=reconcile("disconnect-b",FakeGateway(),broker_account=account_b)
    healthy=FakeGateway(connected=True,reconciled=True)
    second=reconcile("recovered-a",healthy,broker_account=account_a)
    assert second.status=="COMPLETED"
    assert first_a.breaks.filter(material=True,resolved=True).exists()
    assert first_b.breaks.filter(material=True,resolved=False).exists()
    account_b.refresh_from_db()
    assert account_b.is_reconciled is False


def test_clean_session_run_resolves_legacy_unscoped_break_for_same_account():
    account,portfolio=create_account("DU-LEGACY")
    legacy=reconcile("legacy-disconnect",FakeGateway(),broker_account=account)
    session=bind_gateway_mode(portfolio,mode="live")

    clean=reconcile(
        "session-recovered",FakeGateway(connected=True,reconciled=True),
        broker_account=account,gateway_session=session,
    )

    assert clean.status=="COMPLETED"
    assert legacy.breaks.filter(material=True,resolved=True).exists()


def test_same_contract_is_compared_by_broker_account():
    account_a,portfolio_a=create_account("DU-A")
    account_b,portfolio_b=create_account("DU-B")
    instrument=Instrument.objects.create(symbol="AAPL")
    BrokerContract.objects.create(instrument=instrument,conid=101)
    PortfolioPosition.objects.create(portfolio=portfolio_a,instrument=instrument,quantity=5)
    PortfolioPosition.objects.create(portfolio=portfolio_b,instrument=instrument,quantity=9)
    gateway=FakeGateway(
        positions=[account_position("DU-A",101,5),account_position("DU-B",101,1)],
        connected=True,reconciled=True,
    )

    run_a=reconcile("account-a",gateway,broker_account=account_a)
    run_b=reconcile("account-b",gateway,broker_account=account_b)

    assert run_a.status=="COMPLETED" and not run_a.breaks.filter(category="POSITION").exists()
    assert run_b.status=="BLOCKED" and run_b.breaks.filter(category="POSITION",material=True).exists()


def test_missing_execution_break_is_account_specific():
    account_a,_=create_account("DU-A")
    account_b,_=create_account("DU-B")
    gateway=FakeGateway(
        executions=[
            {"account":"DU-A","execution_id":"EXEC-A"},
            {"account":"DU-B","execution_id":"EXEC-B"},
        ],
        connected=True,reconciled=True,
    )

    run=reconcile("executions-a",gateway,broker_account=account_a)

    broker_values=list(run.breaks.filter(category="EXECUTION").values_list("broker_value",flat=True))
    assert broker_values==[{"account":"DU-A","execution_id":"EXEC-A"}]
    account_b.refresh_from_db()
    assert account_b.is_reconciled is False
