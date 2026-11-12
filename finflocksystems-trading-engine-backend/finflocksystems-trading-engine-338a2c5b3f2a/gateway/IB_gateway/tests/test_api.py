import json
import pytest
from gateway_service.models import GatewayCommand, GatewayEvent

pytestmark=pytest.mark.django_db
AUTH={"HTTP_AUTHORIZATION":"Bearer test-token"}

def test_service_authentication(client):
    assert client.get("/api/v1/health/").status_code==401
    body=client.get("/api/v1/health/",**AUTH).json()
    assert body["ok"] and "connected" in body["data"]

def test_order_command_idempotency(client):
    payload={"internal_id":"I1","account":"DU1","symbol":"AAPL","local_symbol":"AAPL",
        "conid":265598,"primary_exchange":"NASDAQ","side":"BUY","quantity":"1"}
    headers={**AUTH,"HTTP_IDEMPOTENCY_KEY":"same","content_type":"application/json"}
    first=client.post("/api/v1/orders/",json.dumps(payload),**headers).json()
    second=client.post("/api/v1/orders/",json.dumps(payload),**headers).json()
    assert first["data"]["command_id"]==second["data"]["command_id"] and GatewayCommand.objects.count()==1


def test_same_idempotency_key_with_different_request_conflicts(client):
    headers={**AUTH,"HTTP_IDEMPOTENCY_KEY":"same","content_type":"application/json"}
    first=client.post("/api/v1/orders/",json.dumps({"internal_id":"I1","account":"DU1","symbol":"AAPL","side":"BUY","quantity":"1"}),**headers)
    conflict=client.post("/api/v1/orders/",json.dumps({"internal_id":"I2","account":"DU1","symbol":"MSFT","side":"BUY","quantity":"2"}),**headers)
    assert first.status_code==202
    assert conflict.status_code==409 and conflict.json()["error"]["code"]=="IDEMPOTENCY_CONFLICT"
    assert GatewayCommand.objects.count()==1


def test_retryable_failure_requires_explicit_retry_header(client):
    payload={"internal_id":"I1","account":"DU1","symbol":"AAPL","side":"BUY","quantity":"1"}
    headers={**AUTH,"HTTP_IDEMPOTENCY_KEY":"retry-me","content_type":"application/json"}
    first=client.post("/api/v1/orders/",json.dumps(payload),**headers)
    command=GatewayCommand.objects.get(pk=first.json()["data"]["command_id"])
    command.status="FAILED";command.retryable=True;command.last_error="temporary";command.save()
    stored=client.post("/api/v1/orders/",json.dumps(payload),**headers)
    retried=client.post("/api/v1/orders/",json.dumps(payload),HTTP_IDEMPOTENCY_RETRY="true",**headers)
    assert stored.json()["data"]["status"]=="FAILED"
    assert retried.json()["data"]["status"]=="PENDING"
    command.refresh_from_db()
    assert command.last_error=="" and command.retryable is False


def test_non_retryable_failure_rejects_explicit_retry(client):
    payload={"internal_id":"I1","account":"DU1","symbol":"AAPL","side":"BUY","quantity":"1"}
    headers={**AUTH,"HTTP_IDEMPOTENCY_KEY":"do-not-retry","content_type":"application/json"}
    first=client.post("/api/v1/orders/",json.dumps(payload),**headers)
    command=GatewayCommand.objects.get(pk=first.json()["data"]["command_id"])
    command.status="FAILED";command.retryable=False;command.last_error="invalid";command.save()
    result=client.post("/api/v1/orders/",json.dumps(payload),HTTP_IDEMPOTENCY_RETRY="true",**headers)
    assert result.status_code==409 and result.json()["error"]["code"]=="RETRY_NOT_ALLOWED"

def test_event_sequence_and_ack(client):
    GatewayEvent.objects.create(event_key="1",event_type="x",payload={})
    second=GatewayEvent.objects.create(event_key="2",event_type="x",payload={})
    body=client.get("/api/v1/events/?after=0",**AUTH).json()
    assert [x["id"] for x in body["data"]]==sorted(x["id"] for x in body["data"])
    client.post("/api/v1/events/ack/",json.dumps({"sequence":second.pk}),content_type="application/json",HTTP_IDEMPOTENCY_KEY="ack:2",**AUTH)
    assert GatewayEvent.objects.filter(acknowledged=True).count()==2

def test_event_sequence_recovers_when_backend_cursor_is_ahead(client):
    event=GatewayEvent.objects.create(event_key="after-reset",event_type="command.qualify.completed",payload={"conid":1})
    body=client.get("/api/v1/events/?after=999999",**AUTH).json()
    assert body["meta"]["sequence_reset"] is True
    assert [row["id"] for row in body["data"]]==[event.pk]

def test_completed_orders_snapshot_endpoint(client):
    GatewayEvent.objects.create(event_key="completed",event_type="snapshot.completed_orders",payload={"value":[{"broker_order_id":"1"}]})
    body=client.get("/api/v1/completed-orders/",**AUTH).json()
    assert body["data"][0]["broker_order_id"]=="1"


def test_order_state_reports_durable_command_and_negative_submission_proof(client):
    payload={"internal_id":"I-STATE","account":"DU1","symbol":"AAPL","side":"BUY","quantity":"1"}
    queued=client.post("/api/v1/orders/",json.dumps(payload),content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="state-command",**AUTH)
    body=client.get("/api/v1/orders/I-STATE/state/",**AUTH).json()["data"]
    assert body["commands"][0]["command_id"]==queued.json()["data"]["command_id"]
    assert body["non_submission_established"] is False
    missing=client.get("/api/v1/orders/NEVER-SUBMITTED/state/",**AUTH).json()["data"]
    assert missing["commands"]==[]
    assert missing["non_submission_established"] is True

def test_contract_search_and_command_detail(client):
    response=client.post("/api/v1/contracts/search/",json.dumps({"query":"BHP"}),content_type="application/json",HTTP_IDEMPOTENCY_KEY="search:BHP",**AUTH)
    assert response.status_code==202
    command=GatewayCommand.objects.get(pk=response.json()["data"]["command_id"])
    command.status="COMPLETED";command.result={"results":[{"symbol":"BHP","conid":123,"primary_exchange":"ASX"}]};command.save()
    detail=client.get(f"/api/v1/commands/{command.pk}/",**AUTH).json()["data"]
    assert detail["status"]=="COMPLETED" and detail["result"]["results"][0]["conid"]==123


def test_contract_search_rejects_too_short_query_before_enqueue(client):
    response=client.post(
        "/api/v1/contracts/search/",
        json.dumps({"query":"A"}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="search:A",
        **AUTH,
    )
    assert response.status_code==400
    assert response.json()["error"]["code"]=="QUERY_TOO_SHORT"
    assert response.json()["error"]["details"]["minimum_length"]==2
    assert GatewayCommand.objects.count()==0


def test_option_chain_command_requires_qualified_underlying(client):
    payload={"underlying_conid":1234,"symbol":"NIFTY","asset_class":"IND","exchange":"NSE","currency":"INR"}
    result=client.post("/api/v1/contracts/option-chain/",json.dumps(payload),content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="chain:nifty",**AUTH)
    assert result.status_code==202
    command=GatewayCommand.objects.get(pk=result.json()["data"]["command_id"])
    assert command.command_type=="OPTION_CHAIN" and command.payload["underlying_conid"]==1234


def test_option_order_has_dedicated_validated_endpoint(client):
    payload={"internal_id":"OPT-1","account":"DU1","symbol":"NIFTY","asset_class":"OPT","conid":7654321,
        "exchange":"NFO","currency":"INR","side":"BUY","quantity":"1","order_type":"LMT","limit_price":"100",
        "expiration":"2026-08-26","strike":"25000","right":"C","multiplier":"75","trading_class":"NIFTY"}
    result=client.post("/api/v1/options/orders/",json.dumps(payload),content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="option-order:1",**AUTH)
    assert result.status_code==202
    command=GatewayCommand.objects.get(pk=result.json()["data"]["command_id"])
    assert command.command_type=="PLACE_OPTION_ORDER" and command.payload["asset_class"]=="OPT"
    invalid={**payload,"internal_id":"OPT-2","quantity":"1.5"}
    rejected=client.post("/api/v1/options/orders/",json.dumps(invalid),content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="option-order:2",**AUTH)
    assert rejected.status_code==400


@pytest.mark.parametrize(
    ("path","payload","command_type"),
    [
        ("/api/v1/contracts/search/",{"query":"AAPL"},"SEARCH_CONTRACTS"),
        (
            "/api/v1/contracts/qualify/",
            {"conid":265598,"symbol":"AAPL","sec_type":"STK","exchange":"SMART","currency":"USD"},
            "QUALIFY",
        ),
    ],
)
def test_safe_contract_commands_can_explicitly_retry_transient_failure(client,path,payload,command_type):
    headers={
        **AUTH,
        "HTTP_IDEMPOTENCY_KEY":f"retry:{command_type}",
        "content_type":"application/json",
    }
    first=client.post(path,json.dumps(payload),**headers)
    command=GatewayCommand.objects.get(pk=first.json()["data"]["command_id"])
    command.status="FAILED";command.retryable=True;command.last_error="temporary IBKR connectivity loss";command.save()

    stored=client.post(path,json.dumps(payload),**headers)
    retried=client.post(path,json.dumps(payload),HTTP_IDEMPOTENCY_RETRY="true",**headers)

    stored_data=stored.json()["data"]
    assert stored_data["status"]=="FAILED"
    assert stored_data["retryable"] is True
    assert stored_data["last_error"]=="temporary IBKR connectivity loss"
    assert retried.status_code==202
    assert retried.json()["data"]["status"]=="PENDING"
    command.refresh_from_db()
    assert command.command_type==command_type
    assert command.last_error==""
    assert command.retryable is False

def test_market_subscription_requires_exact_contract(client):
    bad=client.post("/api/v1/market-data/subscriptions/",json.dumps({"symbol":"AAPL"}),content_type="application/json",**AUTH)
    assert bad.status_code==400
    payload={"subscription_key":"1:1m","instrument_id":1,"conid":265598,"symbol":"AAPL","timeframe":"1m"}
    good=client.post("/api/v1/market-data/subscriptions/",json.dumps(payload),content_type="application/json",HTTP_IDEMPOTENCY_KEY="subscribe:1:1m",**AUTH)
    assert good.status_code==202 and GatewayCommand.objects.get().command_type=="SUBSCRIBE_MARKET_DATA"

def test_market_subscription_cancel_preserves_route_action(client):
    response=client.post(
        "/api/v1/market-data/subscriptions/cancel/",
        json.dumps({"subscription_key":"1:1m"}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="cancel-market-data:1:1m",
        **AUTH,
    )
    assert response.status_code==202
    command=GatewayCommand.objects.get(pk=response.json()["data"]["command_id"])
    assert command.command_type=="CANCEL_MARKET_DATA"
    assert command.payload=={"subscription_key":"1:1m"}


def test_bounded_authenticated_daily_history_command(client):
    payload={"conid":265598,"symbol":"AAPL","exchange":"SMART","currency":"USD",
             "bar_size":"1 day","duration":"5 Y","what_to_show":"ADJUSTED_LAST",
             "use_rth":True,"end_time":""}
    unauthorized=client.post("/api/v1/market-data/history/",json.dumps(payload),content_type="application/json")
    assert unauthorized.status_code==401
    accepted=client.post("/api/v1/market-data/history/",json.dumps(payload),content_type="application/json",
                         HTTP_IDEMPOTENCY_KEY="history:aapl",**AUTH)
    assert accepted.status_code==202
    command=GatewayCommand.objects.get(pk=accepted.json()["data"]["command_id"])
    assert command.command_type=="REQUEST_HISTORICAL_DATA" and command.payload["what_to_show"]=="ADJUSTED_LAST"


@pytest.mark.parametrize("update",[
    {"duration":"11 Y"},{"bar_size":"1 hour"},{"what_to_show":"MIDPOINT"},{"use_rth":"yes"},
])
def test_history_rejects_requests_outside_daily_and_intraday_bounds(client,update):
    payload={"conid":265598,"symbol":"AAPL","exchange":"SMART","currency":"USD",
             "bar_size":"1 day","duration":"5 Y","what_to_show":"TRADES","use_rth":True,"end_time":""}
    payload.update(update)
    result=client.post("/api/v1/market-data/history/",json.dumps(payload),content_type="application/json",
                       HTTP_IDEMPOTENCY_KEY=f"invalid-history:{list(update)[0]}",**AUTH)
    assert result.status_code==400 and GatewayCommand.objects.count()==0


def test_bounded_intraday_history_command(client):
    payload={"conid":265598,"symbol":"AAPL","exchange":"SMART","currency":"USD",
             "bar_size":"1 hour","duration":"90 D","what_to_show":"TRADES",
             "use_rth":True,"end_time":""}
    accepted=client.post("/api/v1/market-data/history/",json.dumps(payload),content_type="application/json",
                         HTTP_IDEMPOTENCY_KEY="history:aapl:1h",**AUTH)
    assert accepted.status_code==202
    command=GatewayCommand.objects.get(pk=accepted.json()["data"]["command_id"])
    assert command.command_type=="REQUEST_HISTORICAL_DATA" and command.payload["bar_size"]=="1 hour"

def test_no_credential_leakage(client,settings):
    settings.IB_USERNAME="SECRET_USER"; settings.IB_PASSWORD="SECRET_PASSWORD"
    content=client.get("/api/v1/session/",**AUTH).content.decode()
    assert "SECRET_USER" not in content and "SECRET_PASSWORD" not in content


@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/api/v1/health/", "/api/v1/diagnostics/", "/api/v1/events/"])
def test_read_endpoints_reject_wrong_method(client, path):
    kwargs={} if path in {"/healthz", "/readyz"} else AUTH
    assert client.post(path, **kwargs).status_code==405


def test_commands_require_explicit_idempotency_key(client):
    payload={"internal_id":"I1","account":"DU1","symbol":"AAPL","side":"BUY","quantity":"1"}
    result=client.post("/api/v1/orders/",json.dumps(payload),content_type="application/json",**AUTH)
    assert result.status_code==400
    assert result.json()["error"]["code"]=="IDEMPOTENCY_KEY_REQUIRED"
    assert GatewayCommand.objects.count()==0


@pytest.mark.parametrize("payload", [
    {"internal_id":"I1","account":"DU1","symbol":"AAPL","side":"BUY","quantity":"0"},
    {"internal_id":"I1","account":"DU1","symbol":"AAPL","side":"BUY","quantity":"1.000000001"},
    {"internal_id":"I1","account":"DU1","symbol":"AAPL","side":"HOLD","quantity":"1"},
    {"internal_id":"I1","account":"DU1","symbol":"AAPL","side":"BUY","quantity":"1","order_type":"LMT"},
    {"internal_id":"I1","account":"DU1","symbol":"AAPL","side":"BUY","quantity":"1","order_type":"MKT","limit_price":"10"},
])
def test_order_payload_validation(client, payload):
    result=client.post(
        "/api/v1/orders/",json.dumps(payload),content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="invalid-order",**AUTH,
    )
    assert result.status_code==400
    assert result.json()["error"]["code"]=="INVALID_REQUEST"
    assert GatewayCommand.objects.count()==0


def test_order_modify_rejects_placement_only_fields(client):
    result=client.patch(
        "/api/v1/orders/I1/",json.dumps({"side":"SELL"}),content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="modify:I1",**AUTH,
    )
    assert result.status_code==400
    assert result.json()["error"]["code"]=="INVALID_REQUEST"


def test_malformed_json_is_a_structured_client_error(client):
    result=client.post(
        "/api/v1/orders/",'{"internal_id":',content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="malformed",**AUTH,
    )
    assert result.status_code==400
    assert result.json()["error"]["code"]=="INVALID_REQUEST"
