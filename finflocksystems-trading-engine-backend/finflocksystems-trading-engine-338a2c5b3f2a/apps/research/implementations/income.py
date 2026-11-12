from .cross_sectional_factor import CompositeFactorResearch


INC_001_DIV_YIELD = CompositeFactorResearch(("dividend_yield", "fcf_coverage", "payout_ratio"))
INC_002_DIV_GROW = CompositeFactorResearch(("dividend_growth_5y", "roe", "debt_to_ebitda"))
INC_003_REIT_QUALITY = CompositeFactorResearch(("funds_from_operations", "net_asset_value_discount", "occupancy", "leverage"))
INC_004_BUYBACK = CompositeFactorResearch(("shares_outstanding_change", "free_cash_flow_yield"))
INC_005_CASH_RETURN = CompositeFactorResearch(("dividend_yield", "net_buyback_yield", "debt_paydown_yield"))

