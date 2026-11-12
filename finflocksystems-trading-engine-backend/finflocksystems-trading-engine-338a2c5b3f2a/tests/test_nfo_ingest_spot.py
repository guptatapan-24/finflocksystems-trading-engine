"""Spot adapter guardrails. No network, no Django: the Backend's spot access is a fake transport and time is injected."""
import threading
import time as real_time
from datetime import date, datetime, timedelta, timezone

import pytest

from nfo_ingestion import spot as sg

IST = sg.IST
SESSION = date(2026, 11, 2)
NIGHT = datetime(2026, 11, 3, 22, 0, tzinfo=IST)
STRATEGY_HOURS = datetime(2026, 11, 3, 11, 0, tzinfo=IST)


def reply(underlying="NIFTY", session=SESSION, n=375, **override):
    conid, symbol = sg.INDEXES[underlying]
    start = datetime(session.year, session.month, session.day, 9, 15, tzinfo=IST).astimezone(timezone.utc)
    bars = [{"observed_at": (start + timedelta(minutes=i)).isoformat(), "open": "100", "high": "102", "low": "99", "close": "101", "volume": "0"}
            for i in range(n)]
    data = {"underlying": underlying, "session_date": session.isoformat(), "conid": conid, "symbol": symbol, "bar_count": n, "bars": bars}
    data.update(override)
    return data


def unavailable(code, retryable=True, message="nope"):
    error = sg.SpotUnavailable(f"X: {code}: {message}")
    error.retryable = retryable
    return error


class FakeTransport:
    """`script` maps underlying -> list of replies/exceptions consumed in order (or a callable(body))."""

    def __init__(self, script=None, default=None):
        self.script, self.default, self.calls, self.active, self.peak = script or {}, default, [], 0, 0
        self._guard = threading.Lock()

    def __call__(self, body):
        with self._guard:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            real_time.sleep(0.005)
            self.calls.append(body)
            queue = self.script.get(body["underlying"])
            item = (queue.pop(0) if queue else None) if not callable(queue) else queue(body)
            if item is None:
                item = self.default(body) if self.default else reply(body["underlying"], date.fromisoformat(body["session_date"]))
            if isinstance(item, Exception):
                raise item
            return item
        finally:
            with self._guard:
                self.active -= 1


class Clock:
    def __init__(self, now=NIGHT):
        self.now = now

    def __call__(self):
        return self.now


def make(transport=None, clock=None, **kw):
    sleeps, logs = [], []
    defaults = dict(transport=transport or FakeTransport(), run_id="run-test-0001", spacing=7.0, retry_wait=60.0, min_bars=300,
                    clock=clock or Clock(), sleep=sleeps.append, log=logs.append)
    defaults.update(kw)
    return sg.GatewaySpotClient(**defaults), sleeps, logs


# ---- validation: fail closed ----------------------------------------------------------------

def check(data, underlying="NIFTY", session=SESSION, min_bars=300):
    return sg.validate_bars(underlying, session, data, min_bars)


def test_good_reply_parses():
    bars = check(reply())
    assert len(bars) == 375 and bars[0]["observed_at"].astimezone(IST).strftime("%H:%M") == "09:15"


@pytest.mark.parametrize("override,what", [
    ({"conid": 56994300}, "different contract"), ({"symbol": "BANKNIFTY"}, "different contract"),
    ({"underlying": "BANKNIFTY"}, "different contract"), ({"session_date": "2026-10-30"}, "different session date"),
    ({"bars": []}, "no bars"),
])
def test_wrong_or_empty_replies_are_refused(override, what):
    with pytest.raises(sg.SpotDataInvalid, match=what):
        check(reply(**override))


def test_a_bar_on_another_date_is_refused():
    data = reply()
    data["bars"][10]["observed_at"] = datetime(2026, 11, 1, 9, 30, tzinfo=IST).isoformat()
    with pytest.raises(sg.SpotDataInvalid, match="outside session date"):
        check(data)


def test_too_few_bars_is_refused():
    with pytest.raises(sg.SpotDataInvalid, match="only 120 bars"):
        check(reply(n=120))


@pytest.mark.parametrize("patch", [{"open": "0"}, {"low": "105"}, {"close": "abc"}, {"high": "-1"}])
def test_bad_prices_are_refused(patch):
    data = reply()
    data["bars"][5].update(patch)
    with pytest.raises(sg.SpotDataInvalid):
        check(data)


def test_timestamp_without_zone_is_refused():
    data = reply()
    data["bars"][0]["observed_at"] = "2026-11-02T09:15:00"
    with pytest.raises(sg.SpotDataInvalid, match="no time zone"):
        check(data)


def test_frame_matches_what_the_pipelines_own_client_returns():
    frame = sg.to_frame("NIFTY", check(reply()))
    assert list(frame.columns) == ["observed_at", "underlying", "open", "high", "low", "close", "volume", "ibkr_con_id"]
    assert str(frame.observed_at.dt.tz) == "Asia/Kolkata" and frame.observed_at.iloc[0].strftime("%H:%M:%S") == "09:15:00"
    assert frame.observed_at.is_unique and set(frame.ibkr_con_id) == {51497778} and frame.open.dtype == float


# ---- guardrails ----------------------------------------------------------------------------------

def test_never_asks_during_strategy_hours():
    transport = FakeTransport()
    client, _, _ = make(transport=transport, clock=Clock(STRATEGY_HOURS))
    with pytest.raises(sg.OutsideWindow):
        client.historical_index("NIFTY", SESSION)
    with pytest.raises(sg.OutsideWindow):
        client.connect_ready("h", 1, 1, 1)
    assert transport.calls == []


@pytest.mark.parametrize("hour,minute,allowed", [(16, 29, False), (16, 30, True), (23, 59, True), (0, 5, True), (8, 29, True), (8, 30, False)])
def test_window_edges(hour, minute, allowed):
    assert sg.in_window(datetime(2026, 11, 3, hour, minute, tzinfo=IST)) is allowed


def test_window_is_checked_before_every_request_not_only_once():
    clock, transport = Clock(), FakeTransport()
    client, _, _ = make(transport=transport, clock=clock)
    client.historical_index("NIFTY", SESSION)
    clock.now = STRATEGY_HOURS   # the night overran into the morning
    with pytest.raises(sg.OutsideWindow):
        client.historical_index("BANKNIFTY", SESSION)
    assert len(transport.calls) == 1


def test_five_threads_are_served_strictly_one_at_a_time_and_spaced():
    transport = FakeTransport()
    client, sleeps, _ = make(transport=transport)
    threads = [threading.Thread(target=client.historical_index, args=(name, SESSION)) for name in sg.INDEXES]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert transport.peak == 1 and len(transport.calls) == 5
    assert len(sleeps) == 4 and all(6.0 < s <= 7.0 for s in sleeps)    # spacing between requests, none before the first
    assert {c["underlying"] for c in transport.calls} == set(sg.INDEXES)


def test_prefetch_goes_nifty_first_and_caches_so_the_pipeline_never_asks_twice():
    transport = FakeTransport()
    client, _, _ = make(transport=transport)
    client.prefetch(SESSION)
    assert [c["underlying"] for c in transport.calls] == list(sg.CANARY_FIRST) and client.requests_sent == 5
    for name in sg.INDEXES:
        assert len(client.historical_index(name, SESSION)) == 375
    assert len(transport.calls) == 5


def test_canary_failure_stops_everything_after_the_first_request():
    transport = FakeTransport({"NIFTY": [sg.SpotDataInvalid("NIFTY: WRONG_CONTRACT")]})
    client, _, _ = make(transport=transport)
    with pytest.raises(sg.SpotDataInvalid):
        client.prefetch(SESSION)
    assert len(transport.calls) == 1


def test_one_retry_after_60_seconds_then_success():
    transport = FakeTransport({"NIFTY": [unavailable("GATEWAY_TIMEOUT"), reply()]})
    client, sleeps, logs = make(transport=transport)
    assert len(client.historical_index("NIFTY", SESSION)) == 375
    assert 60.0 in sleeps and [c["attempt"] for c in transport.calls] == [1, 2]
    assert any("retrying once in 60s" in line for line in logs)


def test_second_failure_stops_and_nothing_more_is_sent():
    transport = FakeTransport({"NIFTY": [unavailable("GATEWAY_UNAVAILABLE"), unavailable("GATEWAY_UNAVAILABLE")]})
    client, _, _ = make(transport=transport)
    with pytest.raises(sg.SpotUnavailable):
        client.historical_index("NIFTY", SESSION)
    before = len(transport.calls)
    assert before == 2
    with pytest.raises(sg.SpotUnavailable):
        client.historical_index("BANKNIFTY", SESSION)
    assert len(transport.calls) == before   # halted: no request after the failure


@pytest.mark.parametrize("code", ["DAILY_LIMIT", "GATEWAY_MODE_MISMATCH", "GATEWAY_AMBIGUOUS", "INVALID_REQUEST"])
def test_non_retryable_errors_are_not_retried(code):
    transport = FakeTransport({"NIFTY": [unavailable(code, retryable=False), reply()]})
    client, sleeps, _ = make(transport=transport)
    with pytest.raises(sg.SpotUnavailable):
        client.historical_index("NIFTY", SESSION)
    assert len(transport.calls) == 1 and 60.0 not in sleeps


def test_outside_window_from_the_backend_is_never_retried():
    transport = FakeTransport({"NIFTY": [sg.OutsideWindow("morning")]})
    client, sleeps, _ = make(transport=transport)
    with pytest.raises(sg.OutsideWindow):
        client.historical_index("NIFTY", SESSION)
    assert len(transport.calls) == 1 and 60.0 not in sleeps


def test_request_names_only_index_date_run_and_attempt():
    transport = FakeTransport()
    client, _, _ = make(transport=transport)
    client.historical_index("FINNIFTY", SESSION)
    assert transport.calls[0] == {"underlying": "FINNIFTY", "session_date": "2026-11-02", "run_id": "run-test-0001", "attempt": 1}


def test_adapter_has_no_order_or_subscription_methods():
    client, _, _ = make()
    assert sg.audit_gateway_adapter(client) == 1
    client.placeOrder = lambda: None
    with pytest.raises(sg.SpotError):
        sg.audit_gateway_adapter(client)


def test_resolve_index_matches_the_pipelines_pinned_identities():
    client, _, _ = make()
    assert client.resolve_index("FINNIFTY")["con_id"] == 468436436 and client.resolve_index("NIFTY")["symbol"] == "NIFTY50"


# ---- the in-process transport maps the Backend's errors -------------------------------------------------

@pytest.mark.parametrize("code,expected,retryable", [
    ("OUTSIDE_WINDOW", sg.OutsideWindow, None), ("WRONG_CONTRACT", sg.SpotDataInvalid, None),
    ("GATEWAY_TIMEOUT", sg.SpotUnavailable, True), ("BUSY", sg.SpotUnavailable, True), ("DAILY_LIMIT", sg.SpotUnavailable, False),
    ("GATEWAY_UNAVAILABLE", sg.SpotUnavailable, True), ("GATEWAY_MODE_MISMATCH", sg.SpotUnavailable, False),
])
def test_backend_transport_maps_errors(monkeypatch, code, expected, retryable):
    from apps.market_data import nfo_spot

    def boom(body):
        raise nfo_spot.SpotError(code, "message", status=503)
    monkeypatch.setattr(nfo_spot, "fetch_spot_bars", boom)
    with pytest.raises(expected) as info:
        sg.backend_transport({"underlying": "NIFTY"})
    if retryable is not None:
        assert info.value.retryable is retryable


def test_backend_transport_returns_the_reply_unchanged(monkeypatch):
    from apps.market_data import nfo_spot

    monkeypatch.setattr(nfo_spot, "fetch_spot_bars", lambda body: {"ok": body["underlying"]})
    assert sg.backend_transport({"underlying": "NIFTY"}) == {"ok": "NIFTY"}
