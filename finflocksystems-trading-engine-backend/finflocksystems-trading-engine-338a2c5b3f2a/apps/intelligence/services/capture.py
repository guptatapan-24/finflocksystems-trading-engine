"""Parameter capture (Phase 4A). Called from small additive try/except hooks in the engine (R13d); it must never
raise into, or slow down, activation or version creation. Everything here is a no-op while the layer is disabled.
"""
import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.intelligence import conf
from apps.intelligence.models import RunConfigSnapshot

from .evidence import sanitize_text, sanitize_tree

logger = logging.getLogger(__name__)
NOTE_LIMIT = 500


def clean_note(value):
    """Operator note: untrusted free text (R8). Length-limited, control characters and delimiters stripped."""
    return sanitize_text(value, NOTE_LIMIT)


def _key(instance_id, version):
    return f"runcfg:{instance_id}:{version}"


def _data_source_flags(instance):
    session = getattr(instance.portfolio, "gateway_session", None)
    return {
        "remote_feature_db": bool(getattr(settings, "REMOTE_FEATURE_DB_URL", "")),
        "option_chain_provider": str(getattr(settings, "OPTION_CHAIN_PROVIDER", "")),
        "option_pretrade_risk_enabled": bool(getattr(settings, "OPTION_PRETRADE_RISK_ENABLED", True)),
        "market_data_type": str(getattr(session, "market_data_type", "") or ""),
        "delayed_data": str(getattr(session, "market_data_type", "")).upper() == "DELAYED",
        "kafka_enabled": bool(getattr(settings, "KAFKA_ENABLED", False)),
    }


def _regime(instance):
    """Realised-volatility percentile of the instrument's own bars at activation time, when enough bars exist."""
    try:
        from datetime import timedelta

        from apps.intelligence.services.metrics import _latest_bars

        now = timezone.now()
        bars = _latest_bars(instance, now - timedelta(days=400), now)
        closes = [float(b.close) for b in bars][-260:]
        if len(closes) < 60:
            return {"available": False, "bars": len(closes)}
        import numpy as np

        returns = np.diff(np.log(np.array(closes)))
        vols = np.array([returns[i - 20:i].std() for i in range(20, len(returns) + 1)])
        return {"available": True, "bars": len(closes), "realized_vol_20": float(vols[-1]),
                "vol_pctile": float((vols[:-1] <= vols[-1]).mean() * 100.0) if len(vols) > 1 else None}
    except Exception:
        return {"available": False}


def _risk_profile_block(instance):
    """The risk profile in force and what it sets, from the engine's own module (never a copy of its numbers).
    `in_sync` says whether the running parameters still equal the profile's resolved values."""
    profile = (instance.target_configuration or {}).get("risk_profile")
    if not profile:
        return {"name": None}
    block = {"name": str(profile)[:24]}
    try:
        from apps.strategies import risk_profiles as profiles

        resolved = profiles.resolve_parameters(profile)
        block["sets"] = {profiles.SPEC_TO_SCHEMA.get(key, key): resolved.get(profiles.SPEC_TO_SCHEMA.get(key, key))
                         for key, _label in profiles.SUMMARY_FIELDS}
        block["in_sync"] = all((instance.parameters or {}).get(k) == v for k, v in resolved.items())
    except Exception:
        block["sets"] = None  # profile module unavailable or the profile name is no longer valid
    return block


def selection_for(instance):
    """What the operator chose for this strategy instance, read from the saved rows (the truth), sanitised."""
    instrument = getattr(instance, "instrument", None)
    selection = {
        "ticker": getattr(instrument, "symbol", None), "exchange": getattr(instrument, "exchange", None),
        "primary_exchange": getattr(instrument, "primary_exchange", None) or None,
        "currency": getattr(instrument, "currency", None), "asset_class": getattr(instrument, "asset_class", None),
        "strategy_key": instance.definition.key, "strategy_name": instance.definition.name,
        "timeframe": instance.timeframe, "execution_mode": instance.execution_mode,
        # the portfolio NAME is deliberately not stored: names are often built from the broker account id (R8)
        "portfolio_id": instance.portfolio_id,
        "risk_policy_id": instance.risk_policy_id, "order_policy_id": instance.order_policy_id,
        "target_configuration": instance.target_configuration or {},
        "risk_profile": _risk_profile_block(instance),
    }
    return sanitize_tree(selection, 200)


def capture_run_config(instance, version=None, trigger="ACTIVATION", intent_note=None):
    """Create or update the snapshot for (instance, version). Returns it, or None. Never raises."""
    if not conf.enabled():
        return None
    try:
        with transaction.atomic():  # savepoint: a failure here cannot poison the engine's transaction
            if version is None:
                version = instance.versions.get(version=instance.version)
            key = _key(instance.pk, version.version)
            now = timezone.now()
            defaults = dict(
                strategy_key=instance.definition.key, instance_name=instance.name[:128], strategy_version_ref=version,
                parameter_hash=version.parameter_hash, execution_mode=instance.execution_mode)
            snapshot, created = RunConfigSnapshot.objects.get_or_create(
                idempotency_key=key, defaults={**defaults, "instance_id": instance.pk,
                                               "strategy_version": version.version})
            snapshot.data_source_flags = _data_source_flags(instance)
            snapshot.selection = selection_for(instance)
            if trigger == "ACTIVATION" or created:
                snapshot.regime_at_activation = _regime(instance)
            log = list(snapshot.activation_log or [])
            log.append({"trigger": trigger, "at": now.isoformat()})
            snapshot.activation_log = log[-20:]
            if intent_note is not None:
                snapshot.intent_note = clean_note(intent_note)
            snapshot.save()
            return snapshot
    except Exception:
        logger.exception("intelligence: run-config capture failed for instance %s", getattr(instance, "pk", None))
        return None


def attach_intent_note(instance, note):
    """Store the operator's intent note on the current version's snapshot (created if needed). Never raises."""
    if not conf.enabled() or note in (None, ""):
        return None
    return capture_run_config(instance, None, "INTENT_NOTE", intent_note=note)


def record_request(instance, payload, trigger, actor=None, idempotency_key=None):
    """Phase 8 entry point for the engine's create / change / enable hooks. Journals the whole request (whitelisted,
    sanitised), the note, and refreshes the run-config snapshot with what was actually saved. Never raises."""
    if not conf.enabled():
        return None
    try:
        from . import journal

        data = payload if isinstance(payload, dict) else {}
        journal.record(journal.OperatorEvent.Type.REQUEST, instance=instance, actor=actor,
                       payload={"trigger": trigger, "request": journal.request_view(data)},
                       idempotency_key=f"request:{idempotency_key}:{trigger}" if idempotency_key else None)
        note = data.get("intent_note")
        if note not in (None, ""):
            journal.record_note(instance, note, actor=actor,
                                idempotency_key=f"note:{idempotency_key}:{trigger}" if idempotency_key else None)
        return capture_run_config(instance, None, trigger, intent_note=note if note not in (None, "") else None)
    except Exception:
        logger.exception("intelligence: request capture failed for instance %s", getattr(instance, "pk", None))
        return None


def snapshot_for(instance_id, version):
    return RunConfigSnapshot.objects.filter(instance_id=instance_id, strategy_version=version).first()


def attach_advisory(instance, summary):
    """Remember which advisory was shown for the current version (R: advisory_shown on the run-config snapshot)."""
    if not conf.enabled():
        return None
    try:
        with transaction.atomic():
            snapshot = capture_run_config(instance, None, "ADVISORY")
            if snapshot is not None:
                snapshot.advisory_shown = summary
                snapshot.save(update_fields=["advisory_shown", "updated_at"])
            return snapshot
    except Exception:
        logger.exception("intelligence: could not attach advisory to snapshot")
        return None
