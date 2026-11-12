import pytest
import importlib
import os
from pathlib import Path
import subprocess
import sys
from django.test import override_settings
from django.urls import clear_url_caches

pytestmark = pytest.mark.django_db


MANAGED_GATEWAY_VARIABLES = (
    "QCH_APP_ID",
    "QCH_API_HOST",
    "QCH_SERVICE_TOKEN",
    "IBKR_GATEWAY_IMAGE",
)


def disable_managed_gateway(settings, monkeypatch):
    settings.BROKER_SESSION_ENCRYPTION_KEY = "test-encryption-key"
    for name in MANAGED_GATEWAY_VARIABLES:
        setattr(settings, name, "")
        monkeypatch.setenv(name, "")


def test_health_and_api_envelope(client):
    body = client.get("/healthz").json()
    assert body["ok"] and body["data"]["status"] == "healthy"
    assert body["data"]["process"] == "running" and "database" not in body["data"]
    assert set(client.get("/api/v1/system/").json()) == {"ok", "data", "error", "meta"}


def test_prefix_preserved_backend_health_api_and_dashboard_alias(client):
    import config.urls

    try:
        with override_settings(APP_BASE_PATH="/ai_trading_engine_backend"):
            importlib.reload(config.urls)
            clear_url_caches()
            assert client.get("/ai_trading_engine_backend/healthz").status_code == 200
            assert client.get("/ai_trading_engine_backend/api/v1/system/").status_code == 200
            base = client.get("/ai_trading_engine_backend")
            assert base.status_code == 200
            assert base.json()["data"]["system"] == "/ai_trading_engine_backend/api/v1/system/"
            redirect = client.get("/ai_trading_engine_backend/dashboard")
            assert redirect.status_code == 302
            assert redirect["Location"] == "/ai_trading_engine_backend/api/v1/dashboard/summary/"
            # Prefix-stripped requests remain valid for QFS's alternate proxy mode.
            assert client.get("/healthz", HTTP_X_FORWARDED_PREFIX="/ai_trading_engine_backend").status_code == 200
    finally:
        importlib.reload(config.urls)
        clear_url_caches()


def test_backend_initializes_without_managed_gateway_environment():
    environment = os.environ.copy()
    for name in MANAGED_GATEWAY_VARIABLES:
        environment[name] = ""
    environment["DATABASE_URL"] = "sqlite:///:memory:"
    result = subprocess.run(
        [sys.executable, "manage.py", "check"],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_health_and_readiness_succeed_when_only_managed_gateway_is_missing(client, settings, monkeypatch):
    disable_managed_gateway(settings, monkeypatch)
    settings.RECOMMENDATION_SYSTEM_ENABLED = False
    assert client.get("/healthz").status_code == 200
    result = client.get("/readyz")
    assert result.status_code == 200
    deployment = result.json()["data"]["deployment"]
    assert deployment["available"] is False and deployment["ready"] is False
    assert deployment["missing"] == [
        "IBKR_GATEWAY_IMAGE",
        "QCH_API_HOST",
        "QCH_APP_ID",
        "QCH_SERVICE_TOKEN",
    ]
    assert "qch-secret" not in result.content.decode()


def test_system_reports_managed_gateway_unavailable_without_exposing_values(client, settings, monkeypatch):
    disable_managed_gateway(settings, monkeypatch)
    result = client.get("/api/v1/system/")
    assert result.status_code == 200
    deployment = result.json()["data"]["broker_deployment"]
    assert deployment == {
        "available": False,
        "ready": False,
        "missing": ["IBKR_GATEWAY_IMAGE", "QCH_API_HOST", "QCH_APP_ID", "QCH_SERVICE_TOKEN"],
        "invalid": [],
    }
    assert "test-encryption-key" not in result.content.decode()


def test_readiness_reports_invalid_managed_gateway_names_without_values(client, settings, monkeypatch):
    settings.RECOMMENDATION_SYSTEM_ENABLED = False
    settings.BROKER_SESSION_ENCRYPTION_KEY = "test-encryption-key"
    settings.IBKR_GATEWAY_IMAGE = "docker.io/OWNER/trading-engine-ib-gateway@sha256:REPLACE_WITH_DIGEST"
    settings.QCH_APP_ID = "configured-app"
    settings.QCH_API_HOST = "https://qch.example"
    settings.QCH_SERVICE_TOKEN = "configured-token"
    monkeypatch.setenv("QCH_APP_ID", settings.QCH_APP_ID)
    monkeypatch.setenv("QCH_API_HOST", settings.QCH_API_HOST)
    monkeypatch.setenv("QCH_SERVICE_TOKEN", settings.QCH_SERVICE_TOKEN)

    result = client.get("/readyz")

    assert result.status_code == 200
    deployment = result.json()["data"]["deployment"]
    assert deployment["available"] is False
    assert deployment["missing"] == []
    assert deployment["invalid"] == ["IBKR_GATEWAY_IMAGE"]
    assert "REPLACE_WITH_DIGEST" not in result.content.decode()
    assert "configured-token" not in result.content.decode()


def test_managed_gateway_allows_a_fixed_non_latest_development_tag(settings, monkeypatch):
    from apps.broker_gateway.configuration import managed_broker_deployment_configuration

    settings.BROKER_SESSION_ENCRYPTION_KEY = "test-encryption-key"
    settings.IBKR_GATEWAY_IMAGE = "docker.io/example/trading-engine-ib-gateway:v1.0.0"
    settings.QCH_APP_ID = "configured-app"
    settings.QCH_API_HOST = "https://qch.example"
    settings.QCH_SERVICE_TOKEN = "configured-token"
    monkeypatch.setenv("QCH_APP_ID", settings.QCH_APP_ID)
    monkeypatch.setenv("QCH_API_HOST", settings.QCH_API_HOST)
    monkeypatch.setenv("QCH_SERVICE_TOKEN", settings.QCH_SERVICE_TOKEN)

    assert managed_broker_deployment_configuration()["available"] is True

    settings.IBKR_GATEWAY_IMAGE = "docker.io/example/trading-engine-ib-gateway:latest"
    deployment = managed_broker_deployment_configuration()
    assert deployment["available"] is False
    assert deployment["invalid"] == ["IBKR_GATEWAY_IMAGE"]


def test_docker_hub_digest_and_fixed_tag_are_accepted_for_either_visibility():
    from apps.broker_gateway.configuration import parse_docker_hub_image_reference

    digest = "docker.io/example/trading-engine-ib-gateway@sha256:" + ("a" * 64)
    tag = "docker.io/example/trading-engine-ib-gateway:v1.0.0"
    for assumed_visibility in ("private", "public"):
        assert assumed_visibility not in digest
        assert parse_docker_hub_image_reference(digest) == digest
        assert parse_docker_hub_image_reference(tag) == tag


@pytest.mark.parametrize("image", [
    "docker.io/example/trading-engine-ib-gateway:latest",
    "docker.io/example/trading-engine-ib-gateway:LATEST",
    "ghcr.io/example/trading-engine-ib-gateway:v1.0.0",
    "quay.io/example/trading-engine-ib-gateway:v1.0.0",
    "localhost/example/trading-engine-ib-gateway:v1.0.0",
    "registry.example/example/trading-engine-ib-gateway:v1.0.0",
    "https://docker.io/example/trading-engine-ib-gateway:v1.0.0",
    "example/trading-engine-ib-gateway:v1.0.0",
    "docker.io/example/trading-engine-ib-gateway",
    "docker.io//trading-engine-ib-gateway:v1.0.0",
    "docker.io/example/:v1.0.0",
    "docker.io/example/nested/trading-engine-ib-gateway:v1.0.0",
    "docker.io/example/trading-engine-ib-gateway@sha256:REPLACE_WITH_DIGEST",
    "docker.io/example/trading-engine-ib-gateway@sha256:" + ("a" * 63),
    "docker.io/example/trading-engine-ib-gateway@sha256:" + ("a" * 65),
    "docker.io/example/trading-engine-ib-gateway:REPLACE_WITH_VERSION",
    "docker.io/replace-me/trading-engine-ib-gateway:v1.0.0",
    "docker.io/example/placeholder-repository:v1.0.0",
    "docker.io/example/trading-engine-ib-gateway:v1.0.0?public=true",
    "docker.io/example/trading-engine-ib-gateway:v1.0.0#fragment",
    "docker.io/example/bad..repository:v1.0.0",
    "docker.io/example/trading-engine-ib-gateway:-v1",
    " docker.io/example/trading-engine-ib-gateway:v1.0.0",
    "docker.io/example/trading-engine-ib-gateway:v1.0.0 ",
    "docker.io/example/trading-engine-ib-gateway:v1.0.0\nnext",
])
def test_non_docker_hub_mutable_or_malformed_image_references_are_rejected(image):
    from apps.broker_gateway.configuration import (
        GatewayImageConfigurationError,
        parse_docker_hub_image_reference,
    )

    with pytest.raises(GatewayImageConfigurationError):
        parse_docker_hub_image_reference(image)


def test_configured_gateway_image_rejects_outer_spacing_and_line_breaks(settings):
    from apps.broker_gateway.configuration import (
        GatewayImageConfigurationError,
        configured_gateway_image,
    )

    image = "docker.io/example/trading-engine-ib-gateway:v1.0.0"
    settings.IBKR_GATEWAY_IMAGE = f"  {image}\t"
    with pytest.raises(GatewayImageConfigurationError):
        configured_gateway_image()

    settings.IBKR_GATEWAY_IMAGE = image + "\n"
    with pytest.raises(GatewayImageConfigurationError):
        configured_gateway_image()
