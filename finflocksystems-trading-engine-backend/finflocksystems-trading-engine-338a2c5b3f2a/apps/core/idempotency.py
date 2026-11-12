import hashlib
import json
from decimal import Decimal


class IdempotencyConflict(ValueError):
    pass


def _canonical(value):
    if isinstance(value, dict):
        return {str(key): _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float, Decimal)):
        normalized = format(Decimal(str(value)).normalize(), "f")
        return "0" if normalized in {"-0", ""} else normalized
    return str(value)


def canonical_request_hash(operation, payload):
    body = json.dumps(
        {"operation": operation, "payload": _canonical(payload)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(body.encode()).hexdigest()


def require_matching_request(existing_hash, expected_hash):
    if existing_hash and existing_hash != expected_hash:
        raise IdempotencyConflict("Idempotency-Key was already used for a different request")
