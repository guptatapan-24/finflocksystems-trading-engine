"""NFO spot access (used in-process by the ingestion worker) + missed-night alert. Everything external (Redis, gateway, SMTP, ledger) is faked."""
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from apps.broker_gateway.client import GatewayCommandFailed, GatewayCommandTimeout
from apps.market_data import ingestion_alert, ingestion_status, nfo_spot
from apps.market_data.nfo_spot import SpotError, SpotGate

NIGHT = datetime(2026, 11, 3, 18, 0, tzinfo=timezone.utc)        # Tue 23:30 IST: inside the idle window
STRATEGY_HOURS = datetime(2026, 11, 3, 6, 0, tzinfo=timezone.utc)  # Tue 11:30 IST: strategy trading
BODY = {"underlying": "NIFTY", "session_date": "2026-11-02", "run_id": "run-abc12345", "attempt": 1}


class FakeRedis:
    def __init__(self, fail=False):
        self.store, self.fail = {}, fail

    def set(self, key, value, nx=False, px=None):
        if self.fail:
            raise ConnectionError("redis down")
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def incr(self, key):
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    def expire(self, key, seconds):
        return True

    def eval(self, *args, **kwargs):
        raise PermissionError("NOPERM this user has no permissions to run the 'eval' command")

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        return 1 if self.store.pop(key, None) is not None else 0


class FakeClient:
    def __init__(self, result=None, error=None, mode="PAPER"):
        self.result, self.error, self.mode, self.requests = result, error, mode, []

    def health(self):
        return {"mode": self.mode}

    def request(self, method, path, *, json=None, idempotency_key=None, retries=2, **kw):
        self.requests.append((method, path, json, idempotency_key, retries))
        return {"command_id": 7, "status": "PENDING"}

    def wait_for_command(self, queued, timeout=None, operation=None):
        if self.error:
            raise self.error
        return self.result


def good_result(conid=51497778, symbol="NIFTY50", n=2):
    return {"conid": conid, "symbol": symbol, "provider": "IBKR",
            "bars": [{"date": f"2026-11-02T03:{45 + i}:00+00:00", "open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume": "0"} for i in range(n)]}


def run(client=None, gate=None, now=NIGHT, body=BODY, resolver=None):
    client = client or FakeClient(good_result())
    return nfo_spot.fetch_spot_bars(body, now=now, gate=gate or SpotGate(FakeRedis()), client_factory=lambda s: client,
                                    session_resolver=resolver or (lambda: SimpleNamespace(mode="PAPER")))


# ---- request shape -------------------------------------------------------------------------

def test_payload_is_exactly_what_the_pipeline_asks_ibkr_for():
    payload = nfo_spot.build_history_payload("NIFTY", date(2026, 11, 2))
    assert payload == {"conid": 51497778, "symbol": "NIFTY50", "exchange": "NSE", "currency": "INR", "bar_size": "1 min",
                       "duration": "1 D", "what_to_show": "TRADES", "use_rth": False, "end_time": "20261102-18:30:00"}  # = 2026-11-03 00:00 IST


def test_all_five_contracts_match_the_pipelines_pinned_ids():
    assert nfo_spot.INDEXES == {"NIFTY": (51497778, "NIFTY50"), "BANKNIFTY": (56994300, "BANKNIFTY"), "FINNIFTY": (468436436, "NSEFIN"),
                                "MIDCPNIFTY": (657342268, "MIDCAPSEL"), "NIFTYNXT50": (279064251, "NIFTYNXT50")}


@pytest.mark.parametrize("change,code", [
    ({"underlying": "SENSEX"}, "INVALID_UNDERLYING"), ({"session_date": "02-11-2026"}, "INVALID_SESSION_DATE"),
    ({"session_date": "2026-11-07"}, "INVALID_SESSION_DATE"),   # a Saturday
    ({"session_date": "2026-11-04"}, "INVALID_SESSION_DATE"),   # tomorrow, not traded yet
    ({"session_date": "2026-10-01"}, "INVALID_SESSION_DATE"),   # too old
    ({"run_id": "x"}, "INVALID_RUN_ID"), ({"attempt": 3}, "INVALID_ATTEMPT"), ({"use_rth": True}, "INVALID_REQUEST"),
])
def test_bad_requests_are_rejected_before_anything_is_contacted(change, code):
    client = FakeClient(good_result())
    with pytest.raises(SpotError) as info:
        run(client=client, body={**BODY, **change})
    assert info.value.code == code and client.requests == []


def test_todays_session_is_accepted_once_the_window_opens_but_not_after_midnight():
    # 23:30 IST: today's session closed at 15:30, so the nightly run may fetch it.
    nfo_spot.validate_request({**BODY, "session_date": "2026-11-03"}, NIGHT)
    # 02:00 IST on Wednesday: Wednesday has not traded yet; Tuesday is the latest finished session.
    after_midnight = datetime(2026, 11, 3, 20, 30, tzinfo=timezone.utc)
    nfo_spot.validate_request({**BODY, "session_date": "2026-11-03"}, after_midnight)
    with pytest.raises(SpotError) as info:
        nfo_spot.validate_request({**BODY, "session_date": "2026-11-04"}, after_midnight)
    assert info.value.code == "INVALID_SESSION_DATE"


# ---- guardrails ----------------------------------------------------------------------------

def test_refuses_during_strategy_hours_without_touching_the_gateway():
    client = FakeClient(good_result())
    with pytest.raises(SpotError) as info:
        run(client=client, now=STRATEGY_HOURS)
    assert (info.value.code, info.value.status) == ("OUTSIDE_WINDOW", 403) and client.requests == []


@pytest.mark.parametrize("utc_hour,utc_minute,allowed", [(11, 0, True), (10, 59, False), (2, 59, True), (3, 0, False), (6, 29, False)])
def test_window_boundaries_are_16_30_to_08_30_ist(utc_hour, utc_minute, allowed):
    assert nfo_spot.in_window(datetime(2026, 11, 3, utc_hour, utc_minute, tzinfo=timezone.utc)) is allowed


def test_only_one_request_in_flight():
    redis = FakeRedis()
    holder = SpotGate(redis).acquire(60)
    with pytest.raises(SpotError) as info:
        run(gate=SpotGate(redis))
    assert (info.value.code, info.value.status) == ("BUSY", 409)
    SpotGate(redis).release(holder)
    assert run(gate=SpotGate(redis))["bar_count"] == 2


def test_lock_is_released_after_success_and_after_failure():
    redis = FakeRedis()
    run(gate=SpotGate(redis))
    assert not any(key.endswith("lock") for key in redis.store)
    with pytest.raises(SpotError):
        run(gate=SpotGate(redis), client=FakeClient(error=GatewayCommandFailed("boom", command={})))
    assert not any(key.endswith("lock") for key in redis.store)


def test_minimum_gap_between_requests_is_enforced():
    gate = SpotGate(FakeRedis())
    run(gate=gate)
    with pytest.raises(SpotError) as info:
        run(gate=gate)
    assert (info.value.code, info.value.status) == ("TOO_SOON", 429)


def test_daily_budget_is_enforced(settings):
    settings.NFO_SPOT_DAILY_REQUEST_LIMIT = 2
    redis = FakeRedis()
    for _ in range(2):
        redis.store.pop("nfo-spot:interval", None)
        run(gate=SpotGate(redis))
    redis.store.pop("nfo-spot:interval", None)
    with pytest.raises(SpotError) as info:
        run(gate=SpotGate(redis))
    assert info.value.code == "DAILY_LIMIT"


def test_fails_closed_when_redis_is_down():
    client = FakeClient(good_result())
    with pytest.raises(SpotError) as info:
        run(client=client, gate=SpotGate(FakeRedis(fail=True)))
    assert (info.value.code, info.value.status) == ("THROTTLE_UNAVAILABLE", 503) and client.requests == []


def test_no_connected_session_is_a_clean_503_and_sends_nothing():
    def none():
        raise SpotError("GATEWAY_UNAVAILABLE", "logged out", status=503)
    client = FakeClient(good_result())
    with pytest.raises(SpotError) as info:
        run(client=client, resolver=none)
    assert info.value.code == "GATEWAY_UNAVAILABLE" and client.requests == []


def test_mode_mismatch_is_refused():
    client = FakeClient(good_result(), mode="LIVE")
    with pytest.raises(SpotError) as info:
        run(client=client)
    assert info.value.code == "GATEWAY_MODE_MISMATCH" and client.requests == []


def test_a_gateway_timeout_is_reported_as_possibly_stuck():
    with pytest.raises(SpotError) as info:
        run(client=FakeClient(error=GatewayCommandTimeout("slow", command={}, timeout=45)))
    assert (info.value.code, info.value.status) == ("GATEWAY_TIMEOUT", 504) and "pending" in str(info.value)


def test_wrong_contract_is_refused():
    with pytest.raises(SpotError) as info:
        run(client=FakeClient(good_result(conid=56994300, symbol="BANKNIFTY")))
    assert info.value.code == "WRONG_CONTRACT"


def test_request_uses_a_unique_idempotency_key_per_run_attempt_and_no_transport_retries():
    client = FakeClient(good_result())
    run(client=client)
    method, path, payload, key, retries = client.requests[0]
    assert (method, path, retries) == ("POST", "market-data/history/", 0)
    assert key == "nfo-spot:run-abc12345:NIFTY:2026-11-02:1" and payload["use_rth"] is False


def test_response_carries_bars_in_order_with_utc_timestamps():
    data = run()
    assert data["underlying"] == "NIFTY" and data["conid"] == 51497778
    assert data["bars"][0] == {"observed_at": "2026-11-02T03:45:00+00:00", "open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume": "0"}


# ---- session choice ------------------------------------------------------------------------

@pytest.mark.django_db
def test_session_resolution_requires_exactly_one_connected_paper_session(settings):
    from apps.broker_gateway.models import BrokerGatewaySession as S

    def make(name, **kw):
        defaults = dict(display_name=name, username_hint="x", mode="paper", status=S.Status.CONNECTED, commands_enabled=True,
                        child_container_name=name, encrypted_gateway_token="t", encrypted_novnc_password="n")
        defaults.update(kw)
        return S.objects.create(**defaults)

    with pytest.raises(SpotError) as info:
        nfo_spot.resolve_session()
    assert info.value.code == "GATEWAY_UNAVAILABLE"
    live = make("gw-live", mode="live")
    with pytest.raises(SpotError):
        nfo_spot.resolve_session()   # a live session is never chosen implicitly
    paper = make("gw-paper")
    assert nfo_spot.resolve_session().pk == paper.pk
    make("gw-paper-2")
    with pytest.raises(SpotError) as info:
        nfo_spot.resolve_session()
    assert info.value.code == "GATEWAY_AMBIGUOUS"
    settings.NFO_SPOT_GATEWAY_SESSION_ID = str(paper.pk)
    assert nfo_spot.resolve_session().pk == paper.pk
    assert live.pk != paper.pk


# ---- holidays and the missed-night alert ---------------------------------------------------

def test_listed_holiday_is_skipped_by_expected_session(settings):
    after_tuesday = datetime(2026, 11, 3, 22, 0, tzinfo=timezone.utc)  # Wed 03:30 IST: Tuesday's window has closed
    assert ingestion_status.expected_session(after_tuesday) == date(2026, 11, 3)
    assert ingestion_status.expected_session(after_tuesday, {date(2026, 11, 3)}) == date(2026, 11, 2)
    settings.NFO_HOLIDAYS = "2026-11-03, junk ,2026-11-09"
    assert ingestion_status.configured_holidays() == {date(2026, 11, 3), date(2026, 11, 9)}


MORNING = datetime(2026, 11, 3, 3, 0, tzinfo=timezone.utc)   # Tue 08:30 IST, Monday's window closed at 01:30


def ledger(latest_session, recent=()):
    latest = (latest_session, 100, None, "x.zip") if latest_session else None
    return lambda url, profile: (latest, list(recent))


@pytest.fixture
def alerts(settings):
    settings.REMOTE_FEATURE_DB_URL = "postgresql://u:p@db/x"
    settings.NFO_LEDGER_PROFILE = "P"
    settings.NFO_FRESHNESS_ALERT_TO = "ops@example.com, second@example.com"
    settings.NFO_HOLIDAYS = ""
    sent = []
    return SimpleNamespace(sent=sent, send=lambda subject, body, sender, to, fail_silently=False: sent.append((subject, body, to)))


def test_alert_is_disabled_without_recipients(alerts, settings):
    settings.NFO_FRESHNESS_ALERT_TO = ""
    assert ingestion_alert.check_and_alert(MORNING, ledger(date(2026, 10, 1)), alerts.send)["status"] == "disabled" and alerts.sent == []


@pytest.mark.django_db
def test_no_alert_when_up_to_date(alerts):
    assert ingestion_alert.check_and_alert(MORNING, ledger(date(2026, 11, 2)), alerts.send)["status"] == "ok" and alerts.sent == []


@pytest.mark.django_db
def test_missed_day_alerts_once_and_names_the_day(alerts):
    first = ingestion_alert.check_and_alert(MORNING, ledger(date(2026, 10, 30)), alerts.send)
    again = ingestion_alert.check_and_alert(MORNING, ledger(date(2026, 10, 30)), alerts.send)
    assert first["status"] == "alerted" and again["status"] == "already_alerted" and len(alerts.sent) == 1
    subject, body, to = alerts.sent[0]
    assert "2026-11-02" in subject and "NFO_HOLIDAYS" in body and to == ["ops@example.com", "second@example.com"]


@pytest.mark.django_db
def test_a_listed_holiday_does_not_alert(alerts, settings):
    settings.NFO_HOLIDAYS = "2026-11-02"
    assert ingestion_alert.check_and_alert(MORNING, ledger(date(2026, 10, 30)), alerts.send)["status"] == "ok"


@pytest.mark.django_db
def test_failed_send_is_retried_next_time(alerts):
    def broken(*a, **k):
        raise OSError("smtp down")
    with pytest.raises(OSError):
        ingestion_alert.check_and_alert(MORNING, ledger(date(2026, 10, 30)), broken)
    assert ingestion_alert.check_and_alert(MORNING, ledger(date(2026, 10, 30)), alerts.send)["status"] == "alerted"


@pytest.mark.django_db
def test_unreadable_ledger_alerts_once_per_day(alerts):
    def down(url, profile):
        raise OSError("no route")
    assert ingestion_alert.check_and_alert(MORNING, down, alerts.send)["status"] == "alerted"
    assert ingestion_alert.check_and_alert(MORNING, down, alerts.send)["status"] == "already_alerted"
    assert "cannot be read" in alerts.sent[0][0]
