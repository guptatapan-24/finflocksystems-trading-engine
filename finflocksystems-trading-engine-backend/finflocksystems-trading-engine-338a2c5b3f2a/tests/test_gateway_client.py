import pytest
import responses
from django.test import override_settings

from apps.broker_gateway.client import (
    GatewayClient,
    GatewayCommandFailed,
    GatewayCommandTimeout,
    GatewayRoute,
)

@responses.activate
def test_gateway_auth_and_safe_retry():
    url = "http://gateway/api/v1/health/"
    responses.get(url, status=503, json={"ok":False})
    responses.get(url, status=200, json={"ok":True,"data":{"connected":True}})
    client = GatewayClient(GatewayRoute("test-health", "http://gateway/api/v1", "secret"))
    assert client.health()["connected"] is True
    assert responses.calls[1].request.headers["Authorization"] == "Bearer secret"


@responses.activate
def test_idempotent_market_subscription_retries_transient_gateway_500():
    url = "http://gateway/api/v1/market-data/subscriptions/"
    responses.post(url, status=500, json={"ok": False})
    responses.post(
        url,
        status=202,
        json={"ok": True, "data": {"command_id": 12, "status": "PENDING"}},
    )
    client = GatewayClient(
        GatewayRoute("test-subscribe", "http://gateway/api/v1", "secret")
    )

    result = client.subscribe_market_data(
        {
            "subscription_key": "subscription-1",
            "instrument_id": 1,
            "conid": 123,
            "symbol": "TEST",
            "timeframe": "1m",
        },
        "market-subscribe:subscription-1",
    )

    assert result["command_id"] == 12
    assert len(responses.calls) == 2
    assert (
        responses.calls[0].request.headers["Idempotency-Key"]
        == responses.calls[1].request.headers["Idempotency-Key"]
    )


@responses.activate
def test_gateway_contract_search_waits_for_durable_command():
    responses.post("http://gateway/api/v1/contracts/search/",json={"ok":True,"data":{"command_id":7,"status":"PENDING"}},status=202)
    responses.get("http://gateway/api/v1/commands/7/",json={"ok":True,"data":{"command_id":7,"status":"COMPLETED","result":{"results":[{"symbol":"BHP","conid":123}]}}})
    results=GatewayClient(GatewayRoute("test-search", "http://gateway/api/v1", "secret")).search_contracts("BHP")
    assert results==[{"symbol":"BHP","conid":123}]


@responses.activate
def test_gateway_option_chain_waits_for_durable_command():
    responses.post("http://gateway/api/v1/contracts/option-chain/",json={"ok":True,"data":{"command_id":8,"status":"PENDING"}},status=202)
    result={"chains":[{"exchange":"NFO","trading_class":"NIFTY","multiplier":"75","expirations":["20260826"],"strikes":[25000]}]}
    responses.get("http://gateway/api/v1/commands/8/",json={"ok":True,"data":{"command_id":8,"status":"COMPLETED","result":result}})
    actual=GatewayClient(GatewayRoute("test-option-chain", "http://gateway/api/v1", "secret")).option_chain({"underlying_conid":1234})
    assert actual==result


@responses.activate
def test_option_order_uses_dedicated_gateway_endpoint():
    responses.post("http://gateway/api/v1/options/orders/",json={"ok":True,"data":{"status":"SUBMITTED"}},status=200)
    result=GatewayClient(GatewayRoute("test-option-order","http://gateway/api/v1","secret")).place_option_order(
        {"internal_id":"OPT-1","asset_class":"OPT","conid":7654321,"quantity":"1"},"option-order:1")
    assert result["status"]=="SUBMITTED"
    assert responses.calls[0].request.headers["Idempotency-Key"]=="session:test-option-order:option-order:1"


@responses.activate
def test_gateway_contract_search_replays_completed_command_result():
    responses.post("http://gateway/api/v1/contracts/search/",json={"ok":True,"data":{"command_id":7,"status":"COMPLETED"}},status=202)
    responses.get("http://gateway/api/v1/commands/7/",json={"ok":True,"data":{"command_id":7,"status":"COMPLETED","result":{"results":[{"symbol":"AAPL","conid":265598}]}}})
    results=GatewayClient(GatewayRoute("test-replay", "http://gateway/api/v1", "secret")).search_contracts("AAPL")
    assert results==[{"symbol":"AAPL","conid":265598}]


@responses.activate
def test_search_explicitly_retries_a_stored_retryable_failure():
    failed={"command_id":7,"command_type":"SEARCH_CONTRACTS","status":"FAILED","retryable":True,
            "attempt_count":1,"last_error":"IBKR temporarily unavailable"}
    responses.post("http://gateway/api/v1/contracts/search/",json={"ok":True,"data":failed},status=202)
    responses.get("http://gateway/api/v1/commands/7/",json={"ok":True,"data":failed})
    responses.post("http://gateway/api/v1/contracts/search/",
                   json={"ok":True,"data":{"command_id":7,"status":"PENDING"}},status=202)
    responses.get("http://gateway/api/v1/commands/7/",json={"ok":True,"data":{
        "command_id":7,"command_type":"SEARCH_CONTRACTS","status":"COMPLETED",
        "result":{"results":[{"symbol":"AAPL","conid":265598}]}}})

    results=GatewayClient(GatewayRoute("test-retry-stored","http://gateway/api/v1","secret")).search_contracts("AAPL")

    assert results[0]["conid"]==265598
    posts=[call for call in responses.calls if call.request.method=="POST"]
    assert "Idempotency-Retry" not in posts[0].request.headers
    assert posts[1].request.headers["Idempotency-Retry"]=="true"
    assert posts[0].request.headers["Idempotency-Key"]==posts[1].request.headers["Idempotency-Key"]


@responses.activate
def test_search_retries_a_new_transient_command_failure():
    failed={"command_id":8,"command_type":"SEARCH_CONTRACTS","status":"FAILED","retryable":True,
            "attempt_count":1,"last_error":"connection reset"}
    responses.post("http://gateway/api/v1/contracts/search/",
                   json={"ok":True,"data":{"command_id":8,"status":"PENDING"}},status=202)
    responses.get("http://gateway/api/v1/commands/8/",json={"ok":True,"data":failed})
    responses.post("http://gateway/api/v1/contracts/search/",
                   json={"ok":True,"data":{"command_id":8,"status":"PENDING"}},status=202)
    responses.get("http://gateway/api/v1/commands/8/",json={"ok":True,"data":{
        "command_id":8,"command_type":"SEARCH_CONTRACTS","status":"COMPLETED",
        "result":{"results":[{"symbol":"AAPL","conid":265598}]}}})

    results=GatewayClient(GatewayRoute("test-retry-new","http://gateway/api/v1","secret")).search_contracts("AAPL")
    assert results[0]["symbol"]=="AAPL"
    assert responses.calls[2].request.headers["Idempotency-Retry"]=="true"


@responses.activate
@override_settings(
    GATEWAY_COMMAND_TIMEOUT_SEARCH_CONTRACTS_SECONDS=0,
    GATEWAY_COMMAND_POLL_INTERVAL_SECONDS=0,
)
def test_search_timeout_exposes_durable_command_metadata():
    responses.post("http://gateway/api/v1/contracts/search/",json={"ok":True,"data":{
        "command_id":9,"command_type":"SEARCH_CONTRACTS","status":"PROCESSING",
        "attempt_count":1,"last_error":""}},status=202)

    with pytest.raises(GatewayCommandTimeout) as raised:
        GatewayClient(GatewayRoute("test-timeout","http://gateway/api/v1","secret")).search_contracts("AAPL")

    assert raised.value.http_status==504
    assert raised.value.details["command_id"]==9
    assert raised.value.details["command_status"]=="PROCESSING"
    assert raised.value.details["retryable"] is True


@responses.activate
def test_non_retryable_command_failure_preserves_exact_error_details():
    failed={"command_id":10,"command_type":"QUALIFY","status":"FAILED","retryable":False,
            "attempt_count":1,"last_error":"No security definition has been found"}
    responses.post("http://gateway/api/v1/contracts/qualify/",json={"ok":True,"data":failed},status=202)
    responses.get("http://gateway/api/v1/commands/10/",json={"ok":True,"data":failed})

    with pytest.raises(GatewayCommandFailed) as raised:
        GatewayClient(GatewayRoute("test-failed","http://gateway/api/v1","secret")).qualify_contract_exact(
            {"conid":999,"symbol":"NOPE","sec_type":"STK","exchange":"SMART","currency":"USD"},
            "qualify:NOPE",
        )

    assert raised.value.http_status==503
    assert raised.value.retryable is False
    assert raised.value.details["last_error"]=="No security definition has been found"


@responses.activate
def test_gateway_daily_history_uses_authenticated_durable_read_only_command():
    payload={"conid":265598,"symbol":"AAPL","exchange":"SMART","currency":"USD","bar_size":"1 day",
             "duration":"5 Y","what_to_show":"ADJUSTED_LAST","use_rth":True,"end_time":""}
    responses.post("http://gateway/api/v1/market-data/history/",
                   json={"ok":True,"data":{"command_id":9,"status":"PENDING"}},status=202)
    responses.get("http://gateway/api/v1/commands/9/",json={"ok":True,"data":{
        "command_id":9,"status":"COMPLETED","result":{"provider":"IBKR","bars":[{"date":"2026-01-02","close":"100"}]}}})
    result=GatewayClient(GatewayRoute("test-history", "http://gateway/api/v1", "secret")).historical_bars(payload)
    assert result["provider"]=="IBKR" and result["bars"][0]["close"]=="100"
    assert responses.calls[0].request.headers["Authorization"]=="Bearer secret"
