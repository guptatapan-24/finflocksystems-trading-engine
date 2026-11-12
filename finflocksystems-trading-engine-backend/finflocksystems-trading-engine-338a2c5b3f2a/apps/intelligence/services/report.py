"""Full trade report (Phase 4C): a generic section/block structure, rendered to JSON or Markdown. PDF/HTML can be added
later by writing another renderer over the same structure. Numbers come from stored deterministic values only;
model text is clearly labelled and sits in its own section.
"""
from decimal import Decimal

from apps.intelligence.models import EpisodeLiveSnapshot, LiveAlert, TradeEpisode, TradeReview

REPORT_VERSION = "2"
PROFILE_LABELS = {"rps_threshold": "RPS entry floor", "rps_iv_score_threshold": "RPS IV score threshold",
                  "rps_tqs_threshold": "RPS TQS threshold", "one_entry_per_expiry": "One entry per expiry"}
NOT_CAPTURED = ("Not captured: this trade closed before the operator's selections and inputs were recorded, "
                "and they cannot be reconstructed.")


def _money(value, currency):
    return f"{Decimal(value).normalize():f} {currency}"


def kv(rows):
    return {"type": "kv", "rows": [{"label": k, "value": v} for k, v in rows if v not in (None, "")]}


def table(columns, rows):
    return {"type": "table", "columns": columns, "rows": rows}


def text(value):
    return {"type": "text", "text": value}


def bullets(items):
    return {"type": "list", "items": list(items)}


GATES = [("margin.rps", "RPS vs threshold", "opt.entry_score", "opt.rps_threshold"),
         ("margin.tqs", "TQS vs threshold", "opt.tqs", None),
         ("margin.iv_score", "IV score vs threshold", "opt.iv_score", None)]


def _parameters_section(episode, metrics, pack):
    params = pack.get("params") or {}
    shown = [(k, v) for k, v in sorted(params.items()) if not isinstance(v, (dict, list))]
    blocks = [table(["Parameter", "Value"], [[k, str(v)] for k, v in shown])]
    margins = []
    for key, label, value_key, threshold_key in GATES:
        if metrics.get(key) is not None:
            tail = f" ({metrics[value_key]} against {metrics[threshold_key]})" if threshold_key and metrics.get(threshold_key) is not None and metrics.get(value_key) is not None else ""
            margins.append(f"{label}: margin {metrics[key]:+g}{tail}")
    if metrics.get("opt.decision_band"):
        margins.append(f"Decision band: {metrics['opt.decision_band']} (the engine does not enforce band sizing)")
    if margins:
        blocks.append(text("How close the entry was to its gates"))
        blocks.append(bullets(margins))
    if metrics.get("opt.tqs_unmeasured"):
        blocks.append(text("Score components left out because they could not be measured (neutral): "
                           + metrics["opt.tqs_unmeasured"] + f" (TQS rests on {metrics.get('opt.tqs_coverage', 'n/a')} of its weight)."))
    run_config = pack.get("run_config") or {}
    if run_config:
        flags = run_config.get("data_source_flags") or {}
        blocks.append(kv([("Remote feature database", "on" if flags.get("remote_feature_db") else "off"),
                          ("Option chain provider", flags.get("option_chain_provider")),
                          ("Delayed data", "yes" if flags.get("delayed_data") else "no"),
                          ("Option pre-trade risk", "on" if flags.get("option_pretrade_risk_enabled", True) else "OFF"),
                          ("Parameter hash", (run_config.get("parameter_hash") or "")[:12]),
                          ("Operator intent note", run_config.get("intent_note"))]))
        regime = run_config.get("regime_at_activation") or {}
        if regime.get("available"):
            blocks.append(text(f"Volatility regime at activation: realised-vol percentile {regime.get('vol_pctile'):.0f} "
                               f"({regime.get('bars')} bars)." if regime.get("vol_pctile") is not None else "Regime measured."))
    return {"id": "parameters", "title": "Parameters used", "blocks": blocks}


def _operator_pack(episode, pack):
    """The frozen operator block; for a trade still open it is read live from the stored rows."""
    if pack.get("operator"):
        return pack["operator"]
    if episode.status != "OPEN":
        return {}
    try:
        from apps.strategies.models import StrategyInstance

        from .evidence import operator_block

        instance = (StrategyInstance.objects.select_related("definition", "instrument")
                    .filter(pk=episode.instance_id).first())
        if instance is not None:
            return operator_block(instance, episode.opened_at, None, episode.instrument_symbol)
    except Exception:
        pass
    return {}


def _context_pack(episode, pack):
    """The frozen context block; for a trade still open it is read live from stored items."""
    if "context" in pack:
        return pack["context"]
    if episode.status != "OPEN":
        return None
    try:
        from apps.strategies.models import StrategyInstance

        from .context_view import compute

        instance = StrategyInstance.objects.select_related("definition", "instrument").filter(pk=episode.instance_id).first()
        return compute(instance, episode.opened_at, None)["block"] if instance is not None else None
    except Exception:
        return None


def _context_section(context, metrics):
    title = "Outside context around this trade"
    if context is None:
        return {"id": "context", "title": title, "blocks": [text("Not captured: this trade closed before outside context was collected, "
                                                             "and it cannot be reconstructed.")]}
    if not context.get("available"):
        return {"id": "context", "title": title, "blocks": [text(f"Not available: {context.get('reason') or 'no context was stored'}.")]}
    blocks = [kv([("Event inside the holding period", "yes" if metrics.get("ctx.event_in_hold") else "no"),
                  ("High-impact events inside the holding period", metrics.get("ctx.high_impact_events_in_hold")),
                  ("Central-bank event inside the holding period", "yes" if metrics.get("ctx.central_bank_in_hold") else "no"),
                  ("Days to the next central-bank event at entry", metrics.get("ctx.days_to_central_bank_at_entry")),
                  ("Earnings inside the holding period", "yes" if metrics.get("ctx.earnings_within_dte") else "no"),
                  ("Stories in the 24 hours before entry", metrics.get("ctx.news_count_24h_before_entry")),
                  ("News volume vs its 30-day norm (z-score)", metrics.get("ctx.news_burst_zscore") if metrics.get("ctx.news_burst_zscore") is not None
                   else "not enough history yet")])]
    for label, key in (("Known when the trade was entered (last 24 hours)", "known_at_entry"), ("Scheduled after entry", "scheduled_after_entry"),
                       ("Arrived or happened while the trade was open", "during_hold")):
        rows = [[(line["at"] or "")[:16].replace("T", " "), line["source"], line["kind"].lower(), line["impact"] or "", line["title"]]
                for line in context.get(key) or []]
        blocks += [text(label), table(["Time (UTC)", "Source", "Kind", "Impact", "Item"], rows) if rows else text("Nothing stored for this window.")]
    blocks.append(text("Items are shown as published. One trade can show that an event was present, not that it caused the result: "
                       "that needs many trades (the learning loop tests it)."))
    return {"id": "context", "title": title, "blocks": blocks}


def _playbook_section(review):
    """'What to try next time': measurement plans from this trade, never instructions. None when there are none."""
    from . import dsl

    rows = []
    for s in review.suggestions.filter(kind="EXPERIMENT").order_by("pk"):
        info = s.experiment or {}
        history = (info.get("history_check") or {}).get("verdict", "")
        conditions = " AND ".join(f"{c['metric']} {c['op']} {c['value']}" for c in (info.get("predicate") or {}).get("all", []))
        rows.append([f"{info.get('title')}: {info.get('try')}", f"{info.get('metric', '').replace('outcome.', '')} when [{conditions}] versus when not",
                     info.get("min_n"), history, f"{s.status.lower()} (id {s.pk})"])
    if not rows:
        return None
    return {"id": "playbook", "title": "What to try next time (measurement plans, hypotheses, nothing is changed)", "blocks": [
        text("Each plan compares trades with and without a condition that looked relevant here. None of them is advice: they say how many "
             "matching trades are needed before a difference should be believed, and the history column shows what your past trades already say."),
        table(["Plan", "What is compared", "Matching trades needed", "History on past trades", "Status"], rows)]}


def _selection_section(operator):
    selection = operator.get("selection") or {}
    if not selection:
        return {"id": "selection", "title": "What you selected", "blocks": [text(NOT_CAPTURED)]}
    profile = selection.get("risk_profile") or {}
    rows = [("Ticker", selection.get("ticker")),
            ("Exchange", " / ".join(x for x in (selection.get("exchange"), selection.get("primary_exchange")) if x)),
            ("Currency", selection.get("currency")), ("Asset class", selection.get("asset_class")),
            ("Strategy", f"{selection.get('strategy_name')} ({selection.get('strategy_key')})"),
            ("Timeframe (duration)", selection.get("timeframe")), ("Execution mode", selection.get("execution_mode")),
            ("Portfolio", f"#{selection['portfolio_id']}" if selection.get("portfolio_id") is not None else None),
            ("Risk profile", profile.get("name") or "none (parameters entered individually)")]
    blocks = [kv(rows)]
    sets = profile.get("sets")
    if profile.get("name") and sets:
        blocks.append(text(f"What the {profile['name']} profile sets"))
        blocks.append(table(["Setting", "Value"], [[PROFILE_LABELS.get(k, k), "Yes" if v is True else "No" if v is False else str(v)]
                                                    for k, v in sets.items()]))
        if profile.get("in_sync") is False:
            blocks.append(text("The strategy's running parameters no longer equal this profile's values."))
    target = {k: v for k, v in (selection.get("target_configuration") or {}).items() if k != "risk_profile"}
    if target:
        blocks.append(text("Other selections (target configuration)"))
        blocks.append(table(["Key", "Value"], [[k, str(v)] for k, v in sorted(target.items())]))
    return {"id": "selection", "title": "What you selected", "blocks": blocks}


def _inputs_section(operator, selection_known):
    if not selection_known:
        return {"id": "inputs", "title": "Your inputs around this trade", "blocks": [text(NOT_CAPTURED)]}
    events = operator.get("events") or []
    facts = [("Hours between the last settings change and entry", operator.get("hours_since_last_change_before_entry")),
             ("Settings changes while the trade was open", operator.get("changes_during_hold")),
             ("Risk profile changed while open", "yes" if operator.get("profile_changed_during_hold") else "no"),
             ("Manual orders while open", operator.get("manual_orders_during_hold")),
             ("Actions while open", ", ".join(operator.get("actions_during_hold") or []) or None)]
    blocks = [kv([(k, v) for k, v in facts if v is not None])]
    if not events:
        blocks.append(text("No operator inputs were journalled for this trade."))
        return {"id": "inputs", "title": "Your inputs around this trade", "blocks": blocks}
    rows = []
    for e in events:
        if e["type"] == "INSTANCE_UPDATED":
            detail = "; ".join(f"{k}: {a} -> {b}" for k, (a, b) in (e.get("changes") or {}).items()) or "settings changed"
            detail = f"version {e.get('version')}: {detail}"
        elif e["type"] == "INSTANCE_CREATED":
            detail = f"version {e.get('version')} created; risk profile {e.get('risk_profile') or 'none'}"
        elif e["type"] == "INSTANCE_ACTION":
            detail = f"{e.get('action')} ({e.get('status')})"
        elif e["type"] == "MANUAL_ORDER":
            detail = f"{e.get('side')} {e.get('quantity')} {e.get('symbol')} ({e.get('order_type')})"
        elif e["type"] == "NOTE":
            detail = f'note (quoted operator text): "{e.get("note")}"'
        else:
            detail = ", ".join(f"{k} {v}" for k, v in e.items() if k not in ("at", "type", "actor"))
        rows.append([e["at"], e["type"].replace("_", " ").lower(), e.get("actor"), detail])
    blocks.append(table(["Time", "What", "Who", "Detail"], rows))
    return {"id": "inputs", "title": "Your inputs around this trade", "blocks": blocks}


def _timeline(episode, pack, snapshots, alerts):
    events = []
    ev = pack.get("decision_evidence") or {}
    if ev.get("entry_signal_type"):
        events.append((episode.opened_at.isoformat(), "Signal", f"{ev['entry_signal_type']}: {ev.get('entry_reason', '')}"))
    sizing = (pack.get("execution") or {}).get("sizing")
    if sizing:
        events.append((episode.opened_at.isoformat(), "Sizing", f"binding constraint {sizing['binding_constraint']}, "
                       f"target {sizing['target_quantity']} approved {sizing['approved_quantity']}"))
    for check in (pack.get("execution") or {}).get("risk_checks", []):
        events.append((episode.opened_at.isoformat(), "Risk", f"{check['check']}: {check['decision']} ({check['reason']})"))
    for fill in (pack.get("execution") or {}).get("fills", []):
        ref = f", reference {fill['reference_price']}" if fill.get("reference_price") else ""
        events.append((fill["executed_at"], "Fill", f"{fill['symbol']} {fill['quantity']} @ {fill['price']}{ref}, "
                       f"commission {fill['commission']}, {fill.get('order_type') or '?'} order"))
    if snapshots:
        best = max(snapshots, key=lambda s: s.mark_pnl or Decimal(-10**18))
        worst = min(snapshots, key=lambda s: s.mark_pnl or Decimal(10**18))
        events.append((best.observed_at.isoformat(), "Live", f"best open mark P&L {Decimal(best.mark_pnl).normalize():f}"))
        events.append((worst.observed_at.isoformat(), "Live", f"worst open mark P&L {Decimal(worst.mark_pnl).normalize():f}"))
    for alert in alerts:
        events.append((alert.first_seen.isoformat(), f"Alert {alert.severity}", alert.text))
    if episode.closed_at:
        events.append((episode.closed_at.isoformat(), "Exit", f"{episode.exit_reason or 'unknown'}"
                       + (f" ({episode.exit_subtype})" if episode.exit_subtype else "")))
    events.sort(key=lambda e: e[0])
    return table(["Time (UTC)", "Event", "Detail"], [list(e) for e in events])


def _learned_blocks(episode, review):
    from apps.intelligence.models import LearningEvent

    events = list(LearningEvent.objects.filter(episode=episode).order_by("pk"))
    blocks = []
    created = [e.payload.get("statement") for e in events if e.type == "LESSON_CREATED"]
    reinforced = [e.payload for e in events if e.type == "LESSON_REINFORCED"]
    contradicted = [e.payload for e in events if e.type == "LESSON_CONTRADICTED"]
    if created:
        blocks += [text("New lesson hypotheses proposed by this trade (tested on later trades, not yet evidence):"), bullets(created)]
    if reinforced:
        blocks += [text("Existing lessons this trade supported:"),
                   bullets(f"{p.get('statement')} (now {p.get('status')}, n = {p.get('n')})" for p in reinforced)]
    if contradicted:
        blocks += [text("Existing lessons this trade contradicted:"),
                   bullets(f"{p.get('statement')} (now {p.get('status')}, n = {p.get('n')})" for p in contradicted)]
    if not blocks:
        blocks.append(text("Nothing was learned that changes a lesson: no lesson matched this trade and none was proposed."
                           if review is not None else "The learning loop runs after the trade is closed and reviewed."))
    blocks.append(text("A lesson only counts as established after many trades, confirmed on later trades; one trade is never proof."))
    return blocks


def build_report(episode):
    """Return {"episode_id", "title", "sections": [...]} for a closed or open episode."""
    snapshot = getattr(episode, "evidence_snapshot", None)
    pack = snapshot.pack if snapshot else {}
    metrics = getattr(getattr(episode, "metrics_row", None), "metrics", {}) or {}
    snapshots = list(EpisodeLiveSnapshot.objects.filter(episode=episode).order_by("observed_at"))
    alerts = list(LiveAlert.objects.filter(episode=episode).order_by("first_seen"))
    review = (TradeReview.objects.filter(episode=episode).order_by("-created_at").prefetch_related("mistakes", "suggestions").first())
    cur = episode.currency
    sections = []
    flags = sorted(episode.data_quality_flags or [])
    sections.append({"id": "header", "title": "Trade", "blocks": [kv([
        ("Instrument", episode.instrument_symbol), ("Strategy", f"{episode.instance_name} ({episode.strategy_key} v{episode.strategy_version})"),
        ("Mode", episode.execution_mode), ("Direction", episode.direction.replace("_", " ")),
        ("Status", episode.status), ("Opened", episode.opened_at.isoformat()),
        ("Closed", episode.closed_at.isoformat() if episode.closed_at else "still open"),
        ("Gross P&L", _money(episode.gross_pnl, cur)), ("Costs", _money(episode.commissions, cur)),
        ("Net P&L", _money(episode.net_pnl, cur) + (" (realised so far)" if episode.status == "OPEN" else "")
         + (" (estimated at expiry)" if episode.pnl_basis != "FILLS" else "")),
        ("Exit", (episode.exit_reason or "") + (f" / {episode.exit_subtype}" if episode.exit_subtype else "")),
        ("Data-quality flags", ", ".join(flags))])]})
    operator = _operator_pack(episode, pack)
    sections.append(_selection_section(operator))
    sections.append(_parameters_section(episode, metrics, pack))
    sections.append(_inputs_section(operator, bool(operator.get("selection"))))
    sections.append(_context_section(_context_pack(episode, pack), metrics))
    sections.append({"id": "timeline", "title": "What happened (facts)", "blocks": [_timeline(episode, pack, snapshots, alerts)]})
    if episode.asset_family == "OPTION":
        rows = []
        for label, predicted, actual in (
                ("Probability of profit", metrics.get("opt.pop"), "won" if episode.net_pnl > 0 else "lost" if episode.status != "OPEN" else "open"),
                ("Expected net P&L", metrics.get("opt.net_ev"), _money(episode.net_pnl, cur)),
                ("Breach probability up / down", f"{metrics.get('opt.breach_up')} / {metrics.get('opt.breach_down')}" if metrics.get("opt.breach_up") is not None else None, "see timeline"),
                ("Credit captured", None, metrics.get("opt.credit_capture")),
                ("Share of max profit realised", None, metrics.get("opt.realized_fraction_of_max_profit"))):
            if predicted is not None or actual not in (None, ""):
                rows.append([label, "" if predicted is None else str(predicted), "" if actual in (None, "") else str(actual)])
        sections.append({"id": "predicted-vs-actual", "title": "Predicted vs actual", "blocks": [
            table(["Measure", "Predicted at entry", "Actual"], rows),
            text("One trade cannot show whether a probability was well calibrated; calibration needs many trades.")]})
    why = []
    if review is None:
        why.append(text("No review has been written for this trade yet."))
    else:
        label = "Rule-based review only (the language model was not available)" if review.is_fallback else \
            f"Written by {review.model} using prompt {review.prompt_version}; confidence {review.confidence}; based on n = {review.sample_n} closed trade(s)"
        why.append(text(label))
        why.append(text(review.verdict))
        why.append(text(review.summary))
        if not review.is_fallback and review.sample_n < 8:
            why.append(text("Insufficient evidence for conclusions about the settings: the sample is small and the outcome is mostly noise."))
        why.append(kv([("Process grade", review.process_grade or "not graded"), ("Outcome class", review.outcome_class)]))
    sections.append({"id": "why", "title": "Why it happened (model commentary, cited and checked)", "blocks": why})
    if review is not None:
        sections.append({"id": "mistakes", "title": "Mistakes and findings", "blocks": [table(
            ["Source", "Category", "Severity", "Finding"],
            [[m.source, m.category, m.severity, m.description] for m in review.mistakes.all().order_by("pk")])]})
        if review.strengths:
            sections.append({"id": "strengths", "title": "Strengths", "blocks": [bullets(s["description"] for s in review.strengths)]})
        sugg = [[s.pk, s.kind, s.parameter_key, f"{s.current_value} -> {s.proposed_value}" if s.kind == "PARAMETER" else "",
                 "HYPOTHESIS" if s.is_hypothesis else "defect-based", s.status, s.rationale,
                 f"POST /api/v1/intelligence/suggestions/{s.pk}/accept/ | /acknowledge/ | /reject/"] for s in review.suggestions.exclude(kind="EXPERIMENT").order_by("pk")]
        sections.append({"id": "suggestions", "title": "Suggestions (proposals only; nothing is applied automatically)", "blocks": [
            table(["Id", "Kind", "Parameter", "Change", "Evidence level", "Status", "Why", "Decide"], sugg) if sugg else text("No suggestions.")]})
    if review is not None:
        playbook_section = _playbook_section(review)
        if playbook_section is not None:
            sections.append(playbook_section)
    sections.append({"id": "learned", "title": "What the system learned from this trade", "blocks": _learned_blocks(episode, review)})
    sections.append({"id": "provenance", "title": "Provenance", "blocks": [kv([
        ("Metrics version", getattr(getattr(episode, "metrics_row", None), "metrics_version", None)),
        ("Evidence hash", snapshot.evidence_hash[:16] if snapshot else "not frozen yet (trade open)"),
        ("Live snapshots", str(len(snapshots))), ("Alerts raised", str(len(alerts)))])]})
    return {"episode_id": episode.pk, "title": f"{episode.instrument_symbol} {episode.direction.replace('_', ' ').lower()} trade report",
            "report_version": REPORT_VERSION,
            "provenance": {
                "report_version": REPORT_VERSION, "episode_status": episode.status,
                "metrics_version": getattr(getattr(episode, "metrics_row", None), "metrics_version", None),
                "evidence_hash": snapshot.evidence_hash if snapshot else None,
                "pack_version": pack.get("pack_version"),
                "review_model": getattr(review, "model", None), "review_prompt_version": getattr(review, "prompt_version", None),
                "review_is_fallback": getattr(review, "is_fallback", None)},
            "sections": sections}


def to_markdown(report):
    lines = [f"# {report['title']}", ""]
    for section in report["sections"]:
        lines += [f"## {section['title']}", ""]
        for block in section["blocks"]:
            kind = block["type"]
            if kind == "kv":
                lines += [f"- **{r['label']}:** {r['value']}" for r in block["rows"]]
            elif kind == "text":
                lines.append(block["text"])
            elif kind == "list":
                lines += [f"- {item}" for item in block["items"]]
            elif kind == "table":
                if block["rows"]:
                    lines.append("| " + " | ".join(block["columns"]) + " |")
                    lines.append("|" + "|".join("---" for _ in block["columns"]) + "|")
                    lines += ["| " + " | ".join(str(c).replace("|", "/").replace("\n", " ") for c in row) + " |" for row in block["rows"]]
                else:
                    lines.append("_None._")
            lines.append("")
    return "\n".join(lines).strip() + "\n"
