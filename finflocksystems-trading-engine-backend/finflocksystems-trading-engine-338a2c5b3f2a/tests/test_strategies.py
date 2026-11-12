from decimal import Decimal
from types import SimpleNamespace

from apps.strategies.plugins import get_plugin, plugin_catalog
from apps.strategies.plugins.base import EvaluationContext


def context(parameters, *, target_weight="0.5", bar=None, indicators=None,
            previous_indicators=None, state="FLAT"):
    instance=SimpleNamespace(parameters=parameters,target_configuration={"target_weight":target_weight})
    return EvaluationContext(instance,None,None,bar or {},indicators or {},previous_indicators or {},state,{})


def test_exactly_five_builtin_strategy_plugins_are_exposed():
    assert {plugin.key for plugin in plugin_catalog()} == {
        "FIXED_WEIGHT_REBALANCE", "SMA_CROSSOVER", "RSI_MEAN_REVERSION",
        "DONCHIAN_BREAKOUT", "VOLATILITY_TARGET_MOMENTUM",
    }


def test_fixed_weight_returns_decimal_target():
    decision=get_plugin("FIXED_WEIGHT_REBALANCE").evaluate(
        context({"direction":"LONG"},target_weight="0.6")
    )
    assert decision.desired_weight == Decimal("0.6") and decision.direction == "LONG"


def test_sma_crossover_enters_and_exits():
    plugin=get_plugin("SMA_CROSSOVER")
    parameters={"fast_window":2,"slow_window":3,"direction":"LONG"}
    entered=plugin.evaluate(context(parameters,indicators={"sma_fast":3,"sma_slow":2},
        previous_indicators={"sma_fast":1,"sma_slow":2}))
    exited=plugin.evaluate(context(parameters,indicators={"sma_fast":1,"sma_slow":2},
        previous_indicators={"sma_fast":3,"sma_slow":2},state="LONG"))
    assert (entered.signal_type,entered.desired_weight)==("ENTER_LONG",Decimal("0.5"))
    assert (exited.signal_type,exited.desired_weight)==("EXIT_LONG",Decimal(0))


def test_rsi_mean_reversion_entry_transition():
    parameters={"window":3,"entry_threshold":30,"exit_threshold":70,"entry_rule":"CROSS_BELOW",
        "exit_rule":"CROSS_ABOVE","direction":"LONG"}
    decision=get_plugin("RSI_MEAN_REVERSION").evaluate(context(parameters,
        indicators={"rsi":20},previous_indicators={"rsi":40},target_weight="0.4"))
    assert decision.signal_type=="ENTER_LONG" and decision.desired_weight==Decimal("0.4")


def test_donchian_breakout_entry():
    parameters={"entry_window":3,"exit_window":2,"direction":"LONG"}
    decision=get_plugin("DONCHIAN_BREAKOUT").evaluate(context(parameters,bar={"close":4},
        indicators={"donchian_upper":3,"donchian_lower":1},target_weight="0.7"))
    assert decision.signal_type=="ENTER_LONG" and decision.desired_weight==Decimal("0.7")


def test_volatility_target_momentum_is_capped():
    parameters={"momentum_window":3,"volatility_window":3,"target_volatility":"0.2",
        "maximum_weight":"0.5","direction":"BOTH"}
    decision=get_plugin("VOLATILITY_TARGET_MOMENTUM").evaluate(context(parameters,
        indicators={"momentum":"0.1","realized_volatility":"0.1"}))
    assert decision.desired_weight==Decimal("0.5")
