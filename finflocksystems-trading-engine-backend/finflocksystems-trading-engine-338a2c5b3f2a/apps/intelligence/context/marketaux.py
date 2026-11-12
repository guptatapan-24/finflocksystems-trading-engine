"""Marketaux: news with entity tagging and sentiment. Used for India market stories (`countries=in`) and, in one call, for
the US symbols the strategies trade.

The key in use is on a free plan (verified: 100 requests per day, 30 per minute), so the poller keeps a hard daily call
cap and each call is made for a reason. Shapes verified against the live API on 2026-10-23.
"""
from . import base, tags

SOURCE = "marketaux"
URL = "https://api.marketaux.com/v1/news/all"


class Marketaux:
    def __init__(self, session=None, api_key=None, base_url=None):
        self.session = session
        self.key = api_key if api_key is not None else base.setting("MARKETAUX_API_KEY")
        self.url = base_url or base.setting("MARKETAUX_BASE_URL", URL)
        self.remaining = None  # the provider's own remaining-quota figure from the last response

    def _fetch(self, params, region):
        if not self.key:
            raise base.NotConfigured("MARKETAUX_API_KEY is not set")
        data, headers = base.http_get(self.session, self.url, params={**params, "language": "en", "api_token": self.key}, secrets=(self.key,))
        remaining = headers.get("x-usagelimit-remaining")
        self.remaining = int(remaining) if remaining and str(remaining).isdigit() else None
        out = []
        for row in (data or {}).get("data") or []:
            published = base.from_iso(row.get("published_at"))
            if not row.get("title") or not row.get("uuid") or published is None:
                continue
            entities = [e for e in (row.get("entities") or []) if e.get("symbol")]
            scores = [float(e["sentiment_score"]) for e in entities if isinstance(e.get("sentiment_score"), (int, float))]
            out.append(base.item(
                source=SOURCE, external_id=f"mx:{row['uuid']}", kind="NEWS", title=row["title"],
                summary=row.get("description") or row.get("snippet"), url=row.get("url"), published_at=published,
                symbols=[e["symbol"] for e in entities], region=region, event_type=tags.classify_event(row["title"]),
                attrs={"publisher": row.get("source"), "sentiment": round(sum(scores) / len(scores), 4) if scores else None,
                       "relevance": row.get("relevance_score")}))
        return out

    def india_news(self, limit=3):
        return self._fetch({"countries": "in", "limit": limit}, "IN")

    def symbol_news(self, symbols, limit=3):
        return self._fetch({"symbols": ",".join(symbols[:10]), "limit": limit}, "US")
