import hashlib
import json


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
    encoded = json.dumps(
        requirement_identity_payload(**values),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def indicator_output_name(name, role=""):
    if name == "donchian":
        return "donchian_upper" if role == "entry" else "donchian_lower"
    return f"{name}_{role}" if role else name
