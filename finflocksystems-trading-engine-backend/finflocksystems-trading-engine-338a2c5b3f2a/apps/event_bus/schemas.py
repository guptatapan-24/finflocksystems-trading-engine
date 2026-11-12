import json
from decimal import Decimal
from pathlib import Path
from jsonschema import Draft202012Validator

SCHEMA_ROOT = Path(__file__).resolve().parents[2] / "assets" / "kafka" / "schemas"


def decimal_safe(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: decimal_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [decimal_safe(item) for item in value]
    return value


def validate_envelope(envelope):
    schema = json.loads((SCHEMA_ROOT / "event-envelope-v1.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(decimal_safe(envelope))
    return True
