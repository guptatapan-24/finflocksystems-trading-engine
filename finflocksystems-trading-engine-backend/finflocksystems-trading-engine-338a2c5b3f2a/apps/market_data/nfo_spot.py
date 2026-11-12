"""Read-only relay of one-minute NSE index bars, for the nightly NFO ingestion (see `ingestionclaude.md`).

The ingestion needs the index spot bars that IBKR serves through the portfolio's IB Gateway. The gateway child only
publishes HTTP, so the ingestion cannot open an IBKR API connection of its own. This relay lets it ask the Backend,
which already holds each session's gateway token, so the token never leaves the Backend and survives re-provisioning.

It is a layer on top of the trading system and is built so it cannot disturb it:

* Off unless `NFO_SPOT_PROXY_TOKEN` is set; the caller authenticates with that token (never the gateway token).
* The caller names only an index and a past session date. The Backend builds the exact request itself (1 minute, TRADES,
  outside regular hours included, one day ending midnight IST after the session) for exactly five pinned contracts.
* Only inside the idle window 16:30-08:30 IST. The strategy runs 09:00-16:00 IST; the gateway's daily restart is 05:15 IST.
* One request in flight at a time across all workers, a minimum gap between requests, and a daily request budget (Redis).
  If Redis cannot be reached the relay refuses (fails closed) rather than run unthrottled.
* Paper session by default (`NFO_SPOT_GATEWAY_MODE`); an explicit `NFO_SPOT_GATEWAY_SESSION_ID` pins the session.
* Nothing is written to the database, and no gateway command is created except the single history request.
"""

import re
import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from django.conf import settings

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
# Pinned once by hand against TWS (kept identical to nfo_pipeline.ibkr.INDEX_REQUESTS; the ingestion worker re-checks every reply).
INDEXES = {
    "NIFTY": (51497778, "NIFTY50"),
    "BANKNIFTY": (56994300, "BANKNIFTY"),
    "FINNIFTY": (468436436, "NSEFIN"),
    "MIDCPNIFTY": (657342268, "MIDCAPSEL"),
    "NIFTYNXT50": (279064251, "NIFTYNXT50"),
}
WINDOW_OPENS = time(16, 30)   # idle window: 16:30 IST ... 08:30 IST the next morning
WINDOW_CLOSES = time(8, 30)
MAX_SESSION_AGE_DAYS = 14
RUN_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class SpotError(Exception):
    def __init__(self, code, message, status=400, details=None):
        super().__init__(message)
        self.code, self.status, self.details = code, status, details or {}


def in_window(now):
    local = now.astimezone(IST).time()
    return local >= WINDOW_OPENS or local < WINDOW_CLOSES


def build_history_payload(underlying, session_date):
    """Exactly what nfo_pipeline asks IBKR for: 1 min TRADES bars, useRTH=0, 1 D ending midnight IST after the session."""
    conid, symbol = INDEXES[underlying]
    end = datetime.combine(session_date + timedelta(days=1), time.min, IST).astimezone(UTC)
    return {"conid": conid, "symbol": symbol, "exchange": "NSE", "currency": "INR", "bar_size": "1 min",
            "duration": "1 D", "what_to_show": "TRADES", "use_rth": False, "end_time": end.strftime("%Y%m%d-%H:%M:%S")}


def validate_request(body, now):
    if not isinstance(body, dict):
        raise SpotError("INVALID_REQUEST", "A JSON object is required")
    unknown = set(body) - {"underlying", "session_date", "run_id", "attempt"}
    if unknown:
        raise SpotError("INVALID_REQUEST", f"Unsupported fields: {', '.join(sorted(unknown))}")
    underlying = str(body.get("underlying", "")).upper()
    if underlying not in INDEXES:
        raise SpotError("INVALID_UNDERLYING", f"underlying must be one of {', '.join(INDEXES)}")
    try:
        session = date.fromisoformat(str(body.get("session_date", "")))
    except ValueError:
        raise SpotError("INVALID_SESSION_DATE", "session_date must be YYYY-MM-DD") from None
    local = now.astimezone(IST)
    today = local.date()
    # Today's session is finished once the idle window opens (16:30 IST, an hour after the 15:30 close),
    # so the nightly run can fetch the session it is ingesting. After midnight "today" has not traded yet.
    finished_through = today if local.time() >= WINDOW_OPENS else today - timedelta(days=1)
    if session.weekday() >= 5 or session > finished_through or (today - session).days > MAX_SESSION_AGE_DAYS:
        raise SpotError("INVALID_SESSION_DATE",
                        f"session_date must be a finished weekday session within {MAX_SESSION_AGE_DAYS} days")
    run_id = str(body.get("run_id", ""))
    if not RUN_ID.fullmatch(run_id):
        raise SpotError("INVALID_RUN_ID", "run_id must be 8-64 characters of letters, digits, '-' or '_'")
    try:
        attempt = int(body.get("attempt", 1))
    except (TypeError, ValueError):
        attempt = 0
    if attempt not in (1, 2):
        raise SpotError("INVALID_ATTEMPT", "attempt must be 1 or 2")
    return underlying, session, run_id, attempt


class SpotGate:
    """One request at a time, a minimum gap, and a daily budget, shared by every Backend worker through Redis."""

    def __init__(self, redis_client=None):
        self._redis = redis_client
        self.prefix = f"{getattr(settings, 'REDIS_KEY_PREFIX', '')}nfo-spot:"

    def _client(self):
        if self._redis is None:
            import redis

            self._redis = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=2, socket_timeout=2)
        return self._redis

    def acquire(self, lease_seconds):
        token = uuid.uuid4().hex
        try:
            if not self._client().set(self.prefix + "lock", token, nx=True, px=int(lease_seconds * 1000)):
                raise SpotError("BUSY", "Another spot request is in flight", status=409)
        except SpotError:
            raise
        except Exception:
            raise SpotError("THROTTLE_UNAVAILABLE", "The request throttle (Redis) is unavailable, so the relay refuses to run", status=503) from None
        return token

    def release(self, token):
        # EVAL is refused for the hosted Redis user, so compare and delete with two plain commands. Only the
        # ingestion worker takes this lock, one request at a time, so the gap between them is harmless.
        try:
            client, key = self._client(), self.prefix + "lock"
            current = client.get(key)
            if isinstance(current, bytes):
                current = current.decode("utf-8", "replace")
            if current == token:
                client.delete(key)
        except Exception:
            pass  # the lease expires on its own

    def pace(self, min_interval_seconds, daily_limit, today):
        try:
            client = self._client()
            if not client.set(self.prefix + "interval", "1", nx=True, px=max(1, int(min_interval_seconds * 1000))):
                raise SpotError("TOO_SOON", f"Requests must be at least {min_interval_seconds:g}s apart", status=429,
                                details={"retry_after_seconds": min_interval_seconds})
            key = f"{self.prefix}budget:{today.isoformat()}"
            used = client.incr(key)
            client.expire(key, 36 * 3600)
            if used > daily_limit:
                raise SpotError("DAILY_LIMIT", f"Daily request budget of {daily_limit} is used up", status=429)
        except SpotError:
            raise
        except Exception:
            raise SpotError("THROTTLE_UNAVAILABLE", "The request throttle (Redis) is unavailable, so the relay refuses to run", status=503) from None


def resolve_session():
    from apps.broker_gateway.models import BrokerGatewaySession

    pinned = str(getattr(settings, "NFO_SPOT_GATEWAY_SESSION_ID", "") or "").strip()
    sessions = BrokerGatewaySession.objects.filter(deleted_at__isnull=True)
    if pinned:
        session = sessions.filter(pk=pinned).first()
        if session is None:
            raise SpotError("GATEWAY_UNAVAILABLE", "The configured gateway session does not exist", status=503)
        return session
    mode = str(getattr(settings, "NFO_SPOT_GATEWAY_MODE", "PAPER")).lower()  # the session model stores "paper" / "live"
    candidates = list(sessions.filter(mode=mode, status=BrokerGatewaySession.Status.CONNECTED, commands_enabled=True))
    if not candidates:
        raise SpotError("GATEWAY_UNAVAILABLE", f"No connected {mode.upper()} gateway session is available "
                        "(logged out, waiting for login, or restarting)", status=503)
    if len(candidates) > 1:
        raise SpotError("GATEWAY_AMBIGUOUS", f"{len(candidates)} connected {mode.upper()} sessions; set NFO_SPOT_GATEWAY_SESSION_ID", status=503)
    return candidates[0]


def _client_for(session):
    from apps.broker_gateway.client import GatewayClient

    return GatewayClient(session, require_commands=True)


def fetch_spot_bars(body, *, now=None, gate=None, client_factory=_client_for, session_resolver=resolve_session):
    now = now or datetime.now(UTC)
    underlying, session_date, run_id, attempt = validate_request(body, now)
    if not in_window(now):
        raise SpotError("OUTSIDE_WINDOW", "Spot requests are only allowed 16:30-08:30 IST (the strategy trades 09:00-16:00 IST)", status=403)
    gate = gate or SpotGate()
    timeout = float(getattr(settings, "NFO_SPOT_COMMAND_TIMEOUT_SECONDS", 45))
    token = gate.acquire(lease_seconds=timeout + 30)
    try:
        gate.pace(float(getattr(settings, "NFO_SPOT_MIN_INTERVAL_SECONDS", 5)),
                  int(getattr(settings, "NFO_SPOT_DAILY_REQUEST_LIMIT", 20)), now.astimezone(IST).date())
        session = session_resolver()
        result = _call_gateway(client_factory(session), session, underlying, session_date, run_id, attempt, timeout)
    finally:
        gate.release(token)
    return _shape(result, underlying, session_date)


def _call_gateway(client, session, underlying, session_date, run_id, attempt, timeout):
    from apps.broker_gateway.client import (
        GatewayCommandFailed, GatewayCommandTimeout, GatewayError,
    )

    payload = build_history_payload(underlying, session_date)
    key = f"nfo-spot:{run_id}:{underlying}:{session_date.isoformat()}:{attempt}"
    try:
        reported = str((client.health() or {}).get("mode", "")).lower()
        if reported and reported != str(session.mode).lower():
            raise SpotError("GATEWAY_MODE_MISMATCH", f"The gateway reports {reported} but the session is {session.mode}", status=503)
        queued = client.request("POST", "market-data/history/", json=payload, idempotency_key=key, retries=0)
        return client.wait_for_command(queued, timeout=timeout, operation="HISTORICAL_DATA")
    except GatewayCommandTimeout:
        raise SpotError("GATEWAY_TIMEOUT", f"The gateway did not answer within {timeout:g}s. The request may still be pending in the "
                        "gateway's queue; do not send more until it clears.", status=504) from None
    except GatewayCommandFailed as exc:
        raise SpotError("GATEWAY_COMMAND_FAILED", str(exc)[:300], status=502) from None
    except GatewayError as exc:
        raise SpotError("GATEWAY_UNAVAILABLE", str(exc)[:300], status=503) from None


def _shape(result, underlying, session_date):
    conid, symbol = INDEXES[underlying]
    if int(result.get("conid") or 0) != conid or str(result.get("symbol", "")).upper() != symbol.upper():
        raise SpotError("WRONG_CONTRACT", "The gateway answered for a different contract than requested", status=502)
    bars = [{"observed_at": str(bar["date"]), "open": str(bar["open"]), "high": str(bar["high"]), "low": str(bar["low"]),
             "close": str(bar["close"]), "volume": str(bar.get("volume", "0"))} for bar in result.get("bars", [])]
    return {"underlying": underlying, "session_date": session_date.isoformat(), "conid": conid, "symbol": symbol,
            "bar_count": len(bars), "bars": bars}
