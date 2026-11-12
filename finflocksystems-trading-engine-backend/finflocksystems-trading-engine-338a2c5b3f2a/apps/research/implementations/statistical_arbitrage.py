from __future__ import annotations

from ..engines.pair_basket import BoundedPeerModel


STAT_001_PAIR_COIN = BoundedPeerModel("COINTEGRATION")
STAT_002_PAIR_BETA = BoundedPeerModel("BETA_NEUTRAL")
STAT_003_PCA = BoundedPeerModel("PCA_RESIDUAL")
STAT_004_SECTOR = BoundedPeerModel("SECTOR_RESIDUAL")
STAT_005_SUBIND = BoundedPeerModel("SUB_INDUSTRY_RESIDUAL")
STAT_006_CLUSTER = BoundedPeerModel("CORRELATION_CLUSTER")
STAT_007_KALMAN = BoundedPeerModel("KALMAN_PAIR")
STAT_008_DISTANCE = BoundedPeerModel("MINIMUM_DISTANCE")

