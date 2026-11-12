import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


# parents[1], not [2]: this file is <repo>/tests/, so [2] pointed one level
# above the checkout -- "/" inside the test container, which made both
# tests below skip themselves as "not available in this test image".
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _removed_variables():
    # Keep the complete obsolete names out of the test source so the
    # repository-wide zero-reference assertion includes this file.
    return (
        "LIVE_BAR_" + "TIMEOUT_MULTIPLIER",
        "LIVE_BAR_" + "TIMEOUT_GRACE_SECONDS",
        "LIVE_BAR_" + "TIMEOUT_MIN_SECONDS",
        "VITE_API_" + "BASE_URL",
        "RESEARCH_DAILY_" + "PROVIDER",
        "USE_" + "SQLITE",
    )


def test_removed_environment_variables_have_no_tracked_references():
    # Needs both the checkout and the git binary; the test image ships neither.
    if shutil.which("git") is None or not (REPOSITORY_ROOT / ".git").exists():
        pytest.skip("full tracked repository is not available in this test image")
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8").split("\0")
    references = {}
    for relative_path in filter(None, tracked):
        try:
            contents = (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for variable in _removed_variables():
            if variable in contents:
                references.setdefault(variable, []).append(relative_path)
    assert references == {}


COMPOSE_FILE = "docker-compose.options.yml"


def test_compose_renders_one_static_gateway_vnc_password_source():
    # Targeted docker-compose.yml and a "paper-ibkr" profile until both were
    # removed (6ba177f); the stack is now a single docker-compose.options.yml
    # with no profiles, and the inputs were renamed to LOCAL_*. The invariant
    # under test is unchanged: the gateway's VNC password has exactly one
    # source, and it is not the service token.
    if shutil.which("docker") is None or not (
        REPOSITORY_ROOT / COMPOSE_FILE
    ).exists():
        pytest.skip("Docker Compose source is not available in this test image")
    environment = os.environ.copy()
    environment.update(
        {
            "LOCAL_DJANGO_SECRET": "dummy-django-secret",
            "LOCAL_GATEWAY_TOKEN": "dummy-service-token",
            "LOCAL_NOVNC_PASSWORD": "dummy-vnc-password",
            "LOCAL_DB_PASSWORD": "dummy-db-password",
            "LOCAL_ENCRYPTION_KEY": "dummy-encryption-key",
            "REDIS_PASSWORD": "dummy-redis-password",
            "username": "dummy-ib-username",
            "password": "dummy-ib-password",
        }
    )
    rendered = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            COMPOSE_FILE,
            "--env-file",
            ".env.example",
            "config",
            "--format",
            "json",
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    services = json.loads(rendered.stdout)["services"]
    backend = services["backend"]["environment"]
    gateway = services["paper-ibkr-gateway"]["environment"]

    assert backend["LOCAL_PAPER_GATEWAY_NOVNC_PASSWORD"] == "dummy-vnc-password"
    assert gateway["NOVNC_PASSWORD"] == "dummy-vnc-password"
    assert gateway["GATEWAY_SERVICE_TOKEN"] == "dummy-service-token"
    assert gateway["GATEWAY_SERVICE_TOKEN"] != gateway["NOVNC_PASSWORD"]
