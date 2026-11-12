from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ResearchProtocolContext:
    frequency: str = "1d"
    annualization: int = 252
    long_only: bool = True
    next_bar_execution: bool = True
    commission_bps: float = 0.0
    spread_bps: float = 0.0
    impact_coefficient: float = 0.0
    maximum_participation: float = 0.10
    cost_stress_bps: float = 0.0


@dataclass(frozen=True)
class ResearchSignalResult:
    desired_exposure: list[float]
    diagnostics: dict[str, Any] = field(default_factory=dict)


class SingleAssetResearchStrategy(ABC):
    @abstractmethod
    def signals(self, bars: list[dict], parameters: dict, context: ResearchProtocolContext) -> ResearchSignalResult:
        raise NotImplementedError


class CrossSectionalSelector(ABC):
    @abstractmethod
    def rank(self, panel, parameters, context):
        raise NotImplementedError


class SleeveAllocator(ABC):
    @abstractmethod
    def allocate(self, returns, holdings, constraints, parameters):
        raise NotImplementedError


class ExposureOverlay(ABC):
    @abstractmethod
    def apply(self, base_weights, risk_state, parameters):
        raise NotImplementedError


class EventResearchStrategy(ABC):
    @abstractmethod
    def signals(self, events, point_in_time_bars, parameters, context):
        raise NotImplementedError


class PairBasketResearchStrategy(ABC):
    """Research-only until runtime supports atomic multi-instrument targets and shorts."""

    @abstractmethod
    def targets(self, panel, parameters, context):
        raise NotImplementedError
