"""The context poller: runs in the `celery-intelligence` worker only (R16), on a beat tick, and decides per job whether it is
due. Each source has its own health row and, for the metered one (Marketaux), a hard daily call cap. A failing source backs
off and is reported; it never raises into the worker or touches trading (R2).
"""
import logging
from datetime import timedelta

from django.utils import timezone

from apps.intelligence import conf
from apps.intelligence.models import ContextSource

from . import base, store
from .finnhub import Finnhub
from .marketaux import Marketaux
from .rss import Feed

logger = logging.getLogger(__name__)
FAILURES_BEFORE_BACKOFF = 3
MAX_BACKOFF_SECONDS = 6 * 3600
QUOTA_FLOOR = 5  # stop calling a metered source when its own reported remaining quota is this low
COMPANY_NEWS_SYMBOL_LIMIT = 15
ALL_SOURCES = ("finnhub", "marketaux", "rbi", "fed")


def us_symbols():
    """Tickers of enabled US stock strategies (company news is fetched only for what is actually traded)."""
    from apps.intelligence.services.session import zone_of
    from apps.strategies.models import StrategyInstance

    out = []
    for inst in StrategyInstance.objects.filter(enabled=True).select_related("instrument"):
        if inst.instrument.asset_class == "STK" and zone_of(inst) == "XNYS" and inst.instrument.symbol not in out:
            out.append(inst.instrument.symbol)
    return out[:COMPANY_NEWS_SYMBOL_LIMIT]


def build_jobs(adapters, now):
    """(name, source, interval seconds, callable) for every job. Callables return provider items."""
    fh, mx = adapters["finnhub"], adapters["marketaux"]

    def company_news():
        symbols = us_symbols()
        if not symbols:
            return None  # nothing is traded in the US: no call is made
        out = []
        for symbol in symbols:
            out += fh.company_news(symbol, now)
        return out

    def marketaux_symbols():
        symbols = us_symbols()
        if not symbols:
            return None  # nothing to ask about: no call is made
        return mx.symbol_news(symbols)

    return [
        ("finnhub:calendar", "finnhub", 7200, lambda: fh.economic_calendar(now)),
        ("finnhub:earnings", "finnhub", 21600, lambda: fh.earnings_calendar(now)),
        ("finnhub:holidays", "finnhub", 86400, lambda: fh.holidays("NS") + fh.holidays("US")),
        ("finnhub:general_news", "finnhub", 900, fh.general_news),
        ("finnhub:company_news", "finnhub", 1800, company_news),
        ("marketaux:india", "marketaux", 5400, mx.india_news),
        ("marketaux:symbols", "marketaux", 10800, marketaux_symbols),
        ("rbi:feed", "rbi", 1800, adapters["rbi"].fetch),
        ("fed:feed", "fed", 3600, adapters["fed"].fetch),
    ]


def default_adapters(session=None):
    return {"finnhub": Finnhub(session), "marketaux": Marketaux(session), "rbi": Feed("rbi", session), "fed": Feed("fed", session)}


def _row(name):
    row, _ = ContextSource.objects.get_or_create(name=name)
    return row


def _due(row, job, interval, now, force):
    """A job is due when its interval has passed; after repeated failures the wait doubles with each further failure
    (capped at six hours), so a broken source is retried gently instead of being hammered."""
    state = (row.jobs or {}).get(job) or {}
    last = state.get("last_run")
    if force or not last:
        return True
    try:
        from datetime import datetime

        elapsed = now - datetime.fromisoformat(last)
    except ValueError:
        return True
    wait = interval
    if row.consecutive_failures >= FAILURES_BEFORE_BACKOFF:
        wait = min(interval * 2 ** (row.consecutive_failures - FAILURES_BEFORE_BACKOFF + 1), MAX_BACKOFF_SECONDS)
    return elapsed >= timedelta(seconds=wait)


def _cap_reached(source, row, now):
    if source != "marketaux":
        return None
    cap = int(base.setting("MARKETAUX_DAILY_CALL_CAP", 60))
    if row.calls_day == now.date() and row.calls_today >= cap:
        return f"daily call cap reached ({cap})"
    if row.remaining_quota is not None and row.remaining_quota <= QUOTA_FLOOR:
        return f"provider quota nearly used ({row.remaining_quota} left)"
    return None


def run(now=None, adapters=None, force=False, only=None):
    """One poller tick. Returns {job: summary}. Never raises."""
    if not conf.context_enabled():
        return {"skipped": "disabled"}
    now = now or timezone.now()
    adapters = adapters or default_adapters()
    enabled = set(conf.context_sources())
    summary = {}
    for job, source, interval, fn in build_jobs(adapters, now):
        if source not in enabled or (only and job not in only):
            continue
        try:
            row = _row(source)
            if not _due(row, job, interval, now, force):
                continue
            capped = _cap_reached(source, row, now)
            if capped:
                summary[job] = {"skipped": capped}
                continue
            if row.calls_day != now.date():
                row.calls_day, row.calls_today = now.date(), 0
            state = dict(row.jobs or {})
            row.last_attempt_at = now
            try:
                items = fn()
                if items is None:  # the job had nothing to ask about: wait out its interval before looking again
                    state[job] = {"last_run": now.isoformat(), "last_ok": (state.get(job) or {}).get("last_ok"), "items": 0}
                    row.jobs = state
                    row.save()
                    summary[job] = {"skipped": "nothing to fetch"}
                    continue
                counts = store.store_items(items, now)
                row.calls_today += 1
                row.consecutive_failures, row.last_error, row.last_success_at = 0, "", now
                row.items_total = row.items_total + counts["new"]
                state[job] = {"last_run": now.isoformat(), "last_ok": now.isoformat(), "items": len(items), **{k: counts[k] for k in ("new", "updated")}}
                summary[job] = {"fetched": len(items), **counts}
                remaining = getattr(adapters.get(source), "remaining", None)
                if remaining is not None:
                    row.remaining_quota = remaining
            except base.NotConfigured as exc:
                state[job] = {"last_run": now.isoformat(), "last_ok": (state.get(job) or {}).get("last_ok"), "items": 0}
                row.last_error = f"NOT_CONFIGURED: {exc}"[:300]
                summary[job] = {"skipped": "not configured"}
            except base.SourceError as exc:
                row.calls_today += 1
                row.consecutive_failures += 1
                row.last_error = str(exc)[:300]
                state[job] = {"last_run": now.isoformat(), "last_ok": (state.get(job) or {}).get("last_ok"), "items": 0}
                summary[job] = {"error": str(exc)[:200]}
            row.jobs = state
            row.save()
        except Exception:
            logger.exception("intelligence: context job %s failed", job)
            summary[job] = {"error": "unexpected"}
    return summary


def source_status(now=None):
    """Per-source health for reports and the health endpoint: configured, last success, items, quota."""
    now = now or timezone.now()
    enabled = set(conf.context_sources()) if conf.context_enabled() else set()
    out = {}
    for name in ALL_SOURCES:
        row = ContextSource.objects.filter(name=name).first()
        out[name] = {
            "enabled": name in enabled, "last_success_at": row.last_success_at if row else None,
            "last_error": (row.last_error if row else "") or "", "consecutive_failures": row.consecutive_failures if row else 0,
            "calls_today": row.calls_today if row and row.calls_day == now.date() else 0,
            "remaining_quota": row.remaining_quota if row else None, "items_total": row.items_total if row else 0}
    return out
