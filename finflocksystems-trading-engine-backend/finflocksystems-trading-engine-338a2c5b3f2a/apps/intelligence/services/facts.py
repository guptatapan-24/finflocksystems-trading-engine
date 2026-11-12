"""Flat, citable fact map for one episode (R3): every leaf has a stable dotted key the model must cite.

Built only from the frozen EpisodeEvidenceSnapshot (already whitelisted and sanitised), the analyzers' output and a
few DB aggregates (sample size, recent episodes). Nothing else can reach a prompt.
"""
import json

from apps.intelligence.models import TradeEpisode

PARAM_SCHEMA_FIELDS = ("type", "minimum", "maximum", "enum", "default", "exclusiveMinimum", "exclusiveMaximum")


def flatten(value, prefix="", out=None):
    out = {} if out is None else out
    if isinstance(value, dict):
        for key, item in value.items():
            flatten(item, f"{prefix}.{key}" if prefix else str(key), out)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            flatten(item, f"{prefix}[{index}]", out)
    else:
        out[prefix] = value
    return out


def estimate_tokens(text):
    return len(text) // 4 + 1


def _param_schema_facts(pack):
    schema = (pack.get("param_schema") or {}).get("properties") or {}
    out = {}
    for name, spec in schema.items():
        if not isinstance(spec, dict):
            continue
        for field in PARAM_SCHEMA_FIELDS:
            if field in spec:
                flatten(spec[field], f"param_schema.{name}.{field}", out)
    return out


def sample_size(episode):
    return TradeEpisode.objects.filter(
        instance_id=episode.instance_id, currency=episode.currency, status__in=["CLOSED", "EXPIRED"],
        opened_at__lte=episode.opened_at).count()


def recent_summary(episode, limit=10):
    rows = (TradeEpisode.objects.filter(instance_id=episode.instance_id, status__in=["CLOSED", "EXPIRED"],
                                        opened_at__lt=episode.opened_at)
            .order_by("-opened_at").values("net_pnl", "currency", "exit_reason", "direction")[:limit])
    return [{"net_pnl": str(r["net_pnl"]), "currency": r["currency"], "exit_reason": r["exit_reason"],
             "direction": r["direction"]} for r in rows]


def _learning_facts(episode, metrics, facts):
    """Validated lessons that apply to this trade and the strategy's track record, with n (Phase 5)."""
    from apps.intelligence.models import PerformanceStat

    from .evidence import sanitize_text
    from .learning import applicable_lessons

    lessons = applicable_lessons(episode.strategy_key, {**metrics, "asset_family": episode.asset_family})
    facts["retrieved_lessons.count"] = len(lessons)
    for index, lesson in enumerate(lessons):
        base = f"retrieved_lessons[{index}]"
        facts[f"{base}.statement"] = sanitize_text(lesson.statement, 300)
        facts[f"{base}.status"] = lesson.status
        facts[f"{base}.supporting_trades"] = lesson.support_n
        facts[f"{base}.contradicting_trades"] = lesson.contradict_n
    stat = PerformanceStat.objects.filter(strategy_key=episode.strategy_key, strategy_version=0, instrument="", regime="",
                                          structure="", mode="", risk_profile="", context="", currency=episode.currency).first()
    facts["strategy_stats.available"] = stat is not None
    if stat is not None:
        facts["strategy_stats.n"] = stat.n
        facts["strategy_stats.wins"] = stat.wins
        facts["strategy_stats.mean_return_bps"] = None if stat.mean_return_bps is None else round(stat.mean_return_bps, 2)
        facts["strategy_stats.ci_low_bps"] = None if stat.ci_low is None else round(stat.ci_low, 2)
        facts["strategy_stats.ci_high_bps"] = None if stat.ci_high is None else round(stat.ci_high, 2)


def build_facts(episode, max_tokens):
    """Return (facts, analyzer_findings, trimmed_groups). `facts` fits in `max_tokens` (estimated)."""
    from apps.intelligence.analyzers.rules import analyze

    snapshot = episode.evidence_snapshot
    pack = snapshot.pack
    facts = {}
    flatten(pack.get("episode") or {}, "episode", facts)
    flatten(pack.get("params") or {}, "params", facts)
    facts.update(_param_schema_facts(pack))
    flatten(pack.get("decision_evidence") or {}, "decision", facts)
    flatten(pack.get("execution") or {}, "execution", facts)
    metrics = {k: v for k, v in (pack.get("metrics") or {}).items() if k != "flags"}
    flatten(metrics, "metrics", facts)
    flatten(pack.get("run_config") or {}, "run_config", facts)
    flatten(pack.get("operator") or {}, "operator", facts)
    flatten(pack.get("context") or {}, "context", facts)
    flatten(pack.get("data_quality_flags") or [], "data_quality_flags", facts)
    facts["sample.n_closed_same_instance_currency"] = sample_size(episode)
    facts["sample.min_for_parameter_advice"] = None  # filled by the caller from settings
    flatten(recent_summary(episode), "recent", facts)
    _learning_facts(episode, metrics, facts)
    facts.pop("sample.min_for_parameter_advice")

    findings = analyze(facts)
    for finding in findings:
        facts[f"analyzer.{finding.rule_id}"] = finding.description

    trimmed = []
    for label, prefix in (("rankings", "decision.option_selection.rankings"), ("risk_checks", "execution.risk_checks"),
                          ("previous_indicators", "metrics.entry.prev."), ("selection_evidence", "decision.option_selection.selection_evidence"),
                          ("entry_context", "decision.entry_context"), ("recent", "recent")):
        if estimate_tokens(json.dumps(facts, default=str)) <= max_tokens:
            break
        victims = [k for k in facts if k.startswith(prefix)]
        for key in victims:
            facts.pop(key, None)
        if victims:
            trimmed.append(label)
    return facts, findings, trimmed
