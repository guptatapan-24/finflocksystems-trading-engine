"""Missed-night alert for the nightly NFO ingestion.

Runs from the Backend's Celery beat, deliberately OUTSIDE the ingestion's own container: if that container ever stops, a
check running inside it would stop too and the failure would be silent. It only reads the ingestion's ledger (via
`ingestion_status`) and sends an e-mail; it never starts, stops or changes an ingestion.

Disabled while NFO_FRESHNESS_ALERT_TO is empty. One e-mail per missed trading day (deduplicated through the existing audit
table, so no migration). List exchange holidays in NFO_HOLIDAYS so they do not read as misses.
"""
from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from .ingestion_status import IST, ingestion_status, read_ledger


def _recipients():
    return [item.strip() for item in str(getattr(settings, "NFO_FRESHNESS_ALERT_TO", "") or "").split(",") if item.strip()]


def check_and_alert(now=None, reader=read_ledger, sender=send_mail):
    from apps.audit.models import AuditEvent

    recipients = _recipients()
    if not recipients:
        return {"status": "disabled"}
    now = now or timezone.now()
    data = ingestion_status(now, reader)
    state = data["state"]
    if state == "UP_TO_DATE":
        return {"status": "ok", "latest_session": data["latest_session"]}
    expected = data["expected_session"]
    if state == "UNAVAILABLE":
        key, subject = f"nfo-freshness-unavailable:{now.astimezone(IST).date()}", "NFO ingestion status cannot be read"
    else:
        key, subject = f"nfo-freshness:{expected}", f"NFO ingestion missed trading day {expected}"
    claim, created = AuditEvent.objects.get_or_create(
        idempotency_key=key,
        defaults={"event_type": "NFO_INGESTION_ALERT", "actor": "system", "aggregate_type": "nfo_ingestion",
                  "aggregate_id": expected, "data": {"state": state, "latest_session": data["latest_session"]}},
    )
    if not created:
        return {"status": "already_alerted", "key": key}
    failure = data.get("failure")
    body = "\n".join(filter(None, [
        data["message"], "",
        f"State: {state}", f"Expected session: {expected}", f"Latest ingested session: {data['latest_session'] or 'none'}",
        f"Latest failure: session {failure['session']} ({failure.get('error_category') or failure['state']})" if failure else "",
        "", "If this was an exchange holiday, add the date to NFO_HOLIDAYS so it is not flagged again.",
        "Otherwise check the nightly run (Kestra) and the System page's Options data ingestion panel. Re-running is safe: the ledger skips finished sessions.",
    ]))
    try:
        sender(subject, body, None, recipients, fail_silently=False)
    except Exception:
        claim.delete()  # nothing was delivered, so let the next run try again
        raise
    return {"status": "alerted", "key": key, "state": state}
