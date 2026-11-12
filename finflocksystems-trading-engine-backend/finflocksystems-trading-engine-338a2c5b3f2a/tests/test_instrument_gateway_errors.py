import json

import pytest

from apps.broker_gateway.client import GatewayCommandTimeout
from apps.broker_gateway.models import BrokerGatewaySession


pytestmark=pytest.mark.django_db


def make_session(*, status="CONNECTED"):
    return BrokerGatewaySession.objects.create(
        display_name="Instrument search session",
        username_hint="du-test",
        mode="paper",
        status=status,
        commands_enabled=status=="CONNECTED",
        child_container_name=f"instrument-test-{status.lower()}",
        internal_base_url=f"http://instrument-test-{status.lower()}:8080/api/v1",
        encrypted_gateway_token="not-read-by-these-tests",
        encrypted_novnc_password="not-read-by-these-tests",
        last_gateway_state={"connected":status=="CONNECTED"},
    )


def test_instrument_search_requires_an_explicit_broker_session(client):
    result=client.get("/api/v1/instruments/search/?query=AAPL")
    assert result.status_code==400
    assert result.json()["error"]["code"]=="BROKER_SESSION_REQUIRED"


def test_instrument_search_returns_504_with_command_status(monkeypatch,client):
    session=make_session()

    def timeout(*args,**kwargs):
        raise GatewayCommandTimeout(
            "Gateway command 42 timed out after 15 seconds",
            command={
                "command_id":42,
                "command_type":"SEARCH_CONTRACTS",
                "status":"PROCESSING",
                "attempt_count":1,
                "last_error":"",
            },
            timeout=15,
        )

    monkeypatch.setattr("apps.strategies.views.search_broker_instruments",timeout)
    result=client.get(f"/api/v1/instruments/search/?query=AAPL&session_id={session.pk}")
    error=result.json()["error"]
    assert result.status_code==504
    assert error["code"]=="GATEWAY_COMMAND_TIMEOUT"
    assert error["details"]=={
        "command_id":42,
        "command_type":"SEARCH_CONTRACTS",
        "command_status":"PROCESSING",
        "attempt_count":1,
        "retryable":True,
        "timeout_seconds":15,
        "last_error":"",
        "operation":"SEARCH_CONTRACTS",
    }


def test_instrument_qualification_returns_504_instead_of_generic_400(monkeypatch,client):
    session=make_session()

    def timeout(*args,**kwargs):
        raise GatewayCommandTimeout(
            "Gateway command 43 timed out after 20 seconds",
            command={
                "command_id":43,
                "command_type":"QUALIFY",
                "status":"PENDING",
                "attempt_count":0,
                "last_error":"",
            },
            timeout=20,
        )

    monkeypatch.setattr("apps.strategies.views.resolve_instrument",timeout)
    result=client.post(
        "/api/v1/instruments/resolve/",
        data=json.dumps({
            "session_id":str(session.pk),
            "ticker":"AAPL",
            "conid":265598,
            "asset_class":"STK",
            "exchange":"SMART",
            "currency":"USD",
            "qualify":True,
        }),
        content_type="application/json",
    )
    assert result.status_code==504
    assert result.json()["error"]["details"]["command_status"]=="PENDING"
    assert result.json()["error"]["details"]["operation"]=="QUALIFY"
