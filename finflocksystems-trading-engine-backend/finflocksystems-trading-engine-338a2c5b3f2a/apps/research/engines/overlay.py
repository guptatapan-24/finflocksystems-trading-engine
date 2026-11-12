from __future__ import annotations

from .base import ExposureOverlay


class PortfolioRiskOverlay(ExposureOverlay):
    def __init__(self, method):
        self.method = method

    def apply(self, base_weights, risk_state, parameters):
        target = float(parameters.get("trigger_or_target", parameters.get("target_volatility", 0.15)))
        if self.method in {"SINGLE_ASSET_VOLATILITY", "PORTFOLIO_VOLATILITY"}:
            scalar = target / max(float(risk_state.get("realized_volatility", target)), 1e-9)
        elif self.method == "DRAWDOWN":
            scalar = 0.0 if abs(float(risk_state.get("drawdown", 0))) >= target else 1.0
        elif self.method == "TREND_VOLATILITY":
            scalar = (1.0 if risk_state.get("market_trend", 0) >= 0 else 0.5) * min(1.0, target / max(float(risk_state.get("realized_volatility", target)), 1e-9))
        elif self.method == "ATR":
            scalar = min(1.0, target / max(float(risk_state.get("atr_percent", target)), 1e-9))
        elif self.method == "CORRELATION_SHOCK":
            scalar = max(0.25, 1.0 - max(0.0, float(risk_state.get("average_correlation", 0.5)) - target))
        elif self.method == "MARKET_REGIME":
            scalar = {"CALM": 1.0, "NORMAL": 0.9, "STRESSED": 0.5, "CRISIS": 0.25}.get(str(risk_state.get("regime", "NORMAL")).upper(), 0.75)
        elif self.method == "LIQUIDITY_STRESS":
            scalar = max(0.25, 1.0 - float(risk_state.get("liquidity_stress", 0)))
        else:
            raise ValueError(f"Unknown overlay method {self.method}")
        scalar = max(float(parameters.get("minimum", 0)), min(float(parameters.get("maximum", 1)), scalar))
        return [max(0.0, float(value) * scalar) for value in base_weights]


class BoundedScalarOverlay(PortfolioRiskOverlay):
    def __init__(self):
        super().__init__("PORTFOLIO_VOLATILITY")
