from __future__ import annotations

from decimal import Decimal

from .base import D, StrategyDecision, StrategyPlugin, StreamInput


class CatalogueLongOnlyPlugin(StrategyPlugin):
    """Disabled-by-default runtime adapter for registered long-only research strategies.

    Definitions created by the research bootstrap bind the concrete registry key to this
    adapter.  The adapter consumes a lagged, audited ``research_signal`` stream value;
    it never evaluates catalogue JSON text or recomputes research inside a web worker.
    """

    key = "CATALOGUE_LONG_ONLY"
    name = "Registered long-only research strategy"
    supported_directions = ("LONG",)
    parameter_schema = {"type": "object", "additionalProperties": True}

    @classmethod
    def for_definition(cls, definition):
        plugin = cls()
        plugin.key = definition.key
        plugin.name = definition.name
        plugin.description = definition.description
        plugin.parameter_schema = definition.parameter_schema or plugin.parameter_schema
        plugin.supported_timeframes = tuple(definition.supported_timeframes or ("1d",))
        return plugin

    def required_stream_inputs(self, parameters):
        integer_values = [int(value) for key, value in parameters.items() if "window" in key and isinstance(value, int)]
        warmup = min(756, max(integer_values, default=1) + 1)
        return [
            StreamInput("BAR", "OHLCV", warmup_bars=warmup),
            StreamInput("INDICATOR", "research_signal", {"strategy_key": self.key}, warmup_bars=warmup),
        ]

    def evaluate(self, context):
        value = context.indicators.get("research_signal")
        if value is None:
            return StrategyDecision("NO_ACTION", None, "FLAT", "Lagged research signal is unavailable", "WARMING_UP")
        desired = max(Decimal(0), min(Decimal(1), D(value)))
        previous_long = context.previous_state in {"LONG", "PARTIALLY_LONG", "ENTRY_PENDING"}
        if desired == 0 and previous_long:
            return StrategyDecision("EXIT_LONG", Decimal(0), "FLAT", "Registered research exposure is flat", "FLAT")
        if desired > 0:
            maximum = D(context.strategy_instance.target_configuration.get("target_weight", desired))
            target = min(desired, max(Decimal(0), maximum))
            return StrategyDecision("SET_TARGET", target, "LONG", "Registered lagged long-only exposure", "LONG")
        return StrategyDecision("HOLD", None, "FLAT", "Registered research exposure remains flat", "FLAT")

