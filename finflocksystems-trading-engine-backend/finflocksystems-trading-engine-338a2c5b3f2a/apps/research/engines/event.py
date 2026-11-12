from __future__ import annotations

import calendar
from datetime import datetime, timezone

from .base import EventResearchStrategy


def _timestamp(event, name):
    value = getattr(event, name, None) if not isinstance(event, dict) else event.get(name)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


class PointInTimeEventResearch(EventResearchStrategy):
    def __init__(self, event_model):
        self.event_model = event_model

    def signals(self, events, point_in_time_bars, parameters, context):
        cutoff = parameters.get("decision_timestamp")
        if isinstance(cutoff, str):
            cutoff = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
        cutoff = cutoff or datetime.now(timezone.utc)
        horizon = int(parameters.get("event_horizon_days", 5))
        if self.event_model in {"TURN_OF_MONTH", "MONTH_END_MOMENTUM"}:
            last_day = calendar.monthrange(cutoff.year, cutoff.month)[1]
            active = cutoff.day <= min(3, horizon) if self.event_model == "TURN_OF_MONTH" else cutoff.day > last_day - min(3, horizon)
            return ([{"event": {"calendar_date": cutoff.date().isoformat()}, "score": 1.0,
                      "available_at_decision": True}] if active else [])
        required_types = {
            "EARNINGS_DRIFT": {"EARNINGS"}, "EARNINGS_GAP": {"EARNINGS"},
            "PRE_EARNINGS_AVOIDANCE": {"EARNINGS"},
            "EX_DIVIDEND": {"DIVIDEND", "EX_DIVIDEND"}, "INDEX_CHANGE": {"INDEX_CHANGE"},
            "STOCK_SPLIT": {"SPLIT"},
        }.get(self.event_model)
        output = []
        for event in events:
            available = _timestamp(event, "available_timestamp")
            effective = _timestamp(event, "effective_timestamp")
            if available is None or effective is None or available > cutoff:
                continue
            event_type = getattr(event, "event_type", None) if not isinstance(event, dict) else event.get("event_type")
            if required_types and str(event_type).upper() not in required_types:
                continue
            age = (cutoff - effective).total_seconds() / 86400
            if self.event_model == "PRE_EARNINGS_AVOIDANCE":
                score = -1.0 if -horizon <= age <= 0 else 0.0
            else:
                payload = getattr(event, "payload", {}) if not isinstance(event, dict) else event.get("payload", {})
                score = float(payload.get("standardized_surprise", payload.get("abnormal_return", 1.0))) if 0 <= age <= horizon else 0.0
            output.append({"event": event, "score": score, "available_at_decision": True})
        return output


class AvailableEventStrategy(PointInTimeEventResearch):
    def __init__(self):
        super().__init__("GENERIC")
