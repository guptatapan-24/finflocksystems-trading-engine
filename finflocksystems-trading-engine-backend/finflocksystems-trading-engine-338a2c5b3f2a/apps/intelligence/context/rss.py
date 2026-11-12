"""Official announcement feeds (RBI press releases, Federal Reserve press releases) read as RSS.

Feeds are third-party XML, so parsing is defensive: a document that declares a DOCTYPE or an entity is refused outright
(the entity-expansion class of attacks), the size is capped, and every text field is stripped of markup. RBI timestamps
carry no zone and are IST; the Fed's are GMT.
"""
import xml.etree.ElementTree as ET
from datetime import timezone as dt_utc
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from . import base, tags

FEEDS = {
    "rbi": {"url": "https://rbi.org.in/pressreleases_rss.xml", "region": "IN", "tz": "Asia/Kolkata", "label": "RBI press releases"},
    "fed": {"url": "https://www.federalreserve.gov/feeds/press_all.xml", "region": "US", "tz": "UTC", "label": "Federal Reserve press releases"},
}
MAX_ITEMS = 25
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; finflock-intelligence/1.0)"}


def parse(content, name, now=None):
    """Items from RSS bytes. Raises SourceError for unsafe or unparseable documents."""
    cfg = FEEDS[name]
    text = content.decode("utf-8-sig", errors="replace") if isinstance(content, (bytes, bytearray)) else str(content)
    lowered = text.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise base.SourceError("feed refused: it declares a DOCTYPE or entity")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise base.SourceError(f"feed is not valid XML: {exc}") from exc
    zone = ZoneInfo(cfg["tz"])
    out = []
    for node in root.iter("item"):
        title = tags.strip_html((node.findtext("title") or "").strip())
        link = (node.findtext("link") or "").strip()
        stamp = (node.findtext("pubDate") or "").strip()
        if not title or not stamp:
            continue
        try:
            published = parsedate_to_datetime(stamp)
        except (TypeError, ValueError):
            continue
        published = (published.replace(tzinfo=zone) if published.tzinfo is None else published).astimezone(dt_utc.utc)
        guid = (node.findtext("guid") or link or title).strip()
        etype = tags.classify_event(title)
        out.append(base.item(
            source=name, external_id=f"rss:{guid}"[:190], kind="ANNOUNCEMENT", title=title,
            summary=tags.strip_html(node.findtext("description") or "")[:500], url=tags.clean_url(link), published_at=published,
            region=cfg["region"], event_type=etype, attrs={"feed": cfg["label"]}))
        if len(out) >= MAX_ITEMS:
            break
    return out


class Feed:
    def __init__(self, name, session=None):
        self.name = name
        self.session = session

    def fetch(self, now=None):
        content, _ = base.http_get(self.session, FEEDS[self.name]["url"], headers=HEADERS, expect="bytes")
        return parse(content, self.name, now)
