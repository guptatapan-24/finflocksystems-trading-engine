import os
from pathlib import Path
import dj_database_url
from corsheaders.defaults import default_headers
from dotenv import load_dotenv

from apps.research.configuration import RecommendationSystemConfiguration

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR.parent / ".env", override=False)
load_dotenv(BASE_DIR / ".env", override=False)
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "test-only-secret")
DEBUG = os.getenv("DJANGO_DEBUG", "false").lower() == "true"
ALLOWED_HOSTS = [x.strip() for x in os.getenv("ALLOWED_HOSTS", "*").split(",") if x.strip()]
APP_BASE_PATH = "/" + os.getenv("APP_BASE_PATH", "").strip("/") if os.getenv("APP_BASE_PATH", "").strip("/") else ""
USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
FORCE_SCRIPT_NAME = APP_BASE_PATH or None

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "corsheaders",
    "rest_framework",
    "django_celery_results",

    "apps.core",
    "apps.instruments",
    "apps.broker_gateway",
    "apps.accounts",
    "apps.portfolios",
    "apps.strategies",
    "apps.allocation",
    "apps.risk",
    "apps.oms",
    "apps.execution",
    "apps.reconciliation",
    "apps.audit",
    "apps.event_bus",
    "apps.market_streams",
    "apps.rebalancing",
    "apps.position_sizing",
    "apps.market_data",
    "apps.portfolio_optimization",
    "apps.portfolio_construction",
    "apps.research",
    "apps.intelligence",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",

    "apps.core.middleware.ApiSlashCompatibilityMiddleware",

    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = []
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'db.sqlite3'}")
DATABASES = {"default": dj_database_url.parse(DATABASE_URL, conn_max_age=60)}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
# Unset, Django uses America/Chicago and also sets it as the process zone.
TIME_ZONE = "UTC"
CORS_ALLOWED_ORIGINS = [x.strip() for x in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if x.strip()]
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_HEADERS = (*default_headers, "idempotency-key")
CSRF_TRUSTED_ORIGINS = [x.strip() for x in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if x.strip()]
REST_FRAMEWORK = {"DEFAULT_AUTHENTICATION_CLASSES": [], "DEFAULT_PERMISSION_CLASSES": []}
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "order_console": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
        },
    },
    "loggers": {
        "apps.execution": {
            "handlers": ["order_console"],
            "level": "INFO",
            "propagate": False,
        },
        "apps.broker_gateway.client": {
            "handlers": ["order_console"],
            "level": "INFO",
            "propagate": False,
        },
        # The managed-session path used to fail silently: a QCH addressing
        # error, a name mismatch and a genuinely absent child all surfaced only
        # as status=ERROR in a Celery result. These three carry the values that
        # tell them apart.
        "apps.broker_gateway.qch": {
            "handlers": ["order_console"],
            "level": "INFO",
            "propagate": False,
        },
        "apps.broker_gateway.services": {
            "handlers": ["order_console"],
            "level": "INFO",
            "propagate": False,
        },
        "apps.broker_gateway.tasks": {
            "handlers": ["order_console"],
            "level": "INFO",
            "propagate": False,
        },
        "apps.broker_gateway.sync": {
            "handlers": ["order_console"],
            "level": "INFO",
            "propagate": False,
        },
        "apps.market_streams.subscriptions": {
            "handlers": ["order_console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}


def _redis_url(name, default):
    """Redis URL from the environment, with the password injected from REDIS_PASSWORD.

    The password is never part of a committed URL. When REDIS_PASSWORD is set and the URL carries no credentials of
    its own, it is added here (URL-quoted, so special characters are safe). A URL that already has credentials wins.
    """
    from urllib.parse import quote, urlsplit

    url = os.getenv(name, default)
    password = os.getenv("REDIS_PASSWORD", "")
    if not password or not url.startswith(("redis://", "rediss://")):
        return url
    parts = urlsplit(url)
    if parts.username or parts.password:
        return url
    user = quote(os.getenv("REDIS_USERNAME", ""), safe="")
    host = parts.netloc
    return url.replace(f"//{host}", f"//{user}:{quote(password, safe='')}@{host}", 1)


REDIS_URL = _redis_url("REDIS_URL", "redis://localhost:6379/0")
# Key prefix of a platform-managed Redis (REDIS_KEY_PREFIX). Empty means unprefixed keys, as before.
REDIS_KEY_PREFIX = os.getenv("REDIS_KEY_PREFIX", "")


def _broker_url():
    """Celery broker URL. An explicit CELERY_BROKER_URL wins. Otherwise the broker uses the same Redis server and
    credentials as REDIS_URL (the platform injects only REDIS_URL), on CELERY_BROKER_DB if set, else REDIS_URL's db."""
    explicit = os.getenv("CELERY_BROKER_URL", "").strip()
    if explicit:
        return _redis_url("CELERY_BROKER_URL", explicit)
    if not os.getenv("REDIS_URL", "").strip():
        return "redis://localhost:6379/1"
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(REDIS_URL)
    database = os.getenv("CELERY_BROKER_DB", "").strip()
    return urlunsplit(parts._replace(path=f"/{database}" if database else parts.path))


CELERY_BROKER_URL = _broker_url()
if REDIS_KEY_PREFIX:
    # kombu prepends this to every queue, binding and unacked key, so an ACL limited to the prefix is respected.
    CELERY_BROKER_TRANSPORT_OPTIONS = {"global_keyprefix": REDIS_KEY_PREFIX}
# CELERY_RESULT_BACKEND is "django-db" in deployment; _redis_url leaves any non-redis value untouched.
CELERY_RESULT_BACKEND = _redis_url("CELERY_RESULT_BACKEND", "redis://localhost:6379/2")

# The beat schedule below fires roughly five tasks a second at idle, and with
# CELERY_RESULT_BACKEND=django-db every one of them wrote a row into
# django_celery_results_taskresult -- around 450k inserts a day, plus the daily
# mass delete celery.backend_cleanup does to expire them. Nothing in this
# codebase ever constructs an AsyncResult or reads a stored result, so that
# table was pure write amplification on the same Postgres the trading path
# uses. Ignoring results drops the inserts; storing errors anyway keeps the
# failure rows, which are the only ones that were ever worth having.
CELERY_TASK_IGNORE_RESULT = os.getenv("CELERY_TASK_IGNORE_RESULT", "true").lower() == "true"
CELERY_TASK_STORE_ERRORS_EVEN_IF_IGNORED = True
CELERY_RESULT_EXPIRES = int(os.getenv("CELERY_RESULT_EXPIRES_SECONDS", "86400"))
# No Flower, no app.control.inspect and no app.events consumer anywhere in the
# tree, so the worker event stream had no reader. Emitting it cost every worker
# a fanout publish per task on the Redis broker.
CELERY_WORKER_SEND_TASK_EVENTS = False
CELERY_TASK_SEND_SENT_EVENT = False
# The remote-control channel (the worker's "pidbox") is the one thing left that makes a worker SUBSCRIBE to Redis pub/sub.
# The hosted Redis user has no channel permission, so every worker died on start with
# NoPermissionError('No permissions to access a channel') and supervisord restarted it forever. Nothing here uses
# app.control (inspect / ping / broadcast), and worker readiness comes from heartbeats the tasks write to the database.
CELERY_WORKER_ENABLE_REMOTE_CONTROL = os.getenv("CELERY_WORKER_ENABLE_REMOTE_CONTROL", "false").lower() == "true"

# Poll cadences for the beat entries below. These are the knobs to turn when a
# deployment needs less idle churn: the order path defaults stay at 1s because
# they sit between a fill and the next intent, the housekeeping ones do not.
STRATEGY_EVALUATION_INTERVAL_SECONDS = float(os.getenv("STRATEGY_EVALUATION_INTERVAL_SECONDS", "1"))
TARGET_COORDINATION_INTERVAL_SECONDS = float(os.getenv("TARGET_COORDINATION_INTERVAL_SECONDS", "1"))
ORDER_INTENT_INTERVAL_SECONDS = float(os.getenv("ORDER_INTENT_INTERVAL_SECONDS", "1"))
BROKER_COMMAND_DISPATCH_INTERVAL_SECONDS = float(os.getenv("BROKER_COMMAND_DISPATCH_INTERVAL_SECONDS", "1"))
OUTBOX_PUBLISH_INTERVAL_SECONDS = float(os.getenv("OUTBOX_PUBLISH_INTERVAL_SECONDS", "2"))
BROKER_SYNC_INTERVAL_SECONDS = float(os.getenv("BROKER_SYNC_INTERVAL_SECONDS", "5"))
MARKET_PROVIDER_MONITOR_INTERVAL_SECONDS = float(os.getenv("MARKET_PROVIDER_MONITOR_INTERVAL_SECONDS", "5"))
MARKET_SUBSCRIPTION_RESTORE_INTERVAL_SECONDS = float(os.getenv("MARKET_SUBSCRIPTION_RESTORE_INTERVAL_SECONDS", "15"))
# A SUBSCRIBING subscription with no event this long after its request is
# requested again; an ERROR one is retried no faster than the retry interval
# (each subscribe runs an IBKR history request, which IBKR paces).
MARKET_SUBSCRIPTION_STALL_SECONDS = float(os.getenv("MARKET_SUBSCRIPTION_STALL_SECONDS", "180"))
MARKET_SUBSCRIPTION_RETRY_SECONDS = float(os.getenv("MARKET_SUBSCRIPTION_RETRY_SECONDS", "60"))
STREAM_HEALTH_INTERVAL_SECONDS = float(os.getenv("STREAM_HEALTH_INTERVAL_SECONDS", "30"))
EXECUTION_RECOVERY_INTERVAL_SECONDS = float(os.getenv("EXECUTION_RECOVERY_INTERVAL_SECONDS", "30"))
# How long a worker-role heartbeat row may go unwritten while the worker is
# healthy and idle. Readiness only asks whether the row is fresher than
# EXECUTION_WORKER_HEARTBEAT_STALE_SECONDS (30s), so writing one every second
# oversampled it 30x. See apps.execution.readiness.record_worker_heartbeat.
EXECUTION_WORKER_HEARTBEAT_WRITE_INTERVAL_SECONDS = float(
    os.getenv("EXECUTION_WORKER_HEARTBEAT_WRITE_INTERVAL_SECONDS", "10")
)
STRATEGY_EVALUATION_MAX_ATTEMPTS = int(os.getenv("STRATEGY_EVALUATION_MAX_ATTEMPTS", "5"))
STRATEGY_EVALUATION_RETRY_BASE_SECONDS = int(os.getenv("STRATEGY_EVALUATION_RETRY_BASE_SECONDS", "5"))
STRATEGY_EVALUATION_RETRY_MAX_SECONDS = int(os.getenv("STRATEGY_EVALUATION_RETRY_MAX_SECONDS", "300"))
STRATEGY_EVALUATION_CLAIM_TIMEOUT_SECONDS = int(os.getenv("STRATEGY_EVALUATION_CLAIM_TIMEOUT_SECONDS", "300"))
STRATEGY_EVALUATION_BATCH_SIZE = int(os.getenv("STRATEGY_EVALUATION_BATCH_SIZE", "50"))
PORTFOLIO_TARGET_MAX_AGE_SECONDS = int(os.getenv("PORTFOLIO_TARGET_MAX_AGE_SECONDS", "900"))
PORTFOLIO_TARGET_COORDINATION_DEBOUNCE_SECONDS = int(
    os.getenv("PORTFOLIO_TARGET_COORDINATION_DEBOUNCE_SECONDS", "2")
)
PORTFOLIO_TARGET_COORDINATION_BATCH_SIZE = int(
    os.getenv("PORTFOLIO_TARGET_COORDINATION_BATCH_SIZE", "50")
)
PORTFOLIO_TARGET_COORDINATION_RETRY_BASE_SECONDS = int(
    os.getenv("PORTFOLIO_TARGET_COORDINATION_RETRY_BASE_SECONDS", "5")
)
PORTFOLIO_TARGET_COORDINATION_RETRY_MAX_SECONDS = int(
    os.getenv("PORTFOLIO_TARGET_COORDINATION_RETRY_MAX_SECONDS", "300")
)
EXECUTION_AVERAGE_VOLUME_WINDOW = int(
    os.getenv("EXECUTION_AVERAGE_VOLUME_WINDOW", "20")
)
if EXECUTION_AVERAGE_VOLUME_WINDOW < 1:
    raise RuntimeError("EXECUTION_AVERAGE_VOLUME_WINDOW must be positive")
EXECUTION_REGISTER_ADV_INPUT = (
    os.getenv("EXECUTION_REGISTER_ADV_INPUT", "true").lower() == "true"
)
EXECUTION_ADV_TIMEFRAME = os.getenv(
    "EXECUTION_ADV_TIMEFRAME", "RUNTIME"
).strip()
ORDER_INTENT_BATCH_SIZE = int(os.getenv("ORDER_INTENT_BATCH_SIZE", "50"))
ORDER_INTENT_CLAIM_TIMEOUT_SECONDS = int(
    os.getenv("ORDER_INTENT_CLAIM_TIMEOUT_SECONDS", "120")
)
BROKER_COMMAND_BATCH_SIZE = int(os.getenv("BROKER_COMMAND_BATCH_SIZE", "50"))
BROKER_COMMAND_RETRY_BASE_SECONDS = int(
    os.getenv("BROKER_COMMAND_RETRY_BASE_SECONDS", "5")
)
BROKER_COMMAND_RETRY_MAX_SECONDS = int(
    os.getenv("BROKER_COMMAND_RETRY_MAX_SECONDS", "300")
)
BROKER_COMMAND_CLAIM_TIMEOUT_SECONDS = int(
    os.getenv("BROKER_COMMAND_CLAIM_TIMEOUT_SECONDS", "120")
)
EXECUTION_REQUIRED_FLINK_JOBS = tuple(
    value.strip()
    for value in os.getenv(
        "EXECUTION_REQUIRED_FLINK_JOBS",
        # One job holds all five market-data stages; see
        # streaming/flink/jobs/market_pipeline.py.
        "market-pipeline-v1",
    ).split(",")
    if value.strip()
)
FLINK_CHECKPOINT_STALE_SECONDS = int(
    os.getenv("FLINK_CHECKPOINT_STALE_SECONDS", "180")
)
EXECUTION_READINESS_HTTP_TIMEOUT_SECONDS = float(
    os.getenv("EXECUTION_READINESS_HTTP_TIMEOUT_SECONDS", "2")
)
FLINK_DIAGNOSTICS_HTTP_TIMEOUT_SECONDS = float(
    os.getenv("FLINK_DIAGNOSTICS_HTTP_TIMEOUT_SECONDS", "5")
)
MARKET_RAW_PRODUCER_HEARTBEAT_STALE_SECONDS = int(
    os.getenv("MARKET_RAW_PRODUCER_HEARTBEAT_STALE_SECONDS", "30")
)
EXECUTION_WORKER_HEARTBEAT_STALE_SECONDS = int(
    os.getenv("EXECUTION_WORKER_HEARTBEAT_STALE_SECONDS", "30")
)
GATEWAY_CONNECTIVITY_STALE_SECONDS = int(
    os.getenv("GATEWAY_CONNECTIVITY_STALE_SECONDS", "30")
)
STRATEGY_JOB_BACKLOG_THRESHOLD = int(
    os.getenv("STRATEGY_JOB_BACKLOG_THRESHOLD", "100")
)
TARGET_COORDINATION_BACKLOG_THRESHOLD = int(
    os.getenv("TARGET_COORDINATION_BACKLOG_THRESHOLD", "100")
)
PENDING_INTENT_MAX_AGE_SECONDS = int(
    os.getenv("PENDING_INTENT_MAX_AGE_SECONDS", "60")
)
BROKER_COMMAND_MAX_AGE_SECONDS = int(
    os.getenv("BROKER_COMMAND_MAX_AGE_SECONDS", "60")
)
# Flink keeps bounded working state; PostgreSQL is the system of record and the
# backend re-feeds Flink through the transactional outbox
# (apps/market_streams/rehydration.py, docs/flink-state-outbox.md).
FLINK_REHYDRATION_ENABLED = os.getenv("FLINK_REHYDRATION_ENABLED", "true").lower() == "true"
FLINK_REHYDRATION_CHECK_INTERVAL_SECONDS = float(os.getenv("FLINK_REHYDRATION_CHECK_INTERVAL_SECONDS", "30"))
# Bars re-published per (instrument, timeframe). Keep <= INDICATOR_HISTORY_MAX_BARS (Flink side).
FLINK_REHYDRATION_SEED_BARS = int(os.getenv("FLINK_REHYDRATION_SEED_BARS", "500"))
FLINK_REHYDRATION_SEED_CHUNK_BARS = int(os.getenv("FLINK_REHYDRATION_SEED_CHUNK_BARS", "100"))
# Re-publishing the registry keeps strategy.inputs.v1 recoverable regardless of Kafka retention.
STRATEGY_INPUTS_SNAPSHOT_INTERVAL_SECONDS = float(os.getenv("STRATEGY_INPUTS_SNAPSHOT_INTERVAL_SECONDS", "900"))
CELERY_BEAT_SCHEDULE = {
    "reconcile": {"task": "apps.reconciliation.tasks.run_scheduled_reconciliation", "schedule": 60.0},
    "sync-broker": {"task": "apps.broker_gateway.tasks.sync_broker_events", "schedule": BROKER_SYNC_INTERVAL_SECONDS},
    "publish-outbox": {"task": "apps.event_bus.tasks.publish_outbox_events", "schedule": OUTBOX_PUBLISH_INTERVAL_SECONDS},
    "stream-health": {"task": "apps.event_bus.tasks.check_stream_health", "schedule": STREAM_HEALTH_INTERVAL_SECONDS},
    "restore-market-subscriptions": {"task": "apps.market_streams.tasks.restore_active_market_subscriptions", "schedule": MARKET_SUBSCRIPTION_RESTORE_INTERVAL_SECONDS},
    "warmup-timeouts": {"task": "apps.market_streams.tasks.check_warmup_timeouts", "schedule": 30.0},
    "strategy-evaluation": {"task": "apps.strategies.tasks.execute_strategy_evaluation_jobs", "schedule": STRATEGY_EVALUATION_INTERVAL_SECONDS},
    "recover-strategy-evaluation": {"task": "apps.strategies.tasks.recover_strategy_evaluation_jobs", "schedule": EXECUTION_RECOVERY_INTERVAL_SECONDS},
    "instrument-registry": {"task": "apps.instruments.tasks.publish_instrument_registry_snapshot", "schedule": 60.0},
    "strategy-inputs-snapshot": {"task": "apps.market_streams.tasks.publish_strategy_inputs_snapshot", "schedule": STRATEGY_INPUTS_SNAPSHOT_INTERVAL_SECONDS},
    "rehydrate-flink-state": {"task": "apps.market_streams.tasks.rehydrate_flink_state", "schedule": FLINK_REHYDRATION_CHECK_INTERVAL_SECONDS},
    "recover-rebalances": {"task": "apps.rebalancing.tasks.recover_incomplete_rebalances", "schedule": 60.0},
    "coordinate-portfolio-targets": {"task": "apps.rebalancing.tasks.coordinate_portfolio_targets", "schedule": TARGET_COORDINATION_INTERVAL_SECONDS},
    "execute-order-intents": {"task": "apps.execution.tasks.execute_order_intents", "schedule": ORDER_INTENT_INTERVAL_SECONDS},
    "recover-order-intents": {"task": "apps.execution.tasks.recover_order_intents", "schedule": EXECUTION_RECOVERY_INTERVAL_SECONDS},
    "dispatch-broker-commands": {"task": "apps.execution.tasks.dispatch_broker_commands", "schedule": BROKER_COMMAND_DISPATCH_INTERVAL_SECONDS},
    "recover-broker-commands": {"task": "apps.execution.tasks.recover_broker_commands", "schedule": EXECUTION_RECOVERY_INTERVAL_SECONDS},
    "sync-finnhub-history": {"task": "apps.market_data.tasks.sync_active_finnhub_universes", "schedule": 21600.0},
    "check-finnhub-history": {"task": "apps.market_data.tasks.check_finnhub_history_staleness", "schedule": 21600.0},
    "verify-finnhub-mappings": {"task": "apps.market_data.tasks.verify_pending_finnhub_mappings", "schedule": 21600.0},
    "monitor-market-data-providers": {"task": "apps.market_streams.tasks.monitor_market_data_providers", "schedule": MARKET_PROVIDER_MONITOR_INTERVAL_SECONDS},
    "compact-operational-records": {"task": "apps.event_bus.tasks.compact_operational_records", "schedule": 86400.0},
    "research-universe-mapping": {"task": "apps.research.tasks.refresh_universe_mapping", "schedule": 86400.0},
    "research-daily-refresh": {"task": "apps.research.tasks.refresh_research_pipeline", "schedule": 86400.0},
    "research-intraday-refresh": {"task": "apps.research.tasks.refresh_intraday_data", "schedule": 3600.0},
    "research-fundamentals": {"task": "apps.research.tasks.refresh_fundamentals", "schedule": 86400.0},
    "research-events": {"task": "apps.research.tasks.refresh_events", "schedule": 3600.0},
    "research-features": {"task": "apps.research.tasks.calculate_features", "schedule": 86400.0},
    "research-experiments": {"task": "apps.research.tasks.schedule_research_experiments", "schedule": 86400.0},
    "research-experiment-dispatch": {"task": "apps.research.tasks.dispatch_research_experiments", "schedule": 60.0},
    "research-scoring": {"task": "apps.research.tasks.score_current_candidates", "schedule": 86400.0},
    "recommendation-cache": {"task": "apps.research.tasks.warm_recommendation_cache", "schedule": 86400.0},
}
ALLOW_LIVE_TRADING = os.getenv("ALLOW_LIVE_TRADING", "false").lower() == "true"
BROKER_SESSION_ENCRYPTION_KEY = os.getenv("BROKER_SESSION_ENCRYPTION_KEY", "")
BROKER_CREDENTIAL_TTL_SECONDS = int(os.getenv("BROKER_CREDENTIAL_TTL_SECONDS", "900"))
BROKER_SESSION_CREATING_STALE_SECONDS = int(os.getenv("BROKER_SESSION_CREATING_STALE_SECONDS", "60"))
BROKER_SESSION_START_TIMEOUT_SECONDS = float(os.getenv("BROKER_SESSION_START_TIMEOUT_SECONDS", "45"))
BROKER_SESSION_HEALTH_TIMEOUT_SECONDS = float(os.getenv("BROKER_SESSION_HEALTH_TIMEOUT_SECONDS", "5"))
NOVNC_ACCESS_TOKEN_TTL_SECONDS = int(os.getenv("NOVNC_ACCESS_TOKEN_TTL_SECONDS", "300"))
NOVNC_PROXY_CONNECT_TIMEOUT_SECONDS = float(os.getenv("NOVNC_PROXY_CONNECT_TIMEOUT_SECONDS", "10"))
NOVNC_PROXY_IDLE_TIMEOUT_SECONDS = float(os.getenv("NOVNC_PROXY_IDLE_TIMEOUT_SECONDS", "300"))
NOVNC_PROXY_MAX_BODY_BYTES = int(os.getenv("NOVNC_PROXY_MAX_BODY_BYTES", str(10 * 1024 * 1024)))
QCH_APP_ID = os.getenv("QCH_APP_ID", "")
QCH_API_HOST = os.getenv("QCH_API_HOST", "").rstrip("/")
QCH_SERVICE_TOKEN = os.getenv("QCH_SERVICE_TOKEN", "")
QCH_REQUEST_TIMEOUT_SECONDS = float(os.getenv("QCH_REQUEST_TIMEOUT_SECONDS", "10"))
QCH_SUBCONTAINER_NETWORK = os.getenv("QCH_SUBCONTAINER_NETWORK", "")
IBKR_GATEWAY_IMAGE = os.getenv("IBKR_GATEWAY_IMAGE", "")
# PostgreSQL server for managed Gateway children. Each session gets its own
# database, <IB_GATEWAY_POSTGRES_DB>_<session uuid hex>, created by the child's
# entrypoint: every child numbers its GatewaySession as pk=1 and claims any
# queued command, so two sessions sharing one database would execute each
# other's orders. Leave the host blank to keep children on SQLite.
IB_GATEWAY_POSTGRES_HOST = os.getenv("IB_GATEWAY_POSTGRES_HOST", "").strip()
IB_GATEWAY_POSTGRES_PORT = os.getenv("IB_GATEWAY_POSTGRES_PORT", "5432").strip()
IB_GATEWAY_POSTGRES_DB = os.getenv("IB_GATEWAY_POSTGRES_DB", "IB_Gateway").strip()
IB_GATEWAY_POSTGRES_USER = os.getenv("IB_GATEWAY_POSTGRES_USER", "").strip()
IB_GATEWAY_POSTGRES_PASSWORD = os.getenv("IB_GATEWAY_POSTGRES_PASSWORD", "")
GLOBAL_KILL_SWITCH = os.getenv("GLOBAL_KILL_SWITCH", "false").lower() == "true"
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
KAFKA_CLIENT_ID = os.getenv("KAFKA_CLIENT_ID", "finflock-backend")
KAFKA_ENABLED = os.getenv("KAFKA_ENABLED", "false").lower() == "true"
MARKET_PRICE_STALE_SECONDS = int(os.getenv("MARKET_PRICE_STALE_SECONDS", "300"))
MANUAL_MARKET_DATA_LEASE_SECONDS = int(os.getenv("MANUAL_MARKET_DATA_LEASE_SECONDS", "120"))
MANUAL_MARKET_DATA_TIMEFRAME = os.getenv("MANUAL_MARKET_DATA_TIMEFRAME", "1m")
OPTION_STRATEGY_MARKET_DATA_TIMEFRAME = os.getenv("OPTION_STRATEGY_MARKET_DATA_TIMEFRAME", "1m")
OPTION_STRATEGY_MARKET_DATA_LEASE_SECONDS = int(
    os.getenv("OPTION_STRATEGY_MARKET_DATA_LEASE_SECONDS", "86400")
)
DHAN_OPTIONS_ENABLED = os.getenv("DHAN_OPTIONS_ENABLED", "false").lower() == "true"
DHAN_CLIENT_ID = os.getenv("DHAN_CLIENT_ID", "").strip()
DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN", "").strip()
DHAN_API_BASE_URL = os.getenv("DHAN_API_BASE_URL", "https://api.dhan.co/v2").rstrip("/")
DHAN_REQUEST_TIMEOUT_SECONDS = float(os.getenv("DHAN_REQUEST_TIMEOUT_SECONDS", "15"))
DHAN_MAX_ATTEMPTS = max(1, int(os.getenv("DHAN_MAX_ATTEMPTS", "4")))
DHAN_RETRY_BASE_DELAY_SECONDS = max(0.0, float(os.getenv("DHAN_RETRY_BASE_DELAY_SECONDS", "1")))
DHAN_RETRY_MAX_DELAY_SECONDS = max(
    DHAN_RETRY_BASE_DELAY_SECONDS,
    float(os.getenv("DHAN_RETRY_MAX_DELAY_SECONDS", "8")),
)
WARMUP_SAFETY_BARS = int(os.getenv("WARMUP_SAFETY_BARS", "5"))
WARMUP_TIMEOUT_SECONDS = int(os.getenv("WARMUP_TIMEOUT_SECONDS", "300"))
WARMUP_REUSE_RECEIPT_SECONDS = int(os.getenv("WARMUP_REUSE_RECEIPT_SECONDS", "3600"))
# Earlier stored windows a re-sent warm-up history could not overwrite are
# counted from at most this many days back.
WARMUP_BACKFILL_LOOKBACK_DAYS = int(os.getenv("WARMUP_BACKFILL_LOOKBACK_DAYS", "10"))
MARKET_CONSUMER_HEARTBEAT_STALE_SECONDS = int(os.getenv("MARKET_CONSUMER_HEARTBEAT_STALE_SECONDS", "30"))
KAFKA_LAG_DEGRADED_THRESHOLD = int(os.getenv("KAFKA_LAG_DEGRADED_THRESHOLD", "1000"))
KAFKA_HEALTH_STALE_SECONDS = int(os.getenv("KAFKA_HEALTH_STALE_SECONDS", "60"))
OUTBOX_PUBLISHER_HEARTBEAT_STALE_SECONDS = int(
    os.getenv("OUTBOX_PUBLISHER_HEARTBEAT_STALE_SECONDS", "30")
)
EXECUTION_REQUIRED_KAFKA_TOPICS = tuple(
    value.strip()
    for value in os.getenv(
        "EXECUTION_REQUIRED_KAFKA_TOPICS",
        "market.raw.v1,market.canonical.v1,market.bars.v1,market.bars.seed.v1,"
        "market.indicators.v1,"
        "market.quality.v1,instrument.registry.v1,strategy.inputs.v1,"
        "strategy.targets.v1,portfolio.rebalance.planned.v1,risk.decisions.v1,"
        "orders.events.v1,executions.events.v1,reconciliation.events.v1,"
        "system.health.v1,dead-letter.v1",
    ).split(",")
    if value.strip()
)
EXECUTION_ACTIVATION_PREFLIGHT_ENABLED = (
    os.getenv("EXECUTION_ACTIVATION_PREFLIGHT_ENABLED", "true").lower() == "true"
)
APPEND_SLASH = False
FLINK_REST_URL = os.getenv("FLINK_REST_URL", "http://localhost:8081")
PYFLINK_CLIENT_EXECUTABLE = os.getenv(
    "PYFLINK_CLIENT_EXECUTABLE", "/opt/pyflink-venv/bin/python"
)
FLINK_PYTHON_RUNTIME_MODE = os.getenv(
    "FLINK_PYTHON_RUNTIME_MODE", "archive"
).strip().lower()
FLINK_PYTHON_ARCHIVE = os.getenv(
    "FLINK_PYTHON_ARCHIVE",
    "/opt/flink-python-runtime/flink-python-runtime.zip",
)
FLINK_PYTHON_ARCHIVE_TARGET = os.getenv(
    "FLINK_PYTHON_ARCHIVE_TARGET", "pyenv"
)
FLINK_PYTHON_EXECUTABLE = os.getenv(
    "FLINK_PYTHON_EXECUTABLE", "pyenv/bin/python"
)
FLINK_AUTO_REPAIR_PYTHON_EXECUTABLE_FAILURE = (
    os.getenv("FLINK_AUTO_REPAIR_PYTHON_EXECUTABLE_FAILURE", "true").lower()
    == "true"
)
FLINK_BOOTSTRAP_STATUS_PATH = os.getenv(
    "FLINK_BOOTSTRAP_STATUS_PATH", "/tmp/flink-job-bootstrap-status.json"
)
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
FINNHUB_BASE_URL = os.getenv("FINNHUB_BASE_URL", "https://finnhub.io/api/v1").rstrip("/")
FINNHUB_API_KEY_OVERRIDE_ENABLED = os.getenv("FINNHUB_API_KEY_OVERRIDE_ENABLED", "false").lower() == "true"
FINNHUB_REQUEST_TIMEOUT_SECONDS = int(os.getenv("FINNHUB_REQUEST_TIMEOUT_SECONDS", "15"))
FINNHUB_MAX_RETRIES = int(os.getenv("FINNHUB_MAX_RETRIES", "2"))
FINNHUB_ENCRYPTION_KEY = os.getenv("FINNHUB_ENCRYPTION_KEY", "")
FINNHUB_OPERATION_THROTTLE_LIMIT = int(os.getenv("FINNHUB_OPERATION_THROTTLE_LIMIT", "30"))
MARKET_DATA_FALLBACK_ENABLED = os.getenv("MARKET_DATA_FALLBACK_ENABLED", "false").lower() == "true"
FINNHUB_HISTORICAL_FALLBACK_ENABLED = os.getenv("FINNHUB_HISTORICAL_FALLBACK_ENABLED", "false").lower() == "true"
FINNHUB_LIVE_FALLBACK_ENABLED = os.getenv("FINNHUB_LIVE_FALLBACK_ENABLED", "false").lower() == "true"
FINNHUB_AUTO_FAILBACK_ENABLED = os.getenv("FINNHUB_AUTO_FAILBACK_ENABLED", "false").lower() == "true"
IBKR_MARKET_DATA_FAILOVER_GRACE_SECONDS = int(os.getenv("IBKR_MARKET_DATA_FAILOVER_GRACE_SECONDS", "15"))
FINNHUB_LIVE_STALE_SECONDS = int(os.getenv("FINNHUB_LIVE_STALE_SECONDS", "15"))
FINNHUB_WS_URL = os.getenv("FINNHUB_WS_URL", "wss://ws.finnhub.io").rstrip("/")
FINNHUB_WS_RECONNECT_MAX_SECONDS = int(os.getenv("FINNHUB_WS_RECONNECT_MAX_SECONDS", "30"))
FINNHUB_ALLOWED_LATENESS_SECONDS = int(os.getenv("FINNHUB_ALLOWED_LATENESS_SECONDS", "2"))
FINNHUB_WS_RECONCILE_SECONDS = int(os.getenv("FINNHUB_WS_RECONCILE_SECONDS", "2"))
PRIMARY_RECOVERY_CONFIRMATION_EVENTS = int(os.getenv("PRIMARY_RECOVERY_CONFIRMATION_EVENTS", "3"))
PRIMARY_PROBE_RETRY_SECONDS = int(os.getenv("PRIMARY_PROBE_RETRY_SECONDS", "30"))
FINNHUB_MAPPING_REVALIDATE_SECONDS = int(os.getenv("FINNHUB_MAPPING_REVALIDATE_SECONDS", "86400"))
FINNHUB_SUPPORTED_ASSET_CLASSES = tuple(
    value.strip().upper() for value in os.getenv("FINNHUB_SUPPORTED_ASSET_CLASSES", "STK").split(",") if value.strip()
)
GATEWAY_HTTP_TIMEOUT_SECONDS = float(os.getenv("GATEWAY_HTTP_TIMEOUT_SECONDS", "10"))
LOCAL_PAPER_GATEWAY_URL = os.getenv("LOCAL_PAPER_GATEWAY_URL", "").strip()
LOCAL_PAPER_GATEWAY_SERVICE_TOKEN = os.getenv(
    "LOCAL_PAPER_GATEWAY_SERVICE_TOKEN", ""
).strip()
LOCAL_PAPER_GATEWAY_NOVNC_PASSWORD = os.getenv(
    "LOCAL_PAPER_GATEWAY_NOVNC_PASSWORD", ""
)
LOCAL_PAPER_GATEWAY_CONTAINER_NAME = os.getenv(
    "LOCAL_PAPER_GATEWAY_CONTAINER_NAME", "paper-ibkr-gateway"
).strip()
GATEWAY_COMMAND_POLL_INTERVAL_SECONDS = float(os.getenv("GATEWAY_COMMAND_POLL_INTERVAL_SECONDS", "0.25"))
GATEWAY_COMMAND_TIMEOUT_DEFAULT_SECONDS = float(os.getenv("GATEWAY_COMMAND_TIMEOUT_DEFAULT_SECONDS", "20"))
GATEWAY_COMMAND_TIMEOUT_SEARCH_CONTRACTS_SECONDS = float(
    os.getenv("GATEWAY_COMMAND_TIMEOUT_SEARCH_CONTRACTS_SECONDS", "15")
)
GATEWAY_COMMAND_TIMEOUT_QUALIFY_SECONDS = float(os.getenv("GATEWAY_COMMAND_TIMEOUT_QUALIFY_SECONDS", "20"))
GATEWAY_COMMAND_TIMEOUT_HISTORICAL_DATA_SECONDS = float(
    os.getenv("GATEWAY_COMMAND_TIMEOUT_HISTORICAL_DATA_SECONDS", "60")
)
GATEWAY_COMMAND_TIMEOUT_HISTORICAL_SCHEDULE_SECONDS = float(
    os.getenv("GATEWAY_COMMAND_TIMEOUT_HISTORICAL_SCHEDULE_SECONDS", "30")
)
GATEWAY_SAFE_COMMAND_RETRIES = max(0, int(os.getenv("GATEWAY_SAFE_COMMAND_RETRIES", "1")))
GATEWAY_CONTRACT_SEARCH_MAX_RESULTS = max(1, min(50, int(os.getenv("GATEWAY_CONTRACT_SEARCH_MAX_RESULTS", "12"))))
GATEWAY_IBKR_REQUEST_TIMEOUT_SEARCH_CONTRACTS_SECONDS = max(
    1, int(os.getenv("GATEWAY_IBKR_REQUEST_TIMEOUT_SEARCH_CONTRACTS_SECONDS", "12"))
)
GATEWAY_IBKR_REQUEST_TIMEOUT_QUALIFY_SECONDS = max(
    1, int(os.getenv("GATEWAY_IBKR_REQUEST_TIMEOUT_QUALIFY_SECONDS", "15"))
)
OPTIMIZATION_THROTTLE_LIMIT = int(os.getenv("OPTIMIZATION_THROTTLE_LIMIT", "30"))
EXPENSIVE_OPERATION_THROTTLE_WINDOW_SECONDS = int(os.getenv("EXPENSIVE_OPERATION_THROTTLE_WINDOW_SECONDS", "60"))
OUTBOX_RETENTION_DAYS = int(os.getenv("OUTBOX_RETENTION_DAYS", "30"))
BROKER_SNAPSHOT_RETENTION_DAYS = int(os.getenv("BROKER_SNAPSHOT_RETENTION_DAYS", "30"))
STREAM_HEALTH_RETENTION_DAYS = int(os.getenv("STREAM_HEALTH_RETENTION_DAYS", "30"))
OPERATIONAL_COMPACTION_BATCH_SIZE = int(os.getenv("OPERATIONAL_COMPACTION_BATCH_SIZE", "1000"))
RECOMMENDATION_CONFIG = RecommendationSystemConfiguration.from_environment(
    os.environ,
    default_artifact_root=BASE_DIR / "research_artifacts",
)
RESEARCH_ENABLED = RECOMMENDATION_CONFIG.research_enabled
RECOMMENDATION_SYSTEM_ENABLED = RECOMMENDATION_CONFIG.recommendation_system_enabled
RECOMMENDATION_UNIVERSE_KEY = RECOMMENDATION_CONFIG.universe_key
RECOMMENDATION_MAX_STOCKS = RECOMMENDATION_CONFIG.maximum_stocks
RECOMMENDATION_MIN_STOCKS = RECOMMENDATION_CONFIG.minimum_stocks
RECOMMENDATION_CANDIDATE_POOL_SIZE = RECOMMENDATION_CONFIG.candidate_pool_size
RECOMMENDATION_MAX_STRATEGIES_PER_STOCK = RECOMMENDATION_CONFIG.maximum_strategies_per_stock
RESEARCH_DAILY_LOOKBACK_YEARS = RECOMMENDATION_CONFIG.daily_lookback_years
RESEARCH_INTRADAY_LOOKBACK_DAYS = RECOMMENDATION_CONFIG.intraday_lookback_days
RESEARCH_MINIMUM_DAILY_BARS = RECOMMENDATION_CONFIG.minimum_daily_bars
RESEARCH_SCORE_MAX_AGE_DAYS = RECOMMENDATION_CONFIG.score_max_age_days
RESEARCH_STALE_SCORE_FALLBACK_DAYS = RECOMMENDATION_CONFIG.stale_score_fallback_days
RECOMMENDATION_SNAPSHOT_MAX_AGE_HOURS = RECOMMENDATION_CONFIG.snapshot_max_age_hours
RESEARCH_MAX_PARALLEL_DATA_TASKS = RECOMMENDATION_CONFIG.maximum_parallel_data_tasks
RESEARCH_MAX_PARALLEL_BACKTEST_TASKS = RECOMMENDATION_CONFIG.maximum_parallel_backtest_tasks
RESEARCH_BUNDLE_PATH = os.getenv(
    "RESEARCH_BUNDLE_PATH", str(BASE_DIR / "research_bundle")
)
RESEARCH_ARTIFACT_ROOT = str(RECOMMENDATION_CONFIG.artifact_root)
RESEARCH_RECOMMENDATION_MAX_AGE_DAYS = max(1, RECOMMENDATION_SNAPSHOT_MAX_AGE_HOURS // 24)
RESEARCH_TASK_ROUTES = {
    "apps.research.tasks.refresh_universe_mapping": {"queue": "research_mapping"},
    "apps.research.tasks.refresh_research_pipeline": {"queue": "research_daily_data"},
    "apps.research.tasks.refresh_intraday_data": {"queue": "research_intraday_data"},
    "apps.research.tasks.refresh_fundamentals": {"queue": "research_fundamentals"},
    "apps.research.tasks.refresh_events": {"queue": "research_events"},
    "apps.research.tasks.calculate_features": {"queue": "research_features"},
    "apps.research.tasks.schedule_research_experiments": {"queue": "celery"},
    "apps.research.tasks.dispatch_research_experiments": {"queue": "celery"},
    "apps.research.tasks.run_single_asset_experiment": {"queue": "research_single_asset"},
    "apps.research.tasks.run_cross_sectional_experiment": {"queue": "research_cross_sectional"},
    "apps.research.tasks.run_allocator_experiment": {"queue": "research_allocators"},
    "apps.research.tasks.run_overlay_experiment": {"queue": "research_overlays"},
    "apps.research.tasks.run_event_experiment": {"queue": "research_cross_sectional"},
    "apps.research.tasks.run_pair_experiment": {"queue": "research_pairs"},
    "apps.research.tasks.score_current_candidates": {"queue": "research_scoring"},
    "apps.research.tasks.warm_recommendation_cache": {"queue": "recommendation_cache"},
    "apps.research.tasks.generate_recommendation_batch": {"queue": "recommendations"},
}
STRATEGY_EVALUATION_TASK_ROUTES = {
    "apps.strategies.tasks.activate_strategy_instance": {"queue": "strategy_evaluation"},
    "apps.strategies.tasks.execute_strategy_evaluation_jobs": {"queue": "strategy_evaluation"},
    "apps.strategies.tasks.recover_strategy_evaluation_jobs": {"queue": "strategy_evaluation"},
}
REBALANCING_TASK_ROUTES = {
    "apps.rebalancing.tasks.coordinate_portfolio_targets": {"queue": "target_coordination"},
    "apps.rebalancing.tasks.recover_incomplete_rebalances": {"queue": "target_coordination"},
}
EXECUTION_TASK_ROUTES = {
    "apps.execution.tasks.execute_order_intents": {"queue": "intent_execution"},
    "apps.execution.tasks.recover_order_intents": {"queue": "intent_execution"},
    "apps.execution.tasks.dispatch_broker_commands": {"queue": "broker_commands"},
    "apps.execution.tasks.recover_broker_commands": {"queue": "broker_commands"},
}
INTELLIGENCE_TASK_ROUTES = {
    "apps.intelligence.tasks.*": {"queue": "intelligence"},
}
CELERY_TASK_ROUTES = {
    **RESEARCH_TASK_ROUTES,
    **STRATEGY_EVALUATION_TASK_ROUTES,
    **REBALANCING_TASK_ROUTES,
    **EXECUTION_TASK_ROUTES,
    **INTELLIGENCE_TASK_ROUTES,
}

OPTION_CHAIN_PROVIDER = os.getenv("OPTION_CHAIN_PROVIDER", "IBKR").upper()
# Risk profiling (apps/strategies/risk_profiles.py), on by default. Off: the legacy parameter
# flow runs unchanged. On: option strategies take only `risk_profile` and the
# server resolves every parameter. SHADOW (only while ENABLED is off) logs how a
# profile would differ from the parameters actually saved; it is never applied.
RISK_PROFILING_ENABLED = os.getenv("RISK_PROFILING_ENABLED", "true").lower() == "true"
RISK_PROFILING_SHADOW = os.getenv("RISK_PROFILING_SHADOW", "false").lower() == "true"
# Pre-trade risk evaluation for option orders. False removes the risk engine
# for options: no policy limits, no exposure or notional caps, no capital
# reservation, and no available-position check -- the last of which rejects the
# short legs of every credit structure, because selling an option the account
# does not already hold is a short sale. Kill switches still apply.
OPTION_PRETRADE_RISK_ENABLED = os.getenv(
    "OPTION_PRETRADE_RISK_ENABLED", "true"
).strip().lower() not in {"0", "false", "no", "off"}
# The remote per-session feature database (the source's `RemoteDBProvider`).
# It supplies the daily RPS features and the precomputed IV rank/percentile;
# IBKR remains the only quote and execution path. Unset means the runtime falls
# back to daily bars persisted by the strategy's own subscription, which ties
# the RPS score to the execution timeframe and leaves IV rank unmeasured.
REMOTE_FEATURE_DB_URL = os.getenv("REMOTE_FEATURE_DB_URL", "")
# Ledger profile the nightly NFO ingestion writes (nfo_pipeline.runner.CALCULATION_PROFILE). Only read for the status card.
NFO_LEDGER_PROFILE = os.getenv("NFO_LEDGER_PROFILE", "INR_INDEX_OPTIONS_6P8_1P2_DECISION_FEATURES_V1")
# Kafka and Flink as QCH sub-containers from public Docker Hub images (apps/broker_gateway/platform_services.py). Off = a no-op.
PLATFORM_SERVICES_ENABLED = os.getenv("PLATFORM_SERVICES_ENABLED", "false").lower() == "true"
PLATFORM_SERVICE_PREFIX = os.getenv("PLATFORM_SERVICE_PREFIX", "finflock")
PLATFORM_KAFKA_IMAGE = os.getenv("PLATFORM_KAFKA_IMAGE", "docker.io/apache/kafka:3.9.0")
PLATFORM_FLINK_IMAGE = os.getenv("PLATFORM_FLINK_IMAGE", "docker.io/library/flink:1.20.1-scala_2.12-java17")
CELERY_BEAT_SCHEDULE["ensure-platform-services"] = {"task": "apps.broker_gateway.tasks.ensure_platform_services", "schedule": 300.0}
# Nightly NFO ingestion, hosted INSIDE this Backend (nfo_ingestion/, vendored nfo_pipeline/). See ingestionclaude.md section 17.
NFO_INGEST_SCHEDULE_ENABLED = os.getenv("NFO_INGEST_SCHEDULE_ENABLED", "false").lower() == "true"  # go-live gate G5; off = the scheduler idles
NFO_INGEST_MIN_SESSION_DATE = os.getenv("NFO_INGEST_MIN_SESSION_DATE", "2026-11-03")  # earlier sessions are never ingested by the job
NFO_INGEST_MAX_SESSIONS = int(os.getenv("NFO_INGEST_MAX_SESSIONS", "2"))  # per night; the worker also clamps to 2 in gateway mode
NFO_INGEST_RUN_DEADLINE_IST = os.getenv("NFO_INGEST_RUN_DEADLINE_IST", "01:30")  # stop waiting for the provider's file
NFO_INGEST_WORK_DIR = os.getenv("NFO_INGEST_WORK_DIR", "/tmp/nfo")  # ephemeral: the DB ledger, not the disk, is the record
NFO_INGEST_NICE = int(os.getenv("NFO_INGEST_NICE", "10"))  # the worker runs as a separate low-priority process
NFO_INGEST_TIMEOUT_HOURS = float(os.getenv("NFO_INGEST_TIMEOUT_HOURS", "5"))
# Read-only spot-bar access through the paper IB Gateway (apps/market_data/nfo_spot.py). Used in-process by the worker.
NFO_SPOT_GATEWAY_SESSION_ID = os.getenv("NFO_SPOT_GATEWAY_SESSION_ID", "")  # pin one BrokerGatewaySession (UUID); blank = the single connected one
NFO_SPOT_GATEWAY_MODE = os.getenv("NFO_SPOT_GATEWAY_MODE", "PAPER").upper()
NFO_SPOT_MIN_INTERVAL_SECONDS = float(os.getenv("NFO_SPOT_MIN_INTERVAL_SECONDS", "5"))
NFO_SPOT_DAILY_REQUEST_LIMIT = int(os.getenv("NFO_SPOT_DAILY_REQUEST_LIMIT", "20"))
NFO_SPOT_COMMAND_TIMEOUT_SECONDS = float(os.getenv("NFO_SPOT_COMMAND_TIMEOUT_SECONDS", "45"))
# Missed-night e-mail (apps/market_data/ingestion_alert.py) is ON HOLD: no SMTP settings and no beat entry, so it never runs.
# The System page still shows a missed night. To revive it later: SMTP settings + recipients + a beat entry for
# apps.market_data.tasks.check_nfo_ingestion_freshness. Never put a blank KEY= line for these in .env.example: the
# platform treats every blank line there as a secret it must attach and fails the deploy.
NFO_FRESHNESS_ALERT_TO = os.getenv("NFO_FRESHNESS_ALERT_TO", "")
NFO_HOLIDAYS = os.getenv("NFO_HOLIDAYS", "")  # comma-separated YYYY-MM-DD exchange holidays (no calendar is built in)
REMOTE_FEATURE_CONNECT_TIMEOUT = int(os.getenv("REMOTE_FEATURE_CONNECT_TIMEOUT", "10"))
# IBKR qualifies the NSE index as NIFTY50; the remote feed keys it as NIFTY.
REMOTE_FEATURE_SYMBOLS = {"NIFTY50": "NIFTY"}
REMOTE_FEATURE_SESSIONS = int(os.getenv("REMOTE_FEATURE_SESSIONS", "500"))
OPTION_QUOTE_MAX_AGE_SECONDS = 30
# A paper session reads IBKR's delayed feed, which stamps a tick roughly fifteen
# minutes behind the book, so the real-time window would reject every row. The
# allowance is that delay plus the live window, and it bounds staleness rather
# than waiving it.
OPTION_QUOTE_DELAYED_MAX_AGE_SECONDS = 900 + OPTION_QUOTE_MAX_AGE_SECONDS
GATEWAY_COMMAND_TIMEOUT_OPTION_QUOTES_SECONDS = 45
GATEWAY_COMMAND_TIMEOUT_OPTION_CHAIN_SECONDS = 45


# ---------------------------------------------------------------------------
# Intelligence layer (advisory only; see docs/intelligence and CLAUDE.md).
# Off by default. None of this is read by the order path or by readiness checks.
# ---------------------------------------------------------------------------
def _optional_float(name):
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else None


INTELLIGENCE_ENABLED = os.getenv("INTELLIGENCE_ENABLED", "false").lower() == "true"
INTELLIGENCE_LLM_PROVIDER = os.getenv("INTELLIGENCE_LLM_PROVIDER", "fake").strip().lower()
# QCH gateway (LiteLLM proxy to DeepSeek, OpenAI-compatible). The key is a secret: environment only, never a default.
QCH_BASE_URL = os.getenv("QCH_BASE_URL", "").strip().rstrip("/")
QCH_API_KEY = os.getenv("QCH_API_KEY", "")
QCH_CONNECT_TIMEOUT_SECONDS = float(os.getenv("QCH_CONNECT_TIMEOUT_SECONDS", "5"))
# "disabled" switches the model's thinking off (cheaper, faster, less careful); empty keeps the provider default.
QCH_THINKING = os.getenv("QCH_THINKING", "").strip().lower()
# Thinking tokens are billed inside max_tokens, so the output cap gets this headroom when thinking is on.
QCH_REASONING_HEADROOM_TOKENS = int(os.getenv("QCH_REASONING_HEADROOM_TOKENS", "4096"))
# An answer cut off by the token limit is retried once with double the budget, never above this ceiling.
QCH_MAX_TOKENS_CEILING = int(os.getenv("QCH_MAX_TOKENS_CEILING", "32768"))
# Read timeout for INTELLIGENCE_MODEL_DEEP calls (measured up to 214 s for deepseek-v4-pro on a long evidence pack).
QCH_TIMEOUT_DEEP_SECONDS = float(os.getenv("QCH_TIMEOUT_DEEP_SECONDS", "300"))
# Calls in flight at once across all processes (the gateway owner stated no rate limit; this stays polite and bounds spend).
INTELLIGENCE_LLM_MAX_CONCURRENCY = int(os.getenv("INTELLIGENCE_LLM_MAX_CONCURRENCY", "3"))
# Stable model ids come from the environment only; there are deliberately no defaults.
INTELLIGENCE_MODEL_REVIEW = os.getenv("INTELLIGENCE_MODEL_REVIEW", "").strip()
INTELLIGENCE_MODEL_DEEP = os.getenv("INTELLIGENCE_MODEL_DEEP", "").strip()
# Optional low-latency model for the live trade commentary; falls back to INTELLIGENCE_MODEL_REVIEW when empty.
INTELLIGENCE_MODEL_FAST = os.getenv("INTELLIGENCE_MODEL_FAST", "").strip()
INTELLIGENCE_DAILY_BUDGET_USD = os.getenv("INTELLIGENCE_DAILY_BUDGET_USD", "0")
INTELLIGENCE_MAX_INPUT_TOKENS = int(os.getenv("INTELLIGENCE_MAX_INPUT_TOKENS", "24000"))
INTELLIGENCE_MAX_OUTPUT_TOKENS = int(os.getenv("INTELLIGENCE_MAX_OUTPUT_TOKENS", "4096"))
INTELLIGENCE_MIN_EPISODES_FOR_SUGGESTION = int(os.getenv("INTELLIGENCE_MIN_EPISODES_FOR_SUGGESTION", "8"))
INTELLIGENCE_LLM_TIMEOUT_SECONDS = float(os.getenv("INTELLIGENCE_LLM_TIMEOUT_SECONDS", "60"))
INTELLIGENCE_LLM_MAX_ATTEMPTS = int(os.getenv("INTELLIGENCE_LLM_MAX_ATTEMPTS", "3"))
INTELLIGENCE_LLM_RETRY_BASE_SECONDS = float(os.getenv("INTELLIGENCE_LLM_RETRY_BASE_SECONDS", "1"))
# Optional generation controls: unset means "use the model's own default".
INTELLIGENCE_TEMPERATURE = _optional_float("INTELLIGENCE_TEMPERATURE")
INTELLIGENCE_THINKING_BUDGET = _optional_float("INTELLIGENCE_THINKING_BUDGET")
INTELLIGENCE_THINKING_LEVEL = os.getenv("INTELLIGENCE_THINKING_LEVEL", "").strip()
# USD per million tokens for the chosen models (cost accounting; 0 = unknown).
INTELLIGENCE_PRICE_INPUT_PER_MTOK_USD = os.getenv("INTELLIGENCE_PRICE_INPUT_PER_MTOK_USD", "0")
INTELLIGENCE_PRICE_OUTPUT_PER_MTOK_USD = os.getenv("INTELLIGENCE_PRICE_OUTPUT_PER_MTOK_USD", "0")

# Beat entries exist only while the layer is enabled, so a disabled deployment schedules nothing extra.
if INTELLIGENCE_ENABLED:
    CELERY_BEAT_SCHEDULE.update({
        "intelligence-sweep-fills": {"task": "apps.intelligence.tasks.sweep_fills", "schedule": 30.0},
        "intelligence-sweep-expired": {"task": "apps.intelligence.tasks.sweep_expired", "schedule": 900.0},
        "intelligence-sweep-operator-events": {"task": "apps.intelligence.tasks.sweep_operator_events", "schedule": 60.0},
        "intelligence-session-records": {"task": "apps.intelligence.tasks.build_session_records", "schedule": 300.0},
        "intelligence-poll-context": {"task": "apps.intelligence.tasks.poll_context", "schedule": 300.0},
        "intelligence-daily-briefs": {"task": "apps.intelligence.tasks.build_daily_briefs", "schedule": 900.0},
        "intelligence-prune-context": {"task": "apps.intelligence.tasks.prune_context", "schedule": 86400.0},
        "intelligence-monitor-live": {"task": "apps.intelligence.tasks.monitor_live", "schedule": 20.0},
        "intelligence-prune-live": {"task": "apps.intelligence.tasks.prune_live_snapshots", "schedule": 86400.0},
        "intelligence-daily-learning": {"task": "apps.intelligence.tasks.daily_learning", "schedule": 86400.0},
        "intelligence-weekly-reviews": {"task": "apps.intelligence.tasks.weekly_reviews", "schedule": 86400.0},
    })

# Phase 3: LLM post-trade reviews. Off until you decide; episodes (Phase 2) work without it.
INTELLIGENCE_REVIEWS_ENABLED = os.getenv("INTELLIGENCE_REVIEWS_ENABLED", "false").lower() == "true"
# A late commission correction re-reviews a closed trade only if net P&L moves by more than this percent.
INTELLIGENCE_REREVIEW_PNL_DELTA_PCT = float(os.getenv("INTELLIGENCE_REREVIEW_PNL_DELTA_PCT", "20"))
# Expensive endpoints (rerun) use a database counter because the engine has no shared cache (R7).
INTELLIGENCE_RERUN_LIMIT_PER_MINUTE = int(os.getenv("INTELLIGENCE_RERUN_LIMIT_PER_MINUTE", "5"))
# Optional JSON overrides for the rule analyzers, e.g. {"high_cost_bps": 40}
import json as _json  # noqa: E402
INTELLIGENCE_RULE_THRESHOLDS = _json.loads(os.getenv("INTELLIGENCE_RULE_THRESHOLDS", "") or "{}")

# Optional JSON overrides for the live monitor thresholds, e.g. {"strike_proximity_medium_pct": 1.5}
INTELLIGENCE_LIVE_THRESHOLDS = _json.loads(os.getenv("INTELLIGENCE_LIVE_THRESHOLDS", "") or "{}")
# Optional JSON overrides for the learning-loop thresholds, e.g. {"min_n_supported": 10}
INTELLIGENCE_LEARNING_THRESHOLDS = _json.loads(os.getenv("INTELLIGENCE_LEARNING_THRESHOLDS", "") or "{}")

# Phase 6 operations. The metrics token lets a Prometheus scraper read /api/v1/intelligence/metrics/ without a staff session.
INTELLIGENCE_METRICS_TOKEN = os.getenv("INTELLIGENCE_METRICS_TOKEN", "")
INTELLIGENCE_BUDGET_ALARM_PCT = float(os.getenv("INTELLIGENCE_BUDGET_ALARM_PCT", "80"))
# Informational only: a HIGH pre-trade advisory can ask for an acknowledgement but never blocks anything.
INTELLIGENCE_ADVISORY_REQUIRE_ACK = os.getenv("INTELLIGENCE_ADVISORY_REQUIRE_ACK", "false").lower() == "true"
INTELLIGENCE_REPLAY_COST_BPS = float(os.getenv("INTELLIGENCE_REPLAY_COST_BPS", "5"))
# PDF export of reports (needs reportlab). Off: the endpoint answers 501 REPORT_PDF_DISABLED; JSON / Markdown / HTML still work.
INTELLIGENCE_REPORT_PDF_ENABLED = os.getenv("INTELLIGENCE_REPORT_PDF_ENABLED", "true").lower() == "true"
# Phase 9: one record per market session and the daily "Intelligence Report". Off until you decide.
INTELLIGENCE_SESSION_RECORD_ENABLED = os.getenv("INTELLIGENCE_SESSION_RECORD_ENABLED", "false").lower() == "true"
# A session record is frozen this many minutes after the exchange close (late commission corrections settle first).
INTELLIGENCE_SESSION_FINALIZE_MINUTES = int(os.getenv("INTELLIGENCE_SESSION_FINALIZE_MINUTES", "30"))
# Phase 10: outside context. Off until you decide. Keys are secrets: environment only.
INTELLIGENCE_CONTEXT_ENABLED = os.getenv("INTELLIGENCE_CONTEXT_ENABLED", "false").lower() == "true"
INTELLIGENCE_CONTEXT_SOURCES = os.getenv("INTELLIGENCE_CONTEXT_SOURCES", "finnhub,marketaux,rbi,fed")
MARKETAUX_API_KEY = os.getenv("MARKETAUX_API_KEY", "")
MARKETAUX_BASE_URL = os.getenv("MARKETAUX_BASE_URL", "https://api.marketaux.com/v1/news/all")
# The key in use is on a free plan (100 requests a day): keep a hard daily cap below it.
MARKETAUX_DAILY_CALL_CAP = int(os.getenv("MARKETAUX_DAILY_CALL_CAP", "60"))
# Items older than this are deleted unless a frozen trade links to them.
INTELLIGENCE_CONTEXT_RETENTION_DAYS = int(os.getenv("INTELLIGENCE_CONTEXT_RETENTION_DAYS", "120"))
