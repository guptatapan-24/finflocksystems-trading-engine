import pytest
from django.test import override_settings

from gateway_service import diagnostics as gateway_diagnostics
from gateway_service import views
from gateway_service.models import GatewayEvent, GatewaySession


pytestmark = pytest.mark.django_db
AUTH = {"HTTP_AUTHORIZATION": "Bearer test-token"}


def diagnostic_snapshot(**updates):
    value = {
        "ib_gateway_process_running": True,
        "expected_tws_api_port": 4002,
        "tws_api_port_listening": True,
        "internal_ports": {"5900": True, "6080": True, "8001": True, "8080": True},
        "broker_connection_state": "CONNECTED",
        "broker_reconciled": True,
        "database_available": True,
        "latest_broker_error": None,
    }
    value.update(updates)
    return value


def test_diagnostics_requires_service_authentication(client, monkeypatch):
    monkeypatch.setattr(views, "collect_gateway_diagnostics", diagnostic_snapshot)

    assert client.get("/api/v1/diagnostics/").status_code == 401
    assert client.get(
        "/api/v1/diagnostics/", HTTP_AUTHORIZATION="Bearer wrong-token"
    ).status_code == 401
    assert client.get("/api/v1/diagnostics/", **AUTH).status_code == 200


def test_paper_diagnostics_report_expected_and_unavailable_ports(client, monkeypatch):
    GatewaySession.objects.create(pk=1, state="CONNECTED", mode="paper", reconciled=True)
    monkeypatch.setattr(gateway_diagnostics, "ibgateway_process_running", lambda: True)
    monkeypatch.setattr(
        gateway_diagnostics, "port_is_listening", lambda port: port != 6080
    )

    data = client.get("/api/v1/diagnostics/", **AUTH).json()["data"]

    assert data["ib_gateway_process_running"] is True
    assert data["expected_tws_api_port"] == 4002
    assert data["tws_api_port_listening"] is True
    assert data["internal_ports"] == {
        "5900": True,
        "6080": False,
        "8001": True,
        "8080": True,
    }
    assert data["broker_connection_state"] == "CONNECTED"


def test_latest_broker_error_is_sanitized(client, monkeypatch):
    monkeypatch.setenv("GATEWAY_SERVICE_TOKEN", "diagnostic-token-value")
    monkeypatch.setenv("IB_USERNAME", "diagnostic-user-value")
    GatewayEvent.objects.create(
        event_key="unsafe-error",
        event_type="session.disconnected",
        payload={
            "error": (
                "login=diagnostic-user-value password=hunter2 "
                "token=diagnostic-token-value Bearer bearer-value "
                "https://url-user:url-password@broker.example failed"
            )
        },
    )
    monkeypatch.setattr(gateway_diagnostics, "ibgateway_process_running", lambda: False)
    monkeypatch.setattr(gateway_diagnostics, "port_is_listening", lambda _port: False)

    content = client.get("/api/v1/diagnostics/", **AUTH).content.decode()

    for secret in (
        "diagnostic-user-value",
        "hunter2",
        "diagnostic-token-value",
        "bearer-value",
        "url-user",
        "url-password",
    ):
        assert secret not in content
    assert "[REDACTED]" in content


def test_ready_state_requires_connected_reconciled_broker(client, monkeypatch):
    monkeypatch.setattr(views, "collect_gateway_diagnostics", diagnostic_snapshot)

    result = client.get("/readyz")

    assert result.status_code == 200
    assert result.json()["data"] == {"status": "ready"}


@override_settings(BROKER_ADAPTER="ib_async", IBC_TRADING_MODE="paper")
def test_paper_login_wait_is_not_ready_but_liveness_stays_healthy(client, monkeypatch):
    monkeypatch.setattr(
        views,
        "collect_gateway_diagnostics",
        lambda: diagnostic_snapshot(
            tws_api_port_listening=False,
            broker_connection_state="CONNECTING",
            broker_reconciled=False,
        ),
    )

    readiness = client.get("/readyz")

    assert readiness.status_code == 503
    assert readiness.json()["error"]["details"] == {
        "status": "waiting_for_login",
        "fatal": False,
    }
    assert client.get("/healthz").status_code == 200


@override_settings(BROKER_ADAPTER="ib_async", IBC_TRADING_MODE="live")
def test_live_2fa_wait_is_not_ready_but_liveness_stays_healthy(client, monkeypatch):
    monkeypatch.setattr(
        views,
        "collect_gateway_diagnostics",
        lambda: diagnostic_snapshot(
            expected_tws_api_port=4001,
            tws_api_port_listening=False,
            broker_connection_state="CONNECTING",
            broker_reconciled=False,
        ),
    )

    readiness = client.get("/readyz")

    assert readiness.status_code == 503
    assert readiness.json()["error"]["details"] == {
        "status": "waiting_for_live_2fa",
        "fatal": False,
    }
    assert client.get("/healthz").json()["data"]["status"] == "alive"


def test_unavailable_internal_port_is_not_ready(client, monkeypatch):
    ports = diagnostic_snapshot()["internal_ports"]
    ports["8001"] = False
    monkeypatch.setattr(
        views, "collect_gateway_diagnostics", lambda: diagnostic_snapshot(internal_ports=ports)
    )

    result = client.get("/readyz")

    assert result.status_code == 503
    assert result.json()["error"]["details"] == {
        "status": "internal_services_unavailable",
        "fatal": False,
        "unavailable_ports": ["8001"],
    }


@override_settings(GATEWAY_REMOTE_DESKTOP_ENABLED=False)
def test_stopped_novnc_viewer_does_not_block_readiness_when_it_is_disabled(client, monkeypatch):
    """The unattended default: x11vnc and websockify are never started."""
    ports = diagnostic_snapshot()["internal_ports"]
    ports["5900"] = False
    ports["6080"] = False
    monkeypatch.setattr(
        views, "collect_gateway_diagnostics", lambda: diagnostic_snapshot(internal_ports=ports)
    )

    result = client.get("/readyz")

    assert result.status_code == 200
    assert result.json()["data"]["status"] == "ready"


@override_settings(GATEWAY_REMOTE_DESKTOP_ENABLED=True)
def test_stopped_novnc_viewer_blocks_readiness_when_it_is_enabled(client, monkeypatch):
    """A managed session proxies noVNC for login, so a dead viewer is not ready."""
    ports = diagnostic_snapshot()["internal_ports"]
    ports["6080"] = False
    monkeypatch.setattr(
        views, "collect_gateway_diagnostics", lambda: diagnostic_snapshot(internal_ports=ports)
    )

    result = client.get("/readyz")

    assert result.status_code == 503
    assert result.json()["error"]["details"] == {
        "status": "internal_services_unavailable",
        "fatal": False,
        "unavailable_ports": ["6080"],
    }


@override_settings(GATEWAY_REMOTE_DESKTOP_ENABLED=False)
def test_readiness_still_requires_a_connected_reconciled_broker(client, monkeypatch):
    """Readiness is the trading gate /healthz deliberately is not."""
    monkeypatch.setattr(
        views,
        "collect_gateway_diagnostics",
        lambda: diagnostic_snapshot(broker_connection_state="CONNECTING"),
    )
    assert client.get("/readyz").status_code == 503
    assert client.get("/healthz").status_code == 200

    monkeypatch.setattr(
        views,
        "collect_gateway_diagnostics",
        lambda: diagnostic_snapshot(broker_reconciled=False),
    )
    assert client.get("/readyz").json()["error"]["details"]["status"] == "broker_reconciling"


@override_settings(BROKER_ADAPTER="mock", IBC_TRADING_MODE="paper")
def test_mock_readiness_does_not_require_gateway_process_or_tws_port(client, monkeypatch):
    monkeypatch.setattr(
        views,
        "collect_gateway_diagnostics",
        lambda: diagnostic_snapshot(
            ib_gateway_process_running=False,
            tws_api_port_listening=False,
        ),
    )

    assert client.get("/readyz").status_code == 200
