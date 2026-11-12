"""Read-only status of the nightly NFO ingestion, taken from its own ledger.

The ingestion (Kestra, see `ingestionclaude.md`) writes `nfo_ingestion_batch` in the remote
TimescaleDB. That table is the single source of truth for what was ingested, so this module only
reads it, with the same read-only/autocommit connection as `RemoteFeatureProvider`: it can never
block the ingestion's writes. It does not start, stop or influence the ingestion, and nothing in
execution depends on it.

A failure to read is reported as `available: false`, never as an exception, so the System page
still renders when the remote database is unreachable.
"""

import logging
import re
from datetime import date, datetime, time, timedelta
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
# Kestra starts at 22:00 IST and stops waiting for the provider's file at 01:30 IST.
RUN_START = time(22, 0)
RUN_DEADLINE_AFTER_START = timedelta(hours=3, minutes=30)
RECENT_SESSIONS = 10
FAILED_STATES = ("FAILED_RETRYABLE", "FAILED_DATA_QUALITY")


def configured_holidays():
    """Exchange holidays from NFO_HOLIDAYS (comma-separated YYYY-MM-DD). There is no built-in calendar."""
    days = set()
    for item in str(getattr(settings, "NFO_HOLIDAYS", "") or "").split(","):
        try:
            days.add(date.fromisoformat(item.strip()))
        except ValueError:
            continue
    return days


def expected_session(now, holidays=()):
    """Newest trading day whose nightly run window has already closed.

    Weekends and the given holidays are skipped. Without a holiday listed, an exchange holiday makes the ingestion look
    one session behind, so the caller words that case as "check for a holiday", not as a failure.
    """
    local = now.astimezone(IST)
    day = local.date()
    while True:
        if day.weekday() < 5 and day not in holidays and local >= datetime.combine(day, RUN_START, tzinfo=IST) + RUN_DEADLINE_AFTER_START:
            return day
        day -= timedelta(days=1)


def _iso(value):
    return value.isoformat() if isinstance(value, (date, datetime)) else None


def _connect(url):
    import psycopg

    return psycopg.connect(
        url, connect_timeout=int(getattr(settings, "REMOTE_FEATURE_CONNECT_TIMEOUT", 10)),
        autocommit=True, options="-c default_transaction_read_only=on -c statement_timeout=8000",
    )


def read_ledger(url, profile):
    with _connect(url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT session_date, selected_rows, completed_at, source_archive FROM nfo_ingestion_batch "
            "WHERE state='COMPLETE' AND calculation_profile=%s "
            "ORDER BY session_date DESC, completed_at DESC NULLS LAST LIMIT 1",
            (profile,),
        )
        latest = cursor.fetchone()
        cursor.execute(
            "SELECT DISTINCT ON (session_date) session_date, state, selected_rows, completed_at, updated_at, error_category "
            "FROM nfo_ingestion_batch WHERE calculation_profile=%s "
            "ORDER BY session_date DESC, (state='COMPLETE') DESC, updated_at DESC LIMIT %s",
            (profile, RECENT_SESSIONS),
        )
        recent = cursor.fetchall()
    return latest, recent


def last_run():
    """The newest scheduler run record (what the nightly job did and why it stopped), or None. Never raises."""
    try:
        from apps.audit.models import AuditEvent

        event = AuditEvent.objects.filter(event_type="NFO_INGEST_RUN").order_by("-created_at").first()
        if event is None:
            return None
        data = event.data or {}
        return {"run_date": data.get("run_date"), "attempt": data.get("attempt"), "status": data.get("status"), "exit_code": data.get("exit_code"),
                "code": data.get("code"), "message": str(data.get("message") or "")[:300], "sessions": [s.get("session") for s in data.get("sessions") or []],
                "failed": data.get("failed"), "at": event.created_at.isoformat()}
    except Exception:  # noqa: BLE001 - the status endpoint must work even if this table cannot be read
        return None


def ingestion_status(now=None, reader=read_ledger):
    now = now or timezone.now()
    url = getattr(settings, "REMOTE_FEATURE_DB_URL", "") or ""
    expected = expected_session(now, configured_holidays())
    base = {"configured": bool(url), "available": False, "state": "UNAVAILABLE", "message": "",
            "expected_session": expected.isoformat(), "latest_session": None, "latest_completed_at": None,
            "latest_rows": None, "latest_source": None, "failure": None, "recent": [], "last_run": last_run(), "checked_at": now.isoformat()}
    if not url:
        base["message"] = "No remote database is configured (REMOTE_FEATURE_DB_URL), so ingestion status cannot be read."
        return base
    try:
        latest, recent = reader(url, getattr(settings, "NFO_LEDGER_PROFILE", ""))
    except Exception as exc:  # noqa: BLE001 - psycopg raises several unrelated types
        detail = _redacted_error(exc, url)
        logger.warning("nfo_ingestion_status stage=ledger_unreadable error=%s", detail)
        base["message"] = f"The ingestion ledger could not be read ({detail}). Check the database connection."
        return base
    base["available"] = True
    base["recent"] = [{"session": _iso(session), "state": state, "rows": int(rows or 0),
                       "completed_at": _iso(completed), "updated_at": _iso(updated), "error_category": error}
                      for session, state, rows, completed, updated, error in recent]
    if latest is None:
        base["state"], base["message"] = "EMPTY", "The ledger has no completed session yet."
        return base
    session, rows, completed, source = latest
    base.update(latest_session=_iso(session), latest_rows=int(rows or 0), latest_completed_at=_iso(completed),
                latest_source=source)
    newer_failure = next((row for row in recent if row[1] in FAILED_STATES and row[0] > session), None)
    if newer_failure:
        base["failure"] = {"session": _iso(newer_failure[0]), "state": newer_failure[1],
                           "error_category": newer_failure[5], "updated_at": _iso(newer_failure[4])}
    if session >= expected:
        base["state"], base["message"] = "UP_TO_DATE", f"Session {session} is ingested and verified."
    elif newer_failure:
        base["state"] = "FAILED"
        base["message"] = f"Session {newer_failure[0]} failed to ingest; the last good session is {session}."
    else:
        base["state"] = "BEHIND"
        base["message"] = (f"Latest ingested session is {session}; {expected} was expected. If {expected} was an "
                           "exchange holiday this is normal, otherwise check the nightly run.")
    return base


def _redacted_error(exc, url):
    """`Type: message` for the operator, with the URL's user, password and host masked.

    The driver's own text (e.g. "could not translate host name", "password authentication failed")
    is what tells a network problem from a credentials problem, so it is kept; the connection
    secrets and the host are never shown or logged.
    """
    text = " ".join(str(exc).split())
    try:
        parts = urlsplit(url)
        masks = {}
        for secret, mask in ((parts.hostname, "<host>"), (parts.username, "<user>"), (parts.password, "<password>")):
            for form in {secret or "", unquote(secret or "")} - {""}:
                masks[form] = mask
        if masks:
            # Whole tokens only, longest first, in one pass: a short secret never masks part of a
            # longer word or of another mask.
            pattern = "|".join(re.escape(form) for form in sorted(masks, key=len, reverse=True))
            text = re.sub(rf"(?<![\w.-])(?:{pattern})(?![\w-])", lambda m: masks[m.group(0)], text)
    except ValueError:
        text = ""
    name = type(exc).__name__
    return f"{name}: {text[:300]}" if text else name
