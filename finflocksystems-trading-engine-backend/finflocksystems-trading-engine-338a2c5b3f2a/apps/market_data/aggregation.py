import hashlib
import json
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class AggregatedTradeBar:
    provider_symbol: str
    window_start: datetime
    window_end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trade_count: int


class FiveSecondTradeAggregator:
    def __init__(self, allowed_lateness_seconds=2, dedup_retention_seconds=120):
        self.allowed_lateness = timedelta(seconds=int(allowed_lateness_seconds))
        self.dedup_retention = timedelta(seconds=int(dedup_retention_seconds))
        self.buckets = defaultdict(list)
        self.seen = OrderedDict()
        self.finalized = OrderedDict()
        self.counters = {"accepted": 0, "duplicate": 0, "late": 0, "invalid": 0}

    @staticmethod
    def _trade_key(trade):
        provider_id = trade.get("id") or trade.get("trade_id")
        if provider_id not in (None, ""):
            return f"id:{provider_id}"
        identity = [trade.get("s"), trade.get("t"), trade.get("p"), trade.get("v"), trade.get("c") or []]
        return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def _window(timestamp_ms):
        seconds = int(timestamp_ms) // 1000
        start = seconds - seconds % 5
        return datetime.fromtimestamp(start, tz=timezone.utc)

    @staticmethod
    def _decimal(value, *, positive=False):
        try:
            result = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("Finnhub trade contains an invalid decimal") from exc
        if not result.is_finite() or (positive and result <= 0) or (not positive and result < 0):
            raise ValueError("Finnhub trade contains an out-of-range decimal")
        return result

    def _expire(self, now):
        cutoff = now - self.dedup_retention
        while self.seen and next(iter(self.seen.values())) < cutoff:
            self.seen.popitem(last=False)
        while self.finalized and next(iter(self.finalized.values())) < cutoff:
            self.finalized.popitem(last=False)

    def add(self, trade, now=None):
        now = now or datetime.now(timezone.utc)
        symbol = str(trade.get("s") or "").strip()
        try:
            timestamp_ms = int(trade.get("t"))
            price = self._decimal(trade.get("p"), positive=True)
            volume = self._decimal(trade.get("v", 0))
        except (TypeError, ValueError):
            self.counters["invalid"] += 1
            return "INVALID"
        if not symbol or timestamp_ms <= 0:
            self.counters["invalid"] += 1
            return "INVALID"
        start = self._window(timestamp_ms)
        bucket_key = (symbol, start)
        if bucket_key in self.finalized:
            self.counters["late"] += 1
            return "LATE"
        trade_key = self._trade_key(trade)
        if trade_key in self.seen:
            self.counters["duplicate"] += 1
            return "DUPLICATE"
        self.seen[trade_key] = now
        self.buckets[bucket_key].append((timestamp_ms, trade_key, price, volume))
        self.counters["accepted"] += 1
        self._expire(now)
        return "ACCEPTED"

    def flush_ready(self, now=None):
        now = now or datetime.now(timezone.utc)
        ready = []
        for (symbol, start), trades in sorted(self.buckets.items(), key=lambda item: item[0]):
            end = start + timedelta(seconds=5)
            if now < end + self.allowed_lateness:
                continue
            ordered = sorted(trades, key=lambda item: (item[0], item[1]))
            prices = [item[2] for item in ordered]
            ready.append(AggregatedTradeBar(
                provider_symbol=symbol, window_start=start, window_end=end, open=prices[0], high=max(prices),
                low=min(prices), close=prices[-1], volume=sum((item[3] for item in ordered), Decimal(0)),
                trade_count=len(ordered),
            ))
            key = (symbol, start)
            self.finalized[key] = now
            del self.buckets[key]
        self._expire(now)
        return ready
