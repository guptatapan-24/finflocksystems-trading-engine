#!/bin/sh
set -eu

export PORT="${PORT:-8000}"
export FORWARDED_ALLOW_IPS="${FORWARDED_ALLOW_IPS:-*}"
export RESEARCH_MAX_PARALLEL_DATA_TASKS="${RESEARCH_MAX_PARALLEL_DATA_TASKS:-8}"
export RESEARCH_MAX_PARALLEL_BACKTEST_TASKS="${RESEARCH_MAX_PARALLEL_BACKTEST_TASKS:-8}"

echo "Checking PostgreSQL connectivity..."
python scripts/ensure_database.py

echo "Checking Redis connectivity..."
if ! python scripts/ensure_redis.py; then
  echo "WARNING: Redis is unreachable or REDIS_PASSWORD is wrong; Celery workers will not run until it is fixed." >&2
fi

echo "Running Django migrations..."
python manage.py migrate --noinput

cleanup_database_on_start="$(python -c 'import os; from dotenv import load_dotenv; load_dotenv("/app/.env", override=False); print(os.getenv("CLEANUP_DATABASE_ON_START", "false").strip().lower())')"
echo "CLEANUP_DATABASE_ON_START=${cleanup_database_on_start}"
if [ "${cleanup_database_on_start}" = "true" ]; then
  # Intelligence data (lessons, reviews, statistics) must survive restarts. If any exists, skip the destructive
  # reset for this start and carry on: trading and streaming start exactly as they would without the reset.
  if ! python manage.py check_intelligence_flush_guard; then
    echo "WARNING: CLEANUP_DATABASE_ON_START=true was SKIPPED because intelligence data exists." >&2
    cleanup_database_on_start=false
  fi
fi
if [ "${cleanup_database_on_start}" = "true" ]; then
  echo "CLEANUP_DATABASE_ON_START=true: canceling required Flink jobs before cleanup..."
  python -u scripts/ensure_flink_jobs.py --cancel-required
  echo "CLEANUP_DATABASE_ON_START=true: flushing existing required Kafka topic records..."
  python -u scripts/ensure_kafka_topics.py --flush-existing
  echo "CLEANUP_DATABASE_ON_START=true: deleting all database rows..."
  python manage.py flush --noinput --traceback
  echo "Re-seeding built-in stock and option strategy definitions and default policies..."
  python manage.py seed_strategy_definitions
  echo "Database and streaming-state reset completed; the next Backend restart will repeat this cleanup while CLEANUP_DATABASE_ON_START=true."
fi

if [ "${PLATFORM_SERVICES_ENABLED:-false}" = "true" ]; then
  echo "Ensuring the Kafka and Flink sub-containers..."
  python scripts/ensure_service_containers.py || echo "WARNING: could not ensure the platform service sub-containers; continuing." >&2
fi

echo "Checking required Kafka topics..."
python scripts/ensure_kafka_topics.py

# A required job can stay RUNNING while its checkpoints stop completing; the
# bootstrap below treats RUNNING as healthy and would leave it stuck. With this
# flag the job is canceled first so it is resubmitted fresh. Touches only Flink:
# no database rows and no Kafka records; consumers resume from committed offsets.
# Read through dotenv like CLEANUP_DATABASE_ON_START above: values in /app/.env
# are not exported into this shell, only platform-injected variables are.
flink_resubmit_on_start="$(python -c 'import os; from dotenv import load_dotenv; load_dotenv("/app/.env", override=False); print(os.getenv("FLINK_RESUBMIT_ON_START", "false").strip().lower())')"
echo "FLINK_RESUBMIT_ON_START=${flink_resubmit_on_start}"
if [ "${flink_resubmit_on_start}" = "true" ]; then
  echo "FLINK_RESUBMIT_ON_START=true: canceling required Flink jobs so they are resubmitted fresh..."
  python -u scripts/ensure_flink_jobs.py --cancel-required || echo "WARNING: Flink job cancel failed; continuing." >&2
fi

echo "Checking required Flink jobs..."
if python -u scripts/ensure_flink_jobs.py; then
  echo "Flink job bootstrap finished."
else
  bootstrap_status=$?
  echo "Flink job bootstrap failed with status ${bootstrap_status}; continuing so Backend diagnostics remain available." >&2
fi

# Skip an old backlog on market.quality.v1 (freshness marks; only the newest
# matters) so it cannot hold new bars and indicators behind it. Runs before the
# market consumer starts. Bars and indicators are never skipped.
skip_market_backlog="$(python -c 'import os; from dotenv import load_dotenv; load_dotenv("/app/.env", override=False); print(os.getenv("MARKET_CONSUMER_SKIP_BACKLOG_ON_START", "false").strip().lower())')"
echo "MARKET_CONSUMER_SKIP_BACKLOG_ON_START=${skip_market_backlog}"
if [ "${skip_market_backlog}" = "true" ]; then
  python -u scripts/skip_kafka_backlog.py || echo "WARNING: could not skip the market consumer backlog; continuing." >&2
fi

echo "Starting Backend services..."
exec supervisord -c /app/supervisord.conf
