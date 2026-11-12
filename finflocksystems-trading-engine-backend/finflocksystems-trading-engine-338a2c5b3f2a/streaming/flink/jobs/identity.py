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
    return str(uuid.uuid5(NAMESPACE, f"{event_type}:{stable_key}"))


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


def canonical_event_key(raw_event_id, instrument_id):
    return stable_hash({
        "raw_event_id": str(raw_event_id),
        "instrument_id": str(instrument_id),
    })


def market_bar_id(instrument_id, timeframe, window_start):
    return stable_hash({
        "instrument_id": str(instrument_id),
        "timeframe": str(timeframe),
        "window_start": str(window_start),
    })


def bar_event_key(bar_id, version):
    return stable_hash({"bar_id": str(bar_id), "version": int(version)})


def requirement_identity_payload(
    *,
    input_type,
    name,
    role,
    parameters,
    instrument_id,
    timeframe,
    implementation_version,
):
    return {
        "implementation_version": int(implementation_version),
        "indicator_name": str(name),
        "indicator_role": str(role or ""),
        "input_type": str(input_type).upper(),
        "instrument_id": str(instrument_id),
        "parameters": parameters or {},
        "timeframe": str(timeframe),
    }


def requirement_identity_hash(**values):
    return stable_hash(requirement_identity_payload(**values))


def indicator_event_key(bar_id, bar_version, requirement_identity, implementation_version):
    return stable_hash({
        "bar_id": str(bar_id),
        "bar_version": int(bar_version),
        "implementation_version": int(implementation_version),
        "requirement_identity_hash": str(requirement_identity),
    })


def market_quality_event_key(source_event_id, status):
    return stable_hash({
        "source_event_id": str(source_event_id),
        "status": str(status).upper(),
    })


def starting_offset_policy(job_name, explicit=None, environment=None):
    environment = environment or {}
    key = "KAFKA_STARTING_OFFSETS_" + "".join(
        character if character.isalnum() else "_" for character in job_name.upper()
    )
    policy = str(
        explicit
        or environment.get(key)
        or environment.get("KAFKA_STARTING_OFFSETS")
        or "committed"
    ).lower()
    if policy not in {"committed", "earliest", "latest"}:
        raise ValueError(f"Unsupported Kafka starting-offset policy {policy} for {job_name}")
    return policy
