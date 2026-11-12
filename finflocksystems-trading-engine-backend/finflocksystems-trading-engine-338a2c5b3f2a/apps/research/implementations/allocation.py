from __future__ import annotations

from ..engines.allocator import PortfolioAllocator


EW_001 = PortfolioAllocator("EQUAL_WEIGHT")
SEC_EW_001 = PortfolioAllocator("SECTOR_NEUTRAL_EQUAL_WEIGHT")
INV_VOL_001 = PortfolioAllocator("INVERSE_VOLATILITY")
RP_001 = PortfolioAllocator("RISK_PARITY")
MINVAR_001 = PortfolioAllocator("MINIMUM_VARIANCE")
MAXDIV_001 = PortfolioAllocator("MAXIMUM_DIVERSIFICATION")
HRP_001 = PortfolioAllocator("HIERARCHICAL_RISK_PARITY")
CVaR_001 = PortfolioAllocator("MINIMUM_CVAR")
BLEND_001 = PortfolioAllocator("CORE_BLEND")

