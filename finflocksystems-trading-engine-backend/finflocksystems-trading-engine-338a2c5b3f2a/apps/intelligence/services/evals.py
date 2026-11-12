"""Prompt evaluation (Phase 6): a golden set of episodes scored on schema validity, grounding, invented numbers,
parameter validity, agreement with the deterministic analyzers and whether the advice discipline holds. A new prompt
may only replace the active one after it beats it here.
"""
import json
import logging
from pathlib import Path
from types import SimpleNamespace

from django.db import transaction

from apps.intelligence.llm.base import validate_against_schema
from apps.intelligence.llm.runner import run_llm
from apps.intelligence.models import PromptTemplate

from . import grounding, prompts
from ..analyzers import rules

logger = logging.getLogger(__name__)
GOLDEN_DIR = Path(__file__).resolve().parent.parent / "evals" / "golden"
HARD_METRICS = ("schema_valid_rate", "fatal_rate", "hallucinated_number_rate")


def load_golden(directory=None):
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((directory or GOLDEN_DIR).glob("*.json"))]


def _instance_for(case):
    from apps.strategies.models import StrategyDefinition
    from apps.strategies.plugins import get_plugin

    definition = StrategyDefinition.objects.filter(key=case["strategy_key"]).first()
    if definition is None:
        return None, None
    plugin = get_plugin(definition)
    params = {**plugin.default_parameters, **case.get("parameters", {})}
    return SimpleNamespace(definition=definition, parameters=params, target_configuration={"target_weight": "0.05"}), plugin


def evaluate_case(case, template, model, client, run_label):
    facts = dict(case["facts"])
    findings = rules.analyze(facts)
    for finding in findings:
        facts[f"analyzer.{finding.rule_id}"] = finding.description
    user = prompts.build_user_prompt(facts)
    log, _ = run_llm(client=client, scope="EVAL", idempotency_key=f"eval:{run_label}:{template.version}:{model}:{case['name']}",
                     system=template.system_prompt, user=user, schema=template.output_schema, model=model,
                     prompt_template=template)
    result = {"case": case["name"], "status": log.status, "schema_valid": False, "fatal": False, "items_total": 0,
              "items_accepted": 0, "hallucinated_items": 0, "param_total": 0, "param_valid": 0, "analyzer_agreement": None,
              "advice_discipline_ok": None, "expectation_met": None}
    parsed = log.parsed_output if log.status == "OK" else None
    if parsed is None:
        return result
    result["schema_valid"] = not validate_against_schema(parsed, template.output_schema)
    if not result["schema_valid"]:
        return result
    instance, plugin = _instance_for(case)
    defect = any(f.is_defect for f in findings)
    sample_n = int(facts.get("sample.n_closed_same_instance_currency", 1))
    if instance is None:
        clean, rejected, fatal = grounding.validate_review(parsed, facts, SimpleNamespace(
            definition=SimpleNamespace(parameter_schema={"properties": {}}), parameters={}, target_configuration={}),
            SimpleNamespace(default_parameters={}, validate_configuration=lambda *a, **k: None), defect, sample_n)
    else:
        clean, rejected, fatal = grounding.validate_review(parsed, facts, instance, plugin, defect, sample_n)
    total = sum(len(parsed.get(k) or []) for k in ("mistakes", "strengths", "suggestions"))
    accepted = sum(len(clean.get(k) or []) for k in ("mistakes", "strengths", "suggestions"))
    params = [s for s in (parsed.get("suggestions") or []) if s.get("kind") == "PARAMETER"]
    params_ok = [s for s in clean.get("suggestions", []) if s.get("kind") == "PARAMETER"]
    result.update(
        fatal=bool(fatal), items_total=total, items_accepted=accepted,
        hallucinated_items=sum(1 for r in rejected if "quotes numbers" in " ".join(r.get("reasons", []))) + len(fatal),
        param_total=len(params), param_valid=len(params_ok))
    cats = {f.category for f in findings if f.severity in ("MEDIUM", "HIGH")}
    llm_cats = {m["category"] for m in clean.get("mistakes", [])}
    result["analyzer_agreement"] = 1.0 if not cats else len(cats & llm_cats) / len(cats)
    expect = case.get("expect", {})
    unflagged = [s for s in params if not s.get("is_hypothesis", True)]
    result["advice_discipline_ok"] = not (expect.get("no_unflagged_parameter_change") and unflagged)
    wanted = set(expect.get("categories_any") or [])
    result["expectation_met"] = (not wanted or bool(wanted & llm_cats)) and result["advice_discipline_ok"]
    return result


def _rate(num, den):
    return None if not den else num / den


def aggregate(results):
    n = len(results)
    ok = [r for r in results if r["schema_valid"]]
    items = sum(r["items_total"] for r in ok)
    params = sum(r["param_total"] for r in ok)
    agreement = [r["analyzer_agreement"] for r in ok if r["analyzer_agreement"] is not None]
    summary = {
        "cases": n,
        "schema_valid_rate": _rate(len(ok), n),
        "fatal_rate": _rate(sum(1 for r in ok if r["fatal"]), len(ok)) if ok else 1.0,
        "grounded_item_rate": _rate(sum(r["items_accepted"] for r in ok), items) if items else 1.0 if ok else 0.0,
        "hallucinated_number_rate": _rate(sum(r["hallucinated_items"] for r in ok), items) if items else 0.0 if ok else 1.0,
        "parameter_validity_rate": _rate(sum(r["param_valid"] for r in ok), params) if params else 1.0,
        "analyzer_agreement": sum(agreement) / len(agreement) if agreement else 0.0,
        "suggestion_hit_rate": _rate(sum(1 for r in ok if r["expectation_met"]), len(ok)) if ok else 0.0,
    }
    positives = [summary["schema_valid_rate"], summary["grounded_item_rate"], 1 - summary["hallucinated_number_rate"],
                 summary["parameter_validity_rate"], summary["analyzer_agreement"], summary["suggestion_hit_rate"], 1 - summary["fatal_rate"]]
    summary["composite"] = sum(positives) / len(positives)
    return summary


def evaluate_prompt(template, model, client, cases=None, run_label="manual"):
    cases = cases if cases is not None else load_golden()
    results = [evaluate_case(case, template, model, client, run_label) for case in cases]
    return {"template": f"{template.name}:v{template.version}", "model": model, "summary": aggregate(results), "cases": results}


def beats(candidate, active):
    """The candidate must not regress on any hard metric and must have a strictly better composite."""
    if active is None:
        return candidate["schema_valid_rate"] >= 0.9 and candidate["fatal_rate"] <= 0.1 and candidate["hallucinated_number_rate"] <= 0.1
    if candidate["schema_valid_rate"] < active["schema_valid_rate"]:
        return False
    if candidate["fatal_rate"] > active["fatal_rate"] or candidate["hallucinated_number_rate"] > active["hallucinated_number_rate"]:
        return False
    return candidate["composite"] > active["composite"]


def promote(candidate_template, model, client, cases=None):
    """Evaluate the candidate and the active template on the same golden set; activate the candidate only if it wins.
    Returns {"activated": bool, "candidate": ..., "active": ...}. Scores are stored on the templates either way."""
    active = PromptTemplate.objects.filter(name=candidate_template.name, active=True).first()
    label = f"promote-{candidate_template.version}"
    candidate_run = evaluate_prompt(candidate_template, model, client, cases, run_label=label)
    active_run = None
    if active is not None and active.pk != candidate_template.pk:
        active_run = evaluate_prompt(active, model, client, cases, run_label=label)
        PromptTemplate.objects.filter(pk=active.pk).update(eval_scores={**(active.eval_scores or {}), model: active_run["summary"]})
    wins = beats(candidate_run["summary"], active_run["summary"] if active_run else None)
    PromptTemplate.objects.filter(pk=candidate_template.pk).update(eval_scores={**(candidate_template.eval_scores or {}), model: candidate_run["summary"]})
    activated = False
    if wins:
        with transaction.atomic():
            PromptTemplate.objects.filter(name=candidate_template.name, active=True).update(active=False)
            PromptTemplate.objects.filter(pk=candidate_template.pk).update(active=True)
        activated = True
    return {"activated": activated, "candidate": candidate_run, "active": active_run}
