from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class StreamInput:
    input_type: str
    name: str
    parameters: dict[str, Any] = field(default_factory=dict)
    bar_fields: tuple[str, ...] = ("open", "high", "low", "close", "volume")
    warmup_bars: int = 0
    role: str = ""
    implementation_version: int = 1


@dataclass(frozen=True)
class EvaluationContext:
    strategy_instance: Any
    strategy_version: Any
    instrument: Any
    bar: dict[str, Any]
    indicators: dict[str, Any]
    previous_indicators: dict[str, Any]
    previous_state: str
    state_data: dict[str, Any]
    attributed_position: Any = None
    active_orders: tuple[Any, ...] = ()
    attributed_positions: tuple[Any, ...] = ()
    portfolio_state: dict[str, Any] = field(default_factory=dict)
    market_session: dict[str, Any] = field(default_factory=dict)
    event_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StrategyDecision:
    signal_type: str
    desired_weight: Decimal | None
    direction: str
    reason: str
    next_state: str
    confidence: Decimal | None = None
    state_data: dict[str, Any] = field(default_factory=dict)
    targets: tuple["StrategyLegTarget", ...] = ()


@dataclass(frozen=True)
class StrategyLegTarget:
    """One exact instrument target emitted by a multi-leg strategy."""

    instrument: Any
    quantity: Decimal
    direction: str
    role: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class StrategyPlugin(ABC):
    key = ""
    name = ""
    description = ""
    supported_asset_types = ("STK",)
    supported_directions = ("LONG",)
    supported_timeframes = ("1m", "5m", "15m", "1h", "1d")
    parameter_schema: dict[str, Any] = {"type": "object"}
    default_parameters: dict[str, Any] = {}
    strategy_kind = "DIRECTIONAL"
    execution_model = "SINGLE_INSTRUMENT_WEIGHT"
    requires_option_context = False

    def validate_configuration(self, parameters, target_configuration=None):
        from jsonschema import Draft202012Validator
        merged = {**self.default_parameters, **(parameters or {})}
        errors = sorted(Draft202012Validator(self.parameter_schema).iter_errors(merged), key=lambda e: list(e.path))
        if errors:
            raise ValueError("; ".join(error.message for error in errors))
        direction = merged.get("direction", "LONG")
        if direction not in self.supported_directions:
            raise ValueError(f"Unsupported direction {direction} for {self.key}")
        self.validate_semantics(merged, target_configuration or {})
        return merged

    def validate_semantics(self, parameters, target_configuration):
        return None

    @abstractmethod
    def required_stream_inputs(self, parameters) -> list[StreamInput]: ...

    def warmup_bars(self, parameters):
        return max((item.warmup_bars for item in self.required_stream_inputs(parameters)), default=0)

    @abstractmethod
    def evaluate(self, context: EvaluationContext) -> StrategyDecision: ...

    def build_target(self, decision, context):
        if decision.desired_weight is None:
            return None
        return {
            "target_type": "FLAT" if decision.desired_weight == 0 else "WEIGHT",
            "target_weight": decision.desired_weight,
            "direction": decision.direction,
            "signal_type": decision.signal_type,
            "reason": decision.reason,
            "confidence": decision.confidence,
        }

    def build_targets(self, decision, context):
        if decision.targets:
            return [
                {
                    "instrument": target.instrument,
                    "target_type": "FLAT" if target.quantity == 0 else "QUANTITY",
                    "target_weight": Decimal(0),
                    "target_quantity": target.quantity,
                    "direction": target.direction,
                    "signal_type": decision.signal_type,
                    "reason": decision.reason,
                    "confidence": decision.confidence,
                    "metadata": {"role": target.role, **target.metadata},
                }
                for target in decision.targets
            ]
        target = self.build_target(decision, context)
        return [{"instrument": context.instrument, **target}] if target else []


def D(value):
    return value if isinstance(value, Decimal) else Decimal(str(value))
