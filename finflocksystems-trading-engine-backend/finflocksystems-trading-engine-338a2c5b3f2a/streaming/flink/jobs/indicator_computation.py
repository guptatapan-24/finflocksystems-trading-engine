"""Stage 3: compute registered indicators over a rolling history of final bars.

State layout (bounded; see docs/flink-state-outbox.md):

* ``rolling-final-bars-v3`` -- one ValueState per (instrument, interval) holding
  ``{scope: [compact bars]}``. Each scope is capped at
  ``INDICATOR_HISTORY_MAX_BARS`` (never below the longest registered warm-up),
  only the fields indicator maths reads are kept, and the whole value carries a
  state TTL so instruments that stop trading are dropped. It used to keep up to
  10,000 full bar payloads per scope with no TTL.

The history is a cache. PostgreSQL (``MarketBar``) is the system of record and
the backend re-publishes recent final bars through the transactional outbox to
``market.bars.seed.v1`` whenever Flink starts without its state. Seed events
are merged into the history and emit nothing.
"""

import json
import os

from pyflink.common import Time, Types
from pyflink.datastream.functions import KeyedBroadcastProcessFunction
from pyflink.datastream.state import MapStateDescriptor, StateTtlConfig, ValueStateDescriptor

from jobs.events import envelope
from jobs.identity import (
    indicator_event_key,
    processing_mode,
    requirement_identity_hash,
)
from jobs.processing import (
    BAR_HISTORY_SEED_EVENT_TYPE,
    apply_registry_event,
    compact_history_bar,
    compute_indicator,
    history_limit,
    is_registry_meta_key,
    merge_bar_history,
)
from jobs.runtime import environment, sink, source

REQUIREMENTS=MapStateDescriptor("active-strategy-inputs-v2",Types.STRING(),Types.STRING())
SEED_TOPIC="market.bars.seed.v1"


def _key(value):
    payload=json.loads(value)["payload"]
    return f"{payload['instrument_id']}:{payload['interval']}"


class RegistryIndicators(KeyedBroadcastProcessFunction):
    def __init__(self, max_bars=1000, ttl_seconds=1209600):
        self.max_bars=max(1,int(max_bars))
        self.ttl_seconds=max(1,int(ttl_seconds))

    def open(self,runtime_context):
        descriptor=ValueStateDescriptor("rolling-final-bars-v3",Types.STRING())
        descriptor.enable_time_to_live(
            StateTtlConfig.new_builder(Time.seconds(self.ttl_seconds))
            .set_update_type(StateTtlConfig.UpdateType.OnCreateAndWrite)
            .set_state_visibility(StateTtlConfig.StateVisibility.NeverReturnExpired)
            .build()
        )
        self.history=runtime_context.get_state(descriptor)

    def process_broadcast_element(self,value,ctx):
        apply_registry_event(ctx.get_broadcast_state(REQUIREMENTS),json.loads(value),"INDICATOR")

    @staticmethod
    def _requirements(ctx,instrument_id,timeframe):
        result=[]
        for identity_hash,encoded in ctx.get_broadcast_state(REQUIREMENTS).items():
            if is_registry_meta_key(identity_hash):continue
            requirement=json.loads(encoded)
            if requirement["instrument_id"]==str(instrument_id) and requirement["timeframe"]==timeframe:
                result.append((identity_hash,requirement))
        return result

    def _load(self):
        stored=json.loads(self.history.value() or "{}")
        return {"LIVE":stored} if isinstance(stored,list) else stored

    def _seed(self,event,ctx):
        payload=event.get("payload",{})
        requirements=self._requirements(ctx,payload["instrument_id"],payload["interval"])
        limit=history_limit([item for _,item in requirements],self.max_bars)
        stored=self._load()
        stored["LIVE"]=merge_bar_history(stored.get("LIVE",[]),payload.get("bars",[]),limit)
        self.history.update(json.dumps(stored,separators=(",",":")))

    def process_element(self,value,ctx):
        source_event=json.loads(value)
        if source_event.get("event_type")==BAR_HISTORY_SEED_EVENT_TYPE:
            self._seed(source_event,ctx)
            return
        bar=source_event["payload"]
        if not bar.get("is_final",False):return
        stored=self._load()
        mode=processing_mode(bar.get("processing_mode"))
        state_scope="LIVE" if mode in {"LIVE","WARMUP"} else mode
        history=stored.get(state_scope,[])
        prior_versions=[int(x.get("version",1)) for x in history if x.get("bar_id")==bar.get("bar_id")]
        if mode=="LIVE":
            current_order=(bar["window_end"],bar["bar_id"],int(bar.get("version",1)))
            accepted_orders=[(x["window_end"],x["bar_id"],int(x.get("version",1))) for x in history]
            if (prior_versions and max(prior_versions)>int(bar.get("version",1))) or (
                accepted_orders and current_order<max(accepted_orders)
            ):
                return
        requirements=self._requirements(ctx,bar["instrument_id"],bar["interval"])
        limit=history_limit([item for _,item in requirements],self.max_bars)
        history=[x for x in history if x.get("bar_id")!=bar.get("bar_id")]+[compact_history_bar(bar)]
        history=sorted(history,key=lambda x:(x["window_end"],int(x.get("version",1))))[-limit:]
        stored[state_scope]=history
        self.history.update(json.dumps(stored,separators=(",",":")))
        for identity_hash,requirement in requirements:
            parameters=requirement.get("parameters",{});name=requirement["name"]
            role=requirement.get("role") or parameters.get("role") or ""
            implementation_version=int(requirement.get("implementation_version",1))
            calculated_identity=requirement_identity_hash(
                input_type="INDICATOR",name=name,role=role,parameters=parameters,
                instrument_id=bar["instrument_id"],timeframe=bar["interval"],
                implementation_version=implementation_version,
            )
            if calculated_identity!=identity_hash:
                continue
            result=compute_indicator(history,name,parameters)
            previous=compute_indicator(history[:-1],name,parameters)
            output_name=f"{name}_{role}" if role else name
            if name=="donchian":output_name="donchian_upper" if role=="entry" else "donchian_lower"
            source_key=indicator_event_key(
                bar["bar_id"],bar["version"],identity_hash,implementation_version,
            )
            payload={"instrument_id":bar["instrument_id"],"timeframe":bar["interval"],"indicator":output_name,
                "indicator_name":name,"indicator_role":role,"implementation_version":implementation_version,
                "value":str(result) if result is not None else None,
                "previous_value":str(previous) if previous is not None else None,"event_time":bar["window_end"],
                "parameters":parameters,"requirement_identity_hash":identity_hash,
                "source_bar_id":bar["bar_id"],"source_bar_version":bar["version"],"is_final":True,"source_key":source_key}
            payload["processing_mode"]=mode
            yield json.dumps(envelope("market.indicator","instrument",bar["instrument_id"],payload,
                source_key,source_event,bar["window_end"]),separators=(",",":"))


JOB_NAME = "indicator-computation-v2"


def build(env):
    """Attach this stage to ``env``. No environment(), no execute() -- so the
    stage can share one job graph with the rest of the market pipeline."""
    max_bars=int(os.getenv("INDICATOR_HISTORY_MAX_BARS","1000"))
    ttl_seconds=int(os.getenv("INDICATOR_HISTORY_TTL_SECONDS","1209600"))
    bars=source(env,"market.bars.v1","indicator-computation-v2")
    # Seeds are idempotent merges, so the topic is re-read from the beginning on
    # every start: a fresh job picks up whatever the backend already published.
    seeds=source(env,SEED_TOPIC,"indicator-history-seed-v1",starting_offsets="earliest")
    registry=source(env,"strategy.inputs.v1","indicator-registry-v1",starting_offsets="earliest").broadcast(REQUIREMENTS)
    # New operator uid: the old 10,000-bar "rolling-final-bars-v2" state is
    # deliberately not restored (ensure_flink_jobs passes --allowNonRestoredState).
    values=bars.union(seeds).key_by(_key).connect(registry).process(
        RegistryIndicators(max_bars,ttl_seconds),output_type=Types.STRING(),
    ).name("indicator-computation-state").uid("registry-indicators-v3")
    sink(values,"market.indicators.v1","indicator-sink-v2")


def main():
    env = environment(JOB_NAME)
    build(env)
    env.execute(JOB_NAME)


if __name__=="__main__":main()
