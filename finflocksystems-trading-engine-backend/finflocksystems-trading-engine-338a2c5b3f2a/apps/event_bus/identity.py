import hashlib
import json
import uuid


NAMESPACE = uuid.UUID("8d0ddbee-cd38-49ad-bc36-3c758d8fbd2b")
PROCESSING_MODES = {"LIVE", "WARMUP", "REPLAY", "BACKFILL"}


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def stable_hash(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def deterministic_event_id(event_type, stable_key):
    return uuid.uuid5(NAMESPACE, f"{event_type}:{stable_key}")


def processing_mode(value, default="LIVE"):
    mode = str(value or default).upper()
    if mode not in PROCESSING_MODES:
        raise ValueError(f"Unsupported processing mode {mode}")
    return mode


def raw_event_key(payload):
    return stable_hash({
        "provider": str(payload.get("provider") or "").upper(),
        "provider_generation": str(payload.get("provider_generation") or ""),
        "source_event_id": str(payload["source_event_id"]),
    })
