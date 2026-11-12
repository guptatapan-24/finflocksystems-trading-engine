"""Finnhub (Pro): economic calendar, earnings calendar, general and company news, exchange holidays.

Shapes verified against the live API on 2026-10-23. Verified limits: India company news and sentiment return empty,
and the India economic calendar holds macro prints (PMI, production), not RBI policy decisions: India news therefore
comes from Marketaux and the RBI feed. Calendar `time` is UTC.
"""
from datetime import datetime, timedelta, timezone as dt_utc

from . import base

SOURCE = "finnhub"
HOLIDAY_EXCHANGES = {"NS": ("IN", "XNSE"), "US": ("US", "XNYS")}


class Finnhub:
    def __init__(self, session=None, api_key=None, base_url=None):
        self.session = session
        self.key = api_key if api_key is not None else base.setting("FINNHUB_API_KEY")
        self.base = (base_url or base.setting("FINNHUB_BASE_URL", "https://finnhub.io/api/v1")).rstrip("/")

    def _get(self, path, params=None):
        if not self.key:
            raise base.NotConfigured("FINNHUB_API_KEY is not set")
        data, _ = base.http_get(self.session, f"{self.base}/{path}", params={**(params or {}), "token": self.key}, secrets=(self.key,))
        return data

    # -- calendars ----------------------------------------------------------------------------------
    def economic_calendar(self, now, back_days=1, ahead_days=14):
        data = self._get("calendar/economic", {"from": (now - timedelta(days=back_days)).date().isoformat(),
                                               "to": (now + timedelta(days=ahead_days)).date().isoformat()})
        out = []
        for row in (data or {}).get("economicCalendar") or []:
            when = _utc_naive(row.get("time"))
            if not row.get("event") or when is None:
                continue
            out.append(base.item(
                source=SOURCE, external_id=f"cal:{row.get('country')}:{row['event']}:{row['time']}", kind="EVENT", title=row["event"],
                event_time=when, region=str(row.get("country") or "")[:8], impact=str(row.get("impact") or "").lower(),
                attrs={k: row.get(k) for k in ("estimate", "prev", "actual", "unit") if row.get(k) is not None}))
        return out

    def earnings_calendar(self, now, ahead_days=14):
        data = self._get("calendar/earnings", {"from": now.date().isoformat(), "to": (now + timedelta(days=ahead_days)).date().isoformat()})
        out = []
        for row in (data or {}).get("earningsCalendar") or []:
            if not row.get("symbol") or not row.get("date"):
                continue
            when = _utc_naive(f"{row['date']} 00:00:00")
            hour = {"bmo": "before open", "amc": "after close", "dmh": "during hours"}.get(row.get("hour"), "time not given")
            out.append(base.item(
                source=SOURCE, external_id=f"earn:{row['symbol']}:{row['date']}", kind="EARNINGS", title=f"{row['symbol']} earnings ({hour})",
                event_time=when, symbols=[row["symbol"]], region="US", event_type="EARNINGS",
                attrs={k: row.get(k) for k in ("hour", "quarter", "year", "epsEstimate", "revenueEstimate") if row.get(k) is not None}))
        return out

    def holidays(self, exchange):
        region, zone = HOLIDAY_EXCHANGES[exchange]
        data = self._get("stock/market-holiday", {"exchange": exchange})
        out = []
        for row in (data or {}).get("data") or []:
            when = _utc_naive(f"{row.get('atDate')} 00:00:00")
            if when is None or not row.get("eventName"):
                continue
            out.append(base.item(
                source=SOURCE, external_id=f"holiday:{exchange}:{row['atDate']}", kind="HOLIDAY", title=row["eventName"], event_time=when,
                region=region, event_type="HOLIDAY",
                attrs={"zone": zone, "date": row["atDate"], "trading_hour": row.get("tradingHour") or "", "post_market": row.get("postMarket") or ""}))
        return out

    # -- news ---------------------------------------------------------------------------------------
    def general_news(self):
        return [n for n in (self._news_row(r, None) for r in self._get("news", {"category": "general"}) or []) if n]

    def company_news(self, symbol, now, back_days=2):
        rows = self._get("company-news", {"symbol": symbol, "from": (now - timedelta(days=back_days)).date().isoformat(), "to": now.date().isoformat()})
        return [n for n in (self._news_row(r, symbol) for r in rows or []) if n]

    @staticmethod
    def _news_row(row, symbol):
        published = base.from_epoch(row.get("datetime"))
        if not row.get("headline") or published is None or row.get("id") is None:
            return None
        related = [s.strip() for s in str(row.get("related") or "").split(",") if s.strip()]
        symbols = [symbol] if symbol else related
        return base.item(source=SOURCE, external_id=f"news:{row['id']}", kind="NEWS", title=row["headline"], summary=row.get("summary"),
                         url=row.get("url"), published_at=published, symbols=symbols, region="US" if symbol else "GLOBAL",
                         attrs={"publisher": row.get("source"), "category": row.get("category")})


def _utc_naive(value):
    """'2026-10-28 18:00:00' (UTC, no zone marker) -> aware UTC datetime."""
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt_utc.utc)
    except ValueError:
        return None
