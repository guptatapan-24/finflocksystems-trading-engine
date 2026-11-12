from __future__ import annotations

import numpy as np

from ..engines.base import CrossSectionalSelector


class CompositeFactorResearch(CrossSectionalSelector):
    def __init__(self, features, *, descending=True):
        self.features = tuple(features)
        self.descending = descending

    def rank(self, panel, parameters, context):
        feature_rows = []
        for row in panel:
            source = row.get("features", row)
            values = [float(source[name]) for name in self.features if source.get(name) is not None]
            if values:
                feature_rows.append((row, values))
        if not feature_rows:
            return []
        raw = np.asarray([np.mean(values) for _, values in feature_rows], dtype=float)
        scale = np.std(raw, ddof=1) if len(raw) > 1 else 0
        scores = (raw - np.mean(raw)) / scale if scale else np.zeros(len(raw))
        rows = [{**row, "model_score": float(score)} for (row, _), score in zip(feature_rows, scores)]
        sector_neutral = bool(parameters.get("sector_neutral", False))
        if sector_neutral:
            by_sector = {}
            for row in rows:
                by_sector.setdefault(row.get("sector", "UNKNOWN"), []).append(row)
            for group in by_sector.values():
                mean = float(np.mean([row["model_score"] for row in group]))
                for row in group:
                    row["model_score"] -= mean
        return sorted(rows, key=lambda row: (row["model_score"], str(row.get("symbol", ""))), reverse=self.descending)


FAC_001_VALUE = CompositeFactorResearch(("earnings_yield", "book_to_market", "sales_to_price", "fcf_yield"))
FAC_002_QUALITY = CompositeFactorResearch(("roe", "roa", "gross_profitability", "accruals"))
FAC_003_PROFIT = CompositeFactorResearch(("gross_profitability", "operating_margin", "roic"))
FAC_004_INVEST = CompositeFactorResearch(("asset_growth", "capex_growth"), descending=False)
FAC_005_LOW_VOL = CompositeFactorResearch(("realized_volatility", "idiosyncratic_volatility"), descending=False)
FAC_006_LOW_BETA = CompositeFactorResearch(("beta",), descending=False)
FAC_007_DIV_GROWTH = CompositeFactorResearch(("dividend_growth", "payout_sustainability"))
FAC_008_SHAREHOLDER = CompositeFactorResearch(("dividend_yield", "net_buyback_yield"))
FAC_009_EARN_QUALITY = CompositeFactorResearch(("cash_conversion", "accruals"))
FAC_010_BALANCE = CompositeFactorResearch(("interest_coverage", "net_debt_to_ebitda"))
FAC_011_GROWTH_QUALITY = CompositeFactorResearch(("revenue_growth", "earnings_growth", "roe"))
FAC_012_VALUE_QUALITY = CompositeFactorResearch(("value", "quality"))
FAC_013_QUALITY_MOM = CompositeFactorResearch(("quality", "momentum"))
FAC_014_MULTI = CompositeFactorResearch(("value", "quality", "momentum", "low_volatility"))
FAC_015_SECTOR_NEUTRAL = CompositeFactorResearch(("within_sector_value", "within_sector_quality", "within_sector_momentum"))
FAC_016_REVISIONS = CompositeFactorResearch(("eps_revision_1m", "eps_revision_3m", "estimate_dispersion"))

