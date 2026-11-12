"""Starts the nightly NFO ingestion from inside the hosted Backend (supervisor program `nfo-ingest-scheduler`).

A layer on top of the trading system: it only ever starts `nfo_ingestion.run_nightly` as a SEPARATE, low-priority process, so a
crash, a hang or a memory spike in the ingestion cannot take the Backend down. It never runs while NFO_INGEST_SCHEDULE_ENABLED is
off (the go-live gate), and it does nothing at all if Redis (its state and lock) cannot be reached.

Schedule: Mon-Fri, starting at 22:00 IST. The worker waits for the provider's file until RUN_DEADLINE_IST (01:30). A Backend that was
down at 22:00 still starts the run if it comes back before 01:30 (the run belongs to the evening it started). A night that fails for
an infrastructure reason (exit 20) is retried after 30 minutes, at most 3 times; data-quality, verification and outside-window failures
(exit 30/40/50) are NOT retried: a human looks. Re-running is always safe: the DB ledger skips finished sessions.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from django.conf import settings

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
START, END = time(22, 0), time(1, 30)
RETRY_GAP = timedelta(minutes=30)
MAX_ATTEMPTS = 3
NO_RETRY_EXIT_CODES = {30, 40, 50}
SUMMARY_PREFIX = "NFO_INGEST_SUMMARY "
AUDIT_FIELDS = ("status", "exit_code", "code", "message", "sessions", "failed", "not_attempted", "spot_requests", "seconds",
                "latest_db_before", "weekdays_without_data")


def run_date_for(now: datetime) -> date | None:
    """The trading evening this moment belongs to: 22:00-24:00 is today's run, 00:00-01:30 is yesterday's, anything else is none."""
    local = now.astimezone(IST)
    if local.time() >= START:
        day = local.date()
    elif local.time() < END:
        day = local.date() - timedelta(days=1)
    else:
        return None
    return day if day.weekday() < 5 else None


def worker_environment(*, schedule_enabled: bool) -> dict:
    """Environment for the worker process. Secrets (Dropbox, DB) are inherited from the Backend's own environment, never logged."""
    work = str(getattr(settings, "NFO_INGEST_WORK_DIR", "/tmp/nfo"))
    env = dict(os.environ)
    env.update({
        "ENV_FILE": "/nonexistent",            # never read a baked .env: only real environment variables
        "INPUT_DIR": f"{work}/pipeline", "SCRATCH_DIR": f"{work}/scratch",
        "SPOT_SOURCE": "gateway", "GDF_ACQUIRER": "dropbox_shared",
        "MIN_SESSION_DATE": str(settings.NFO_INGEST_MIN_SESSION_DATE),
        "MAX_SESSIONS_PER_RUN": str(settings.NFO_INGEST_MAX_SESSIONS),
        "RUN_DEADLINE_IST": str(settings.NFO_INGEST_RUN_DEADLINE_IST),
        "SCHEDULE_ENABLED": "true" if schedule_enabled else "false",
        "DJANGO_SETTINGS_MODULE": "config.settings", "PYTHONUNBUFFERED": "1",
    })
    # A blank TIMESCALE_DB_URL (e.g. an empty line in the baked .env) means "not set": fall back to the same database.
    if not str(env.get("TIMESCALE_DB_URL") or "").strip():
        env["TIMESCALE_DB_URL"] = str(getattr(settings, "REMOTE_FEATURE_DB_URL", "") or "")
    return env


def run_worker(extra_args=(), *, schedule_enabled=False, popen=subprocess.Popen, log=print):
    """Run the worker as a child process, stream its output to the Backend's log, return its summary dict."""
    nice = int(getattr(settings, "NFO_INGEST_NICE", 10))
    command = [sys.executable, "-m", "nfo_ingestion.run_nightly", "--no-file-exit-code", "0", *extra_args]
    kwargs = {"preexec_fn": (lambda: os.nice(nice))} if os.name == "posix" and nice else {}
    process = popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=worker_environment(schedule_enabled=schedule_enabled),
                    cwd=str(settings.BASE_DIR), **kwargs)
    timeout = float(getattr(settings, "NFO_INGEST_TIMEOUT_HOURS", 5)) * 3600
    timer = threading.Timer(timeout, process.kill)
    timer.daemon = True
    timer.start()
    summary = None
    try:
        for line in process.stdout:
            line = line.rstrip("\n")
            log(f"[nfo-ingest] {line}")
            if line.startswith(SUMMARY_PREFIX):
                try:
                    summary = json.loads(line[len(SUMMARY_PREFIX):])
                except ValueError:
                    pass
        code = process.wait()
    finally:
        timer.cancel()
    if summary is None:
        summary = {"status": "FAILED", "exit_code": code if code else 20, "code": "no_summary",
                   "message": "The ingestion process ended without a summary (killed, timed out or crashed)."}
    summary.setdefault("exit_code", code)
    return summary


class RunState:
    """Per-evening state in Redis: attempts, last attempt, done flag, and a lock so only one run is ever active."""

    def __init__(self, redis_client=None):
        self._redis = redis_client
        self.prefix = f"{getattr(settings, 'REDIS_KEY_PREFIX', '')}nfo-ingest:"

    def _client(self):
        if self._redis is None:
            import redis

            self._redis = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=2, socket_timeout=2, decode_responses=True)
        return self._redis

    def is_done(self, day):
        return bool(self._client().get(f"{self.prefix}done:{day}"))

    def mark_done(self, day):
        self._client().set(f"{self.prefix}done:{day}", "1", ex=48 * 3600)

    def attempts(self, day):
        return int(self._client().get(f"{self.prefix}attempts:{day}") or 0)

    def last_attempt(self, day):
        value = self._client().get(f"{self.prefix}last:{day}")
        return datetime.fromtimestamp(float(value), timezone.utc) if value else None

    def record_attempt(self, day, now):
        client = self._client()
        count = client.incr(f"{self.prefix}attempts:{day}")
        client.expire(f"{self.prefix}attempts:{day}", 48 * 3600)
        client.set(f"{self.prefix}last:{day}", str(now.timestamp()), ex=48 * 3600)
        return count

    def stop_for_tonight(self, day):
        self._client().set(f"{self.prefix}attempts:{day}", str(MAX_ATTEMPTS), ex=48 * 3600)

    def acquire(self, lease_seconds):
        import uuid

        token = uuid.uuid4().hex
        return token if self._client().set(f"{self.prefix}lock", token, nx=True, px=int(lease_seconds * 1000)) else None

    def release(self, token):
        # The hosted Redis user may not run scripts (EVAL is refused), so compare and delete with two
        # plain commands. The gap between them is harmless: only one scheduler process takes this lock.
        try:
            client, key = self._client(), f"{self.prefix}lock"
            if _same_token(client.get(key), token):
                client.delete(key)
        except Exception:  # noqa: BLE001 - the lease expires on its own
            pass


def record_run(day, attempt, summary):
    """Keep a secret-free record of the run (shown on the System page). Never raises."""
    try:
        from apps.audit.models import AuditEvent

        AuditEvent.objects.get_or_create(
            idempotency_key=f"nfo-ingest-run:{day}:{attempt}",
            defaults={"event_type": "NFO_INGEST_RUN", "actor": "system", "aggregate_type": "nfo_ingestion", "aggregate_id": str(day),
                      "data": {**{k: summary.get(k) for k in AUDIT_FIELDS}, "run_date": str(day), "attempt": attempt}},
        )
    except Exception:  # noqa: BLE001
        logger.warning("nfo_ingest stage=audit_failed run_date=%s", day)


class Scheduler:
    def __init__(self, *, state=None, runner=None, enabled=None, clock=None, log=print):
        self.state = state or RunState()
        self.runner = runner or (lambda day, attempt: run_worker(schedule_enabled=True, log=log))
        self.enabled = enabled or (lambda: bool(getattr(settings, "NFO_INGEST_SCHEDULE_ENABLED", False)))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.log = log

    def tick(self) -> str:
        if not self.enabled():
            return "disabled"
        now = self.clock()
        day = run_date_for(now)
        if day is None:
            return "idle"
        try:
            if self.state.is_done(day):
                return "done"
            if self.state.attempts(day) >= MAX_ATTEMPTS:
                return "exhausted"
            last = self.state.last_attempt(day)
            if last and now - last < RETRY_GAP:
                return "waiting"
            token = self.state.acquire(float(getattr(settings, "NFO_INGEST_TIMEOUT_HOURS", 5)) * 3600 + 600)
            if token is None:
                return "busy"
        except Exception as exc:  # noqa: BLE001 - Redis down: do nothing rather than run without state
            logger.warning("nfo_ingest stage=state_unavailable error=%s", type(exc).__name__)
            return "state_unavailable"
        try:
            attempt = self.state.record_attempt(day, now)
            self.log(f"[nfo-ingest] starting run for {day} (attempt {attempt}/{MAX_ATTEMPTS})")
            summary = self.runner(day, attempt)
            record_run(day, attempt, summary)
            exit_code = int(summary.get("exit_code", 20))
            if exit_code == 0:
                self.state.mark_done(day)
            elif exit_code in NO_RETRY_EXIT_CODES:
                self.state.stop_for_tonight(day)
            self.log(f"[nfo-ingest] run for {day} finished: exit {exit_code} {summary.get('code') or ''}")
            return "ran"
        finally:
            self.state.release(token)

    def loop(self, *, once=False, sleep=_time.sleep, interval=30):
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the scheduler must outlive any single failure
                logger.exception("nfo_ingest stage=tick_failed")
            if once:
                return
            sleep(interval)


def _same_token(value, token):
    """True when the stored lock value is our token (str, or bytes from a client without decode_responses)."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return value == token
