from pathlib import Path


def test_entrypoint_stores_the_configured_novnc_password():
    entrypoint = (Path(__file__).resolve().parents[1] / "entrypoint.sh").read_text()

    assert 'x11vnc -storepasswd "$novnc_password"' in entrypoint
    assert "| x11vnc -storepasswd -" not in entrypoint
    assert "--write-normalized-vnc-password" in entrypoint
    assert 'novnc_password="$NOVNC_PASSWORD"' not in entrypoint


def test_entrypoint_validates_before_writes_migrations_or_supervisor():
    entrypoint = (Path(__file__).resolve().parents[1] / "entrypoint.sh").read_text()

    validation = entrypoint.index("runtime_config.py")
    assert validation < entrypoint.index("mkdir -p")
    assert validation < entrypoint.index("manage.py migrate")
    assert validation < entrypoint.index("supervisord")
    assert 'GATEWAY_DB_PATH="${GATEWAY_DB_PATH:-/data/gateway.sqlite3}"' in entrypoint
    validate_only = entrypoint.split(
        'if [ "${1:-}" = "--validate-only" ]', 1
    )[1].split("\nfi", 1)[0]
    assert 'rm -f "$normalized_vnc_password_file"' in validate_only
    assert "unset NOVNC_PASSWORD" in validate_only


def test_entrypoint_only_creates_ibc_config_for_real_adapter():
    entrypoint = (Path(__file__).resolve().parents[1] / "entrypoint.sh").read_text()

    assert 'if [ "$BROKER_ADAPTER" = "ib_async" ]' in entrypoint
    assert "python manage.py configure_ibc" in entrypoint
    assert "rm -f /home/ibgateway/ibc/config.ini" in entrypoint


def test_entrypoint_prepares_x11_runtime_before_supervisor():
    entrypoint = (Path(__file__).resolve().parents[1] / "entrypoint.sh").read_text()

    supervisor = entrypoint.index("supervisord")
    for command in (
        "/home/ibgateway/Jts",
        "mkdir -p /data /home/ibgateway/.vnc /home/ibgateway/ibc /home/ibgateway/Jts /tmp/.X11-unix",
        "chown root:root /tmp/.X11-unix",
        "chmod 1777 /tmp/.X11-unix",
        "rm -f /tmp/.X1-lock /tmp/.X11-unix/X1",
    ):
        assert command in entrypoint
        assert entrypoint.index(command) < supervisor


def test_x11_clients_wait_for_display_and_gateway_has_bounded_retries():
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    supervisor = (root / "supervisord.conf").read_text(encoding="utf-8")
    wait_script = (root / "wait-for-x.sh").read_text(encoding="utf-8")

    assert "x11-utils" in dockerfile
    assert "wait-for-x.sh" in dockerfile
    assert 'xdpyinfo -display "$display"' in wait_script
    for command in (
        "/app/wait-for-x.sh /usr/bin/fluxbox -display :1",
        "/app/wait-for-x.sh /usr/bin/x11vnc -display :1",
        "/app/wait-for-x.sh /app/start-ibgateway.sh",
    ):
        assert command in supervisor

    gateway = supervisor.split("[program:ibgateway]", 1)[1].split("[program:web]", 1)[0]
    assert "autorestart=unexpected" in gateway
    assert "startretries=5" in gateway
    assert "startsecs=10" in gateway
    assert "[unix_http_server]" in supervisor
    assert "serverurl=unix:///tmp/supervisor.sock" in supervisor


def test_existing_api_path_contract_is_unchanged():
    urls = (Path(__file__).resolve().parents[1] / "config" / "urls.py").read_text()

    for route in ("healthz", "readyz", "api/v1/", "diagnostics/", "session/", "orders/", "events/", "market-data/history/"):
        assert route in urls


def test_ibc_launch_uses_detected_gateway_major_without_hard_coded_fallback():
    root = Path(__file__).resolve().parents[1]
    start_script = (root / "start-ibgateway.sh").read_text(encoding="utf-8")
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")

    assert "ibgateway-version install" in dockerfile
    assert "ibgateway-version verify" in start_script
    assert 'ibcstart.sh "$installed_major"' in start_script
    assert "1045" not in dockerfile
    assert "1045" not in start_script


def test_docker_healthcheck_uses_liveness_not_readiness():
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")

    healthcheck = next(line for line in dockerfile.splitlines() if line.startswith("HEALTHCHECK "))
    assert "/healthz" in healthcheck
    assert "/readyz" not in healthcheck


def test_entrypoint_creates_the_postgres_database_before_migrating():
    root = Path(__file__).resolve().parents[1]
    entrypoint = (root / "entrypoint.sh").read_text()
    dockerfile = (root / "Dockerfile").read_text()

    assert entrypoint.index("python ensure_database.py") < entrypoint.index("manage.py migrate")
    assert "ensure_database.py" in dockerfile.split("COPY manage.py", 1)[1].split("\n", 1)[0]


def test_settings_use_postgres_when_the_backend_supplies_a_host():
    import os
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    script = "import json; from django.conf import settings; print(json.dumps(settings.DATABASES['default']))"
    base = {
        **os.environ, "DJANGO_SETTINGS_MODULE": "config.settings", "DJANGO_SECRET_KEY": "k",
        "GATEWAY_SERVICE_TOKEN": "t", "BROKER_ADAPTER": "mock", "IBC_TRADING_MODE": "paper",
    }
    for key in ("POSTGRES_HOST", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"):
        base.pop(key, None)

    def database(extra):
        out = subprocess.run([sys.executable, "-c", "import django; django.setup(); " + script],
            cwd=root, env={**base, **extra}, capture_output=True, text=True, check=True).stdout
        import json
        return json.loads(out)

    postgres = database({"POSTGRES_HOST": "postgres", "POSTGRES_DB": "IB_Gateway_abc",
        "POSTGRES_USER": "admin", "POSTGRES_PASSWORD": "secret"})
    assert postgres["ENGINE"] == "django.db.backends.postgresql"
    assert (postgres["HOST"], postgres["NAME"], postgres["USER"]) == ("postgres", "IB_Gateway_abc", "admin")
    assert database({})["ENGINE"] == "django.db.backends.sqlite3"


def test_command_idempotency_key_fits_the_backend_session_prefixed_key():
    # The Backend sends "session:<uuid>:<key>" truncated to 255 characters;
    # PostgreSQL rejects anything longer than the column, SQLite never did.
    from gateway_service.models import GatewayCommand

    assert GatewayCommand._meta.get_field("idempotency_key").max_length >= 255
