import os
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv
from gateway_service.modes import normalize_trading_mode, tws_port_for_mode
from runtime_config import normalize_broker_adapter, remote_desktop_enabled

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env", override=False)


def required_environment(name):
    value = os.getenv(name, "")
    if not value.strip():
        raise ImproperlyConfigured(f"{name} is required")
    return value


SECRET_KEY = required_environment("DJANGO_SECRET_KEY")
DEBUG = False
ALLOWED_HOSTS = ["*"]
USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
INSTALLED_APPS = ["django.contrib.contenttypes", "gateway_service"]
MIDDLEWARE = ["django.middleware.security.SecurityMiddleware", "django.middleware.common.CommonMiddleware"]
ROOT_URLCONF = "config.urls"
TEMPLATES = []
WSGI_APPLICATION = "config.wsgi.application"
# Postgres when the Backend supplies POSTGRES_HOST. SQLite serialized the web
# threads and the broker worker on one file lock; a write that outlived the
# busy timeout raised "database is locked", which the Backend then treated as
# an IBKR outage. SQLite remains only for local runs and tests.
if os.getenv("POSTGRES_HOST", "").strip():
    DATABASES = {"default":{
        "ENGINE":"django.db.backends.postgresql",
        "HOST":os.environ["POSTGRES_HOST"].strip(),
        "PORT":os.getenv("POSTGRES_PORT", "5432"),
        "NAME":required_environment("POSTGRES_DB"),
        "USER":required_environment("POSTGRES_USER"),
        "PASSWORD":required_environment("POSTGRES_PASSWORD"),
        "CONN_MAX_AGE":60,
        "CONN_HEALTH_CHECKS":True,
        "OPTIONS":{"connect_timeout":10},
    }}
else:
    DATABASES = {"default":{
        "ENGINE":"django.db.backends.sqlite3",
        "NAME":os.getenv("GATEWAY_DB_PATH", str(BASE_DIR / "gateway.sqlite3")),
        "CONN_MAX_AGE":60,
        "OPTIONS":{"timeout":20},
    }}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
# Django sets the process time zone from TIME_ZONE (default America/Chicago),
# and ib_async reads zone-less TWS timestamps as process-local time.
TIME_ZONE = "UTC"
# The zone IB Gateway writes zone-less timestamps (execution times) in. The
# image runs IB Gateway without a TZ, so it writes UTC.
IBKR_TWS_TIMEZONE = os.getenv("IBKR_TWS_TIMEZONE", "UTC").strip() or "UTC"
GATEWAY_SERVICE_TOKEN = required_environment("GATEWAY_SERVICE_TOKEN")
IBC_TRADING_MODE = normalize_trading_mode(os.getenv("IBC_TRADING_MODE", "paper"))
IBKR_CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID", "17"))
BROKER_ADAPTER = normalize_broker_adapter(os.getenv("BROKER_ADAPTER", "ib_async"))
GATEWAY_REMOTE_DESKTOP_ENABLED = remote_desktop_enabled(os.environ)
TWS_PORT = tws_port_for_mode(IBC_TRADING_MODE)
IBKR_MARKET_DATA_TYPE = os.getenv(
    "IBKR_MARKET_DATA_TYPE",
    "DELAYED" if IBC_TRADING_MODE == "paper" else "LIVE",
).strip().upper()
if IBKR_MARKET_DATA_TYPE not in {"LIVE", "DELAYED"}:
    raise ImproperlyConfigured("IBKR_MARKET_DATA_TYPE must be exactly LIVE or DELAYED")
if IBC_TRADING_MODE != "paper" and IBKR_MARKET_DATA_TYPE == "DELAYED":
    raise ImproperlyConfigured("Delayed IBKR market data is allowed only in paper mode")
BROKER_REFRESH_SECONDS = max(2, int(os.getenv("BROKER_REFRESH_SECONDS", "5")))
GATEWAY_EVENT_RETENTION_DAYS = int(os.getenv("GATEWAY_EVENT_RETENTION_DAYS", "7"))
GATEWAY_HEALTH_RETENTION_DAYS = int(os.getenv("GATEWAY_HEALTH_RETENTION_DAYS", "7"))
GATEWAY_COMPACTION_SECONDS = max(300, int(os.getenv("GATEWAY_COMPACTION_SECONDS", "3600")))
GATEWAY_COMPACTION_BATCH_SIZE = int(os.getenv("GATEWAY_COMPACTION_BATCH_SIZE", "1000"))
GATEWAY_CONTRACT_SEARCH_MAX_RESULTS = max(
    1, min(50, int(os.getenv("GATEWAY_CONTRACT_SEARCH_MAX_RESULTS", "12")))
)
GATEWAY_IBKR_REQUEST_TIMEOUT_SEARCH_CONTRACTS_SECONDS = max(
    1, int(os.getenv("GATEWAY_IBKR_REQUEST_TIMEOUT_SEARCH_CONTRACTS_SECONDS", "12"))
)
GATEWAY_IBKR_REQUEST_TIMEOUT_QUALIFY_SECONDS = max(
    1, int(os.getenv("GATEWAY_IBKR_REQUEST_TIMEOUT_QUALIFY_SECONDS", "15"))
)
GATEWAY_IBKR_REQUEST_TIMEOUT_OPTION_CHAIN_SECONDS = max(
    1, int(os.getenv("GATEWAY_IBKR_REQUEST_TIMEOUT_OPTION_CHAIN_SECONDS", "30"))
)
# Without this, DEBUG=False sends django.request tracebacks only to mail_admins,
# so a failing endpoint left nothing but "500" in the container log.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"stderr": {"class": "logging.StreamHandler"}},
    "loggers": {"django.request": {"handlers": ["stderr"], "level": "ERROR", "propagate": False}},
    "root": {"handlers": ["stderr"], "level": "WARNING"},
}
