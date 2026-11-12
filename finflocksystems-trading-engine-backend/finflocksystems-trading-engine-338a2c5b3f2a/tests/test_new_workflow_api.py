import json
import pytest
from django.test import override_settings
from django.test import Client
from apps.accounts.models import BrokerAccount
from apps.allocation.models import RebalanceRun
from apps.broker_gateway.models import BrokerGatewaySession
from apps.instruments.models import Instrument
from apps.portfolios.models import TradingPortfolio

pytestmark=pytest.mark.django_db


@pytest.fixture
def portfolio():
    account=BrokerAccount.objects.create(account_id="DU-API",net_liquidation=10000,available_cash=5000)
    session=BrokerGatewaySession.objects.create(
        display_name="API Paper",
        username_hint="api",
        mode="paper",
        child_container_name="api-paper-gateway",
        encrypted_gateway_token="test",
        encrypted_novnc_password="test",
    )
    return TradingPortfolio.objects.create(name="API",account=account,gateway_session=session)


@override_settings(KAFKA_ENABLED=False,FLINK_REST_URL="http://127.0.0.1:1")
def test_streaming_health_topics_and_metrics(client):
    assert client.get("/api/v1/streaming/health/").status_code==200
    assert client.get("/api/v1/streaming/topics/").json()["data"]
    assert client.get("/api/v1/streaming/dead-letter/").status_code==200
    assert client.get("/metrics").status_code==200


def test_flow_api_requires_key_and_is_idempotent(client,portfolio):
    payload={"portfolio_id":portfolio.pk,"flow_type":"DEPOSIT","amount":"100"}
    assert client.post("/api/v1/allocations/flows/",json.dumps(payload),content_type="application/json").status_code==400
    first=client.post("/api/v1/allocations/flows/",json.dumps(payload),content_type="application/json",HTTP_IDEMPOTENCY_KEY="flow-api")
    second=client.post("/api/v1/allocations/flows/",json.dumps(payload),content_type="application/json",HTTP_IDEMPOTENCY_KEY="flow-api")
    assert first.status_code==201 and second.json()["data"]["id"]==first.json()["data"]["id"]
    payload["amount"]="101"
    conflict=client.post("/api/v1/allocations/flows/",json.dumps(payload),content_type="application/json",HTTP_IDEMPOTENCY_KEY="flow-api")
    assert conflict.status_code==400 and "different request" in conflict.json()["error"]["message"]


def test_flow_and_rebalance_browser_mutations_require_csrf(portfolio):
    browser=Client(enforce_csrf_checks=True)
    browser.get("/api/v1/system/")
    token=browser.cookies["csrftoken"].value
    flow_payload=json.dumps({"portfolio_id":portfolio.pk,"flow_type":"DEPOSIT","amount":"100"})
    assert browser.post("/api/v1/allocations/flows/",flow_payload,content_type="application/json",HTTP_IDEMPOTENCY_KEY="csrf-flow").status_code==403
    accepted=browser.post("/api/v1/allocations/flows/",flow_payload,content_type="application/json",HTTP_IDEMPOTENCY_KEY="csrf-flow",HTTP_X_CSRFTOKEN=token)
    assert accepted.status_code==201

    instrument=Instrument.objects.create(symbol="CSRF")
    rebalance_payload=json.dumps({"portfolio_id":portfolio.pk,"prices":{str(instrument.pk):"10"}})
    assert browser.post("/api/v1/rebalancing/preview/",rebalance_payload,content_type="application/json",HTTP_IDEMPOTENCY_KEY="csrf-rebalance").status_code==403
    accepted_rebalance=browser.post("/api/v1/rebalancing/preview/",rebalance_payload,content_type="application/json",HTTP_IDEMPOTENCY_KEY="csrf-rebalance",HTTP_X_CSRFTOKEN=token)
    assert accepted_rebalance.status_code==202


def test_rebalance_preview_and_sizing_preview_never_create_orders(client,portfolio):
    instrument=Instrument.objects.create(symbol="API")
    rebalance=client.post("/api/v1/rebalancing/preview/",json.dumps({"portfolio_id":portfolio.pk,"prices":{str(instrument.pk):"10"}}),
        content_type="application/json",HTTP_IDEMPOTENCY_KEY="preview-api")
    assert rebalance.status_code==202 and rebalance.json()["data"]["mode"]=="PAPER"
    assert rebalance.json()["data"]["run_type"]=="PREVIEW"
    assert rebalance.json()["data"]["status"]=="QUEUED"
    from apps.rebalancing.tasks import execute_rebalance_run
    execute_rebalance_run.run(portfolio.pk,"MANUAL","preview-api",{instrument.pk:"10"},None,"PAPER",False,None,"PREVIEW")
    sizing=client.post("/api/v1/position-sizing/preview/",json.dumps({"portfolio_id":portfolio.pk,"instrument_id":instrument.pk,
        "target_quantity":"10","entry_price":"10","stop_price":"9","adv":"10000"}),content_type="application/json",HTTP_IDEMPOTENCY_KEY="size-api")
    assert sizing.status_code==201 and "binding_constraint" in sizing.json()["data"]
    from apps.oms.models import Order,OrderIntent
    assert not Order.objects.exists() and not OrderIntent.objects.exists()


def test_async_rebalance_failure_is_visible_and_requires_explicit_retry(client,portfolio,monkeypatch):
    payload=json.dumps({"portfolio_id":portfolio.pk,"trigger":"MANUAL","prices":{}})
    queued=client.post("/api/v1/rebalancing/preview/",payload,content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="async-rebalance-failure")
    assert queued.status_code==202
    from apps.rebalancing import tasks
    def fail(*args,**kwargs):raise RuntimeError("planner unavailable")
    monkeypatch.setattr(tasks,"plan_rebalance",fail)
    with pytest.raises(RuntimeError,match="planner unavailable"):
        tasks.execute_rebalance_run.run(portfolio.pk,"MANUAL","async-rebalance-failure",None,None,"PAPER",True,None,"PREVIEW")
    stored=RebalanceRun.objects.get(pk=queued.json()["data"]["id"])
    assert stored.status=="FAILED" and stored.retryable and "planner unavailable" in stored.last_error
    no_retry=client.post("/api/v1/rebalancing/preview/",payload,content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="async-rebalance-failure")
    assert no_retry.status_code==409
    retry=client.post("/api/v1/rebalancing/preview/",payload,content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="async-rebalance-failure",HTTP_IDEMPOTENCY_RETRY="true")
    assert retry.status_code==202 and retry.json()["data"]["status"]=="QUEUED"


def test_rebalance_same_key_with_different_request_conflicts(client,portfolio):
    instrument=Instrument.objects.create(symbol="HASH")
    first_payload={"portfolio_id":portfolio.pk,"prices":{str(instrument.pk):"10"}}
    second_payload={"portfolio_id":portfolio.pk,"prices":{str(instrument.pk):"11"}}
    first=client.post("/api/v1/rebalancing/preview/",json.dumps(first_payload),content_type="application/json",HTTP_IDEMPOTENCY_KEY="rebalance-hash")
    second=client.post("/api/v1/rebalancing/preview/",json.dumps(second_payload),content_type="application/json",HTTP_IDEMPOTENCY_KEY="rebalance-hash")
    assert first.status_code==202 and second.status_code==400
    assert "different request" in second.json()["error"]["message"]
