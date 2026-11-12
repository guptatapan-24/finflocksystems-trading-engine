"""Daily brief and "outside context that day" (Phase 10): what the world looked like around one exchange session, from stored
items only (R18). Used three ways: the pre-open brief (items known until the open), the session record's context section
(items up to the end of the day), and the daily report.

Deterministic: calendar events and earnings are listed as scheduled, headlines are as published, and every open position is
joined to the events that fall before its expiry. No model is involved, and nothing is shown that was not stored.
"""
import hashlib
import json
import logging
from datetime import datetime, time, timedelta, timezone as dt_utc

from django.utils import timezone

from apps.intelligence import conf
from apps.intelligence.models import ContextItem, DailyBrief, TradeEpisode
from apps.intelligence.services.evidence import sanitize_tree

from . import session as session_service
from .context_view import HOME, NEWS_KINDS, _stories, relevant_event, relevant_news_q

logger = logging.getLogger(__name__)
HEADLINES = 8
ANNOUNCEMENTS = 5
EVENTS_LIMIT = 25
EXPOSURE_EVENTS = 4
PRE_OPEN_NEWS_HOURS = 18


def _iso(value):
    return value.isoformat() if value else None


def source_states(now=None):
    """[{name, state, last_success, items_total, calls_today, remaining_quota, last_error}] with a plain state word."""
    from apps.intelligence.context.poller import source_status

    out = []
    for name, s in source_status(now).items():
        error = s["last_error"]
        if not s["enabled"]:
            state = "DISABLED"
        elif error.startswith("NOT_CONFIGURED"):
            state = "NOT_CONFIGURED"
        elif s["consecutive_failures"] > 0:
            state = "FAILING"
        elif s["last_success_at"] is None:
            state = "NO_DATA_YET"
        else:
            state = "OK"
        out.append({"name": name, "state": state, "last_success": _iso(s["last_success_at"]), "items_total": s["items_total"],
                    "calls_today": s["calls_today"], "remaining_quota": s["remaining_quota"], "last_error": error[:160]})
    return out


def _event_row(item):
    attrs = item.attrs or {}
    return {"at": _iso(item.event_time), "event": item.title, "impact": item.impact or None, "region": item.region or None,
            "event_type": item.event_type or None, "estimate": attrs.get("estimate"), "previous": attrs.get("prev"), "source": item.source}


def _news_row(item):
    attrs = item.attrs or {}
    return {"at": _iso(item.published_at), "headline": item.title, "source": item.source, "publisher": attrs.get("publisher"),
            "sentiment": attrs.get("sentiment"), "kind": item.kind, "event_type": item.event_type or None, "symbols": list(item.symbols or [])[:3]}


def build_content(zone, day, now=None, until=None):
    """Context around (zone, day) as known until `until` (default: the end of that day, or now). Pure reads."""
    from apps.strategies.models import StrategyInstance

    now = now or timezone.now()
    start, end, open_dt, close_dt = session_service.window(zone, day)
    until = min(until or end, now)
    instances = [i for i in StrategyInstance.objects.select_related("instrument") if session_service.zone_of(i) == zone]
    symbols = sorted({i.instrument.symbol for i in instances})
    sources = source_states(now)
    total = ContextItem.objects.count()

    holiday = None
    for item in ContextItem.objects.filter(kind="HOLIDAY", event_time__gte=datetime.combine(day, time.min, tzinfo=dt_utc.utc),
                                           event_time__lt=datetime.combine(day + timedelta(days=1), time.min, tzinfo=dt_utc.utc)):
        if (item.attrs or {}).get("zone") == zone:
            holiday = {"name": item.title, "trading_hour": (item.attrs or {}).get("trading_hour") or None}

    events = [e for e in ContextItem.objects.filter(kind="EVENT", event_time__gte=start, event_time__lt=end).order_by("event_time")
              if relevant_event(e, zone)][:EVENTS_LIMIT]
    earnings = []
    for symbol in symbols:
        earnings += list(ContextItem.objects.filter(kind="EARNINGS", symbol_index__contains=f"|{symbol}|", event_time__gte=start, event_time__lt=end))
    news_since = (until - timedelta(hours=PRE_OPEN_NEWS_HOURS)) if until <= open_dt else start
    headlines = _stories(ContextItem.objects.filter(kind="NEWS", published_at__gt=news_since, published_at__lte=until)
                         .filter(relevant_news_q(zone, symbols)), HEADLINES)
    announcements = _stories(ContextItem.objects.filter(kind="ANNOUNCEMENT", published_at__gt=start - timedelta(hours=12), published_at__lte=until)
                             .filter(relevant_news_q(zone, symbols)), ANNOUNCEMENTS)

    exposures = []
    ids = [i.pk for i in instances]
    for episode in TradeEpisode.objects.filter(instance_id__in=ids, opened_at__lt=until).exclude(status__in=["CLOSED", "EXPIRED"]).order_by("opened_at"):
        expiry = (episode.legs.exclude(expiration__isnull=True).order_by("expiration").values_list("expiration", flat=True).first())
        upcoming = []
        if expiry is not None:
            deadline = datetime.combine(expiry, time.max, tzinfo=dt_utc.utc)
            upcoming = [_event_row(e) for e in ContextItem.objects.filter(kind="EVENT", event_time__gt=until, event_time__lte=deadline)
                        .order_by("event_time") if relevant_event(e, zone) and e.impact == "high"][:EXPOSURE_EVENTS]
        exposures.append({"instrument": episode.instrument_symbol, "instance": episode.instance_name, "direction": episode.direction,
                          "opened_at": _iso(episode.opened_at), "expiry": expiry.isoformat() if expiry else None,
                          "days_to_expiry": (expiry - until.date()).days if expiry else None, "high_impact_events_before_expiry": upcoming})

    notes = []
    if total == 0:
        notes.append("No outside context has been stored yet (the context sources are off, not configured, or have not run).")
    if holiday:
        notes.append(f"Exchange holiday: {holiday['name']}" + (f" (trading hours: {holiday['trading_hour']})" if holiday["trading_hour"] else "") + ".")
    if any(s["state"] in ("FAILING", "NOT_CONFIGURED") for s in sources if s["name"] in conf.context_sources()):
        notes.append("At least one enabled context source is failing or not configured; the lists below may be incomplete.")
    content = {
        "zone": zone, "date": day.isoformat(), "label": session_service.SESSIONS[zone]["label"], "known_until": _iso(until),
        "available": total > 0, "sources": sources, "holiday": holiday, "events": [_event_row(e) for e in events],
        "earnings": [{"at": _iso(e.event_time), "symbol": (e.symbols or [""])[0], "title": e.title} for e in earnings],
        "headlines": [_news_row(i) for i in headlines], "announcements": [_news_row(i) for i in announcements],
        "open_positions": exposures, "traded_symbols": symbols, "home_regions": sorted(HOME.get(zone, [])), "notes": notes}
    return sanitize_tree(content, 300)


def content_hash(content):
    stable = {k: v for k, v in content.items() if k not in ("known_until", "sources")}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def upsert(zone, day, now=None):
    """Store the pre-open brief for (zone, day): rebuilt until the exchange opens, then frozen."""
    now = now or timezone.now()
    existing = DailyBrief.objects.filter(zone=zone, brief_date=day).first()
    if existing is not None and existing.final:
        return existing
    _, _, open_dt, _ = session_service.window(zone, day)
    content = build_content(zone, day, now, until=open_dt)
    brief, _ = DailyBrief.objects.update_or_create(zone=zone, brief_date=day, defaults={
        "content": content, "content_hash": content_hash(content), "final": now >= open_dt, "built_at": now})
    return brief


def run_all(now=None):
    """Beat entry. Never raises."""
    if not conf.context_enabled():
        return {"skipped": "disabled"}
    now = now or timezone.now()
    done = {}
    for zone in session_service.zones_with_strategies():
        day = session_service.local_date(zone, now)
        try:
            done[f"{zone}:{day}"] = "final" if upsert(zone, day, now).final else "open"
        except Exception:
            logger.exception("intelligence: daily brief failed for %s %s", zone, day)
            done[f"{zone}:{day}"] = "error"
    return done
