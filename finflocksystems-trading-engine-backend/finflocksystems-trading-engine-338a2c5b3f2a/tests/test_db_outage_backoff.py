import json
import signal
import sys
import uuid
from functools import partial
from types import SimpleNamespace

import pytest
from confluent_kafka import KafkaError
from django.core.management import call_command
from django.db.utils import IntegrityError, InterfaceError, OperationalError
from django.test import override_settings

from apps.core import db_outage
from apps.core.db_outage import DbOutageBackoff
from apps.event_bus.models import DeadLetterEvent, StreamHealthMetric


class FakeClock:
    """Time only moves when something sleeps, so tests never really wait."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture(autouse=True)
def restore_signal_handlers():
    # The commands install SIGTERM/SIGINT handlers; do not leak them into pytest.
    previous = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    yield
    for number, handler in previous.items():
        signal.signal(number, handler)


def backoff(clock, **kwargs):
    return DbOutageBackoff(
        "test", clock=clock, sleep=clock.sleep, jitter=lambda: 0.5, **kwargs
    )


def flaky(failures, error=OperationalError, result="ok"):
    state = {"calls": 0}

    def operation():
        state["calls"] += 1
        if state["calls"] <= failures:
            raise error("the connection is closed")
        return result

    return operation, state


# ---------------------------------------------------------------- helper unit


def test_healthy_operation_runs_once_and_never_waits(clock):
    operation, state = flaky(0)
    assert backoff(clock).run(operation) == "ok"
    assert state["calls"] == 1
    assert clock.sleeps == []


def test_connectivity_errors_back_off_exponentially_up_to_the_cap(clock):
    operation, state = flaky(7)
    assert backoff(clock, give_up_after_seconds=1000).run(operation) == "ok"
    assert state["calls"] == 8
    # Delays 2, 4, 8, 16, then capped at 30 for the rest: 2+4+8+16+30*3 = 120.
    assert clock.now - 1000.0 == pytest.approx(120.0)


def test_interface_error_is_also_treated_as_connectivity(clock):
    operation, state = flaky(2, error=InterfaceError)
    assert backoff(clock).run(operation) == "ok"
    assert state["calls"] == 3


def test_gives_up_after_the_window_and_raises_the_original_error(clock):
    error = OperationalError("server closed the connection")

    def operation():
        raise error

    with pytest.raises(OperationalError) as caught:
        backoff(clock, give_up_after_seconds=10).run(operation)
    assert caught.value is error
    # Never waits past the window.
    assert clock.now - 1000.0 == pytest.approx(10.0)


def test_other_exceptions_are_never_retried(clock):
    for error in (ValueError, RuntimeError, IntegrityError):
        operation, state = flaky(5, error=error)
        with pytest.raises(error):
            backoff(clock).run(operation)
        assert state["calls"] == 1
    assert clock.sleeps == []


def test_stop_request_ends_the_wait_and_raises(clock):
    operation, state = flaky(10)
    guard = backoff(clock, stop=lambda: len(clock.sleeps) >= 2)
    with pytest.raises(OperationalError):
        guard.run(operation)
    assert state["calls"] == 1
    assert len(clock.sleeps) == 2


def test_stop_already_requested_raises_without_waiting(clock):
    operation, state = flaky(10)
    with pytest.raises(OperationalError):
        backoff(clock, stop=lambda: True).run(operation)
    assert state["calls"] == 1
    assert clock.sleeps == []


def test_connections_are_closed_after_every_failure(clock, monkeypatch):
    closed = []
    monkeypatch.setattr(
        db_outage, "connections", SimpleNamespace(close_all=lambda: closed.append(1))
    )
    operation, _ = flaky(3)
    backoff(clock).run(operation)
    assert len(closed) == 3


def test_a_long_healthy_run_starts_a_new_outage_window(clock):
    state = {"calls": 0}

    def operation():
        state["calls"] += 1
        if state["calls"] == 1:
            raise OperationalError("blip")
        if state["calls"] == 2:
            clock.now += 3600  # ran healthily for an hour, then lost the database
            raise OperationalError("blip again")
        return "ok"

    # 3600 s elapsed overall, far beyond the 10 s window, but the second failure
    # is a fresh outage so it must still be retried.
    assert backoff(clock, give_up_after_seconds=10).run(operation) == "ok"
    assert state["calls"] == 3


def test_sleeps_are_short_slices_so_a_stop_is_noticed_quickly(clock):
    operation, _ = flaky(1)
    backoff(clock).run(operation)
    assert clock.sleeps and max(clock.sleeps) <= 0.5


# ------------------------------------------------------- market consumer


class StopLoop(BaseException):
    """Ends the consumer's otherwise endless loop from inside the fake poll()."""


class Message:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def value(self):
        return self.payload

    def error(self):
        return None

    def topic(self):
        return "market.bars.v1"

    def partition(self):
        return 0

    def offset(self):
        return 7


class OneMessageThenStop:
    def __init__(self, payload):
        self.message = Message(payload)
        self.commits = []
        self.closed = False

    def subscribe(self, topics):
        self.topics = topics

    def poll(self, timeout):
        message, self.message = self.message, None
        if message is None:
            raise StopLoop()
        return message

    def commit(self, **kwargs):
        self.commits.append(kwargs)

    def close(self):
        self.closed = True


CONSUMER_MODULE = "apps.market_streams.management.commands.consume_market_streams"


@pytest.fixture
def consumer_env(monkeypatch, clock):
    payload = {"event_id": str(uuid.uuid4())}
    fake = OneMessageThenStop(payload)
    monkeypatch.setitem(
        sys.modules,
        "confluent_kafka",
        SimpleNamespace(Consumer=lambda _configuration: fake, KafkaError=KafkaError),
    )
    monkeypatch.setattr(
        f"{CONSUMER_MODULE}.DbOutageBackoff",
        partial(DbOutageBackoff, clock=clock, sleep=clock.sleep, jitter=lambda: 0.5),
    )
    return SimpleNamespace(consumer=fake, payload=payload, monkeypatch=monkeypatch)


def run_consumer():
    with pytest.raises(StopLoop):
        call_command("consume_market_streams")


@pytest.mark.django_db
@override_settings(KAFKA_ENABLED=True)
def test_consumer_healthy_path_is_unchanged(consumer_env, clock):
    seen = []
    consumer_env.monkeypatch.setattr(
        f"{CONSUMER_MODULE}.consume_market_event",
        lambda name, envelope: seen.append((name, envelope)) or {"ok": True},
    )
    run_consumer()
    assert seen == [("market-persistence-v2", consumer_env.payload)]
    assert [c["message"].offset() for c in consumer_env.consumer.commits] == [7]
    assert consumer_env.consumer.closed
    assert clock.sleeps == []
    assert DeadLetterEvent.objects.count() == 0


@pytest.mark.django_db
@override_settings(KAFKA_ENABLED=True)
def test_consumer_retries_the_same_message_in_place_during_a_database_outage(
    consumer_env, clock
):
    seen = []

    def consume(name, envelope):
        seen.append(envelope)
        if len(seen) <= 2:
            raise OperationalError("terminating connection due to administrator command")
        return {"ok": True}

    consumer_env.monkeypatch.setattr(f"{CONSUMER_MODULE}.consume_market_event", consume)
    run_consumer()
    # Same message, three attempts, then exactly one commit of that offset: nothing
    # skipped, nothing committed early.
    assert seen == [consumer_env.payload] * 3
    assert [c["message"].offset() for c in consumer_env.consumer.commits] == [7]
    assert clock.sleeps
    assert DeadLetterEvent.objects.count() == 0


@pytest.mark.django_db
@override_settings(KAFKA_ENABLED=True)
def test_consumer_survives_a_failing_heartbeat_write(consumer_env, clock):
    real = StreamHealthMetric.objects.update_or_create
    calls = {"n": 0}

    def flaky_update_or_create(**kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise OperationalError("the connection is closed")
        return real(**kwargs)

    consumer_env.monkeypatch.setattr(
        f"{CONSUMER_MODULE}.StreamHealthMetric",
        SimpleNamespace(objects=SimpleNamespace(update_or_create=flaky_update_or_create)),
    )
    consumer_env.monkeypatch.setattr(
        f"{CONSUMER_MODULE}.consume_market_event", lambda *_a, **_k: {"ok": True}
    )
    run_consumer()
    assert calls["n"] >= 3
    assert [c["message"].offset() for c in consumer_env.consumer.commits] == [7]


@pytest.mark.django_db
@override_settings(KAFKA_ENABLED=True)
def test_consumer_gives_up_after_the_window_and_does_not_commit(consumer_env, clock):
    consumer_env.monkeypatch.setattr(
        f"{CONSUMER_MODULE}.consume_market_event",
        lambda *_a, **_k: (_ for _ in ()).throw(OperationalError("still down")),
    )
    with pytest.raises(OperationalError, match="still down"):
        call_command("consume_market_streams")
    assert consumer_env.consumer.commits == []
    assert consumer_env.consumer.closed
    assert DeadLetterEvent.objects.count() == 0
    # Gave up inside the 120 s window, well under Kafka's 300 s poll limit.
    assert 100 <= clock.now - 1000.0 <= 125


@pytest.mark.django_db
@override_settings(KAFKA_ENABLED=True)
def test_consumer_does_not_retry_non_database_errors(consumer_env, clock):
    calls = []

    def consume(*_args):
        calls.append(1)
        raise RuntimeError("bug in handler")

    consumer_env.monkeypatch.setattr(f"{CONSUMER_MODULE}.consume_market_event", consume)
    with pytest.raises(RuntimeError, match="bug in handler"):
        call_command("consume_market_streams")
    assert calls == [1]
    assert clock.sleeps == []
    assert consumer_env.consumer.commits == []


@pytest.mark.django_db
@override_settings(KAFKA_ENABLED=True)
def test_consumer_dead_letter_write_waits_out_an_outage_then_commits(consumer_env, clock):
    consumer_env.monkeypatch.setattr(
        f"{CONSUMER_MODULE}.consume_market_event",
        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("invalid schema")),
    )
    from apps.event_bus.services import route_dead_letter as real_route

    attempts = {"n": 0}

    def flaky_route(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OperationalError("the connection is closed")
        return real_route(*args, **kwargs)

    consumer_env.monkeypatch.setattr(f"{CONSUMER_MODULE}.route_dead_letter", flaky_route)
    run_consumer()
    row = DeadLetterEvent.objects.get()
    assert row.reason == "invalid schema"
    assert attempts["n"] == 2
    assert [c["message"].offset() for c in consumer_env.consumer.commits] == [7]


# -------------------------------------------------------- finnhub consumer

FINNHUB_MODULE = "apps.market_data.management.commands.consume_finnhub_market_data"


@pytest.fixture
def finnhub_env(monkeypatch, clock):
    monkeypatch.setattr(
        f"{FINNHUB_MODULE}.DbOutageBackoff",
        partial(DbOutageBackoff, clock=clock, sleep=clock.sleep, jitter=lambda: 0.5),
    )
    instances = []

    def install(behaviours):
        remaining = list(behaviours)

        class FakeWorker:
            def __init__(self, stop_event=None):
                self.stop_event = stop_event
                instances.append(self)

            def run_forever(self):
                behaviour = remaining.pop(0) if remaining else None
                if behaviour is not None:
                    raise behaviour

        monkeypatch.setattr(f"{FINNHUB_MODULE}.FinnhubRealtimeWorker", FakeWorker)

    return SimpleNamespace(install=install, instances=instances)


def test_finnhub_healthy_path_runs_one_worker_and_never_waits(finnhub_env, clock):
    finnhub_env.install([None])
    call_command("consume_finnhub_market_data")
    assert len(finnhub_env.instances) == 1
    assert clock.sleeps == []


def test_finnhub_restarts_a_fresh_worker_after_a_database_outage(finnhub_env, clock):
    finnhub_env.install([OperationalError("down"), OperationalError("down"), None])
    call_command("consume_finnhub_market_data")
    # A new worker per attempt, as a restarted process would have.
    assert len(finnhub_env.instances) == 3
    assert len({id(w) for w in finnhub_env.instances}) == 3
    assert clock.sleeps


def test_finnhub_non_database_errors_still_exit_immediately(finnhub_env, clock):
    finnhub_env.install([RuntimeError("bug")])
    with pytest.raises(RuntimeError, match="bug"):
        call_command("consume_finnhub_market_data")
    assert len(finnhub_env.instances) == 1
    assert clock.sleeps == []


def test_finnhub_gives_up_after_the_window(finnhub_env, clock):
    finnhub_env.install([OperationalError("down")] * 100)
    with pytest.raises(OperationalError):
        call_command("consume_finnhub_market_data")
    assert 100 <= clock.now - 1000.0 <= 125
