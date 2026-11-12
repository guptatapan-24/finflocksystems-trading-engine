import asyncio
import json
import uuid
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
import requests
import responses
from django.test import RequestFactory, override_settings
from django.utils import timezone

from apps.accounts.models import BrokerAccount
from apps.broker_gateway.client import GatewayClient, GatewayRoute, GatewayRouteError, GatewaySessionUnavailable
from apps.broker_gateway.configuration import ManagedBrokerGatewayUnavailable
from apps.broker_gateway.crypto import decrypt_secret, encrypt_secret, issue_novnc_access_token
from apps.broker_gateway.models import (
    BrokerGatewaySession,
    BrokerGatewaySessionSecret,
    BrokerSessionAccount,
    BrokerSyncCursor,
)
from apps.broker_gateway.qch import QCHBrokerClient, QCHConflict, QCHContainer, QCHError, QCHNotFound
from apps.broker_gateway.services import (
    container_name_for,
    delete_session,
    inspect_gateway_session,
    provision_session,
    record_provision_failure,
    gateway_environment,
    synchronize_accounts,
    synchronize_local_paper_gateway,
)
from apps.broker_gateway.tasks import monitor_broker_sessions
from apps.broker_gateway.vnc import (
    VNCPasswordConfigurationError,
    normalize_vnc_password,
)
from apps.portfolios.models import TradingPortfolio


pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def broker_settings(settings, monkeypatch):
    settings.BROKER_SESSION_ENCRYPTION_KEY = "broker-session-unit-test-key"
    settings.BROKER_CREDENTIAL_TTL_SECONDS = 900
    settings.BROKER_SESSION_CREATING_STALE_SECONDS = 60
    settings.BROKER_SESSION_START_TIMEOUT_SECONDS = 0
    settings.BROKER_SESSION_HEALTH_TIMEOUT_SECONDS = 1
    settings.NOVNC_ACCESS_TOKEN_TTL_SECONDS = 300
    settings.NOVNC_PROXY_CONNECT_TIMEOUT_SECONDS = 1
    settings.NOVNC_PROXY_MAX_BODY_BYTES = 1024 * 1024
    settings.IBKR_GATEWAY_IMAGE = "docker.io/example/trading-engine-ib-gateway@sha256:" + ("a" * 64)
    settings.QCH_SUBCONTAINER_NETWORK = "traefik"
    settings.QCH_API_HOST = "https://qch.example"
    settings.QCH_APP_ID = "app-1"
    settings.QCH_SERVICE_TOKEN = "qch-secret"
    settings.QCH_REQUEST_TIMEOUT_SECONDS = 3
    monkeypatch.setenv("QCH_API_HOST", "https://qch.example")
    monkeypatch.setenv("QCH_APP_ID", "app-1")
    monkeypatch.setenv("QCH_SERVICE_TOKEN", "qch-secret")


def make_session(name="Paper one", mode="paper", *, status="CREATING"):
    session = BrokerGatewaySession(
        display_name=name,
        username_hint="du••er",
        mode=mode,
        status=status,
        commands_enabled=status == "CONNECTED",
        child_container_name="pending",
        encrypted_gateway_token=encrypt_secret(f"token-{name}"),
        encrypted_novnc_password=encrypt_secret("vnc-pass"),
    )
    session.child_container_name = container_name_for(session.pk)
    session.internal_base_url = f"http://{session.child_container_name}:8080/api/v1"
    if status == "CONNECTED":
        session.last_gateway_state = {"connected": True, "mode": mode}
    session.save()
    return session


def add_secret(session, username="ib-user", password="ib-password"):
    return BrokerGatewaySessionSecret.objects.create(
        session=session,
        encrypted_username=encrypt_secret(username),
        encrypted_password=encrypt_secret(password),
        expires_at=timezone.now() + timedelta(minutes=5),
    )


def disable_managed_gateway(settings, monkeypatch):
    settings.IBKR_GATEWAY_IMAGE = ""
    settings.QCH_API_HOST = ""
    settings.QCH_APP_ID = ""
    settings.QCH_SERVICE_TOKEN = ""
    monkeypatch.setenv("QCH_API_HOST", "")
    monkeypatch.setenv("QCH_APP_ID", "")
    monkeypatch.setenv("QCH_SERVICE_TOKEN", "")


@responses.activate
def test_gateway_client_rejects_parameterless_construction_and_isolates_two_fake_gateways():
    with pytest.raises((TypeError, GatewayRouteError)):
        GatewayClient()
    first = GatewayClient(GatewayRoute("session-a", "http://gateway-a/api/v1", "token-a"))
    second = GatewayClient(GatewayRoute("session-b", "http://gateway-b/api/v1", "token-b"))
    assert (first.base_url, first.token) != (second.base_url, second.token)
    for url, mode in (("http://gateway-a/api/v1/health/", "paper"), ("http://gateway-b/api/v1/health/", "live")):
        responses.get(url, json={"ok":True, "data":{"connected":True, "mode":mode}, "error":None, "meta":{}})
    assert first.health()["mode"] == "paper" and second.health()["mode"] == "live"
    assert [call.request.headers["Authorization"] for call in responses.calls] == ["Bearer token-a", "Bearer token-b"]


def test_portfolio_without_managed_session_is_always_unavailable():
    account = BrokerAccount.objects.create(account_id="DU-NO-SESSION")
    portfolio = TradingPortfolio.objects.create(name="Unmanaged", account=account)

    with pytest.raises(GatewaySessionUnavailable, match="not bound"):
        GatewayClient.for_portfolio(portfolio)


def test_child_name_uses_the_full_session_uuid():
    session_id = "11111111-2222-3333-4444-555555555555"
    name = container_name_for(session_id)

    assert name == "trading-engine-ibkr-11111111222233334444555555555555"
    assert len(name.removeprefix("trading-engine-ibkr-")) == 32


@pytest.mark.parametrize("status", [
    BrokerGatewaySession.Status.WAITING_FOR_LOGIN,
    BrokerGatewaySession.Status.WAITING_FOR_2FA,
    BrokerGatewaySession.Status.DISCONNECTED,
    BrokerGatewaySession.Status.ERROR,
])
def test_waiting_and_disconnected_sessions_cannot_trade_but_can_reconnect(status):
    session = make_session(f"Lifecycle {status}", "live", status=status)
    session.commands_enabled = True
    session.last_gateway_state = {"connected": False}
    session.save(update_fields=["commands_enabled", "last_gateway_state"])
    with pytest.raises(GatewaySessionUnavailable):
        GatewayClient(session, require_commands=True)
    assert GatewayClient(session, purpose="reconnect").base_url == session.internal_base_url


def test_creation_accepts_only_paper_live_encrypts_credentials_and_returns_no_secret(client, monkeypatch, django_capture_on_commit_callbacks):
    queued = []
    monkeypatch.setattr("apps.broker_gateway.views.provision_broker_session.delay", lambda session_id: queued.append(session_id))
    with django_capture_on_commit_callbacks(execute=True):
        for mode in ("Paper", "LIVE"):
            result = client.post("/api/v1/broker-sessions/", data=json.dumps({
                "display_name": mode, "username": "plain-user", "password": "plain-password", "mode": mode,
            }), content_type="application/json")
            assert result.status_code == 202
            body = result.content.decode()
            assert "plain-user" not in body and "plain-password" not in body
            assert "internal_base_url" not in body and "encrypted_" not in body
    invalid = client.post("/api/v1/broker-sessions/", data=json.dumps({
        "display_name": "bad", "username": "user", "password": "pass", "mode": "demo",
    }), content_type="application/json")
    assert invalid.status_code == 400
    secret = BrokerGatewaySessionSecret.objects.first()
    assert secret.encrypted_username != "plain-user" and decrypt_secret(secret.encrypted_username) == "plain-user"
    assert len(queued) == 2


# IBKR serves real time to a subscribed login even when delayed is requested,
# so the declared subscription, not the mode, picks the child's data type.
@pytest.mark.parametrize("mode,declared,expected", [
    ("paper", True, "LIVE"), ("paper", False, "DELAYED"), ("paper", None, "DELAYED"),
    ("live", True, "LIVE"), ("live", None, "LIVE"),
])
def test_creation_stores_declared_market_data_type_and_passes_it_to_the_child(
    client, monkeypatch, mode, declared, expected
):
    monkeypatch.setattr("apps.broker_gateway.views.provision_broker_session.delay", lambda session_id: None)
    payload = {"display_name": "Data", "username": "user", "password": "pass", "mode": mode}
    if declared is not None:
        payload["market_data_subscription"] = declared
    result = client.post("/api/v1/broker-sessions/", data=json.dumps(payload), content_type="application/json")
    assert result.status_code == 202, result.json()
    assert result.json()["data"]["market_data_type"] == expected
    session = BrokerGatewaySession.objects.get()
    assert gateway_environment(session, "user", "pass", "token", "vnc-pass")["IBKR_MARKET_DATA_TYPE"] == expected


@pytest.mark.parametrize("declared", [False, "yes"])
def test_creation_rejects_live_without_subscription_and_non_boolean_declarations(client, declared):
    mode = "live" if declared is False else "paper"
    result = client.post("/api/v1/broker-sessions/", data=json.dumps({
        "display_name": "Bad", "username": "user", "password": "pass", "mode": mode,
        "market_data_subscription": declared,
    }), content_type="application/json")
    assert result.status_code == 400
    assert BrokerGatewaySession.objects.count() == 0


def test_replacing_credentials_can_change_the_declared_market_data_type(
    client, monkeypatch, django_capture_on_commit_callbacks
):
    session = make_session("Switch", status=BrokerGatewaySession.Status.CONNECTED)
    assert session.market_data_type == "DELAYED"
    monkeypatch.setattr("apps.broker_gateway.views.provision_broker_session.delay", lambda session_id: None)
    monkeypatch.setattr("apps.broker_gateway.views.QCHBrokerClient",
        lambda: SimpleNamespace(delete_container=lambda name: True))
    with django_capture_on_commit_callbacks(execute=True):
        result = client.post(f"/api/v1/broker-sessions/{session.pk}/credentials/", data=json.dumps({
            "username": "new-user", "password": "new-password", "market_data_subscription": True,
        }), content_type="application/json")
    assert result.status_code == 202, result.json()
    session.refresh_from_db()
    assert session.market_data_type == "LIVE"
    assert session.status == BrokerGatewaySession.Status.CREATING


@pytest.mark.parametrize("field", [
    "image", "IBKR_GATEWAY_IMAGE", "registry_auth", "DOCKERHUB_USERNAME", "DOCKERHUB_TOKEN",
])
def test_creation_rejects_browser_image_and_registry_auth_overrides(client, field):
    result = client.post("/api/v1/broker-sessions/", data=json.dumps({
        "display_name": "No override",
        "username": "plain-user",
        "password": "plain-password",
        "mode": "paper",
        field: "must-not-be-accepted",
    }), content_type="application/json")

    assert result.status_code == 400
    assert result.json()["error"]["code"] == "BROKER_SESSION_INVALID"
    assert "must-not-be-accepted" not in result.content.decode()
    assert BrokerGatewaySession.objects.count() == 0
    assert BrokerGatewaySessionSecret.objects.count() == 0


def test_creation_preflight_returns_503_without_records_secrets_or_task(
    client, settings, monkeypatch, django_capture_on_commit_callbacks
):
    disable_managed_gateway(settings, monkeypatch)
    queued = []
    monkeypatch.setattr("apps.broker_gateway.views.provision_broker_session.delay", lambda value: queued.append(value))
    monkeypatch.setattr(
        "apps.broker_gateway.views.generate_service_token",
        lambda: (_ for _ in ()).throw(AssertionError("secret generation must not run")),
    )
    with django_capture_on_commit_callbacks(execute=True):
        result = client.post("/api/v1/broker-sessions/", data=json.dumps({
            "display_name": "Unavailable", "username": "plain-user", "password": "plain-password", "mode": "paper",
        }), content_type="application/json")
    assert result.status_code == 503
    assert result.json()["error"] == {
        "code": "BROKER_GATEWAY_NOT_CONFIGURED",
        "message": "Managed IB Gateway is unavailable because QCH configuration is incomplete.",
        "details": {
            "missing": ["IBKR_GATEWAY_IMAGE", "QCH_API_HOST", "QCH_APP_ID", "QCH_SERVICE_TOKEN"],
            "invalid": [],
        },
    }
    assert BrokerGatewaySession.objects.count() == 0
    assert BrokerGatewaySessionSecret.objects.count() == 0
    assert queued == []
    assert "plain-user" not in result.content.decode() and "plain-password" not in result.content.decode()


@responses.activate
def test_qch_payload_and_bearer_auth_do_not_leak_into_error():
    url = "https://qch.example/api/apps/app-1/containers"
    responses.post(url, status=201, json={"id": "child-1", "name": "session-child", "status": "RUNNING"})
    qch = QCHBrokerClient()
    child = qch.create_container(
        name="session-child", image="registry/ibkr@sha256:abc",
        env={"IB_USERNAME": "secret-user", "IB_PASSWORD": "secret-password"}, network="traefik",
    )
    request = responses.calls[0].request
    payload = json.loads(request.body)
    assert child.id == "child-1"
    assert request.headers["Authorization"] == "Bearer qch-secret"
    assert payload["network"] == "traefik" and "command" not in payload
    assert set(payload) == {"name", "image", "env", "network"}
    assert not ({"registry_username", "registry_password", "registry_token", "registry_auth", "docker_config", "pull_secret"} & set(payload))
    assert payload["env"]["IB_PASSWORD"] == "secret-password" and "environment" not in payload
    responses.post(url, status=500, json={"error": "secret-password qch-secret"})
    with pytest.raises(QCHError) as error:
        qch.create_container(
            name="session-child-2", image="registry/ibkr@sha256:abc",
            env={"IB_PASSWORD": "secret-password"}, network="traefik",
        )
    assert "secret-password" not in str(error.value) and "qch-secret" not in str(error.value)


@responses.activate
def test_qch_blank_network_is_omitted_from_create_payload():
    url = "https://qch.example/api/apps/app-1/containers"
    responses.post(url, status=201, json={"id": "child-1", "name": "session-child", "status": "RUNNING"})

    QCHBrokerClient().create_container(
        name="session-child",
        image="docker.io/example/gateway:v1",
        env={"PORT": "8080"},
        network=None,
    )

    assert set(json.loads(responses.calls[0].request.body)) == {"name", "image", "env"}


@pytest.mark.parametrize("payload", [
    {"name": "", "image": "docker.io/example/gateway:v1"},
    {"name": "child", "image": ""},
    {"name": "child\nother", "image": "docker.io/example/gateway:v1"},
    {"name": "child", "image": "docker.io/example/gateway:v1\nother"},
    {"name": "child", "image": "docker.io/example/gateway:v1", "env": []},
    {"name": "child", "image": "docker.io/example/gateway:v1", "network": ""},
    {"name": "child", "image": "docker.io/example/gateway:v1", "network": "traefik\rnext"},
])
def test_qch_create_rejects_invalid_local_payload_without_http(payload):
    with pytest.raises(QCHError):
        QCHBrokerClient().create_container(**payload)


@responses.activate
def test_qch_direct_list_409_adoption_and_name_encoded_idempotent_delete():
    url = "https://qch.example/api/apps/app-1/containers"
    existing = {"id": "opaque-id", "name": "session child/one", "state": "running"}
    responses.get(url, status=200, json=[existing])
    assert QCHBrokerClient().list_containers()[0].name == existing["name"]

    responses.post(url, status=409, json={"error": "already exists"})
    responses.get(url, status=200, json={"containers": [existing]})
    adopted = QCHBrokerClient().create_container(name=existing["name"], image="registry/image@sha256:abc")
    assert adopted.id == "opaque-id" and adopted.status == "RUNNING"

    delete_url = f"{url}/session%20child%2Fone"
    responses.delete(delete_url, status=404)
    assert QCHBrokerClient().delete_container(existing["name"]) is False
    assert responses.calls[-1].request.url == delete_url


@responses.activate
def test_qch_expected_name_lookup_ignores_unrelated_ibkr_containers():
    url = "https://qch.example/api/apps/app-1/containers"
    responses.get(url, status=200, json=[
        {"id": "other-1", "name": "some-ibkr-container", "status": "RUNNING"},
        {"id": "other-2", "name": "trading-engine-ibkr-similar", "status": "RUNNING"},
    ])

    assert QCHBrokerClient().find_by_name("trading-engine-ibkr-expected") is None


class FakeQCH:
    def __init__(self, existing=None, conflict=False):
        self.existing = existing
        self.conflict = conflict
        self.created = []
        self.deleted = []
        self.find_calls = 0

    def find_by_name(self, name):
        self.find_calls += 1
        if self.conflict and self.find_calls == 1:
            return None
        return self.existing

    def create_container(self, **payload):
        self.created.append(payload)
        if self.conflict:
            raise QCHConflict("exists")
        self.existing = QCHContainer("child", payload["name"], "RUNNING", {})
        return self.existing

    def delete_container(self, container_id):
        self.deleted.append(container_id)
        return True


class HealthyResponse:
    def raise_for_status(self):
        return None


def test_provision_consumes_secret_builds_required_environment_and_live_waits_for_2fa(monkeypatch):
    session = make_session("Live one", "live")
    add_secret(session)
    qch = FakeQCH()
    monkeypatch.setattr("apps.broker_gateway.services.requests.get", lambda *args, **kwargs: HealthyResponse())
    monkeypatch.setattr(GatewayClient, "health", lambda self: {"connected": False, "mode": "live"})
    assert provision_session(session.pk, qch_client=qch) == BrokerGatewaySession.Status.WAITING_FOR_2FA
    assert not BrokerGatewaySessionSecret.objects.filter(session=session).exists()
    environment = qch.created[0]["env"]
    assert qch.created[0]["name"] == session.child_container_name
    assert qch.created[0]["image"] == "docker.io/example/trading-engine-ib-gateway@sha256:" + ("a" * 64)
    assert set(qch.created[0]) == {"name", "image", "env", "network"}
    assert environment["IB_USERNAME"] == "ib-user" and environment["IB_PASSWORD"] == "ib-password"
    assert environment["IBC_TRADING_MODE"] == "live" and environment["PORT"] == "8080"
    assert set(environment) == {
        "DJANGO_SECRET_KEY", "IB_USERNAME", "IB_PASSWORD", "IBC_TRADING_MODE",
        "GATEWAY_SERVICE_TOKEN", "NOVNC_PASSWORD", "BROKER_ADAPTER", "PORT",
        "GATEWAY_REMOTE_DESKTOP_ENABLED", "IBKR_MARKET_DATA_TYPE",
        "GATEWAY_CONTRACT_SEARCH_MAX_RESULTS",
        "GATEWAY_IBKR_REQUEST_TIMEOUT_SEARCH_CONTRACTS_SECONDS",
        "GATEWAY_IBKR_REQUEST_TIMEOUT_QUALIFY_SECONDS",
    }
    assert environment["IBKR_MARKET_DATA_TYPE"] == "LIVE"
    # This session parks in WAITING_FOR_2FA until a human completes the dialog
    # over the Backend's noVNC proxy, and the child gates x11vnc/websockify on
    # this variable, defaulting it to off. Managed children must opt in.
    assert environment["GATEWAY_REMOTE_DESKTOP_ENABLED"] == "true"
    assert environment["GATEWAY_CONTRACT_SEARCH_MAX_RESULTS"] == "12"
    assert environment["GATEWAY_IBKR_REQUEST_TIMEOUT_SEARCH_CONTRACTS_SECONDS"] == "12"
    assert environment["GATEWAY_IBKR_REQUEST_TIMEOUT_QUALIFY_SECONDS"] == "15"
    assert environment["GATEWAY_SERVICE_TOKEN"] != environment["NOVNC_PASSWORD"]
    assert len(environment["DJANGO_SECRET_KEY"]) >= 64
    assert "command" not in qch.created[0]
    session.refresh_from_db()
    assert session.commands_enabled is False


def test_invalid_configured_image_stops_before_credentials_are_decrypted(settings, monkeypatch):
    session = make_session("Invalid image", "paper")
    add_secret(session)
    settings.IBKR_GATEWAY_IMAGE = "ghcr.io/example/trading-engine-ib-gateway:v1.0.0"
    monkeypatch.setattr(
        "apps.broker_gateway.services.decrypt_secret",
        lambda value: (_ for _ in ()).throw(AssertionError("credentials must not be decrypted")),
    )

    with pytest.raises(ManagedBrokerGatewayUnavailable):
        provision_session(session.pk, qch_client=FakeQCH())

    session.refresh_from_db()
    assert session.status == BrokerGatewaySession.Status.CREATING
    assert BrokerGatewaySessionSecret.objects.filter(session=session).exists()


def test_gateway_environment_uses_an_ephemeral_unique_django_secret():
    session = SimpleNamespace(mode="paper", market_data_type="DELAYED")
    first = gateway_environment(session, "user", "password", "token", "vnc-pass")
    second = gateway_environment(session, "user", "password", "token", "vnc-pass")

    assert first["DJANGO_SECRET_KEY"] != second["DJANGO_SECRET_KEY"]
    assert "DJANGO_SECRET_KEY" not in {field.name for field in BrokerGatewaySession._meta.fields}


def test_gateway_environment_gives_each_session_its_own_postgres_database(settings):
    settings.IB_GATEWAY_POSTGRES_HOST = "postgres"
    settings.IB_GATEWAY_POSTGRES_PORT = "5432"
    settings.IB_GATEWAY_POSTGRES_DB = "IB_Gateway"
    settings.IB_GATEWAY_POSTGRES_USER = "admin"
    settings.IB_GATEWAY_POSTGRES_PASSWORD = "secret"
    first = SimpleNamespace(mode="paper", market_data_type="DELAYED", pk=uuid.uuid4())
    second = SimpleNamespace(mode="paper", market_data_type="DELAYED", pk=uuid.uuid4())

    env = gateway_environment(first, "user", "password", "token", "vnc-pass")
    other = gateway_environment(second, "user", "password", "token", "vnc-pass")

    assert env["POSTGRES_HOST"] == "postgres"
    assert env["POSTGRES_USER"] == "admin"
    assert env["POSTGRES_PASSWORD"] == "secret"
    assert env["POSTGRES_DB"] == f"IB_Gateway_{first.pk.hex}"
    assert env["POSTGRES_DB"] != other["POSTGRES_DB"]
    assert len(env["POSTGRES_DB"]) <= 63


def test_gateway_environment_keeps_sqlite_without_a_postgres_host(settings):
    settings.IB_GATEWAY_POSTGRES_HOST = ""
    env = gateway_environment(SimpleNamespace(mode="paper", market_data_type="DELAYED", pk=uuid.uuid4()), "user", "password", "token", "vnc-pass")

    assert not any(key.startswith("POSTGRES_") for key in env)


def _configure_local_gateway(settings, *, token, novnc_password):
    settings.ALLOW_LIVE_TRADING = False
    settings.LOCAL_PAPER_GATEWAY_URL = (
        "http://paper-ibkr-gateway:8080/api/v1"
    )
    settings.LOCAL_PAPER_GATEWAY_CONTAINER_NAME = "paper-ibkr-gateway"
    settings.LOCAL_PAPER_GATEWAY_SERVICE_TOKEN = token
    settings.LOCAL_PAPER_GATEWAY_NOVNC_PASSWORD = novnc_password


def _waiting_local_gateway(monkeypatch):
    monkeypatch.setattr(
        GatewayClient,
        "health",
        lambda self: {"connected": False, "mode": "paper"},
    )


def test_static_gateway_stores_and_repairs_each_credential_independently(
    settings, monkeypatch
):
    _waiting_local_gateway(monkeypatch)
    _configure_local_gateway(
        settings,
        token="dummy-service-token-a",
        novnc_password="vnc-pass-alpha",
    )

    session = synchronize_local_paper_gateway()

    assert decrypt_secret(session.encrypted_gateway_token) == "dummy-service-token-a"
    assert decrypt_secret(session.encrypted_novnc_password) == "vnc-pass"
    assert (
        decrypt_secret(session.encrypted_gateway_token)
        != decrypt_secret(session.encrypted_novnc_password)
    )

    # Repair a session created by the old service-token-as-VNC-password bug.
    broken_vnc = encrypt_secret("dummy-se")
    BrokerGatewaySession.objects.filter(pk=session.pk).update(
        encrypted_novnc_password=broken_vnc
    )
    repaired = synchronize_local_paper_gateway()
    assert decrypt_secret(repaired.encrypted_novnc_password) == "vnc-pass"

    vnc_ciphertext = repaired.encrypted_novnc_password
    settings.LOCAL_PAPER_GATEWAY_SERVICE_TOKEN = "dummy-service-token-b"
    service_rotated = synchronize_local_paper_gateway()
    assert decrypt_secret(service_rotated.encrypted_gateway_token) == (
        "dummy-service-token-b"
    )
    assert service_rotated.encrypted_novnc_password == vnc_ciphertext

    service_ciphertext = service_rotated.encrypted_gateway_token
    settings.LOCAL_PAPER_GATEWAY_NOVNC_PASSWORD = "new-pass-bravo"
    vnc_rotated = synchronize_local_paper_gateway()
    assert vnc_rotated.encrypted_gateway_token == service_ciphertext
    assert decrypt_secret(vnc_rotated.encrypted_novnc_password) == "new-pass"


@pytest.mark.parametrize(
    "invalid_password",
    ["", "short", "linefeed\nvalue", "return\rvalue", "password-\N{SNOWMAN}"],
)
def test_static_gateway_invalid_vnc_configuration_fails_closed_without_secret(
    invalid_password, settings, monkeypatch
):
    _waiting_local_gateway(monkeypatch)
    _configure_local_gateway(
        settings,
        token="dummy-service-token",
        novnc_password=invalid_password,
    )

    with pytest.raises(VNCPasswordConfigurationError) as error:
        synchronize_local_paper_gateway()

    assert not BrokerGatewaySession.objects.filter(
        child_container_name="paper-ibkr-gateway"
    ).exists()
    if invalid_password:
        assert invalid_password not in str(error.value)


def test_static_gateway_secrets_are_not_serialized(client, settings, monkeypatch):
    _waiting_local_gateway(monkeypatch)
    service_token = "dummy-service-token-not-public"
    vnc_password = "vnc-hide-not-public"
    _configure_local_gateway(
        settings, token=service_token, novnc_password=vnc_password
    )
    session = synchronize_local_paper_gateway()

    result = client.get(f"/api/v1/broker-sessions/{session.pk}/")

    assert result.status_code == 200
    payload = result.content.decode("utf-8")
    assert service_token not in payload
    assert vnc_password not in payload
    assert "encrypted_gateway_token" not in payload
    assert "encrypted_novnc_password" not in payload


def test_vnc_password_normalization_matches_classic_rfb_effective_bytes():
    assert normalize_vnc_password("12345678-extra") == "12345678"


def test_provision_adoption_deletes_secret_and_retryable_failure_keeps_it(monkeypatch):
    adopted = make_session("Adopt", "paper")
    add_secret(adopted)
    child = QCHContainer("existing-id", adopted.child_container_name, "RUNNING", {})
    qch = FakeQCH(existing=child)
    monkeypatch.setattr("apps.broker_gateway.services.requests.get", lambda *args, **kwargs: HealthyResponse())
    monkeypatch.setattr(GatewayClient, "health", lambda self: {"connected": False, "mode": "paper"})
    assert provision_session(adopted.pk, qch_client=qch) == BrokerGatewaySession.Status.WAITING_FOR_LOGIN
    adopted.refresh_from_db()
    assert adopted.child_container_id == "existing-id" and len(qch.created) == 0
    assert not BrokerGatewaySessionSecret.objects.filter(session=adopted).exists()

    failed = make_session("Failed", "paper")
    add_secret(failed)
    broken = FakeQCH()
    broken.create_container = lambda **kwargs: (_ for _ in ()).throw(QCHError("QCH unavailable", retryable=True))
    with pytest.raises(QCHError):
        provision_session(failed.pk, qch_client=broken)
    assert BrokerGatewaySessionSecret.objects.filter(session=failed).exists()
    record_provision_failure(failed.pk, QCHError("QCH unavailable", retryable=True), final=True)
    assert not BrokerGatewaySessionSecret.objects.filter(session=failed).exists()


def test_final_provisioning_failure_deletes_temporary_credentials():
    session = make_session("Final failure", "paper")
    add_secret(session)
    qch = FakeQCH()
    qch.create_container = lambda **kwargs: (_ for _ in ()).throw(
        QCHError("QCH sub-container request failed with HTTP 400", status_code=400)
    )

    assert provision_session(session.pk, qch_client=qch) == BrokerGatewaySession.Status.ERROR
    assert not BrokerGatewaySessionSecret.objects.filter(session=session).exists()


def test_ambiguous_create_timeout_lists_expected_name_before_retry(monkeypatch):
    session = make_session("Ambiguous", "paper")
    add_secret(session)
    child = QCHContainer("created-despite-timeout", session.child_container_name, "RUNNING", {})
    qch = FakeQCH()

    def timed_out_create(**payload):
        qch.created.append(payload)
        qch.existing = child
        raise QCHError("QCH sub-container broker is unavailable", retryable=True)

    qch.create_container = timed_out_create
    monkeypatch.setattr("apps.broker_gateway.services.requests.get", lambda *args, **kwargs: HealthyResponse())
    monkeypatch.setattr(GatewayClient, "health", lambda self: {"connected": False, "mode": "paper"})
    assert provision_session(session.pk, qch_client=qch) == BrokerGatewaySession.Status.WAITING_FOR_LOGIN
    assert qch.find_calls == 2 and len(qch.created) == 1
    assert not BrokerGatewaySessionSecret.objects.filter(session=session).exists()


def test_stale_creating_session_is_requeued_and_expired_credentials_are_final(monkeypatch, settings):
    settings.BROKER_SESSION_CREATING_STALE_SECONDS = 60
    stale = make_session("Stale", "paper")
    add_secret(stale)
    old = timezone.now() - timedelta(minutes=5)
    BrokerGatewaySession.objects.filter(pk=stale.pk).update(updated_at=old, last_checked_at=None)
    queued = []
    monkeypatch.setattr("apps.broker_gateway.tasks.provision_broker_session.delay", lambda value: queued.append(value))
    result = monitor_broker_sessions()
    assert queued == [str(stale.pk)] and result["recovered_creating"] == [str(stale.pk)]

    expired = make_session("Expired", "live")
    add_secret(expired)
    BrokerGatewaySessionSecret.objects.filter(session=expired).update(expires_at=timezone.now() - timedelta(seconds=1))
    monitor_broker_sessions()
    expired.refresh_from_db()
    assert expired.status == BrokerGatewaySession.Status.LOGIN_FAILED
    assert expired.commands_enabled is False
    assert not BrokerGatewaySessionSecret.objects.filter(session=expired).exists()


def test_monitoring_is_disabled_without_qch_and_preserves_existing_sessions_and_secrets(monkeypatch, settings):
    connected = make_session("Existing", status=BrokerGatewaySession.Status.CONNECTED)
    creating = make_session("Pending", status=BrokerGatewaySession.Status.CREATING)
    secret = add_secret(creating)
    BrokerGatewaySessionSecret.objects.filter(pk=secret.pk).update(
        expires_at=timezone.now() - timedelta(minutes=1)
    )
    disable_managed_gateway(settings, monkeypatch)
    monkeypatch.setattr(
        "apps.broker_gateway.tasks.inspect_gateway_session",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("monitor must not inspect sessions")),
    )
    monkeypatch.setattr(
        "apps.broker_gateway.tasks.provision_broker_session.delay",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("monitor must not schedule provisioning")),
    )

    result = monitor_broker_sessions()

    assert result["status"] == "disabled"
    assert result["reason"] == "BROKER_GATEWAY_NOT_CONFIGURED"
    assert result["broker_deployment"]["missing"] == [
        "IBKR_GATEWAY_IMAGE", "QCH_API_HOST", "QCH_APP_ID", "QCH_SERVICE_TOKEN",
    ]
    connected.refresh_from_db()
    creating.refresh_from_db()
    assert connected.status == BrokerGatewaySession.Status.CONNECTED
    assert creating.status == BrokerGatewaySession.Status.CREATING
    assert BrokerGatewaySessionSecret.objects.filter(session=creating).exists()


def test_qch_dependent_existing_session_actions_return_configuration_error_without_mutation(
    client, settings, monkeypatch
):
    session = make_session("Existing", status=BrokerGatewaySession.Status.CONNECTED)
    original_version = session.lifecycle_version
    disable_managed_gateway(settings, monkeypatch)
    monkeypatch.setattr(
        "apps.broker_gateway.views.QCHBrokerClient",
        lambda: (_ for _ in ()).throw(AssertionError("QCH client must not be created")),
    )

    credentials = client.post(
        f"/api/v1/broker-sessions/{session.pk}/credentials/",
        data=json.dumps({"username": "new-user", "password": "new-password"}),
        content_type="application/json",
    )
    deleted = client.delete(f"/api/v1/broker-sessions/{session.pk}/")

    assert credentials.status_code == 503 and deleted.status_code == 503
    assert credentials.json()["error"]["code"] == "BROKER_GATEWAY_NOT_CONFIGURED"
    assert deleted.json()["error"]["code"] == "BROKER_GATEWAY_NOT_CONFIGURED"
    session.refresh_from_db()
    assert session.status == BrokerGatewaySession.Status.CONNECTED
    assert session.lifecycle_version == original_version
    assert not BrokerGatewaySessionSecret.objects.filter(session=session).exists()


class AccountsGateway:
    def __init__(self, account):
        self.account = account

    def accounts(self):
        return [{"account_id": self.account, "alias": self.account}]

    def account_summary(self):
        return [{"account": self.account, "tag": "NetLiquidation", "value": "1000", "currency": "USD"}]


def test_two_sessions_keep_accounts_portfolios_and_cursors_independent():
    first, second = make_session("First", status="CONNECTED"), make_session("Second", "live", status="CONNECTED")
    synchronize_accounts(first, AccountsGateway("DU-A"))
    synchronize_accounts(second, AccountsGateway("DU-B"))
    assert first.session_accounts.get().broker_account.account_id == "DU-A"
    assert second.session_accounts.get().broker_account.account_id == "DU-B"
    first_portfolio = TradingPortfolio.objects.get(gateway_session=first)
    second_portfolio = TradingPortfolio.objects.get(gateway_session=second)
    assert first_portfolio.account.account_id == "DU-A"
    assert second_portfolio.account.account_id == "DU-B"
    assert GatewayClient.for_portfolio(first_portfolio).base_url == first.internal_base_url
    assert GatewayClient.for_order(SimpleNamespace(intent=SimpleNamespace(portfolio=second_portfolio))).token == "token-Second"

    from apps.broker_gateway.sync import sync_events
    class Events:
        def __init__(self, session, account): self.gateway_session=session;self.account=account;self.acked=0
        def events(self, after=0): return [] if after else [{"id":1,"event_type":"snapshot.accounts","payload":{"value":[{"account_id":self.account}]}}]
        def ack_events(self, sequence): self.acked=sequence
    assert sync_events(Events(first,"DU-A")) == 1 and sync_events(Events(second,"DU-B")) == 1
    assert set(BrokerSyncCursor.objects.values_list("session_id","last_sequence")) == {(first.pk,1),(second.pk,1)}


def test_reconnect_and_idempotent_delete_leave_other_session_intact(client, monkeypatch):
    first, second = make_session("Delete", status="CONNECTED"), make_session("Keep", status="CONNECTED")
    first.child_container_id = "opaque-delete-id"; first.save(update_fields=["child_container_id"])
    account = BrokerAccount.objects.create(account_id="DU-DELETE", is_reconciled=True)
    BrokerSessionAccount.objects.create(session=first, broker_account=account)
    TradingPortfolio.objects.create(name="Delete route", account=account, gateway_session=first)
    monkeypatch.setattr(GatewayClient, "reconnect", lambda self: {"command_id": 7, "status": "PENDING"})
    reconnect = client.post(f"/api/v1/broker-sessions/{first.pk}/reconnect/", data="{}", content_type="application/json")
    assert reconnect.status_code == 202
    qch = FakeQCH()
    removed, changed = delete_session(first.pk, qch_client=qch)
    removed_again, changed_again = delete_session(first.pk, qch_client=qch)
    second.refresh_from_db()
    assert changed is True and changed_again is False and removed_again.status == "DELETED"
    assert qch.deleted == [first.child_container_name] and second.status == "CONNECTED"
    assert BrokerAccount.objects.get(pk=account.pk).is_reconciled is False


def _http_scope(session_id, asset, token, extra_query=""):
    query = f"token={token}{extra_query}".encode()
    return {"type":"http","method":"GET","path":f"/api/v1/broker-sessions/{session_id}/novnc/{asset}",
        "query_string":query,"headers":[],"scheme":"http"}


@override_settings(
    APP_BASE_PATH="/ai_trading_engine_backend",
    PUBLIC_BASE_URL="https://qfsplatform.com/ai_trading_engine_backend",
)
def test_public_novnc_url_and_asgi_connect_page_use_exact_prefix_once(monkeypatch):
    from apps.broker_gateway import proxy, views

    session = make_session("Prefix", "live", status=BrokerGatewaySession.Status.WAITING_FOR_2FA)
    request = RequestFactory().get(
        f"/api/v1/broker-sessions/{session.pk}/",
        HTTP_HOST="qfsplatform.com",
        HTTP_X_FORWARDED_PROTO="https",
        HTTP_X_FORWARDED_PREFIX="/ai_trading_engine_backend",
    )
    public_url = views._public_novnc_url(request, session)
    expected_connect = (
        f"https://qfsplatform.com/ai_trading_engine_backend/api/v1/broker-sessions/{session.pk}/novnc/connect/"
    )
    assert public_url.startswith(expected_connect + "#access_token=")
    assert public_url.count("/ai_trading_engine_backend") == 1

    async def load(_):
        return session

    monkeypatch.setattr(proxy, "_load_session", load)

    async def fallback(scope, receive, send):
        raise AssertionError("ASGI noVNC route fell through to Django")

    async def run(scope):
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await proxy.BrokerProxyRouter(fallback)(scope, receive, send)
        return sent

    preserved_scope = {
        "type": "http",
        "method": "GET",
        "scheme": "https",
        "path": f"/ai_trading_engine_backend/api/v1/broker-sessions/{session.pk}/novnc/connect",
        "query_string": b"",
        "headers": [(b"host", b"qfsplatform.com")],
    }
    preserved = asyncio.run(run(preserved_scope))
    page = preserved[-1]["body"].decode()
    prefix = f"/ai_trading_engine_backend/api/v1/broker-sessions/{session.pk}/novnc"
    assert preserved[0]["status"] == 200
    assert f'"authorize": "{prefix}/authorize/"' in page
    assert f'"vnc": "{prefix}/vnc.html"' in page
    assert '"websockify": "websockify"' in page
    assert "path=websockify" in page
    assert "/novnc/novnc/" not in page
    assert session.child_container_name not in page
    assert "access_token=" not in page
    assert "../authorize/" not in page and "../vnc.html" not in page

    stripped_scope = {
        **preserved_scope,
        "path": f"/api/v1/broker-sessions/{session.pk}/novnc/connect",
        "headers": [(b"host", b"qfsplatform.com"), (b"x-forwarded-prefix", b"/ai_trading_engine_backend")],
    }
    stripped = asyncio.run(run(stripped_scope))
    assert stripped[-1]["body"] == preserved[-1]["body"]

    websocket_scope = {
        "type": "websocket",
        "path": f"/ai_trading_engine_backend/api/v1/broker-sessions/{session.pk}/novnc/websockify",
        "query_string": b"",
        "headers": [],
    }
    assert proxy._route(websocket_scope) == (str(session.pk), "websockify", prefix)


def test_novnc_http_binary_forwarding_token_expiry_and_ssrf_rejection(monkeypatch):
    from apps.broker_gateway import proxy
    session = SimpleNamespace(pk="11111111-1111-1111-1111-111111111111", child_container_name="safe-child")
    async def load(_): return session
    monkeypatch.setattr(proxy,"_load_session",load)
    real_validate=proxy.validate_novnc_access_token
    monkeypatch.setattr(proxy,"validate_novnc_access_token",lambda sid,value: real_validate(sid,value,now=100))
    token,_=issue_novnc_access_token(session.pk,now=100)
    captured=[]
    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): return None
        async def request(self,method,url,**kwargs):
            captured.append(url);return httpx.Response(200,content=b"\x00\xffasset",headers={"content-type":"application/octet-stream"})
    monkeypatch.setattr(proxy.httpx,"AsyncClient",lambda **kwargs:FakeClient())
    async def run(scope):
        sent=[]
        async def receive(): return {"type":"http.request","body":b"","more_body":False}
        async def send(message): sent.append(message)
        await proxy.proxy_http(scope,receive,send,proxy._route(scope))
        return sent
    valid=asyncio.run(run(_http_scope(session.pk,"app/ui.js",token)))
    assert valid[-1]["body"] == b"\x00\xffasset" and captured[0].startswith("http://safe-child:8080/")
    rejected=asyncio.run(run(_http_scope(session.pk,"app/ui.js",token,"&host=evil.example&port=22")))
    assert rejected[0]["status"] == 400
    expired,_=issue_novnc_access_token(session.pk,ttl_seconds=30,now=1)
    expired_result=asyncio.run(run(_http_scope(session.pk,"app/ui.js",expired)))
    assert expired_result[0]["status"] == 403


def test_novnc_websocket_preserves_binary_frames(monkeypatch):
    from apps.broker_gateway import proxy
    session = SimpleNamespace(pk="22222222-2222-2222-2222-222222222222", child_container_name="safe-child")
    token,_=issue_novnc_access_token(session.pk)
    async def load(_): return session
    monkeypatch.setattr(proxy,"_load_session",load)
    class Upstream:
        subprotocol="binary"
        def __init__(self): self.sent=[];self.iterated=False
        async def send(self,data): self.sent.append(data)
        async def close(self,code=1000): return None
        def __aiter__(self): return self
        async def __anext__(self):
            if self.iterated: raise StopAsyncIteration
            self.iterated=True;return b"\x00\x01\xff"
    upstream=Upstream()
    class Context:
        async def __aenter__(self): return upstream
        async def __aexit__(self,*args): return None
    monkeypatch.setattr(proxy,"websocket_connect",lambda *args,**kwargs:Context())
    scope={"type":"websocket","path":f"/api/v1/broker-sessions/{session.pk}/novnc/websockify",
        "query_string":f"token={token}".encode(),"headers":[],"subprotocols":["binary"]}
    messages=iter([{"type":"websocket.connect"},{"type":"websocket.receive","bytes":b"\x10\x11"},{"type":"websocket.disconnect","code":1000}])
    sent=[]
    async def receive(): return next(messages)
    async def send(message): sent.append(message)
    asyncio.run(proxy.proxy_websocket(scope,receive,send,proxy._route(scope)))
    assert {"type":"websocket.send","bytes":b"\x00\x01\xff"} in sent


def test_novnc_proxy_terminates_vnc_auth_without_returning_password_or_challenge():
    from apps.broker_gateway import proxy

    challenge = bytes(range(16))

    class Upstream:
        def __init__(self):
            self.frames = iter([b"RFB 003.008\n", b"\x01\x02", challenge, b"\x00\x00\x00\x00"])
            self.sent = []
        def __aiter__(self): return self
        async def __anext__(self):
            try: return next(self.frames)
            except StopIteration: raise StopAsyncIteration
        async def send(self, value): self.sent.append(value)

    upstream = Upstream()
    client_messages = iter([
        {"type":"websocket.receive", "bytes":b"RFB 003.008\n"},
        {"type":"websocket.receive", "bytes":b"\x01"},
    ])
    browser = []
    async def receive(): return next(client_messages)
    async def send(message): browser.append(message)

    asyncio.run(proxy._prepare_rfb_connection(upstream, receive, send, encrypt_secret("vnc-pass")))
    browser_bytes = [message["bytes"] for message in browser]
    assert browser_bytes == [b"RFB 003.008\n", b"\x01\x01", b"\x00\x00\x00\x00"]
    assert upstream.sent[:2] == [b"RFB 003.008\n", b"\x02"]
    assert len(upstream.sent[2]) == 16 and upstream.sent[2] != challenge
    assert b"vnc-pass" not in b"".join(browser_bytes)


def test_a_flapping_qch_listing_does_not_downgrade_a_live_session(monkeypatch):
    """A failed observation is not evidence about the session it observes.

    QCH was seen returning HTTP 404 for the container collection every few
    syncs while the child was running and healthy. That flipped the session to
    ERROR and back to WAITING_FOR_LOGIN on a five-second cycle, disabling
    commands and remounting the frontend's noVNC view each time.
    """
    session = make_session("Flapping", status=BrokerGatewaySession.Status.CONNECTED)
    monkeypatch.setattr(
        "apps.broker_gateway.tasks.inspect_gateway_session",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            QCHError("QCH container listing returned HTTP 404", status_code=404, retryable=True)
        ),
    )

    result = monitor_broker_sessions()

    session.refresh_from_db()
    assert session.status == BrokerGatewaySession.Status.CONNECTED
    assert session.commands_enabled is True
    assert session.last_error == ""
    assert result[str(session.pk)] == "CONNECTED+CHECK_DEFERRED"


def test_a_non_retryable_qch_failure_still_marks_the_session_errored(monkeypatch):
    session = make_session("Broken", status=BrokerGatewaySession.Status.CONNECTED)
    monkeypatch.setattr(
        "apps.broker_gateway.tasks.inspect_gateway_session",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            QCHError("QCH rejected the request", status_code=403, retryable=False)
        ),
    )

    result = monitor_broker_sessions()

    session.refresh_from_db()
    assert session.status == BrokerGatewaySession.Status.ERROR
    assert session.commands_enabled is False
    assert result[str(session.pk)] == BrokerGatewaySession.Status.ERROR


def test_a_child_that_answers_healthz_outvotes_an_empty_qch_listing(monkeypatch):
    """QCH also returns HTTP 200 with an empty list while a child is running.

    That is a "successful" listing, so the absence rule alone would trust it.
    The container answering on its own port is the stronger evidence, and it
    does not travel through the API that is misbehaving.
    """
    session = make_session("Contradicted", status=BrokerGatewaySession.Status.CONNECTED)
    monkeypatch.setattr(
        "apps.broker_gateway.services.requests.get",
        lambda *args, **kwargs: HealthyResponse(),
    )

    with pytest.raises(QCHError) as contradiction:
        inspect_gateway_session(session, qch_client=FakeQCH(existing=None))

    assert contradiction.value.retryable is True
    assert "answered /healthz" in str(contradiction.value)
    session.refresh_from_db()
    assert session.status == BrokerGatewaySession.Status.CONNECTED
    assert session.commands_enabled is True


def test_an_empty_listing_and_an_unreachable_child_together_mean_missing(monkeypatch):
    session = make_session("Absent", status=BrokerGatewaySession.Status.CONNECTED)

    def unreachable(*args, **kwargs):
        raise requests.ConnectionError("no such host")

    monkeypatch.setattr("apps.broker_gateway.services.requests.get", unreachable)

    locked = inspect_gateway_session(session, qch_client=FakeQCH(existing=None))

    assert locked.status == BrokerGatewaySession.Status.ERROR
    assert locked.commands_enabled is False
    assert session.child_container_name in locked.last_error
    assert "did not answer /healthz" in locked.last_error


def test_a_collection_404_is_retryable_while_a_container_404_is_not():
    class Response:
        status_code = 404

        def json(self):
            return {}

    class Http:
        def request(self, method, url, **kwargs):
            return Response()

    client = QCHBrokerClient(http_session=Http())

    with pytest.raises(QCHError) as listing:
        client.list_containers()
    assert not isinstance(listing.value, QCHNotFound)
    assert listing.value.retryable is True
    assert "QCH_APP_ID" in str(listing.value)

    # A 404 on one container really does mean that container is absent.
    assert client.delete_container("trading-engine-ibkr-abc") is False
