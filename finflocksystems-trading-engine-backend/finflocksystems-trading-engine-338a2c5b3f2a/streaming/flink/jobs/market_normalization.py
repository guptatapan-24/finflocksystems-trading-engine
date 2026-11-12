import hashlib
import json
import os

from pyflink.common import Time, Types
from pyflink.datastream.functions import KeyedCoProcessFunction, KeyedProcessFunction
from pyflink.datastream.output_tag import OutputTag
from pyflink.datastream.state import StateTtlConfig, ValueStateDescriptor

from jobs.events import envelope, payload_of
from jobs.identity import canonical_event_key, raw_event_key
from jobs.processing import (
    buffer_unknown_registry_event,
    normalize_market_event,
    partition_expired_registry_events,
)
from jobs.runtime import environment, sink, source


DLQ = OutputTag("normalization-dead-letter-v2", Types.STRING())


class Validate(KeyedCoProcessFunction):
    """Join raw events to registry data without dropping startup-order races."""

    def open(self, runtime_context):
        self.registry = runtime_context.get_state(
            ValueStateDescriptor("instrument-registry-by-conid-v2", Types.STRING())
        )
        self.pending = runtime_context.get_state(
            ValueStateDescriptor("unknown-conid-pending-v1", Types.STRING())
        )
        self.expiry_timer = runtime_context.get_state(
            ValueStateDescriptor("unknown-conid-expiry-timer-v1", Types.LONG())
        )
        self.timeout_ms = max(
            1,
            int(os.getenv("UNKNOWN_CONID_BUFFER_TIMEOUT_MS", "30000")),
        )
        self.maximum_events = max(
            1,
            int(os.getenv("UNKNOWN_CONID_BUFFER_MAX_EVENTS", "1000")),
        )
        self.mode_override = str(os.getenv("FLINK_PROCESSING_MODE", "")).upper() or None

    def _dead_letter(self, incoming, reason_code, reason):
        raw = payload_of(incoming)
        try:
            identity = raw_event_key(raw)
        except Exception:
            identity = hashlib.sha256(
                json.dumps(incoming, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        payload = {
            "source_topic": "market.raw.v1",
            "reason_code": reason_code,
            "reason": str(reason),
            "raw": incoming,
        }
        return json.dumps(
            envelope(
                "dead-letter",
                "stream",
                "market.raw.v1",
                payload,
                f"{identity}:{reason_code}",
                incoming,
                raw.get("event_time"),
            ),
            separators=(",", ":"),
        )

    def _canonical(self, incoming, registered):
        raw = payload_of(incoming)
        canonical = normalize_market_event(
            {**raw, "instrument_id": registered["instrument_id"]},
            {},
            mode_override=self.mode_override,
        )
        stable_key = canonical_event_key(raw_event_key(raw), canonical["instrument_id"])
        return json.dumps(
            envelope(
                "market.canonical",
                "instrument",
                canonical["instrument_id"],
                canonical,
                stable_key,
                incoming,
                canonical["event_time"],
            ),
            separators=(",", ":"),
        )

    def _schedule_next_expiry(self, pending, ctx):
        previous = self.expiry_timer.value()
        if previous:
            ctx.timer_service().delete_processing_time_timer(previous)
        if pending:
            next_expiry = min(item["expires_at_ms"] for item in pending)
            self.expiry_timer.update(next_expiry)
            ctx.timer_service().register_processing_time_timer(next_expiry)
        else:
            self.expiry_timer.clear()

    def process_element1(self, value, ctx):
        try:
            incoming = json.loads(value)
            raw = payload_of(incoming)
            if raw.get("conid") is None:
                raise ValueError("raw event has no conId")
            encoded = self.registry.value()
            if encoded:
                yield self._canonical(incoming, json.loads(encoded))
                return
            pending = json.loads(self.pending.value() or "[]")
            expires_at = ctx.timer_service().current_processing_time() + self.timeout_ms
            pending, overflow = buffer_unknown_registry_event(
                pending,
                incoming,
                expires_at,
                self.maximum_events,
            )
            self.pending.update(json.dumps(pending, separators=(",", ":")))
            self._schedule_next_expiry(pending, ctx)
            for item in overflow:
                yield DLQ, self._dead_letter(
                    item["event"],
                    "UNKNOWN_CONID_BUFFER_CAPACITY",
                    "unknown conId buffer reached its configured capacity",
                )
        except Exception as exc:
            try:
                incoming
            except UnboundLocalError:
                incoming = {"raw": value}
            yield DLQ, self._dead_letter(incoming, "INVALID_RAW_EVENT", exc)

    def process_element2(self, value, ctx):
        try:
            event = json.loads(value)
            registered = event.get("payload", {})
            if registered.get("active", True):
                self.registry.update(json.dumps(registered, separators=(",", ":")))
                pending = json.loads(self.pending.value() or "[]")
                for item in pending:
                    try:
                        yield self._canonical(item["event"], registered)
                    except Exception as exc:
                        yield DLQ, self._dead_letter(
                            item["event"],
                            "INVALID_BUFFERED_RAW_EVENT",
                            exc,
                        )
                self.pending.clear()
                self._schedule_next_expiry([], ctx)
            else:
                self.registry.clear()
        except Exception as exc:
            yield DLQ, self._dead_letter(
                {"payload": {"source_event_id": f"registry:{ctx.get_current_key()}"}},
                "INVALID_REGISTRY_EVENT",
                exc,
            )

    def on_timer(self, timestamp, ctx):
        pending = json.loads(self.pending.value() or "[]")
        remaining, expired = partition_expired_registry_events(pending, timestamp)
        if remaining:
            self.pending.update(json.dumps(remaining, separators=(",", ":")))
        else:
            self.pending.clear()
        self._schedule_next_expiry(remaining, ctx)
        for item in expired:
            raw = payload_of(item["event"])
            yield DLQ, self._dead_letter(
                item["event"],
                "UNKNOWN_CONID_TIMEOUT",
                f"unknown conId {raw.get('conid')} after bounded registry wait",
            )


class Deduplicate(KeyedProcessFunction):
    def open(self, runtime_context):
        descriptor = ValueStateDescriptor("seen-canonical-event-v2", Types.BOOLEAN())
        descriptor.enable_time_to_live(
            StateTtlConfig.new_builder(
                Time.seconds(int(os.getenv("DEDUPLICATION_STATE_TTL_SECONDS", "86400")))
            ).build()
        )
        self.seen = runtime_context.get_state(descriptor)

    def process_element(self, value, ctx):
        if not self.seen.value():
            self.seen.update(True)
            yield value


JOB_NAME = "market-normalization-v2"


def build(env):
    """Attach this stage to ``env``. No environment(), no execute() -- so the
    stage can share one job graph with the rest of the market pipeline."""
    raw = source(env, "market.raw.v1", "market-normalization-v2").key_by(
        lambda value: str(payload_of(json.loads(value)).get("conid"))
    )
    registry = source(
        env,
        "instrument.registry.v1",
        "instrument-registry-v2",
        starting_offsets="earliest",
    ).key_by(lambda value: str(payload_of(json.loads(value)).get("conid")))
    checked = raw.connect(registry).process(
        Validate(),
        output_type=Types.STRING(),
    ).uid("market-normalization-validate-v3")
    canonical = checked.key_by(
        lambda value: json.loads(value)["event_id"]
    ).process(
        Deduplicate(),
        output_type=Types.STRING(),
    ).uid("market-normalization-deduplicate-v2")
    sink(canonical, "market.canonical.v1", "market-normalization-canonical-sink-v2")
    sink(
        checked.get_side_output(DLQ),
        "dead-letter.v1",
        "market-normalization-dlq-sink-v2",
    )


def main():
    env = environment(JOB_NAME)
    build(env)
    env.execute(JOB_NAME)


if __name__ == "__main__":
    main()
