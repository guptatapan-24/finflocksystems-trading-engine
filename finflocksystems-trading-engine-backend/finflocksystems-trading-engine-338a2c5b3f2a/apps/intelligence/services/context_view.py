"""Point-in-time context for one trade (R18): what was known when it was entered, what was scheduled ahead, and what
arrived while it was open, plus the deterministic `ctx.*` metrics derived from that.

Reads stored `ContextItem` rows only. It never calls a source, and it never uses an item published after the moment it
is describing as "known". A trade with no stored context says so (`available: false` with the reason) instead of
guessing, and every metric that cannot be computed is simply absent.
"""
import statistics
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from apps.intelligence.models import ContextItem, EpisodeContext

from .session import zone_of

NEWS_KINDS = ("NEWS", "ANNOUNCEMENT")
NEWS_LOOKBACK_HOURS = 24
UPCOMING_DAYS = 14
CENTRAL_BANK_HORIZON_DAYS = 30
BASELINE_DAYS = 30
MIN_BASELINE_DAYS = 10
NEWS_LIMIT = 8
EVENT_LIMIT = 12
# Which calendar countries matter to which exchange: home country at medium impact or more, and for India also the
# United States at high impact (a Fed decision moves Indian markets).
HOME = {"XNSE": {"IN"}, "XNYS": {"US"}}
ALSO_HIGH = {"XNSE": {"US"}, "XNYS": set()}


def relevant_event(item, zone):
    """Is this calendar entry worth showing for an exchange? Home country at medium+, selected others only at high."""
    country = item.region or ""
    if country in HOME.get(zone, set()):
        return item.impact in ("medium", "high")
    return country in ALSO_HIGH.get(zone, set()) and item.impact == "high"


def relevant_news_q(zone, symbols):
    regions = set(HOME.get(zone, set())) | {"GLOBAL"}
    q = Q(region__in=regions)
    for symbol in symbols:
        q |= Q(symbol_index__contains=f"|{symbol}|")
    return q


def _line(item, when_attr):
    when = getattr(item, when_attr, None) or item.published_at or item.event_time
    return {"id": item.pk, "kind": item.kind, "source": item.source, "at": when.isoformat() if when else None,
            "title": item.title, "impact": item.impact or None, "event_type": item.event_type or None,
            "region": item.region or None, "symbols": list(item.symbols or [])[:3]}


def _stories(qs, limit):
    """Newest first, one item per story (near-duplicate headlines share a story key)."""
    seen, out = set(), []
    for item in qs.order_by("-published_at", "-pk")[:limit * 6]:
        key = item.story_key or f"id:{item.pk}"
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _distinct_story_count(qs):
    return len({(i.story_key or f"id:{i.pk}") for i in qs.only("pk", "story_key")})


def _burst_zscore(zone, symbols, opened_at, count_24h):
    """How unusual the 24 hours before entry were, against the same scope's previous 30 days. None with too little history."""
    first = ContextItem.objects.filter(kind__in=NEWS_KINDS).order_by("first_seen_at").values_list("first_seen_at", flat=True).first()
    window_end = opened_at - timedelta(hours=NEWS_LOOKBACK_HOURS)
    if first is None or window_end - first < timedelta(days=MIN_BASELINE_DAYS):
        return None
    counts = []
    for day in range(BASELINE_DAYS):
        end = window_end - timedelta(days=day)
        qs = ContextItem.objects.filter(kind__in=NEWS_KINDS, published_at__gt=end - timedelta(hours=24), published_at__lte=end)
        counts.append(_distinct_story_count(qs.filter(relevant_news_q(zone, symbols))))
    deviation = statistics.pstdev(counts)
    return None if deviation == 0 else round((count_24h - statistics.fmean(counts)) / deviation, 3)


def compute(instance, opened_at, closed_at, expiration=None, now=None):
    """{"block": {...frozen into the evidence pack...}, "metrics": {"ctx.*": ...}} for one trade."""
    now = now or timezone.now()
    end = closed_at or now
    zone = zone_of(instance)
    symbol = instance.instrument.symbol
    symbols = [symbol]
    seen_before_entry = ContextItem.objects.filter(first_seen_at__lte=opened_at).exists()
    if not ContextItem.objects.exists() or not seen_before_entry:
        reason = ("no outside context had been collected when this trade was entered"
                  if ContextItem.objects.exists() else "no outside context has been collected yet")
        return {"block": {"available": False, "reason": reason}, "metrics": {"ctx.available": False}}

    lookback = opened_at - timedelta(hours=NEWS_LOOKBACK_HOURS)
    news_qs = ContextItem.objects.filter(kind__in=NEWS_KINDS).filter(relevant_news_q(zone, symbols))
    known_news_qs = news_qs.filter(published_at__gt=lookback, published_at__lte=opened_at)
    known_news = _stories(known_news_qs, NEWS_LIMIT)
    hold_news = _stories(news_qs.filter(published_at__gt=opened_at, published_at__lte=end), NEWS_LIMIT)

    horizon = opened_at + timedelta(days=UPCOMING_DAYS)
    events = [e for e in ContextItem.objects.filter(kind="EVENT", event_time__gt=opened_at, event_time__lte=max(horizon, end))
              .order_by("event_time") if relevant_event(e, zone)]
    # earnings that matter fall between entry and the end of the trade; for options the structure's expiry is the end
    deadline = end
    if expiration is not None:
        from datetime import datetime, time, timezone as dt_utc

        deadline = datetime.combine(expiration, time.max, tzinfo=dt_utc.utc)
    earnings = list(ContextItem.objects.filter(kind="EARNINGS", symbol_index__contains=f"|{symbol}|",
                                               event_time__gt=opened_at - timedelta(days=1), event_time__lte=deadline).order_by("event_time"))
    ahead = [e for e in events if e.event_time <= horizon][:EVENT_LIMIT]
    in_hold_events = [e for e in events if e.event_time <= end]
    announcements = list(ContextItem.objects.filter(kind="ANNOUNCEMENT", event_type="CENTRAL_BANK",
                                                    published_at__gt=opened_at, published_at__lte=end)
                         .filter(relevant_news_q(zone, symbols)))
    earn_hold = earnings
    next_cb = next((e for e in events if e.event_type == "CENTRAL_BANK"), None)

    count_24h = _distinct_story_count(known_news_qs)
    metrics = {
        "ctx.available": True,
        "ctx.event_in_hold": bool(in_hold_events),
        "ctx.high_impact_events_in_hold": sum(1 for e in in_hold_events if e.impact == "high"),
        "ctx.macro_release_in_hold": any(e.event_type == "MACRO_RELEASE" for e in in_hold_events),
        "ctx.central_bank_in_hold": any(e.event_type == "CENTRAL_BANK" for e in in_hold_events) or bool(announcements),
        "ctx.earnings_within_dte": bool(earn_hold),
        "ctx.news_count_24h_before_entry": count_24h,
    }
    if next_cb is not None and next_cb.event_time - opened_at <= timedelta(days=CENTRAL_BANK_HORIZON_DAYS):
        metrics["ctx.days_to_central_bank_at_entry"] = round((next_cb.event_time - opened_at).total_seconds() / 86400.0, 2)
    z = _burst_zscore(zone, symbols, opened_at, count_24h)
    if z is not None:
        metrics["ctx.news_burst_zscore"] = z
    block = {
        "available": True, "zone": zone, "sources": sorted({i.source for i in known_news + hold_news + events + earn_hold}),
        "known_at_entry": [_line(i, "published_at") for i in known_news],
        "scheduled_after_entry": [_line(i, "event_time") for i in ahead] + [_line(i, "event_time") for i in earn_hold if i not in ahead][:4],
        "during_hold": [_line(i, "published_at") for i in hold_news] + [_line(i, "event_time") for i in in_hold_events[:EVENT_LIMIT]]
                       + [_line(i, "published_at") for i in announcements[:4]],
        "burst_history_days": None if z is None else BASELINE_DAYS,
        "notes": ["News is matched by region and the traded symbol; items are as published, not as judged."]}
    return {"block": block, "metrics": metrics}


def link(episode, block):
    """Freeze which stored items surrounded this trade (called once, when its evidence is frozen)."""
    if not block or not block.get("available"):
        return 0
    rows = []
    for relation, key in (("KNOWN_AT_ENTRY", "known_at_entry"), ("SCHEDULED_AFTER_ENTRY", "scheduled_after_entry"), ("DURING_HOLD", "during_hold")):
        for line in block.get(key) or []:
            rows.append(EpisodeContext(episode=episode, item_id=line["id"], relation=relation))
    EpisodeContext.objects.bulk_create(rows, ignore_conflicts=True)
    return len(rows)
