"""REDIS_PASSWORD is injected into every Redis URL, and Celery actually uses the injected URL."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "p@ss:w/rd#1"
QUOTED = "p%40ss%3Aw%2Frd%231"


def _run(code, **env):
    environment = {k: v for k, v in os.environ.items() if not k.startswith(("REDIS_", "CELERY_"))}
    # Empty values stop a developer's local .env from filling these in (load_dotenv never overrides a set variable).
    blank = {k: "" for k in ("REDIS_URL", "REDIS_PASSWORD", "REDIS_USERNAME", "REDIS_KEY_PREFIX", "CELERY_BROKER_URL",
                             "CELERY_BROKER_DB")}
    environment.update({"DJANGO_SETTINGS_MODULE": "config.test_settings", "PYTHONPATH": str(ROOT), **blank, **env})
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=environment, capture_output=True, text=True,
                            timeout=120)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1]


def test_password_is_injected_and_quoted_into_both_urls():
    out = _run("from django.conf import settings as s; print(s.REDIS_URL, s.CELERY_BROKER_URL)",
               REDIS_PASSWORD=PASSWORD, REDIS_URL="redis://redis:6379/7", CELERY_BROKER_URL="redis://redis:6379/9")
    assert out == f"redis://:{QUOTED}@redis:6379/7 redis://:{QUOTED}@redis:6379/9"


def test_url_with_its_own_credentials_is_left_alone():
    out = _run("from django.conf import settings as s; print(s.REDIS_URL)", REDIS_PASSWORD=PASSWORD,
               REDIS_URL="redis://user:own@redis:6379/7")
    assert out == "redis://user:own@redis:6379/7"


def test_no_password_leaves_urls_unchanged_and_non_redis_backend_untouched():
    out = _run("from django.conf import settings as s; print(s.REDIS_URL, s.CELERY_RESULT_BACKEND)",
               REDIS_URL="redis://redis:6379/7", CELERY_RESULT_BACKEND="django-db")
    assert out == "redis://redis:6379/7 django-db"


def test_celery_app_uses_the_url_with_the_password():
    # Regression: Celery prefers the raw CELERY_BROKER_URL env var, which has no password.
    out = _run("from config.celery import app; print(app.conf.broker_url)", REDIS_PASSWORD=PASSWORD,
               CELERY_BROKER_URL="redis://redis:6379/9")
    assert out == f"redis://:{QUOTED}@redis:6379/9"


def test_broker_defaults_to_the_redis_url_server_and_credentials():
    # The platform injects only REDIS_URL; the broker must reuse its host and credentials, not a stale host.
    out = _run("from config.celery import app; print(app.conf.broker_url)",
               REDIS_URL="redis://app_user:s3cret@app-redis:6379/0")
    assert out == "redis://app_user:s3cret@app-redis:6379/0"


def test_broker_db_override_and_explicit_url_still_win():
    out = _run("from django.conf import settings as s; print(s.CELERY_BROKER_URL)",
               REDIS_URL="redis://u:p@app-redis:6379/0", CELERY_BROKER_DB="9")
    assert out == "redis://u:p@app-redis:6379/9"
    out = _run("from django.conf import settings as s; print(s.CELERY_BROKER_URL)",
               REDIS_URL="redis://u:p@app-redis:6379/0", CELERY_BROKER_URL="redis://other:6379/3")
    assert out == "redis://other:6379/3"


def test_key_prefix_reaches_celery_transport_options():
    out = _run("from config.celery import app; print(app.conf.broker_transport_options)",
               REDIS_URL="redis://u:p@app-redis:6379/0", REDIS_KEY_PREFIX="app:x:")
    assert out == "{'global_keyprefix': 'app:x:'}"
