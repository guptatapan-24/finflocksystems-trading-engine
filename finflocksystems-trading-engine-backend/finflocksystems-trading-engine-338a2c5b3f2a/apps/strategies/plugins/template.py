"""Copy this module when implementing a repository-local strategy plugin."""
from decimal import Decimal
from .base import StrategyDecision, StrategyPlugin, StreamInput


class ExampleStrategyPlugin(StrategyPlugin):
    key="EXAMPLE_STRATEGY"
    name="Example Strategy"
    description="Documentation template; it is not registered by default."
    supported_directions=("LONG",)
    default_parameters={"window":20,"direction":"LONG"}
    parameter_schema={"type":"object","required":["window","direction"],"properties":{
        "window":{"type":"integer","minimum":2},"direction":{"enum":["LONG"]}},"additionalProperties":False}

    def required_stream_inputs(self,parameters):
        return [StreamInput("BAR","OHLCV",warmup_bars=parameters["window"]),
            StreamInput("INDICATOR","sma",{"window":parameters["window"]},warmup_bars=parameters["window"])]

    def evaluate(self,context):
        value=context.indicators.get("sma")
        if value is None:return StrategyDecision("NO_ACTION",None,"FLAT","SMA is warming up","WARMING_UP")
        if Decimal(str(context.bar["close"]))>Decimal(str(value)):
            weight=Decimal(str(context.strategy_instance.target_configuration["target_weight"]))
            return StrategyDecision("SET_TARGET",weight,"LONG","Close is above SMA","LONG")
        return StrategyDecision("SET_TARGET",Decimal(0),"FLAT","Close is not above SMA","FLAT")
