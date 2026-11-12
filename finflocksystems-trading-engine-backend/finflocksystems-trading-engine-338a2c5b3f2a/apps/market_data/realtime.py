import hashlib
import json
import random
import time
from datetime import datetime, timezone
from urllib.parse import quote as url_quote

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone as django_timezone

from apps.event_bus.models import StreamHealthMetric
from apps.market_streams.models import MarketDataSubscription

from .aggregation import FiveSecondTradeAggregator
from .fallback import publish_provider_event
from .providers.base import ProviderError, ProviderErrorCode
from .providers.finnhub import effective_api_key


def reconnect_delay(attempt, maximum, jitter_value=None):
    base = min(float(maximum), float(2 ** max(0, int(attempt))))
    jitter = random.random() if jitter_value is None else float(jitter_value)
    return min(float(maximum), base * (0.75 + 0.5 * jitter))


class FinnhubWebSocketTransport:
    def __init__(self):
        self.connection = None

    def connect(self):
        import websocket
        api_key, _, _ = effective_api_key()
        uri = f"{settings.FINNHUB_WS_URL}?token={url_quote(api_key, safe='')}"
        try:
            self.connection = websocket.create_connection(
                uri, timeout=5, enable_multithread=False, suppress_origin=True,
            )
        except (OSError, websocket.WebSocketException) as exc:
            raise ProviderError("Finnhub WebSocket connection failed",
                                code=ProviderErrorCode.FINNHUB_UNAVAILABLE, provider="FINNHUB",
                                retryable=True) from None

    def send(self, payload):
        import websocket
        if not self.connection:
            raise ProviderError("Finnhub WebSocket is not connected",
                                code=ProviderErrorCode.FINNHUB_UNAVAILABLE, provider="FINNHUB", retryable=True)
        try:
            self.connection.send(json.dumps(payload, separators=(",", ":")))
        except (OSError, websocket.WebSocketException) as exc:
            raise ProviderError("Finnhub WebSocket send failed",
                                code=ProviderErrorCode.FINNHUB_UNAVAILABLE, provider="FINNHUB",
                                retryable=True) from None

    def receive(self, timeout=0.5):
        import websocket
        if not self.connection:
            return None
        try:
            self.connection.settimeout(timeout)
            raw = self.connection.recv()
            if raw in ("", b""):
                raise ProviderError("Finnhub WebSocket connection closed",
                                    code=ProviderErrorCode.FINNHUB_UNAVAILABLE, provider="FINNHUB",
                                    retryable=True)
            return raw
        except websocket.WebSocketTimeoutException:
            return None
        except ProviderError:
            raise
        except (OSError, websocket.WebSocketException) as exc:
            raise ProviderError("Finnhub WebSocket receive failed",
                                code=ProviderErrorCode.FINNHUB_UNAVAILABLE, provider="FINNHUB",
                                retryable=True) from None

    def close(self):
        if self.connection:
            self.connection.close()
            self.connection = None


def desired_finnhub_subscriptions():
    rows = MarketDataSubscription.objects.filter(
        active_provider="FINNHUB", fallback_state__in=["FALLBACK", "RECOVERING"], consumer_count__gt=0,
        instrument__provider_mappings__provider="FINNHUB",
        instrument__provider_mappings__status="VERIFIED",
    ).select_related("instrument__broker_contract").values(
        "id", "gateway_session_id", "instrument_id", "conid", "timeframe", "provider_generation", "fallback_reason",
        "instrument__symbol", "instrument__exchange", "instrument__currency",
        "instrument__provider_mappings__provider_symbol",
    )
    desired = {}
    for row in rows:
        symbol = row["instrument__provider_mappings__provider_symbol"]
        if symbol:
            desired.setdefault(symbol, []).append(row)
    return desired


class FinnhubRealtimeWorker:
    def __init__(self, transport=None, aggregator=None, stop_event=None):
        self.transport = transport or FinnhubWebSocketTransport()
        self.aggregator = aggregator or FiveSecondTradeAggregator(settings.FINNHUB_ALLOWED_LATENESS_SECONDS)
        self.stop_event = stop_event
        self.subscribed = set()
        self.desired = {}
        self.reconnect_count = 0

    def stopped(self):
        return bool(self.stop_event and self.stop_event.is_set())

    def _wait(self, seconds):
        if self.stop_event:
            self.stop_event.wait(seconds)
        else:
            time.sleep(seconds)

    def _connection_metric(self, status, **value):
        StreamHealthMetric.objects.update_or_create(
            component="finnhub-websocket", metric="connection",
            defaults={"status": status, "value": {"subscribed_symbols": len(self.subscribed),
                "reconnect_count":self.reconnect_count,"trade_aggregation":dict(self.aggregator.counters),**value}},
        )

    def _reconcile(self):
        desired = desired_finnhub_subscriptions()
        symbols = set(desired)
        for symbol in sorted(symbols - self.subscribed):
            self.transport.send({"type": "subscribe", "symbol": symbol})
        for symbol in sorted(self.subscribed - symbols):
            self.transport.send({"type": "unsubscribe", "symbol": symbol})
        self.subscribed = symbols
        self.desired = desired

    def _handle_message(self, raw):
        if not raw:
            return
        try:
            message = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return
        if message.get("type") != "trade" or not isinstance(message.get("data"), list):
            return
        now = datetime.now(timezone.utc)
        for trade in message["data"]:
            if isinstance(trade, dict) and trade.get("s") in self.desired:
                self.aggregator.add(trade, now=now)

    def _publish_ready(self):
        for bar in self.aggregator.flush_ready(datetime.now(timezone.utc)):
            for subscription in self.desired.get(bar.provider_symbol, []):
                prefix=f"{subscription['gateway_session_id']}:" if subscription.get("gateway_session_id") else ""
                subscription_key = f"{prefix}{subscription['instrument_id']}:{subscription['timeframe']}"
                stable = hashlib.sha256(
                    f"FINNHUB:{bar.provider_symbol}:{subscription_key}:{bar.window_start.isoformat()}".encode()
                ).hexdigest()
                payload = {
                    "source_event_id": stable, "subscription_key": subscription_key,
                    "instrument_id": subscription["instrument_id"], "conid": subscription["conid"],
                    "symbol": subscription["instrument__symbol"], "exchange": subscription["instrument__exchange"],
                    "currency": subscription["instrument__currency"], "event_kind": "BAR", "timeframe": "5s",
                    "event_time": bar.window_start.isoformat(), "window_start": bar.window_start.isoformat(),
                    "window_end": bar.window_end.isoformat(), "open": str(bar.open), "high": str(bar.high),
                    "low": str(bar.low), "close": str(bar.close), "volume": str(bar.volume), "is_final": True,
                    "source": "finnhub_live", "provider": "FINNHUB", "provider_symbol": bar.provider_symbol,
                    "provider_generation": str(subscription["provider_generation"]),
                    "fallback_reason": subscription["fallback_reason"], "provider_trade_count": bar.trade_count,
                    "processing_mode": "LIVE",
                }
                publish_provider_event(payload)

    def run_forever(self):
        attempt = 0
        while not self.stopped():
            close_old_connections()
            if not (settings.MARKET_DATA_FALLBACK_ENABLED and settings.FINNHUB_LIVE_FALLBACK_ENABLED):
                self._connection_metric("DISABLED", enabled=False)
                self._wait(settings.FINNHUB_WS_RECONCILE_SECONDS)
                continue
            self.desired = desired_finnhub_subscriptions()
            if not self.desired:
                self._connection_metric("IDLE", enabled=True)
                self._wait(settings.FINNHUB_WS_RECONCILE_SECONDS)
                continue
            try:
                self.transport.connect()
                self.subscribed = set()
                self._reconcile()
                self._connection_metric("HEALTHY", connected_at=django_timezone.now().isoformat())
                attempt = 0
                next_reconcile = time.monotonic() + settings.FINNHUB_WS_RECONCILE_SECONDS
                while not self.stopped():
                    self._handle_message(self.transport.receive(0.5))
                    self._publish_ready()
                    if time.monotonic() >= next_reconcile:
                        close_old_connections()
                        self._reconcile()
                        self._connection_metric("HEALTHY", last_heartbeat=django_timezone.now().isoformat())
                        next_reconcile = time.monotonic() + settings.FINNHUB_WS_RECONCILE_SECONDS
            except ProviderError as exc:
                self.reconnect_count += 1
                self._connection_metric("DEGRADED", error_code=exc.code)
                attempt += 1
                self._wait(reconnect_delay(attempt, settings.FINNHUB_WS_RECONNECT_MAX_SECONDS))
            finally:
                self.transport.close()
                self.subscribed = set()
