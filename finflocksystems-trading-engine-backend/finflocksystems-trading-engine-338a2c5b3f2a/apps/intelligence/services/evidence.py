"""Frozen evidence pack (R6, R8). Built field by field from an explicit whitelist; there is no model_to_dict.

Never included: broker account ids, contract ids (conid), gateway session ids, tokens, raw broker events,
engine idempotency keys, session-prefixed execution ids. Free text (names, symbols, reasons) is sanitised and
capped so it cannot carry instructions to a model (prompt-injection hardening; tests cover it).
"""
import hashlib
import json
import re

from django.utils import timezone

PACK_VERSION = "3"
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_text(value, limit=120):
    text = _CONTROL.sub(" ", str(value or ""))
    for token in ("```", "<<<", ">>>"):
        text = text.replace(token, "'''")
    text = text.strip()
    return text[:limit]


def sanitize_tree(value, limit=300):
    """Sanitise EVERY string in a structure (values and keys): names, symbols, reasons, parameters, engine JSON.
    Nothing that reaches a model prompt may carry control characters, code fences or our data-block markers."""
    if isinstance(value, str):
        return sanitize_text(value, limit)
    if isinstance(value, dict):
        return {sanitize_text(k, 96): sanitize_tree(v, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_tree(v, limit) for v in value]
    return value


def sanitize_values(mapping, limit=300):
    """Flat fact maps keep their (engine-defined) keys; only the values are sanitised."""
    return {k: sanitize_tree(v, limit) for k, v in mapping.items()}


def _jsonable(value):
    return json.loads(json.dumps(value, default=str))


def _option_snapshot_block(snapshot):
    if snapshot is None:
        return None
    source = snapshot.source_snapshot or {}
    rankings = []
    for row in (snapshot.candidate_rankings or [])[:6]:
        if isinstance(row, dict):
            rankings.append({k: row.get(k) for k in (
                "strategy_key", "strategy", "eligible", "tqs", "tqs_factor_coverage", "tqs_unmeasured_factors",
                "decision_band", "size_multiplier", "pop", "net_expected_pnl", "expected_pnl", "max_profit",
                "max_loss", "upper_breach_probability", "lower_breach_probability", "rejection_reason",
                "entry_signal", "factors", "gate_failures") if k in row})
    return {
        "selected_strategy": sanitize_text(snapshot.selected_strategy, 64),
        "model_version": sanitize_text(snapshot.model_version, 64),
        "entry_score": str(snapshot.entry_score), "spot": str(snapshot.spot),
        "atr_value": str(snapshot.atr_value), "expiration": snapshot.expiration.isoformat(),
        "rankings": rankings,
        "rps_observation": source.get("rps_observation") or {},
        "selection_evidence": source.get("selection_evidence") or {},
        "entry_threshold": source.get("entry_threshold"),
        "entry_score_weights": source.get("entry_score_weights"),
        "exit_baseline": source.get("exit_baseline"),
    }


def build_pack(episode, instance, summary, ctx, entry_info, exit_info, metrics):
    from apps.risk.models import RiskCheckResult

    entry_run, snapshot, _linked = entry_info
    fills = []
    for index, part in enumerate(summary["parts"], start=1):
        intent = None
        fill = ctx["fills"].get(part.get("origin_execution_id") or part["execution_id"])
        if fill is not None:
            intent = fill.order.intent
        fills.append({
            "n": index, "symbol": sanitize_text(part["symbol"], 64), "quantity": str(part["quantity"]),
            "price": str(part["price"]), "commission": str(part["commission"]),
            "executed_at": part["time"].isoformat(), "currency": part["currency"],
            "order_type": getattr(intent, "order_type", None), "origin": getattr(intent, "origin", None),
            "reference_price": str(intent.reference_price) if intent and intent.reference_price else None,
        })
    risk = []
    intents = [i for i in {summary["first_intent"], summary["last_intent"]} if i is not None]
    for row in RiskCheckResult.objects.filter(order_intent__in=intents).order_by("pk")[:30]:
        risk.append({"check": row.check_name, "decision": row.decision, "reason": sanitize_text(row.reason, 200),
                     "requested": str(row.requested_quantity), "approved": str(row.approved_quantity)})
    sizing = None
    first = summary["first_intent"]
    try:
        decision = first.sizing_decision if first is not None else None
    except Exception:
        decision = None
    if decision is not None:
        sizing = {"binding_constraint": decision.binding_constraint, "target_quantity": str(decision.target_quantity),
                  "approved_quantity": str(decision.approved_quantity), "risk_budget": str(decision.risk_budget),
                  "entry_price": str(decision.entry_price),
                  "stop_price": str(decision.stop_price) if decision.stop_price is not None else None}
    entry_signal = getattr(entry_run, "signal", None) if entry_run is not None else None
    pack = {
        "pack_version": PACK_VERSION,
        "episode": {
            "strategy_key": instance.definition.key, "strategy_version": summary["version_number"],
            "instance_name": sanitize_text(instance.name, 64), "instrument": sanitize_text(instance.instrument.symbol, 32),
            "asset_family": summary["family"], "execution_mode": instance.execution_mode,
            "currency": summary["currency"], "direction": summary["direction"], "timeframe": instance.timeframe,
            "opened_at": summary["opened_at"].isoformat(),
            "closed_at": summary["closed_at"].isoformat() if summary["closed_at"] else None,
            "gross_pnl": str(summary["gross"]), "commissions": str(summary["commissions"]),
            "net_pnl": str(summary["net"]), "exit_reason": exit_info[0], "exit_subtype": sanitize_text(exit_info[1], 64),
        },
        "params": _jsonable(instance.parameters or {}),
        "param_schema": _jsonable(instance.definition.parameter_schema or {}),
        "decision_evidence": {
            "entry_signal_type": getattr(entry_signal, "signal_type", None),
            "entry_reason": sanitize_text(getattr(entry_signal, "reason", ""), 200),
            "entry_context": _jsonable({
                "indicators": ((entry_run.context_snapshot or {}).get("indicators") if entry_run else None),
                "previous_indicators": ((entry_run.context_snapshot or {}).get("previous_indicators") if entry_run else None),
                "close": (((entry_run.context_snapshot or {}).get("bar") or {}).get("close") if entry_run else None),
            }),
            "option_selection": _jsonable(_option_snapshot_block(snapshot)),
        },
        "execution": {
            "fills": fills, "sizing": sizing, "risk_checks": risk,
            "legs": [{"symbol": sanitize_text(leg["symbol"], 64), "entry_side": leg["entry_side"],
                      "entry_qty": str(leg["entry_qty"]), "exit_qty": str(leg["exit_qty"]),
                      "realized_pnl": str(leg["realized"])} for leg in summary["legs"].values()],
        },
        "metrics": _jsonable(metrics),
        "run_config": _run_config_block(instance.pk, summary["version_number"]),
        "operator": operator_block(instance, summary["opened_at"], summary["closed_at"], instance.instrument.symbol,
                                   summary.get("version_number")),
        "context": context_block(instance, summary, entry_info),
        "data_quality_flags": sorted(episode.data_quality_flags or []),
    }
    pack = sanitize_tree(pack)
    digest = hashlib.sha256(json.dumps(pack, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return pack, digest


JOURNAL_LOOKBACK_HOURS = 72
JOURNAL_EVENT_LIMIT = 40
CHANGE_KEYS_PER_EVENT = 12


def _event_line(event):
    """One journal event as a compact, citable dict. Free text is already sanitised; notes are tagged as quoted data."""
    payload = event.payload or {}
    line = {"at": event.occurred_at.isoformat(), "type": event.event_type, "actor": event.actor}
    if event.event_type == "INSTANCE_UPDATED":
        changes = payload.get("changes") or {}
        line["version"] = payload.get("version")
        line["changes"] = {k: changes[k] for k in list(changes)[:CHANGE_KEYS_PER_EVENT]}
    elif event.event_type == "INSTANCE_CREATED":
        line["version"] = payload.get("version")
        line["risk_profile"] = payload.get("risk_profile")
    elif event.event_type == "INSTANCE_ACTION":
        line["action"], line["status"] = payload.get("action"), payload.get("status")
    elif event.event_type == "MANUAL_ORDER":
        line.update({k: payload.get(k) for k in ("symbol", "side", "quantity", "order_type")})
    elif event.event_type == "NOTE":
        line["note"] = payload.get("note")
    elif event.event_type in ("SUGGESTION_DECISION", "ADVISORY_ACK", "REVIEW_FEEDBACK"):
        line.update({k: v for k, v in payload.items() if k in ("decision", "suggestion_id", "rating", "status")})
    return line


def context_block(instance, summary, entry_info):
    """Outside context as it stood around this trade (R18), from stored items only. Never raises."""
    try:
        from .context_view import compute

        snapshot = entry_info[1] if entry_info else None
        return compute(instance, summary["opened_at"], summary["closed_at"], getattr(snapshot, "expiration", None))["block"]
    except Exception:
        return {"available": False, "reason": "context could not be read"}


def operator_block(instance, opened_at, closed_at, symbol, version=None):
    """What the operator chose and did around this trade: selections, the risk profile in force, journal slice and
    deterministic timing facts (hours since the last change before entry, changes / manual orders during the hold)."""
    from datetime import timedelta

    from apps.intelligence.models import OperatorEvent

    from .capture import snapshot_for

    end = closed_at or timezone.now()
    events = list(OperatorEvent.objects.filter(
        instance_id=instance.pk, occurred_at__gte=opened_at - timedelta(hours=JOURNAL_LOOKBACK_HOURS),
        occurred_at__lte=end).exclude(event_type="REQUEST").order_by("occurred_at", "pk")[:JOURNAL_EVENT_LIMIT])
    # manual orders carry no instance when placed outside a strategy: match them by symbol over the hold only
    loose = OperatorEvent.objects.filter(event_type="MANUAL_ORDER", instance_id__isnull=True,
                                         instrument_symbol=symbol, occurred_at__gte=opened_at, occurred_at__lte=end)
    events = sorted(events + list(loose[:JOURNAL_EVENT_LIMIT]), key=lambda e: (e.occurred_at, e.pk))[:JOURNAL_EVENT_LIMIT]
    before = [e for e in events if e.event_type == "INSTANCE_UPDATED" and e.occurred_at < opened_at]
    during = [e for e in events if opened_at <= e.occurred_at <= end]
    profile_changed = any(
        any(str(k).startswith("target_configuration.risk_profile") for k in (e.payload.get("changes") or {}))
        for e in during if e.event_type == "INSTANCE_UPDATED")
    block = {
        "event_count": len(events),
        "hours_since_last_change_before_entry": (round((opened_at - before[-1].occurred_at).total_seconds() / 3600.0, 2)
                                                 if before else None),
        "changes_during_hold": sum(1 for e in during if e.event_type == "INSTANCE_UPDATED"),
        "profile_changed_during_hold": profile_changed,
        "manual_orders_during_hold": sum(1 for e in during if e.event_type == "MANUAL_ORDER"),
        "actions_during_hold": sorted({(e.payload or {}).get("action") for e in during
                                       if e.event_type == "INSTANCE_ACTION" and (e.payload or {}).get("action")}),
        "events": [_event_line(e) for e in events],
    }
    snap = snapshot_for(instance.pk, version or instance.version)
    selection = (snap.selection if snap is not None else None) or {}
    block["selection"] = selection
    block["profile"] = selection.get("risk_profile") or {"name": None}
    return block


def _run_config_block(instance_id, version):
    from .capture import snapshot_for

    snap = snapshot_for(instance_id, version)
    if snap is None:
        return {}
    block = {"data_source_flags": snap.data_source_flags, "regime_at_activation": snap.regime_at_activation,
             "parameter_hash": snap.parameter_hash}
    if snap.intent_note:  # untrusted operator text: sanitised, only ever shown to a model as quoted data (R8)
        block["intent_note"] = sanitize_text(snap.intent_note, 500)
    return _jsonable(block)


def build_expiry_pack(episode):
    """Minimal frozen pack for an episode closed by the expiry sweeper (no closing fill exists)."""
    metrics = getattr(episode, "metrics_row", None)
    legs = [{"symbol": sanitize_text(leg.symbol, 64), "right": leg.right,
             "strike": str(leg.strike) if leg.strike is not None else None,
             "expiration": leg.expiration.isoformat() if leg.expiration else None,
             "entry_side": leg.entry_side, "entry_qty": str(leg.entry_quantity),
             "exit_qty": str(leg.exit_quantity), "entry_avg_price": str(leg.entry_avg_price)}
            for leg in episode.legs.all()]
    pack = {
        "pack_version": PACK_VERSION,
        "episode": {
            "strategy_key": episode.strategy_key, "strategy_version": episode.strategy_version,
            "instance_name": sanitize_text(episode.instance_name, 64),
            "instrument": sanitize_text(episode.instrument_symbol, 32), "asset_family": episode.asset_family,
            "execution_mode": episode.execution_mode, "currency": episode.currency, "direction": episode.direction,
            "opened_at": episode.opened_at.isoformat(), "closed_at": episode.closed_at.isoformat(),
            "gross_pnl": str(episode.gross_pnl), "commissions": str(episode.commissions),
            "net_pnl": str(episode.net_pnl), "exit_reason": episode.exit_reason, "pnl_basis": episode.pnl_basis,
        },
        "execution": {"legs": legs},
        "metrics": _jsonable(metrics.metrics if metrics is not None else {}),
        "data_quality_flags": sorted(episode.data_quality_flags or []),
    }
    pack = sanitize_tree(pack)
    digest = hashlib.sha256(json.dumps(pack, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return pack, digest
