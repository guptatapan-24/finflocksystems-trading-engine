from __future__ import annotations

from ..engines.event import PointInTimeEventResearch


EVT_001_PEAD = PointInTimeEventResearch("EARNINGS_DRIFT")
EVT_002_EARN_GAP = PointInTimeEventResearch("EARNINGS_GAP")
EVT_003_PRE_EARN_AVOID = PointInTimeEventResearch("PRE_EARNINGS_AVOIDANCE")
EVT_004_TURN_MONTH = PointInTimeEventResearch("TURN_OF_MONTH")
EVT_005_MONTH_END = PointInTimeEventResearch("MONTH_END_MOMENTUM")
EVT_006_EXDIV = PointInTimeEventResearch("EX_DIVIDEND")
EVT_007_INDEX = PointInTimeEventResearch("INDEX_CHANGE")
EVT_008_SPLIT = PointInTimeEventResearch("STOCK_SPLIT")

