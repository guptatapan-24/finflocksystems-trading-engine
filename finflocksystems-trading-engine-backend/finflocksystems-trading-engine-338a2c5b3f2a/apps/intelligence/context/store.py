"""Storing context items: sanitise, de-duplicate, upsert, prune.

Point in time (R18): `first_seen_at` is set once, when we first stored the item, and never moves. A repeated fetch of
the same item only refreshes fields a source legitimately revises (a calendar entry's actual value, a corrected headline).
"""
import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.intelligence.models import ContextItem
from apps.intelligence.services.evidence import sanitize_text, sanitize_tree

from . import tags

logger = logging.getLogger(__name__)
NEAR_DUPLICATE_WINDOW_HOURS = 48
NEAR_DUPLICATE_CANDIDATES = 300
NEAR_DUPLICATE_THRESHOLD = 0.75
STORY_KINDS = ("NEWS", "ANNOUNCEMENT")


def _clean(raw):
    """Sanitised copy of a provider item; None when nothing usable is left."""
    title = sanitize_text(raw.get("title"), 300)
    if not title:
        return None
    return {
        "title": title, "summary": sanitize_text(tags.strip_html(raw.get("summary")), 500), "url": tags.clean_url(raw.get("url")),
        "symbols": [sanitize_text(s, 24) for s in (raw.get("symbols") or [])][:8], "region": sanitize_text(raw.get("region"), 8),
        "symbol_index": "|" + "|".join(sanitize_text(s, 24).replace("|", "") for s in (raw.get("symbols") or [])[:8]) + "|",
        "event_type": sanitize_text(raw.get("event_type"), 20), "impact": sanitize_text(raw.get("impact"), 8).lower(),
        "attrs": sanitize_tree(raw.get("attrs") or {}, 120)}


def _story_key(kind, title, when, now):
    if kind not in STORY_KINDS:
        return tags.new_story_key(f"{kind} {title}")
    mine = tags.tokens(title)
    since = (when or now) - timedelta(hours=NEAR_DUPLICATE_WINDOW_HOURS)
    recent = (ContextItem.objects.filter(kind__in=STORY_KINDS, published_at__gte=since)
              .order_by("-published_at").values_list("title", "story_key")[:NEAR_DUPLICATE_CANDIDATES])
    for other_title, key in recent:
        if key and tags.jaccard(mine, tags.tokens(other_title)) >= NEAR_DUPLICATE_THRESHOLD:
            return key
    return tags.new_story_key(title)


def store_items(items, now=None):
    """Upsert provider items. Returns {"new": n, "updated": n, "unchanged": n, "skipped": n}. Never raises for one bad item."""
    now = now or timezone.now()
    counts = {"new": 0, "updated": 0, "unchanged": 0, "skipped": 0}
    for raw in items:
        try:
            clean = _clean(raw)
            if clean is None or not raw.get("source") or not raw.get("external_id") or raw.get("kind") not in ContextItem.Kind.values:
                counts["skipped"] += 1
                continue
            fingerprint = tags.raw_hash([clean, raw.get("published_at"), raw.get("event_time")])
            with transaction.atomic():
                existing = ContextItem.objects.filter(source=raw["source"], external_id=raw["external_id"]).first()
                if existing is not None:
                    if existing.raw_hash == fingerprint:
                        counts["unchanged"] += 1
                        continue
                    for field, value in clean.items():
                        setattr(existing, field, value)
                    existing.event_time = raw.get("event_time") or existing.event_time
                    existing.raw_hash = fingerprint
                    existing.save()
                    counts["updated"] += 1
                    continue
                ContextItem.objects.create(
                    source=raw["source"], external_id=raw["external_id"], kind=raw["kind"], published_at=raw.get("published_at"),
                    event_time=raw.get("event_time"), first_seen_at=now, raw_hash=fingerprint,
                    story_key=_story_key(raw["kind"], clean["title"], raw.get("published_at"), now), **clean)
                counts["new"] += 1
        except IntegrityError:  # a concurrent writer stored it first; the next poll reconciles
            counts["unchanged"] += 1
        except Exception:
            logger.exception("intelligence: could not store a context item")
            counts["skipped"] += 1
    return counts


def prune(now=None, days=120, batch=1000):
    """Delete old items that no frozen episode links to. Returns the number deleted."""
    now = now or timezone.now()
    cutoff = now - timedelta(days=days)
    old = (ContextItem.objects.filter(first_seen_at__lt=cutoff, episode_links__isnull=True)
           .exclude(kind="HOLIDAY", event_time__gte=now - timedelta(days=1)))
    ids = list(old.values_list("pk", flat=True)[:batch])
    if not ids:
        return 0
    deleted, _ = ContextItem.objects.filter(pk__in=ids).delete()
    return deleted
