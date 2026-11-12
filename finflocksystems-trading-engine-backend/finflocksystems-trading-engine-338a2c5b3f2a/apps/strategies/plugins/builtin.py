from decimal import Decimal
from .base import D, EvaluationContext, StrategyDecision, StrategyPlugin, StreamInput


def _weight(context):
    return D(context.strategy_instance.target_configuration.get("target_weight", context.strategy_instance.parameters.get("target_weight", "0")))


class RSIMeanReversionPlugin(StrategyPlugin):
    key = "RSI_MEAN_REVERSION"
    name = "RSI Mean Reversion"
    description = "Enters and exits on configurable RSI threshold rules."
    supported_directions = ("LONG", "SHORT", "BOTH")
    default_parameters = {"window": 14, "entry_threshold": 30, "exit_threshold": 65,
        "entry_rule": "CROSS_ABOVE", "exit_rule": "CROSS_ABOVE", "direction": "LONG"}
    parameter_schema = {"type":"object", "required":["window","entry_threshold","exit_threshold","direction"],
        "properties":{"window":{"type":"integer","minimum":2},"entry_threshold":{"type":"number","minimum":0,"maximum":100},
        "exit_threshold":{"type":"number","minimum":0,"maximum":100},"entry_rule":{"enum":["CROSS_ABOVE","CROSS_BELOW","BELOW","ABOVE"]},
        "exit_rule":{"enum":["CROSS_ABOVE","CROSS_BELOW","BELOW","ABOVE"]},"direction":{"enum":["LONG","SHORT","BOTH"]}},
        "additionalProperties":True}

    def validate_semantics(self, p, target):
        if D(p["entry_threshold"]) >= D(p["exit_threshold"]):
            raise ValueError("entry_threshold must be below exit_threshold")

    def required_stream_inputs(self, p):
        n=int(p["window"]); return [StreamInput("BAR","OHLCV",warmup_bars=n+1),StreamInput("INDICATOR","rsi",{"window":n},warmup_bars=n+1)]

    @staticmethod
    def _matches(rule, previous, current, level):
        if previous is None or current is None: return False
        return {"CROSS_ABOVE":previous <= level < current,"CROSS_BELOW":previous >= level > current,
            "BELOW":current < level,"ABOVE":current > level}[rule]

    def evaluate(self, c):
        current=c.indicators.get("rsi"); previous=c.previous_indicators.get("rsi"); p=c.strategy_instance.parameters
        if current is None: return StrategyDecision("NO_ACTION",None,"FLAT","RSI is warming up","WARMING_UP")
        current,previous=D(current),D(previous) if previous is not None else None
        held=c.previous_state in {"LONG","PARTIALLY_LONG","ENTRY_PENDING"}
        if held and self._matches(p.get("exit_rule","CROSS_ABOVE"),previous,current,D(p["exit_threshold"])):
            return StrategyDecision("EXIT_LONG",Decimal(0),"FLAT",f"RSI exit rule matched at {current}","FLAT")
        if not held and self._matches(p.get("entry_rule","CROSS_ABOVE"),previous,current,D(p["entry_threshold"])):
            return StrategyDecision("ENTER_LONG",_weight(c),"LONG",f"RSI entry rule matched at {current}","LONG")
        return StrategyDecision("HOLD",None,"LONG" if held else "FLAT",f"RSI {current}; no threshold transition",c.previous_state)


class SMACrossoverPlugin(StrategyPlugin):
    key="SMA_CROSSOVER"; name="SMA Crossover"; description="Targets exposure on fast/slow moving-average transitions."
    supported_directions=("LONG","SHORT","BOTH")
    default_parameters={"fast_window":20,"slow_window":50,"entry_rule":"CROSS_ABOVE","exit_rule":"CROSS_BELOW","direction":"LONG"}
    parameter_schema={"type":"object","required":["fast_window","slow_window","direction"],"properties":{
        "fast_window":{"type":"integer","minimum":2},"slow_window":{"type":"integer","minimum":3},
        "entry_rule":{"enum":["CROSS_ABOVE"]},"exit_rule":{"enum":["CROSS_BELOW"]},"direction":{"enum":["LONG","SHORT","BOTH"]}},"additionalProperties":True}
    def validate_semantics(self,p,target):
        if p["fast_window"]>=p["slow_window"]:raise ValueError("fast_window must be less than slow_window")
    def required_stream_inputs(self,p):
        return [StreamInput("BAR","OHLCV",warmup_bars=p["slow_window"]),
            StreamInput("INDICATOR","sma",{"window":p["fast_window"],"role":"fast"},warmup_bars=p["fast_window"]),
            StreamInput("INDICATOR","sma",{"window":p["slow_window"],"role":"slow"},warmup_bars=p["slow_window"])]
    def evaluate(self,c):
        f,s=c.indicators.get("sma_fast"),c.indicators.get("sma_slow");pf,ps=c.previous_indicators.get("sma_fast"),c.previous_indicators.get("sma_slow")
        if None in (f,s,pf,ps):return StrategyDecision("NO_ACTION",None,"FLAT","SMA windows are warming up","WARMING_UP")
        crossed_up=D(pf)<=D(ps) and D(f)>D(s);crossed_down=D(pf)>=D(ps) and D(f)<D(s);held=c.previous_state in {"LONG","PARTIALLY_LONG","ENTRY_PENDING"}
        if held and crossed_down:return StrategyDecision("EXIT_LONG",Decimal(0),"FLAT","Fast SMA crossed below slow SMA","FLAT")
        if not held and crossed_up:return StrategyDecision("ENTER_LONG",_weight(c),"LONG","Fast SMA crossed above slow SMA","LONG")
        return StrategyDecision("HOLD",None,"LONG" if held else "FLAT","No SMA crossover",c.previous_state)


class DonchianBreakoutPlugin(StrategyPlugin):
    key="DONCHIAN_BREAKOUT";name="Donchian Breakout";description="Targets exposure on configurable channel breakouts."
    supported_directions=("LONG","SHORT","BOTH")
    default_parameters={"entry_window":20,"exit_window":10,"direction":"LONG"}
    parameter_schema={"type":"object","required":["entry_window","exit_window","direction"],"properties":{
        "entry_window":{"type":"integer","minimum":2},"exit_window":{"type":"integer","minimum":2},"direction":{"enum":["LONG","SHORT","BOTH"]}},"additionalProperties":True}
    def required_stream_inputs(self,p):
        warm=max(p["entry_window"],p["exit_window"])+1
        return [StreamInput("BAR","OHLCV",warmup_bars=warm),StreamInput("INDICATOR","donchian",{"window":p["entry_window"],"role":"entry"},warmup_bars=warm),StreamInput("INDICATOR","donchian",{"window":p["exit_window"],"role":"exit"},warmup_bars=warm)]
    def evaluate(self,c):
        close=c.bar.get("close");upper=c.indicators.get("donchian_upper");lower=c.indicators.get("donchian_lower");held=c.previous_state in {"LONG","PARTIALLY_LONG","ENTRY_PENDING"}
        if None in (close,upper,lower):return StrategyDecision("NO_ACTION",None,"FLAT","Donchian channel is warming up","WARMING_UP")
        if held and D(close)<D(lower):return StrategyDecision("EXIT_LONG",Decimal(0),"FLAT","Close broke below exit channel","FLAT")
        if not held and D(close)>D(upper):return StrategyDecision("ENTER_LONG",_weight(c),"LONG","Close broke above entry channel","LONG")
        return StrategyDecision("HOLD",None,"LONG" if held else "FLAT","Price remains inside channel",c.previous_state)


class VolatilityTargetMomentumPlugin(StrategyPlugin):
    key="VOLATILITY_TARGET_MOMENTUM";name="Volatility-Target Momentum";description="Scales directional momentum exposure to realized volatility."
    supported_directions=("LONG","SHORT","BOTH")
    default_parameters={"momentum_window":20,"volatility_window":20,"target_volatility":0.10,"maximum_weight":0.20,"direction":"BOTH"}
    parameter_schema={"type":"object","required":["momentum_window","volatility_window","target_volatility","maximum_weight","direction"],"properties":{
        "momentum_window":{"type":"integer","minimum":2},"volatility_window":{"type":"integer","minimum":2},"target_volatility":{"type":"number","exclusiveMinimum":0},"maximum_weight":{"type":"number","exclusiveMinimum":0,"maximum":1},"direction":{"enum":["LONG","SHORT","BOTH"]}},"additionalProperties":True}
    def required_stream_inputs(self,p):
        warm=max(p["momentum_window"],p["volatility_window"])+1
        return [StreamInput("BAR","OHLCV",warmup_bars=warm),StreamInput("INDICATOR","momentum",{"window":p["momentum_window"]},warmup_bars=warm),StreamInput("INDICATOR","realized_volatility",{"window":p["volatility_window"]},warmup_bars=warm)]
    def evaluate(self,c):
        momentum=c.indicators.get("momentum");vol=c.indicators.get("realized_volatility");p=c.strategy_instance.parameters
        if momentum is None or vol is None:return StrategyDecision("NO_ACTION",None,"FLAT","Momentum or volatility is warming up","WARMING_UP")
        momentum,vol=D(momentum),D(vol)
        if vol<=0 or momentum==0:weight=Decimal(0)
        else:
            sign=Decimal(1) if momentum>0 else Decimal(-1);weight=sign*D(p["target_volatility"])/vol
            maximum=D(p["maximum_weight"]);weight=max(-maximum,min(maximum,weight))
            if p.get("direction")=="LONG":weight=max(Decimal(0),weight)
        direction="LONG" if weight>0 else "SHORT" if weight<0 else "FLAT"
        return StrategyDecision("SET_TARGET",weight,direction,"Momentum exposure scaled by realized volatility",direction)


class FixedWeightPlugin(StrategyPlugin):
    key="FIXED_WEIGHT_REBALANCE";name="Fixed-Weight Rebalance";description="Maintains a configured portfolio weight without indicator inputs."
    supported_directions=("LONG","SHORT","BOTH")
    default_parameters={"direction":"LONG"}
    parameter_schema={"type":"object","required":["direction"],"properties":{"direction":{"enum":["LONG","SHORT","BOTH"]}},"additionalProperties":True}
    def required_stream_inputs(self,p):return [StreamInput("BAR","OHLCV",warmup_bars=1)]
    def evaluate(self,c):
        weight=_weight(c);direction="LONG" if weight>0 else "SHORT" if weight<0 else "FLAT"
        return StrategyDecision("SET_TARGET",weight,direction,"Configured fixed target weight",direction)
