"""Grounding validation (R3): nothing the model says reaches an operator unless it cites keys that exist,
quotes only numbers present in the evidence, and (for parameter advice) passes the real strategy plugin.

Item-level problems drop that item and are recorded; a problem in the headline text (verdict or summary) is
fatal for the whole response, which then goes to one repair retry and finally the deterministic fallback.
"""
import json
import re

from django.conf import settings

NUMBER = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?%?")
SMALL_INTS = {0.0, 1.0, 2.0, 3.0, 4.0, 5.0}  # ordinals and counts in prose ("two legs"); everything else must match
REL_TOL, ABS_TOL = 0.001, 0.011  # rounding to a whole number or one decimal is also accepted


def _to_float(token):
    token = token.replace(",", "").rstrip("%")
    try:
        return float(token)
    except ValueError:
        return None


def numbers_in(text):
    out = []
    for token in NUMBER.findall(str(text or "")):
        value = _to_float(token)
        if value is not None:
            out.append((abs(value), token.endswith("%")))
    return out


def allowed_numbers(facts):
    allowed = set()

    def add(value):
        allowed.add(abs(value))
        if abs(value) <= 1.5:
            allowed.add(abs(value) * 100.0)
        if abs(value) >= 1:
            allowed.add(abs(value) / 100.0)

    for key, value in facts.items():
        if (isinstance(value, bool) or value is None or key == "run_config.intent_note"
                or (key.startswith("operator.") and key.endswith(".note"))
                or (key.startswith("context.") and key.endswith((".title", ".reason")))):
            continue  # operator free text must never make a number "allowed"
        if isinstance(value, (int, float)):
            add(float(value))
        else:
            for number, _pct in numbers_in(value):
                add(number)
    return allowed


def _matches(number, allowed):
    if number in SMALL_INTS:
        return True
    return any(abs(number - a) <= max(ABS_TOL, REL_TOL * abs(a)) or number in (float(round(a)), round(a, 1))
               for a in allowed)


def bad_numbers(text, allowed):
    return [n for n, _pct in numbers_in(text) if not _matches(n, allowed)]


def _valid_refs(refs, facts):
    good = [r for r in (refs or []) if isinstance(r, str) and r in facts]
    bad = [r for r in (refs or []) if r not in good]
    return good, bad


def _loads(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _same(a, b):
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return a == b


def _check_parameter(item, instance, plugin, defect_exists, sample_n, min_n):
    """Return (normalized_fields, errors). Uses the real plugin validation (merges defaults, cross-field rules)."""
    errors = []
    key = (item.get("parameter_key") or "").strip()
    properties = (instance.definition.parameter_schema or {}).get("properties") or {}
    if not key or key not in properties:
        return None, [f"parameter_key '{key}' is not in the strategy's parameter schema"]
    current = _loads(item.get("current_value"))
    proposed = _loads(item.get("proposed_value"))
    actual = (instance.parameters or {}).get(key, plugin.default_parameters.get(key))
    if not _same(current, actual):
        errors.append(f"current_value {current!r} does not match the instance's actual value {actual!r}")
    if _same(proposed, actual):
        errors.append("proposed_value equals the current value")
    merged = {**(instance.parameters or {}), key: proposed}
    try:
        plugin.validate_configuration(merged, instance.target_configuration)
    except Exception as exc:  # ValueError from schema or cross-field rules
        errors.append(f"proposed value rejected by the strategy: {str(exc)[:200]}")
    if errors:
        return None, errors
    # R5: a parameter change is only advice without a defect once there is a real sample; otherwise it is a hypothesis.
    force_hypothesis = (not defect_exists) and sample_n < min_n
    return {"current": actual, "proposed": proposed, "force_hypothesis": force_hypothesis}, []


def validate_review(parsed, facts, instance, plugin, defect_exists, sample_n):
    """Return (clean, rejected, fatal). `clean` has normalized mistakes/strengths/suggestions."""
    from apps.intelligence.models import MISTAKE_CATEGORIES

    min_n = int(getattr(settings, "INTELLIGENCE_MIN_EPISODES_FOR_SUGGESTION", 8))
    allowed = allowed_numbers(facts)
    rejected, fatal = [], []
    for field in ("verdict", "summary"):
        bad = bad_numbers(parsed.get(field), allowed)
        if bad:
            fatal.append(f"{field} quotes numbers not in the evidence: {sorted(set(bad))[:5]}")
    clean = {k: parsed.get(k) for k in ("verdict", "process_grade", "outcome_class", "summary", "confidence",
                                        "lesson_proposals", "open_questions")}
    clean["mistakes"], clean["strengths"], clean["suggestions"] = [], [], []

    for index, item in enumerate(parsed.get("mistakes") or []):
        refs, bad_refs = _valid_refs(item.get("evidence_refs"), facts)
        reasons = []
        if item.get("category") not in MISTAKE_CATEGORIES:
            reasons.append("unknown category")
        if not refs and item.get("category") != "NONE":
            reasons.append("no valid evidence key cited")
        bad = bad_numbers(item.get("description"), allowed)
        if bad:
            reasons.append(f"quotes numbers not in the evidence: {sorted(set(bad))[:3]}")
        if reasons:
            rejected.append({"item": f"mistakes[{index}]", "reasons": reasons, "invalid_refs": bad_refs})
            continue
        clean["mistakes"].append({**item, "evidence_refs": refs})

    for index, item in enumerate(parsed.get("strengths") or []):
        refs, bad_refs = _valid_refs(item.get("evidence_refs"), facts)
        bad = bad_numbers(item.get("description"), allowed)
        if not refs or bad:
            rejected.append({"item": f"strengths[{index}]", "invalid_refs": bad_refs,
                             "reasons": (["no valid evidence key cited"] if not refs else [])
                             + ([f"quotes numbers not in the evidence: {sorted(set(bad))[:3]}"] if bad else [])})
            continue
        clean["strengths"].append({**item, "evidence_refs": refs})

    for index, item in enumerate(parsed.get("suggestions") or []):
        refs, bad_refs = _valid_refs(item.get("evidence_refs"), facts)
        reasons = []
        if not refs:
            reasons.append("no valid evidence key cited")
        for text_field in ("rationale", "expected_effect", "risk_of_change", "evidence_needed"):
            bad = bad_numbers(item.get(text_field), allowed)
            if bad:
                reasons.append(f"{text_field} quotes numbers not in the evidence: {sorted(set(bad))[:3]}")
        normalized = dict(item, evidence_refs=refs)
        if item.get("kind") == "PARAMETER" and not reasons:
            fields, errors = _check_parameter(item, instance, plugin, defect_exists, sample_n, min_n)
            if errors:
                reasons.extend(errors)
            else:
                normalized.update(current_value=fields["current"], proposed_value=fields["proposed"])
                if fields["force_hypothesis"]:
                    normalized["is_hypothesis"] = True
        elif item.get("kind") != "PARAMETER":
            normalized.update(parameter_key="", current_value=None, proposed_value=None)
        if reasons:
            rejected.append({"item": f"suggestions[{index}]", "reasons": reasons, "invalid_refs": bad_refs})
            continue
        clean["suggestions"].append(normalized)
    return clean, rejected, fatal
