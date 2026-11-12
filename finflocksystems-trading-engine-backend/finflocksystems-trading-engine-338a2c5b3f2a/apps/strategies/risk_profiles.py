"""Risk profiles for the MULTI_LEG option strategies.

The client chooses one of CONSERVATIVE, NEUTRAL or AGGRESSIVE; the server builds
the whole parameter set as `base` merged with that profile's overrides. The
numbers live only in `risk_profile_presets.json` (Appendix A of the spec). The
rules below are the spec's validation rules (sections 3 and 7) and the
spec-name to schema-name mapping is the single translation layer: nothing in the
engine or the parameter schema is renamed.

Importing this module validates every preset and raises `RiskProfileConfigError`
if a rule is broken, so a bad edit to the presets cannot reach live trading.

Feature flags (both default off, so the legacy flow is untouched):
  RISK_PROFILING_ENABLED  the server resolves parameters from `risk_profile`.
  RISK_PROFILING_SHADOW   with ENABLED off, log how a profile would differ from
                          what the legacy request actually saved. Never used.
"""
import json
import logging
from pathlib import Path

from django.conf import settings

from .option_templates import AUTO_OPTION_STRATEGY_KEY, OPTION_TEMPLATES, option_parameter_schema

logger = logging.getLogger(__name__)

PRESETS_PATH = Path(__file__).with_name("risk_profile_presets.json")

# Spec docx section 7: "If no profile is supplied, fall back to NEUTRAL".
DEFAULT_PROFILE = "NEUTRAL"
PROFILE_NAMES = ("CONSERVATIVE", "NEUTRAL", "AGGRESSIVE")

# Spec keys whose schema field has a different name. Everything else is identical.
SPEC_TO_SCHEMA = {
    "daily_wilder_atr_period": "atr_period",
    "rps_entry_floor": "rps_threshold",
    "session_open": "rps_market_open_time",
    "entry_delay_after_open": "rps_entry_delay_minutes",
    "exchange_timezone": "rps_market_timezone",
}

# Shown read-only to the client, in this order (spec section 2).
SUMMARY_FIELDS = (
    ("rps_entry_floor", "RPS Entry Floor"),
    ("rps_iv_score_threshold", "RPS IV Score Threshold"),
    ("rps_tqs_threshold", "RPS TQS Threshold"),
    ("one_entry_per_expiry", "One Entry Per Expiry"),
)

# Spec sections 3 and 7. Each rule is checked against every resolved profile.
RULES = (
    ("rps_entry_floor", "must be below 42", lambda v: _number(v) and v < 42),
    ("rps_iv_score_threshold", "must be between 20 and 30 inclusive", lambda v: _number(v) and 20 <= v <= 30),
    ("rps_tqs_threshold", "must be at least 35 and below 40", lambda v: _number(v) and 35 <= v < 40),
    ("one_entry_per_expiry", "must be false", lambda v: v is False),
)

# The seeded definitions whose schema defaults match the spec base. Two fixed
# templates carry their own DTE/hold defaults (`parameter_defaults`) that the
# spec base would silently change, so they keep the legacy form.
ELIGIBLE_DEFINITION_KEYS = frozenset(
    {AUTO_OPTION_STRATEGY_KEY}
    | {key for key, template in OPTION_TEMPLATES.items() if not template.get("parameter_defaults")}
)


class RiskProfileConfigError(RuntimeError):
    """A preset breaks a spec rule. Raised at import so the process never starts with it."""


class RiskProfileRequestError(ValueError):
    """The client request is not acceptable. A ValueError so the view returns a 400."""


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def enabled():
    return bool(getattr(settings, "RISK_PROFILING_ENABLED", False))


def shadow_enabled():
    return bool(getattr(settings, "RISK_PROFILING_SHADOW", False))


def applies_to(definition_key):
    return enabled() and str(definition_key or "").upper() in ELIGIBLE_DEFINITION_KEYS


def _to_schema_keys(config):
    return {SPEC_TO_SCHEMA.get(key, key): value for key, value in config.items()}


def _coerce_to_schema(parameters, schema):
    """A null the schema does not allow becomes that field's own default.

    The spec stores blank fields as null; the schema declares three of them as
    plain strings whose default is "" (what the form submits today).
    """
    properties = schema["properties"]
    out = {}
    for key, value in parameters.items():
        declared = properties[key].get("type")
        allows_null = declared == "null" or (isinstance(declared, list) and "null" in declared)
        out[key] = properties[key].get("default") if value is None and not allows_null else value
    return out


def _validate_presets(presets):
    errors = []
    base, profiles = presets.get("base"), presets.get("profiles")
    if not isinstance(base, dict) or not isinstance(profiles, dict):
        raise RiskProfileConfigError("Risk profile presets need a 'base' object and a 'profiles' object")
    if set(profiles) != set(PROFILE_NAMES):
        errors.append(f"profiles must be exactly {sorted(PROFILE_NAMES)}, found {sorted(profiles)}")
    profile_keys = {frozenset(profile) for profile in profiles.values()}
    if len(profile_keys) > 1:
        errors.append("every profile must override the same set of parameters")
    overridden = set().union(*profiles.values()) if profiles else set()
    if overridden & set(base):
        errors.append(f"parameters in both base and a profile: {sorted(overridden & set(base))}")
    schema_keys = set(option_parameter_schema()["properties"])
    resolved_keys = {SPEC_TO_SCHEMA.get(key, key) for key in set(base) | overridden}
    if resolved_keys != schema_keys:
        errors.append(
            f"keys do not match the parameter schema: missing {sorted(schema_keys - resolved_keys)}, "
            f"unknown {sorted(resolved_keys - schema_keys)}"
        )
    for name, profile in profiles.items():
        resolved = {**base, **profile}
        for key, requirement, check in RULES:
            if key not in resolved or not check(resolved[key]):
                errors.append(f"{name}: {key} {requirement} (found {resolved.get(key, 'nothing')!r})")
    if errors:
        raise RiskProfileConfigError("Invalid risk profile presets: " + "; ".join(errors))
    return presets


def _load():
    with PRESETS_PATH.open(encoding="utf-8") as handle:
        return _validate_presets(json.load(handle))


_PRESETS = _load()


def normalise_profile(value):
    """The profile name to use: the default when none was supplied, else a valid name."""
    if value is None or value == "":
        return DEFAULT_PROFILE
    if not isinstance(value, str) or value not in PROFILE_NAMES:
        raise RiskProfileRequestError(
            f"Unknown risk_profile {value!r}; choose one of {', '.join(PROFILE_NAMES)}"
        )
    return value


def resolve_profile(profile=None):
    """The full configuration under the spec's own key names."""
    name = normalise_profile(profile)
    return {**_PRESETS["base"], **_PRESETS["profiles"][name]}


def resolve_parameters(profile=None):
    """The full configuration under the schema's field names, ready for
    `plugin.validate_configuration`, the same path the form submission takes."""
    return _coerce_to_schema(_to_schema_keys(resolve_profile(profile)), option_parameter_schema())


def diff_parameters(legacy, resolved):
    """{key: (legacy, resolved)} for every key that differs."""
    return {
        key: (legacy.get(key), resolved.get(key))
        for key in sorted(set(legacy) | set(resolved))
        if legacy.get(key) != resolved.get(key)
    }


def catalogue():
    """Read-only payload for the frontend: the numbers are never duplicated there."""
    schema_properties = option_parameter_schema()["properties"]
    summary = [
        {"key": SPEC_TO_SCHEMA.get(key, key), "label": label, "type": schema_properties[SPEC_TO_SCHEMA.get(key, key)]["type"]}
        for key, label in SUMMARY_FIELDS
    ]
    profiles = {}
    for name in PROFILE_NAMES:
        resolved = resolve_parameters(name)
        profiles[name] = {
            "summary": {item["key"]: resolved[item["key"]] for item in summary},
            "parameters": resolved,
        }
    return {
        "enabled": enabled(),
        "default_profile": DEFAULT_PROFILE,
        "profile_names": list(PROFILE_NAMES),
        "definition_keys": sorted(ELIGIBLE_DEFINITION_KEYS),
        "summary_fields": summary,
        "profiles": profiles,
    }


def prepare_create(payload):
    """(parameters, target_configuration) for a new strategy; the server decides every value."""
    if payload.get("parameters"):
        raise RiskProfileRequestError(
            "Individual parameters are not accepted for this strategy; send only risk_profile"
        )
    profile = normalise_profile(payload.get("risk_profile"))
    target_configuration = {**(payload.get("target_configuration") or {}), "risk_profile": profile}
    return resolve_parameters(profile), target_configuration


def apply_to_changes(instance, payload, changes):
    """Adjust a PATCH's `changes` in place for a profiled strategy.

    A strategy is profiled when its target_configuration carries `risk_profile`.
    Strategies without one are never touched here, whatever the request says,
    unless the request itself names a risk_profile.
    """
    current = (instance.target_configuration or {}).get("risk_profile")
    definition = changes.get("definition") or instance.definition
    if not applies_to(definition.key):
        return
    if "risk_profile" in payload:
        if payload.get("parameters"):
            raise RiskProfileRequestError(
                "Individual parameters are not accepted with risk_profile; send only risk_profile"
            )
        profile = normalise_profile(payload["risk_profile"])
        changes["parameters"] = resolve_parameters(profile)
        changes["target_configuration"] = {
            **(changes.get("target_configuration", instance.target_configuration) or {}),
            "risk_profile": profile,
        }
    elif current:
        if "parameters" in payload:
            raise RiskProfileRequestError(
                "Parameters of a risk-profile strategy are set by the server; send risk_profile to change them"
            )
        if "target_configuration" in changes:
            changes["target_configuration"] = {**(changes["target_configuration"] or {}), "risk_profile": current}


def log_shadow(definition_key, legacy_parameters, profile=None):
    """Flag off, shadow on: report how the profile would differ. Never raises, never used."""
    try:
        if enabled() or not shadow_enabled() or str(definition_key or "").upper() not in ELIGIBLE_DEFINITION_KEYS:
            return None
        differences = diff_parameters(legacy_parameters, resolve_parameters(profile))
        logger.info(
            "risk_profile_shadow definition=%s profile=%s differences=%s",
            definition_key, normalise_profile(profile), json.dumps(differences, sort_keys=True, default=str),
        )
        return differences
    except Exception:  # shadow mode must never affect the request
        logger.exception("risk_profile_shadow failed")
        return None
