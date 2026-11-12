"""Stage 2: aggregate canonical ticks into final, versioned bars.

State layout (bounded; see docs/flink-state-outbox.md):

* ``open-bar-buckets-v1`` -- MapState, one entry per open window bucket with its
  raw ticks. A bucket is evicted ``BAR_BUCKET_RETENTION_SECONDS`` (processing
  time) after its window last fired, so only open windows plus a short grace
  period are held. Before this change every bucket ever created lived in one
  JSON ValueState per instrument that was never evicted and was re-serialised
  on every tick.
* ``bar-version-ledger-v1`` -- MapState of ``{version, fingerprint}`` per bucket
  key, with a state TTL. It keeps bar versioning deterministic for re-sent
  pass-through bars and lets a tick that arrives after its bucket was evicted
  be dead-lettered instead of producing a partial "version 1" bar.
* ``bar-fire-timers-v1`` / ``bar-cleanup-timers-v1`` -- timer indexes so a timer
  touches only its own buckets instead of scanning all state.
"""

import json
import os
from datetime import datetime

from pyflink.common import Duration, Time, Types, WatermarkStrategy
from pyflink.common.watermark_strategy import TimestampAssigner
from pyflink.datastream import TimeDomain
from pyflink.datastream.functions import KeyedBroadcastProcessFunction
from pyflink.datastream.output_tag import OutputTag
from pyflink.datastream.state import MapStateDescriptor, StateTtlConfig

from jobs.events import envelope
from jobs.identity import (
    bar_event_key,
    canonical_event_key,
    deterministic_event_id,
    market_bar_id,
    processing_mode,
    raw_event_key,
)
from jobs.processing import (
    advance_bar_version,
    aggregate_bars,
    apply_registry_event,
    is_registry_meta_key,
)
from jobs.runtime import environment, sink, source

REQUIREMENTS = MapStateDescriptor("active-bar-inputs-v2", Types.STRING(), Types.STRING())
LATE_TICKS = OutputTag("bar-aggregation-late-tick-v1", Types.STRING())


def timeframe_seconds(value):
    value=str(value).strip().lower();units={"s":1,"m":60,"h":3600,"d":86400}
    if len(value)<2 or value[-1] not in units or not value[:-1].isdigit():raise ValueError(f"Unsupported timeframe {value}")
    seconds=int(value[:-1])*units[value[-1]]
    if seconds<=0:raise ValueError("Timeframe must be positive")
    return seconds


def _ttl(seconds):
    return (
        StateTtlConfig.new_builder(Time.seconds(int(seconds)))
        .set_update_type(StateTtlConfig.UpdateType.OnCreateAndWrite)
        .set_state_visibility(StateTtlConfig.StateVisibility.NeverReturnExpired)
        .build()
    )


def _dumps(value):
    return json.dumps(value, separators=(",", ":"))


class EnvelopeTimestamp(TimestampAssigner):
    def extract_timestamp(self,value,record_timestamp):
        payload=json.loads(value)["payload"]
        return int(datetime.fromisoformat(payload["event_time"].replace("Z","+00:00")).timestamp()*1000)


class RegistryBars(KeyedBroadcastProcessFunction):
    def __init__(self, bucket_retention_seconds=900, ledger_ttl_seconds=604800):
        self.bucket_retention_ms = max(0, int(bucket_retention_seconds)) * 1000
        self.ledger_ttl_seconds = max(1, int(ledger_ttl_seconds))

    def open(self,runtime_context):
        self.buckets = runtime_context.get_map_state(
            MapStateDescriptor("open-bar-buckets-v1", Types.STRING(), Types.STRING()))
        ledger = MapStateDescriptor("bar-version-ledger-v1", Types.STRING(), Types.STRING())
        ledger.enable_time_to_live(_ttl(self.ledger_ttl_seconds))
        self.ledger = runtime_context.get_map_state(ledger)
        self.fire_index = runtime_context.get_map_state(
            MapStateDescriptor("bar-fire-timers-v1", Types.STRING(), Types.STRING()))
        self.cleanup_index = runtime_context.get_map_state(
            MapStateDescriptor("bar-cleanup-timers-v1", Types.STRING(), Types.STRING()))

    # -- small state helpers --------------------------------------------------
    @staticmethod
    def _get_json(state, key, default=None):
        return json.loads(state.get(key)) if state.contains(key) else default

    @staticmethod
    def _index_add(index, timestamp, key):
        slot = str(timestamp)
        keys = json.loads(index.get(slot)) if index.contains(slot) else []
        if key not in keys:
            keys.append(key)
            index.put(slot, _dumps(keys))

    @staticmethod
    def _index_pop(index, timestamp):
        slot = str(timestamp)
        if not index.contains(slot):
            return []
        keys = json.loads(index.get(slot))
        index.remove(slot)
        return keys

    def _late_tick(self, source_event, tick, bucket_key):
        payload = {
            "source_topic": "market.canonical.v1",
            "reason_code": "LATE_TICK_AFTER_BAR_EVICTION",
            "reason": "tick arrived after its bar window was finalised and evicted from Flink state",
            "bucket_key": bucket_key,
            "raw": source_event,
        }
        return _dumps(envelope(
            "dead-letter", "stream", "market.canonical.v1", payload,
            f"{source_event.get('event_id')}:{bucket_key}:LATE_TICK_AFTER_BAR_EVICTION",
            source_event, tick.get("event_time"),
        ))

    # -- registry -------------------------------------------------------------
    def process_broadcast_element(self,value,ctx):
        apply_registry_event(ctx.get_broadcast_state(REQUIREMENTS), json.loads(value), "BAR")

    # -- ticks ----------------------------------------------------------------
    def process_element(self,value,ctx):
        source_event=json.loads(value);tick={**source_event["payload"],
            "_envelope_event_id":source_event["event_id"],
            "_correlation_id":source_event.get("correlation_id")}
        stamp=int(datetime.fromisoformat(tick["event_time"].replace("Z","+00:00")).timestamp())
        for identity_hash,encoded in ctx.get_broadcast_state(REQUIREMENTS).items():
            if is_registry_meta_key(identity_hash):continue
            requirement=json.loads(encoded)
            if requirement["instrument_id"]!=str(tick["instrument_id"]):continue
            if tick.get("event_kind")=="BAR" and tick.get("timeframe")==requirement["timeframe"] and tick.get("is_final",True):
                bar_id=market_bar_id(tick["instrument_id"],requirement["timeframe"],tick["window_start"])
                bar={"bar_id":bar_id,"instrument_id":str(tick["instrument_id"]),"interval":requirement["timeframe"],
                    "window_start":tick["window_start"],"window_end":tick["window_end"],"open":tick["open"],"high":tick["high"],
                    "low":tick["low"],"close":tick["close"],"volume":tick.get("volume","0"),"source_event_count":1,
                    "provider":tick.get("provider",""),"provider_generation":tick.get("provider_generation") or "",
                    "source":tick.get("source",""),
                    "market_data_type":tick.get("market_data_type","LIVE"),
                    "received_at":tick.get("received_at"),
                    "version":1,"is_final":True,"processing_mode":processing_mode(tick.get("processing_mode"))}
                key=f"direct:{identity_hash}:{bar['window_start']}:{bar['processing_mode']}"
                entry=self._get_json(self.ledger,key,{"version":0,"fingerprint":""})
                entry,version=advance_bar_version(entry,bar)
                if version is None:
                    continue
                self.ledger.put(key,_dumps({"version":entry["version"],"fingerprint":entry["fingerprint"]}))
                bar["version"]=version
                yield _dumps(envelope("market.bar","instrument",bar["instrument_id"],bar,
                    bar_event_key(bar_id,bar["version"]),source_event,bar["window_end"]))
                continue
            seconds=timeframe_seconds(requirement["timeframe"]);start=stamp-stamp%seconds;end=start+seconds
            mode=processing_mode(tick.get("processing_mode"))
            key=f"{identity_hash}:{start}:{mode}"
            bucket=self._get_json(self.buckets,key)
            if bucket is None:
                if self.ledger.contains(key):
                    # The window already fired and its ticks were evicted. Re-aggregating
                    # from this tick alone would publish a wrong bar, so dead-letter it.
                    yield LATE_TICKS, self._late_tick(source_event,tick,key)
                    continue
                bucket={"ticks":[],"version":0,"fingerprint":"","timeframe":requirement["timeframe"],
                    "seconds":seconds,"end":end}
            bucket["ticks"]=[x for x in bucket["ticks"] if x["source_event_id"]!=tick["source_event_id"]]+[tick]
            self.buckets.put(key,_dumps(bucket))
            fire_at=end*1000
            ctx.timer_service().register_event_time_timer(fire_at)
            self._index_add(self.fire_index,fire_at,key)

    # -- timers ---------------------------------------------------------------
    def on_timer(self,timestamp,ctx):
        if ctx.time_domain()==TimeDomain.PROCESSING_TIME:
            self._evict(timestamp)
            return
        for key in self._index_pop(self.fire_index,timestamp):
            bucket=self._get_json(self.buckets,key)
            if bucket is None or bucket.get("end") is None or bucket["end"]*1000!=timestamp:
                continue
            bars=aggregate_bars(bucket["ticks"],bucket["timeframe"],bucket["seconds"],final=True)
            if bars:
                bar=bars[0];bucket,version=advance_bar_version(bucket,bar)
                if version is not None:
                    bar["version"]=version;bar["is_final"]=True
                    causal_tick=max(bucket["ticks"],key=lambda item:(item["event_time"],item["source_event_id"]))
                    causal_event_id=causal_tick.get("_envelope_event_id") or deterministic_event_id(
                        "market.canonical",
                        canonical_event_key(raw_event_key(causal_tick),causal_tick["instrument_id"]),
                    )
                    source_event={"event_id":causal_event_id,
                        "correlation_id":causal_tick.get("_correlation_id"),"payload":causal_tick}
                    yield _dumps(envelope("market.bar","instrument",bar["instrument_id"],bar,
                        bar_event_key(bar["bar_id"],bar["version"]),source_event,bar["window_end"]))
            # Keep the fired bucket for a short grace period so a late tick can still
            # produce the next version, then evict it (see _evict).
            cleanup_at=ctx.timer_service().current_processing_time()+self.bucket_retention_ms
            bucket["cleanup_at"]=cleanup_at
            self.buckets.put(key,_dumps(bucket))
            ctx.timer_service().register_processing_time_timer(cleanup_at)
            self._index_add(self.cleanup_index,cleanup_at,key)

    def _evict(self,timestamp):
        for key in self._index_pop(self.cleanup_index,timestamp):
            bucket=self._get_json(self.buckets,key)
            if bucket is None or bucket.get("cleanup_at")!=timestamp:
                continue  # already evicted, or re-armed by a later fire
            self.ledger.put(key,_dumps({"version":int(bucket.get("version",0)),
                "fingerprint":bucket.get("fingerprint","")}))
            self.buckets.remove(key)


JOB_NAME = "bar-aggregation-v2"


def build(env):
    """Attach this stage to ``env``. No environment(), no execute() -- so the
    stage can share one job graph with the rest of the market pipeline."""
    lateness_seconds=int(os.getenv("ALLOWED_LATENESS_SECONDS","30"))
    idle_seconds=int(os.getenv("SOURCE_IDLE_TIMEOUT_SECONDS","15"))
    # Read on the submitting client and carried in the pickled function, so the
    # TaskManager does not need these variables in its own environment.
    retention_seconds=int(os.getenv("BAR_BUCKET_RETENTION_SECONDS","900"))
    ledger_ttl_seconds=int(os.getenv("BAR_VERSION_LEDGER_TTL_SECONDS","604800"))
    ticks=source(env,"market.canonical.v1","bar-aggregation-v2").assign_timestamps_and_watermarks(
        WatermarkStrategy.for_bounded_out_of_orderness(Duration.of_seconds(lateness_seconds))
        .with_idleness(Duration.of_seconds(idle_seconds))
        .with_timestamp_assigner(EnvelopeTimestamp())).key_by(
        lambda value:str(json.loads(value)["payload"]["instrument_id"]))
    # The broadcast registry is finite between strategy lifecycle changes. If
    # it remains watermark-active at MIN_VALUE, the connected operator's
    # watermark can never advance and live bar event-time timers never fire.
    registry=source(env,"strategy.inputs.v1","bar-registry-v1",starting_offsets="earliest").assign_timestamps_and_watermarks(
        WatermarkStrategy.no_watermarks().with_idleness(Duration.of_seconds(idle_seconds))).broadcast(REQUIREMENTS)
    # New operator uid: the old unbounded "dynamic-bar-buckets-v1" state is
    # deliberately not restored (ensure_flink_jobs passes --allowNonRestoredState).
    bars=ticks.connect(registry).process(
        RegistryBars(retention_seconds,ledger_ttl_seconds),output_type=Types.STRING(),
    ).name("bar-aggregation-state").uid("registry-bars-v3")
    sink(bars,"market.bars.v1","bar-sink-v2")
    sink(bars.get_side_output(LATE_TICKS),"dead-letter.v1","bar-late-tick-dlq-sink-v1")


def main():
    env = environment(JOB_NAME)
    build(env)
    env.execute(JOB_NAME)


if __name__=="__main__":main()
