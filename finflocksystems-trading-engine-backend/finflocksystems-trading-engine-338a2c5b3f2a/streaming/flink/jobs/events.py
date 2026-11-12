from datetime import datetime, timezone

try:
    from .identity import deterministic_event_id
except ImportError:
    from jobs.identity import deterministic_event_id


def payload_of(message):
    return message.get("payload",message)


def envelope(event_type,aggregate_type,aggregate_id,payload,stable_key,source=None,occurred_at=None):
    source=source or {}
    event_id=deterministic_event_id(event_type,stable_key)
    now=datetime.now(timezone.utc).isoformat()
    return {"event_id":event_id,"event_type":event_type,"schema_version":1,
        "occurred_at":occurred_at or now,"produced_at":now,"producer":"flink",
        "aggregate_type":aggregate_type,"aggregate_id":str(aggregate_id),
        "correlation_id":source.get("correlation_id") or source.get("event_id") or event_id,
        "causation_id":source.get("event_id"),"idempotency_key":f"{event_type}:{stable_key}","payload":payload}
