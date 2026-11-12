"""Run the market-pipeline Flink operators in plain Python, without Flink.

The operators in ``streaming/flink/jobs`` are ordinary Python classes; only their
state, timers and broadcast state come from PyFlink. This module supplies a
small in-memory stand-in for those runtime pieces so the *real* operator code
(current, or any git revision) can be driven tick by tick on a laptop:

* keyed ``ValueState`` / ``MapState`` with state TTL (processing-time clock),
* broadcast ``MapState``,
* event-time timers driven by a bounded-out-of-orderness watermark, and
  processing-time timers driven by a simulated clock,
* main output and side outputs.

It is a verification aid, not a Flink emulator: no checkpoints, no
parallelism, no network buffers. State size is measured as the serialised
bytes the operator stores, which is what Flink's HashMap backend keeps on heap
and copies into every checkpoint.
"""

from __future__ import annotations

import contextlib
import heapq
import json
import subprocess
import sys
import types
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FLINK_ROOT = BACKEND_ROOT / "streaming" / "flink"
JOBS_DIR = FLINK_ROOT / "jobs"
BASELINE_REF = "13be80a"  # repository state before the Flink-state/outbox fix


# ---------------------------------------------------------------------------
# Fake PyFlink surface
# ---------------------------------------------------------------------------

class _Anything:
    """Absorbs any call/attribute chain (Types.STRING(), Duration..., etc.)."""

    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return _Anything()

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Anything()


class _Module(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Anything()


class TimeDomain(Enum):
    EVENT_TIME = 0
    PROCESSING_TIME = 1


class Time:
    def __init__(self, milliseconds):
        self.milliseconds = int(milliseconds)

    @staticmethod
    def seconds(value):
        return Time(int(value) * 1000)

    @staticmethod
    def milliseconds_of(value):
        return Time(int(value))


class _TtlConfig:
    def __init__(self, ttl_ms):
        self.ttl_ms = ttl_ms


class _TtlBuilder:
    def __init__(self, time):
        self.time = time

    def __getattr__(self, name):
        if name.startswith(("set_", "cleanup", "disable", "use_")):
            return lambda *args, **kwargs: self
        raise AttributeError(name)

    def build(self):
        return _TtlConfig(self.time.milliseconds)


class StateTtlConfig:
    UpdateType = _Anything()
    StateVisibility = _Anything()

    @staticmethod
    def new_builder(time):
        return _TtlBuilder(time)


class _StateDescriptor:
    kind = "value"

    def __init__(self, name, *type_info):
        self.name = name
        self.ttl_ms = None

    def enable_time_to_live(self, config):
        self.ttl_ms = config.ttl_ms


class ValueStateDescriptor(_StateDescriptor):
    kind = "value"


class MapStateDescriptor(_StateDescriptor):
    kind = "map"


class OutputTag:
    def __init__(self, tag_id, type_info=None):
        self.tag_id = tag_id


class _Function:
    def open(self, runtime_context):
        pass

    def close(self):
        pass


class KeyedBroadcastProcessFunction(_Function):
    pass


class KeyedProcessFunction(_Function):
    pass


class KeyedCoProcessFunction(_Function):
    pass


class TimestampAssigner:
    pass


_FAKE_MODULES = (
    "pyflink",
    "pyflink.common",
    "pyflink.common.watermark_strategy",
    "pyflink.datastream",
    "pyflink.datastream.functions",
    "pyflink.datastream.state",
    "pyflink.datastream.output_tag",
    "pyflink.datastream.connectors",
    "pyflink.datastream.connectors.kafka",
)


def _build_fake_pyflink():
    modules = {name: _Module(name) for name in _FAKE_MODULES}
    modules["pyflink.common"].Time = Time
    modules["pyflink.common.watermark_strategy"].TimestampAssigner = TimestampAssigner
    modules["pyflink.datastream"].TimeDomain = TimeDomain
    functions = modules["pyflink.datastream.functions"]
    functions.KeyedBroadcastProcessFunction = KeyedBroadcastProcessFunction
    functions.KeyedProcessFunction = KeyedProcessFunction
    functions.KeyedCoProcessFunction = KeyedCoProcessFunction
    state = modules["pyflink.datastream.state"]
    state.StateTtlConfig = StateTtlConfig
    state.ValueStateDescriptor = ValueStateDescriptor
    state.MapStateDescriptor = MapStateDescriptor
    modules["pyflink.datastream.output_tag"].OutputTag = OutputTag
    for name, module in modules.items():
        if "." in name:
            parent, child = name.rsplit(".", 1)
            setattr(modules[parent], child, module)
    return modules


@contextlib.contextmanager
def fake_pyflink():
    """Temporarily install the fake PyFlink and the ``jobs`` package path.

    Everything added to ``sys.modules``/``sys.path`` is removed again, so other
    tests that install their own PyFlink stand-ins are unaffected. Modules
    loaded inside keep working afterwards: they hold direct references.
    """
    saved = {name: module for name, module in sys.modules.items()
             if name == "pyflink" or name.startswith(("pyflink.", "jobs"))}
    for name in saved:
        del sys.modules[name]
    added_path = str(FLINK_ROOT) not in sys.path
    if added_path:
        sys.path.insert(0, str(FLINK_ROOT))
    sys.modules.update(_build_fake_pyflink())
    try:
        yield
    finally:
        for name in [name for name in sys.modules
                     if name == "pyflink" or name.startswith(("pyflink.", "jobs"))]:
            del sys.modules[name]
        sys.modules.update(saved)
        if added_path:
            sys.path.remove(str(FLINK_ROOT))


def _source(stage, ref):
    if ref is None:
        return (JOBS_DIR / f"{stage}.py").read_text(encoding="utf-8")
    return subprocess.check_output(
        ["git", "show", f"{ref}:./streaming/flink/jobs/{stage}.py"],
        cwd=BACKEND_ROOT, text=True, encoding="utf-8",
    )


def load_stage(stage, ref=None):
    """Load ``jobs/<stage>.py`` from the working tree (``ref=None``) or a git ref.

    Old revisions are executed against the *current* ``jobs.processing`` and
    ``jobs.identity``; the functions old stages import from them are unchanged.
    """
    source = _source(stage, ref)
    name = f"harness_{stage}_{(ref or 'current').replace('/', '_')}"
    with fake_pyflink():
        module = types.ModuleType(name)
        module.__file__ = f"<{name}>"
        exec(compile(source, module.__file__, "exec"), module.__dict__)
        import jobs.identity as identity  # noqa: E402  (inside the fake context)
        module._harness_identity = identity
    return module


# ---------------------------------------------------------------------------
# In-memory runtime
# ---------------------------------------------------------------------------

class _KeyedValueState:
    def __init__(self, harness, descriptor):
        self.h, self.d = harness, descriptor
        self.store = harness.keyed.setdefault(descriptor.name, {})

    def value(self):
        entry = self.store.get(self.h.current_key)
        if entry is None or self.h._expired(self.d, entry[1]):
            return None
        return entry[0]

    def update(self, value):
        self.store[self.h.current_key] = (value, self.h.processing_ms)

    def clear(self):
        self.store.pop(self.h.current_key, None)


class _MapState:
    def __init__(self, harness, descriptor, data_for):
        self.h, self.d, self._data_for = harness, descriptor, data_for

    def _live(self):
        data = self._data_for()
        return {key: value for key, value in data.items()
                if not self.h._expired(self.d, value[1])}

    def _entry(self, key):
        entry = self._data_for().get(key)
        if entry is None or self.h._expired(self.d, entry[1]):
            return None
        return entry

    def get(self, key):
        entry = self._entry(key)
        return None if entry is None else entry[0]

    def put(self, key, value):
        self._data_for()[key] = (value, self.h.processing_ms)

    def remove(self, key):
        self._data_for().pop(key, None)

    def contains(self, key):
        return self._entry(key) is not None

    def items(self):
        return [(key, value[0]) for key, value in self._live().items()]

    def keys(self):
        return list(self._live())

    def values(self):
        return [value[0] for value in self._live().values()]

    def is_empty(self):
        return not self._live()

    def clear(self):
        self._data_for().clear()


class _TimerService:
    def __init__(self, harness):
        self.h = harness

    def current_processing_time(self):
        return self.h.processing_ms

    def current_watermark(self):
        return self.h.watermark()

    def register_event_time_timer(self, timestamp):
        self.h._register(TimeDomain.EVENT_TIME, int(timestamp))

    def register_processing_time_timer(self, timestamp):
        self.h._register(TimeDomain.PROCESSING_TIME, int(timestamp))

    def delete_event_time_timer(self, timestamp):
        self.h.timer_set.discard((TimeDomain.EVENT_TIME, int(timestamp), self.h.current_key))

    def delete_processing_time_timer(self, timestamp):
        self.h.timer_set.discard((TimeDomain.PROCESSING_TIME, int(timestamp), self.h.current_key))


class _Context:
    def __init__(self, harness, domain=None):
        self.h, self.domain = harness, domain

    def get_broadcast_state(self, descriptor):
        return self.h.broadcast_state(descriptor)

    def timer_service(self):
        return _TimerService(self.h)

    def time_domain(self):
        return self.domain

    def get_current_key(self):
        return self.h.current_key

    def timestamp(self):
        return None


class _RuntimeContext:
    def __init__(self, harness):
        self.h = harness

    def get_state(self, descriptor):
        self.h.descriptors[descriptor.name] = descriptor
        return _KeyedValueState(self.h, descriptor)

    def get_map_state(self, descriptor):
        self.h.descriptors[descriptor.name] = descriptor
        store = self.h.keyed.setdefault(descriptor.name, {})
        return _MapState(self.h, descriptor, lambda: store.setdefault(self.h.current_key, {}))


class OperatorHarness:
    """Drive one keyed (broadcast) process function."""

    def __init__(self, function, key_selector, *, lateness_ms=30_000):
        self.fn = function
        self.key_selector = key_selector
        self.lateness_ms = lateness_ms
        self.keyed = {}
        self.descriptors = {}
        self.broadcast = {}
        self.current_key = None
        self.processing_ms = 0
        self.max_event_ms = None
        self.event_heap, self.processing_heap = [], []
        self.timer_set = set()
        self.main, self.side = [], {}
        function.open(_RuntimeContext(self))

    # clocks -----------------------------------------------------------------
    def watermark(self):
        if self.max_event_ms is None:
            return -(2 ** 63)
        return self.max_event_ms - self.lateness_ms - 1

    def _expired(self, descriptor, written_ms):
        return descriptor.ttl_ms is not None and self.processing_ms - written_ms >= descriptor.ttl_ms

    def _register(self, domain, timestamp):
        entry = (domain, timestamp, self.current_key)
        if entry in self.timer_set:
            return
        self.timer_set.add(entry)
        heap = self.event_heap if domain is TimeDomain.EVENT_TIME else self.processing_heap
        heapq.heappush(heap, (timestamp, str(self.current_key), self.current_key))

    def _fire(self, heap, domain, limit):
        while heap and heap[0][0] <= limit:
            timestamp, _, key = heapq.heappop(heap)
            if (domain, timestamp, key) not in self.timer_set:
                continue
            self.timer_set.discard((domain, timestamp, key))
            self.current_key = key
            self._collect(self.fn.on_timer(timestamp, _Context(self, domain)))

    def advance_processing_time(self, milliseconds):
        self.processing_ms = max(self.processing_ms, int(milliseconds))
        self._fire(self.processing_heap, TimeDomain.PROCESSING_TIME, self.processing_ms)

    # inputs -----------------------------------------------------------------
    def broadcast_state(self, descriptor):
        store = self.broadcast.setdefault(descriptor.name, {})
        return _MapState(self, _StateDescriptor(descriptor.name), lambda: store)

    def process_broadcast(self, value):
        self._collect(self.fn.process_broadcast_element(value, _Context(self)))

    def process_element(self, value, event_time_ms=None):
        self.current_key = self.key_selector(value)
        self._collect(self.fn.process_element(value, _Context(self)))
        if event_time_ms is not None:
            self.max_event_ms = event_time_ms if self.max_event_ms is None else max(self.max_event_ms, event_time_ms)
            self._fire(self.event_heap, TimeDomain.EVENT_TIME, self.watermark())

    def _collect(self, result):
        if result is None:
            return
        for item in result:
            if isinstance(item, tuple):
                self.side.setdefault(item[0].tag_id, []).append(item[1])
            else:
                self.main.append(item)

    def drain(self):
        items, self.main = self.main, []
        return items

    # measurement ------------------------------------------------------------
    def state_report(self):
        """Live serialised bytes and entry counts per keyed state (expired entries purged,
        as Flink's TTL cleanup eventually does)."""
        report = {}
        for name, per_key in self.keyed.items():
            descriptor = self.descriptors.get(name)
            total = entries = 0
            for key in list(per_key):
                value = per_key[key]
                if isinstance(value, dict):  # map state
                    for map_key in list(value):
                        stored, written = value[map_key]
                        if descriptor and self._expired(descriptor, written):
                            del value[map_key]
                            continue
                        total += len(str(map_key)) + len(str(stored))
                        entries += 1
                else:
                    stored, written = value
                    if descriptor and self._expired(descriptor, written):
                        del per_key[key]
                        continue
                    total += len(str(stored))
                    entries += 1
            report[name] = {"bytes": total, "entries": entries}
        return report

    def state_bytes(self):
        return sum(item["bytes"] for item in self.state_report().values())


# ---------------------------------------------------------------------------
# Synthetic market feed
# ---------------------------------------------------------------------------

INSTRUMENT_ID = "7"
TIMEFRAME = "1m"
SMA_WINDOW = 20
BAR_REQUIREMENT_HASH = "b" * 64
EPOCH_MS = int(datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)


def _iso(milliseconds):
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc).isoformat()


def indicator_requirement_hash(identity):
    return identity.requirement_identity_hash(
        input_type="INDICATOR", name="sma", role="", parameters={"window": SMA_WINDOW},
        instrument_id=INSTRUMENT_ID, timeframe=TIMEFRAME, implementation_version=1,
    )


def strategy_inputs_event(identity, occurred_ms, *, event_type="strategy.inputs.changed",
                          requirements=None, removed=()):
    if requirements is None:
        requirements = [
            {"identity_hash": BAR_REQUIREMENT_HASH, "input_type": "BAR", "name": "bar", "role": "",
             "parameters": {}, "implementation_version": 1, "warmup_bars": SMA_WINDOW},
            {"identity_hash": indicator_requirement_hash(identity), "input_type": "INDICATOR",
             "name": "sma", "role": "", "parameters": {"window": SMA_WINDOW},
             "implementation_version": 1, "warmup_bars": SMA_WINDOW},
        ]
    return json.dumps({
        "event_id": str(uuid.uuid4()), "event_type": event_type, "occurred_at": _iso(occurred_ms),
        "payload": {"instrument_id": int(INSTRUMENT_ID), "timeframe": TIMEFRAME,
                    "requirements": requirements, "removed_requirement_hashes": list(removed)},
    })


def tick(sequence, event_ms, mode="LIVE"):
    price = Decimal("100") + Decimal(sequence % 97) / Decimal(100)
    stamp = _iso(event_ms)
    payload = {
        "source_event_id": f"tick-{sequence}", "instrument_id": INSTRUMENT_ID, "conid": 1007,
        "symbol": "HARNESS", "event_time": stamp, "exchange": "SMART", "currency": "USD",
        "price": str(price), "volume": "10", "source": "ibkr", "event_kind": "TICK",
        "provider": "IBKR", "provider_generation": "g1", "subscription_key": "harness",
        "market_data_type": "LIVE", "received_at": stamp, "fallback_reason": "",
        "processing_mode": mode,
    }
    return json.dumps({"event_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"tick-{sequence}")),
                       "event_type": "market.canonical", "correlation_id": None, "payload": payload})


def bar_key(value):
    return str(json.loads(value)["payload"]["instrument_id"])


def indicator_key(value):
    payload = json.loads(value)["payload"]
    return f"{payload['instrument_id']}:{payload['interval']}"


def seed_event(bars, instrument_id=INSTRUMENT_ID, interval=TIMEFRAME):
    """What apps.market_streams.rehydration publishes: MarketBar rows as stored by
    PostgreSQL (8 decimal places), oldest first."""
    rows = []
    for bar in bars:
        row = {key: bar[key] for key in ("bar_id", "window_start", "window_end", "version")}
        row.update({key: f"{Decimal(str(bar[key])):.8f}" for key in ("open", "high", "low", "close", "volume")})
        row.update({"instrument_id": str(instrument_id), "interval": interval, "is_final": True})
        rows.append(row)
    return json.dumps({"event_id": str(uuid.uuid4()), "event_type": "market.bar.seed",
                       "payload": {"instrument_id": str(instrument_id), "interval": interval, "bars": rows}})


class Pipeline:
    """Bar aggregation -> indicator computation, fed by a synthetic tick stream."""

    def __init__(self, bar_module, indicator_module, *, bar_kwargs=None, indicator_kwargs=None,
                 register=True):
        self.identity = bar_module._harness_identity
        self.bars = OperatorHarness(bar_module.RegistryBars(**(bar_kwargs or {})), bar_key)
        self.indicators = OperatorHarness(
            indicator_module.RegistryIndicators(**(indicator_kwargs or {})), indicator_key)
        self.bar_events, self.indicator_events = [], []
        self.sequence = 0
        if register:
            self.register(strategy_inputs_event(self.identity, EPOCH_MS - 60_000))

    def register(self, event):
        self.bars.process_broadcast(event)
        self.indicators.process_broadcast(event)

    def feed(self, event_ms, mode="LIVE"):
        self.sequence += 1
        self.bars.advance_processing_time(event_ms)
        self.indicators.advance_processing_time(event_ms)
        self.bars.process_element(tick(self.sequence, event_ms, mode), event_ms)
        for value in self.bars.drain():
            self.bar_events.append(value)
            self.indicators.process_element(value)
            self.indicator_events.extend(self.indicators.drain())

    def run(self, start_ms, seconds, tick_seconds=5, on_sample=None, sample_every_s=1800):
        for offset in range(0, int(seconds), tick_seconds):
            self.feed(start_ms + offset * 1000)
            if on_sample and offset and offset % sample_every_s == 0:
                on_sample(offset, self)

    def bar_payloads(self):
        return [json.loads(value)["payload"] for value in self.bar_events]

    def indicator_payloads(self):
        return [json.loads(value)["payload"] for value in self.indicator_events]


def comparable(events):
    """Envelopes without the wall-clock ``produced_at``."""
    result = []
    for value in events:
        envelope = json.loads(value)
        envelope.pop("produced_at", None)
        result.append(envelope)
    return result
