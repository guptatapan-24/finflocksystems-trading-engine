"""Shared plumbing for context sources: one HTTP helper with timeouts and key scrubbing, and the item dict they all return.

A provider function returns a list of plain dicts (see `item`), never model rows, so parsing is testable without a
database and a broken source cannot half-write anything.
"""
from datetime import datetime, timezone as dt_utc

from django.conf import settings

from . import tags

HTTP_TIMEOUT = (5, 20)
MAX_BODY_BYTES = 4_000_000


class SourceError(Exception):
    """A context source could not be read. The message never contains a key."""


class NotConfigured(SourceError):
    """The source needs a key (or similar) that is not set. Not an outage: it is reported as NOT_CONFIGURED."""


class QuotaExhausted(SourceError):
    """The provider's own quota or our daily call cap is used up."""


def setting(name, default=""):
    return getattr(settings, name, default)


def http_get(session, url, *, params=None, headers=None, secrets=(), expect="json"):
    """GET with a timeout; returns parsed JSON or raw bytes. Secrets are scrubbed from every error message."""
    import requests

    def scrub(text):
        out = str(text)
        for secret in secrets:
            if secret:
                out = out.replace(secret, "***")
        return out[:300]

    try:
        response = (session or requests).get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT, stream=False)
    except requests.RequestException as exc:
        raise SourceError(scrub(exc)) from exc
    if response.status_code == 429:
        raise QuotaExhausted("rate limited by the source (HTTP 429)")
    if response.status_code >= 400:
        raise SourceError(scrub(f"HTTP {response.status_code}"))
    if len(response.content) > MAX_BODY_BYTES:
        raise SourceError("response too large")
    if expect == "bytes":
        return response.content, response.headers
    try:
        return response.json(), response.headers
    except ValueError as exc:
        raise SourceError("response was not JSON") from exc


def item(*, source, external_id, kind, title, published_at=None, event_time=None, summary="", url="", symbols=(), region="",
         event_type="", impact="", attrs=None):
    """The normalised shape every provider returns (text is sanitised later, by the store)."""
    return {"source": source, "external_id": str(external_id)[:190], "kind": kind, "title": str(title or "").strip(),
            "published_at": published_at, "event_time": event_time, "summary": summary or "", "url": tags.clean_url(url),
            "symbols": [str(s)[:24] for s in symbols][:8], "region": region, "event_type": event_type or tags.classify_event(title),
            "impact": impact or "", "attrs": attrs or {}}


def from_epoch(value):
    try:
        return datetime.fromtimestamp(int(value), tz=dt_utc.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def from_iso(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_utc.utc)
