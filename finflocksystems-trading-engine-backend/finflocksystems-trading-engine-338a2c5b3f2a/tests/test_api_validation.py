import json

import pytest

from apps.accounts.models import BrokerAccount
from apps.allocation.models import AllocationRun
from apps.instruments.models import Instrument
from apps.portfolios.models import TradingPortfolio
from apps.position_sizing.models import PositionSizingDecision
from apps.risk.models import KillSwitch


pytestmark=pytest.mark.django_db


@pytest.mark.parametrize("path",[
    "/healthz",
    "/api/v1/system/",
    "/api/v1/accounts/",
    "/api/v1/streaming/health/",
    "/api/v1/allocations/policies/",
    "/api/v1/rebalancing/runs/",
    "/api/v1/portfolio-optimization/runs/",
    "/api/v1/strategy-definitions/",
    "/api/v1/dashboard/summary/",
])
def test_read_endpoints_reject_unsupported_methods(client,path):
    result=client.post(path,data="{}",content_type="application/json")
    assert result.status_code==405
    assert result.json()["error"]["code"]=="METHOD_NOT_ALLOWED"


def test_kill_switch_request_validates_key_scope_relationship_and_boolean(client):
    no_key=client.post("/api/v1/risk/",data="{}",content_type="application/json")
    assert no_key.status_code==400 and no_key.json()["error"]["code"]=="IDEMPOTENCY_KEY_REQUIRED"
    for key,payload in [
        ("missing-account",{"scope":"ACCOUNT","scope_id":"not-an-account","enabled":True}),
        ("global-id",{"scope":"GLOBAL","scope_id":"unexpected","enabled":True}),
        ("bad-boolean",{"scope":"GLOBAL","enabled":"false"}),
    ]:
        result=client.post("/api/v1/risk/",json.dumps(payload),content_type="application/json",HTTP_IDEMPOTENCY_KEY=key)
        assert result.status_code==400 and result.json()["error"]["code"]=="INVALID_RISK_REQUEST"
    account=BrokerAccount.objects.create(account_id="DU-RISK")
    portfolio=TradingPortfolio.objects.create(name="Scoped",account=account)
    valid=client.post("/api/v1/risk/",json.dumps({"scope":"PORTFOLIO","scope_id":str(portfolio.pk),"enabled":True}),
        content_type="application/json",HTTP_IDEMPOTENCY_KEY="portfolio-switch")
    assert valid.status_code==200
    assert KillSwitch.objects.get().scope_id==str(portfolio.pk)


def test_flow_sizing_and_rebalance_reject_invalid_decimals_before_work(client):
    account=BrokerAccount.objects.create(account_id="DU-VALIDATION",net_liquidation=10000,available_cash=10000)
    portfolio=TradingPortfolio.objects.create(name="Validation",account=account)
    instrument=Instrument.objects.create(symbol="STRICT")
    flow=client.post("/api/v1/allocations/flows/",json.dumps({"portfolio_id":portfolio.pk,"flow_type":"DEPOSIT",
        "amount":"1.000000001"}),content_type="application/json",HTTP_IDEMPOTENCY_KEY="invalid-flow")
    sizing=client.post("/api/v1/position-sizing/preview/",json.dumps({"portfolio_id":portfolio.pk,
        "instrument_id":instrument.pk,"target_quantity":"0","entry_price":"10"}),content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="invalid-sizing")
    rebalance=client.post("/api/v1/rebalancing/preview/",json.dumps({"portfolio_id":portfolio.pk,
        "prices":{str(instrument.pk):"0"}}),content_type="application/json",HTTP_IDEMPOTENCY_KEY="invalid-rebalance")
    assert flow.status_code==sizing.status_code==rebalance.status_code==400
    assert AllocationRun.objects.count()==PositionSizingDecision.objects.count()==0
