import json
from pyflink.common import Types
from pyflink.datastream.functions import KeyedBroadcastProcessFunction
from pyflink.datastream.state import MapStateDescriptor, ValueStateDescriptor
from jobs.processing import compute_indicator
from jobs.runtime import environment, sink, source
from jobs.events import envelope
from jobs.identity import (
    indicator_event_key,
    processing_mode,
    requirement_identity_hash,
)

REQUIREMENTS=MapStateDescriptor("active-strategy-inputs-v1",Types.STRING(),Types.STRING())


class RegistryIndicators(KeyedBroadcastProcessFunction):
    def open(self,runtime_context):
        self.history=runtime_context.get_state(ValueStateDescriptor("rolling-final-bars-v2",Types.STRING()))

    def process_broadcast_element(self,value,ctx):
        event=json.loads(value);payload=event.get("payload",{});state=ctx.get_broadcast_state(REQUIREMENTS)
        for identity_hash in payload.get("removed_requirement_hashes",[]):state.remove(identity_hash)
        for requirement in payload.get("requirements",[]):
            if requirement.get("input_type")!="INDICATOR":continue
            item={**requirement,"instrument_id":str(payload["instrument_id"]),"timeframe":payload["timeframe"]}
            state.put(requirement["identity_hash"],json.dumps(item,separators=(",",":")))

    def process_element(self,value,ctx):
        source_event=json.loads(value);bar=source_event["payload"]
        if not bar.get("is_final",False):return
        stored=json.loads(self.history.value() or "{}")
        if isinstance(stored,list):
            stored={"LIVE":stored}
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
        history=[x for x in history if x.get("bar_id")!=bar.get("bar_id")]+[bar]
        history=sorted(history,key=lambda x:(x["window_end"],int(x.get("version",1))))[-10000:]
        stored[state_scope]=history
        self.history.update(json.dumps(stored,separators=(",",":")))
        for identity_hash,encoded in ctx.get_broadcast_state(REQUIREMENTS).items():
            requirement=json.loads(encoded)
            if requirement["instrument_id"]!=str(bar["instrument_id"]) or requirement["timeframe"]!=bar["interval"]:continue
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
    bars=source(env,"market.bars.v1","indicator-computation-v2").key_by(
        lambda value:f"{json.loads(value)['payload']['instrument_id']}:{json.loads(value)['payload']['interval']}")
    registry=source(env,"strategy.inputs.v1","indicator-registry-v1",starting_offsets="earliest").broadcast(REQUIREMENTS)
    values=bars.connect(registry).process(RegistryIndicators(),output_type=Types.STRING()).uid("registry-indicators-v2")
    sink(values,"market.indicators.v1","indicator-sink-v2")


def main():
    env = environment(JOB_NAME)
    build(env)
    env.execute(JOB_NAME)


if __name__=="__main__":main()
