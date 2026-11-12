from __future__ import annotations

from ..engines.overlay import PortfolioRiskOverlay


RISK_001_VOL_TARGET = PortfolioRiskOverlay("SINGLE_ASSET_VOLATILITY")
RISK_002_PORT_VOL = PortfolioRiskOverlay("PORTFOLIO_VOLATILITY")
RISK_003_DD = PortfolioRiskOverlay("DRAWDOWN")
RISK_004_TREND_VOL = PortfolioRiskOverlay("TREND_VOLATILITY")
RISK_005_ATR = PortfolioRiskOverlay("ATR")
RISK_006_CORR = PortfolioRiskOverlay("CORRELATION_SHOCK")
RISK_007_REGIME = PortfolioRiskOverlay("MARKET_REGIME")
RISK_008_LIQ = PortfolioRiskOverlay("LIQUIDITY_STRESS")

