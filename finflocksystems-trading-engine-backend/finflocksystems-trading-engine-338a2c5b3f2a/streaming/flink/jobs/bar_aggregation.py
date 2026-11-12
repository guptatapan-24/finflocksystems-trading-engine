import json
import os
from datetime import datetime
from pyflink.common import Duration, Types, WatermarkStrategy
from pyflink.common.watermark_strategy import TimestampAssigner
from pyflink.datastream.functions import KeyedBroadcastProcessFunction
from pyflink.datastream.state import MapStateDescriptor, ValueStateDescriptor
from jobs.processing import aggregate_bars
from jobs.runtime import environment, sink, source
from jobs.events import envelope
from jobs.identity import (
    bar_event_key,
    canonical_event_key,
    deterministic_event_id,
    market_bar_id,
    processing_mode,
    raw_event_key,
)
from jobs.processing import advance_bar_version

REQUIREMENTS=MapStateDescriptor("active-bar-inputs-v1",Types.STRING(),Types.STRING())


def timeframe_seconds(value):
    value=str(value).strip().lower();units={"s":1,"m":60,"h":3600,"d":86400}
    if len(value)<2 or value[-1] not in units or not value[:-1].isdigit():raise ValueError(f"Unsupported timeframe {value}")
    seconds=int(value[:-1])*units[value[-1]]
    if seconds<=0:raise ValueError("Timeframe must be positive")
    return seconds


class EnvelopeTimestamp(TimestampAssigner):
    def extract_timestamp(self,value,record_timestamp):
        payload=json.loads(value)["payload"]
        return int(datetime.fromisoformat(payload["event_time"].replace("Z","+00:00")).timestamp()*1000)


class RegistryBars(KeyedBroadcastProcessFunction):
    def open(self,runtime_context):
        self.buckets=runtime_context.get_state(ValueStateDescriptor("dynamic-bar-buckets-v1",Types.STRING()))

    def process_broadcast_element(self,value,ctx):
        event=json.loads(value);payload=event.get("payload",{});state=ctx.get_broadcast_state(REQUIREMENTS)
        for identity_hash in payload.get("removed_requirement_hashes",[]):state.remove(identity_hash)
        for requirement in payload.get("requirements",[]):
            if requirement.get("input_type")!="BAR":continue
            item={**requirement,"instrument_id":str(payload["instrument_id"]),"timeframe":payload["timeframe"]}
            state.put(requirement["identity_hash"],json.dumps(item,separators=(",",":")))

    def process_element(self,value,ctx):
        source_event=json.loads(value);tick={**source_event["payload"],
            "_envelope_event_id":source_event["event_id"],
            "_correlation_id":source_event.get("correlation_id")}
        stored=json.loads(self.buckets.value() or "{}")
        stamp=int(datetime.fromisoformat(tick["event_time"].replace("Z","+00:00")).timestamp())
        for identity_hash,encoded in ctx.get_broadcast_state(REQUIREMENTS).items():
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
                bucket=stored.get(key,{"version":0,"fingerprint":""})
                bucket,version=advance_bar_version(bucket,bar)
                if version is None:
                    continue
                stored[key]=bucket;bar["version"]=version
                yield json.dumps(envelope("market.bar","instrument",bar["instrument_id"],bar,
                    bar_event_key(bar_id,bar["version"]),source_event,bar["window_end"]),separators=(",",":"))
                continue
            seconds=timeframe_seconds(requirement["timeframe"]);start=stamp-stamp%seconds
            mode=processing_mode(tick.get("processing_mode"))
            key=f"{identity_hash}:{start}:{mode}";bucket=stored.get(key,{"ticks":[],"version":0,
                "fingerprint":"","timeframe":requirement["timeframe"],"seconds":seconds,"end":start+seconds})
            bucket["ticks"]=[x for x in bucket["ticks"] if x["source_event_id"]!=tick["source_event_id"]]+[tick];stored[key]=bucket
            ctx.timer_service().register_event_time_timer((start+seconds)*1000)
        self.buckets.update(json.dumps(stored,separators=(",",":")))

    def on_timer(self,timestamp,ctx):
        stored=json.loads(self.buckets.value() or "{}")
        for key,bucket in list(stored.items()):
            # Direct final bars share this state with timer-driven tick
            # buckets, but have no event-time timer metadata.
            if bucket.get("end") is None or bucket["end"]*1000!=timestamp:continue
            bars=aggregate_bars(bucket["ticks"],bucket["timeframe"],bucket["seconds"],final=True)
            if bars:
                bar=bars[0];bucket,version=advance_bar_version(bucket,bar)
                if version is None:
                    continue
                bar["version"]=version;bar["is_final"]=True
                causal_tick=max(bucket["ticks"],key=lambda item:(item["event_time"],item["source_event_id"]))
                causal_event_id=causal_tick.get("_envelope_event_id") or deterministic_event_id(
                    "market.canonical",
                    canonical_event_key(raw_event_key(causal_tick),causal_tick["instrument_id"]),
                )
                source_event={"event_id":causal_event_id,
                    "correlation_id":causal_tick.get("_correlation_id"),"payload":causal_tick}
                yield json.dumps(envelope("market.bar","instrument",bar["instrument_id"],bar,
                    bar_event_key(bar["bar_id"],bar["version"]),source_event,bar["window_end"]),separators=(",",":"))
            stored[key]=bucket
        self.buckets.update(json.dumps(stored,separators=(",",":")))


JOB_NAME = "bar-aggregation-v2"


def build(env):
    """Attach this stage to ``env``. No environment(), no execute() -- so the
    stage can share one job graph with the rest of the market pipeline."""
    lateness_seconds=int(os.getenv("ALLOWED_LATENESS_SECONDS","30"))
    idle_seconds=int(os.getenv("SOURCE_IDLE_TIMEOUT_SECONDS","15"))
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
    bars=ticks.connect(registry).process(RegistryBars(),output_type=Types.STRING()).uid("registry-bars-v2")
    sink(bars,"market.bars.v1","bar-sink-v2")


def main():
    env = environment(JOB_NAME)
    build(env)
    env.execute(JOB_NAME)


if __name__=="__main__":main()
