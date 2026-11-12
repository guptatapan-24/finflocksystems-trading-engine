import pytest
from pathlib import Path

pytestmark=pytest.mark.django_db
def test_public_health_is_basic_liveness(client):
    assert client.get("/healthz").json()["data"]["status"] == "alive"


def test_root_health_and_authenticated_api_are_child_only(client):
    auth = {"HTTP_AUTHORIZATION": "Bearer test-token"}
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code in {200, 503}
    assert client.get("/api/v1/health/").status_code == 401
    assert client.get("/api/v1/diagnostics/").status_code == 401
    assert client.get("/api/v1/health/", **auth).status_code == 200
    assert client.get("/public-prefix/healthz").status_code == 404


def test_public_nginx_contract_and_spelling():
    root = Path(__file__).resolve().parents[1]
    nginx = (root / "nginx.conf.template").read_text(encoding="utf-8")
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = (root / "entrypoint.sh").read_text(encoding="utf-8")
    combined = nginx + dockerfile + entrypoint
    assert "APP_BASE_PATH" not in combined
    assert "qfsplatform.com" not in combined
    assert "/novnc/websockify" in nginx and "/novnc/(.*)" in nginx
    assert "location = /readyz" in nginx
    assert '"service":"ibkr-gateway"' not in nginx
    assert "location / { return 404; }" in nginx
