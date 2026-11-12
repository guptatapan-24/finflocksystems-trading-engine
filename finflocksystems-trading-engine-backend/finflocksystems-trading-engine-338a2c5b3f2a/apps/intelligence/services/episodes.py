"""Trade episodes derived from attributed fills (R4, R5, R12, R13).

An episode is one round trip of one strategy instance: from its attributed exposure leaving flat to it
returning flat (STOCK: one instrument, a sign flip closes one episode and opens the next; OPTION: all legs of the
structure). Episodes are rebuilt idempotently from the engine's own per-strategy fill attribution
(`reporting.attributed_fills`), using the same weighted-average-cost arithmetic (`reporting._apply`) the ledger
and the strategy P&L endpoint use, so the numbers reconcile. This module only reads engine tables.
"""
import hashlib
import json
import logging
from collections import defaultdict
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.intelligence.models import (
    EpisodeEvidenceSnapshot, EpisodeMetrics, TradeEpisode, TradeEpisodeLeg,
)

logger = logging.getLogger(__name__)

D = Decimal
ZERO = Decimal(0)
BUILDER_VERSION = "1"

ENTRY_SIGNALS = {"ENTER_LONG", "ENTER_OPTIONS", "ENTER_SHORT", "SET_TARGET"}

# flags
PAPER_DELAYED_DATA = "PAPER_DELAYED_DATA"
ATTRIBUTION_DRIFT_POSSIBLE = "ATTRIBUTION_DRIFT_POSSIBLE"
MISSING_REMOTE_FEATURES = "MISSING_REMOTE_FEATURES"
OPTION_RISK_BYPASS = "OPTION_RISK_BYPASS"
RECONCILIATION_BREAK_DURING_TRADE = "RECONCILIATION_BREAK_DURING_TRADE"
MANUAL_INTERVENTION = "MANUAL_INTERVENTION"
PARTIAL_FILLS = "PARTIAL_FILLS"
OPTION_PATH_APPROXIMATE = "OPTION_PATH_APPROXIMATE"
OPTION_PATH_UNAVAILABLE = "OPTION_PATH_UNAVAILABLE"
OPTION_SNAPSHOT_UNLINKED = "OPTION_SNAPSHOT_UNLINKED"
MIXED_CURRENCY = "MIXED_CURRENCY"


def _dec(value, default=ZERO):
    try:
        return D(str(value))
    except Exception:
        return default


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


# ------------------------------------------------------------------------------------------------
# segmentation
# ------------------------------------------------------------------------------------------------
def segment_events(events, single_instrument):
    """Split time-ordered attributed fills into [(parts, closed)].

    A part is a fill (or a piece of a fill). With `single_instrument` (stocks) a fill that crosses zero is split:
    the closing piece ends one episode and the remainder opens the next, priced at the fill price with the
    commission pro rata by quantity.
    """
    quantities = defaultdict(lambda: ZERO)
    episodes, current = [], None
    for event in events:
        pieces = [event]
        prior = quantities[event["instrument_id"]]
        qty = event["quantity"]
        if single_instrument and prior and prior * qty < 0 and abs(qty) > abs(prior):
            closing_qty = -prior
            fraction = abs(closing_qty) / abs(qty)
            pieces = [
                {**event, "quantity": closing_qty, "commission": event["commission"] * fraction},
                {**event, "quantity": qty + prior, "commission": event["commission"] * (1 - fraction),
                 "execution_id": f"{event['execution_id']}#flip", "origin_execution_id": event["execution_id"]},
            ]
        for piece in pieces:
            if all(v == 0 for v in quantities.values()):
                current = []
            quantities[piece["instrument_id"]] += piece["quantity"]
            current.append(piece)
            if all(v == 0 for v in quantities.values()):
                episodes.append((current, True))
                current = None
    if current:
        episodes.append((current, False))
    return episodes


# ------------------------------------------------------------------------------------------------
# context loading (read-only)
# ------------------------------------------------------------------------------------------------
def _load_context(instance, events):
    from apps.allocation.models import OrderIntentAttribution
    from apps.execution.models import Fill
    from apps.instruments.models import Instrument

    exec_ids = {e.get("origin_execution_id") or e["execution_id"] for e in events}
    exec_ids |= {e["execution_id"] for e in events}
    fills = {f.execution_id: f for f in Fill.objects.filter(execution_id__in=exec_ids)
             .select_related("order__intent")}
    intent_ids = {f.order.intent_id for f in fills.values()}
    versions = {a.order_intent_id: a for a in OrderIntentAttribution.objects.filter(
        strategy_instance=instance, order_intent_id__in=intent_ids).select_related("strategy_version")}
    instruments = {i.pk: i for i in Instrument.objects.filter(pk__in={e["instrument_id"] for e in events})}
    return {"fills": fills, "attributions": versions, "instruments": instruments}


def _intent_for(ctx, event):
    fill = ctx["fills"].get(event.get("origin_execution_id") or event["execution_id"])
    return fill.order.intent if fill else None


def _option_details(instrument):
    try:
        return instrument.option_contract
    except Exception:
        return None


# ------------------------------------------------------------------------------------------------
# summarisation of one episode
# ------------------------------------------------------------------------------------------------
def summarize(instance, parts, closed, ctx):
    from apps.strategies.reporting import _apply

    instruments = ctx["instruments"]
    family = "OPTION" if any(getattr(instruments.get(p["instrument_id"]), "asset_class", "") == "OPT"
                             for p in parts) else "STOCK"
    books = defaultdict(lambda: {"quantity": ZERO, "average": ZERO, "last_price": ZERO, "multiplier": D(1)})
    legs = {}
    gross = commissions = ZERO
    currencies = []
    for part in parts:
        iid = part["instrument_id"]
        book = books[iid]
        prior_abs = abs(book["quantity"])
        realized = _apply(book, part)
        after_abs = abs(book["quantity"])
        gross += realized
        commissions += part["commission"]
        currencies.append(part["currency"])
        qty, price = part["quantity"], part["price"]
        intent = _intent_for(ctx, part)
        ref = _dec(intent.reference_price, None) if intent and intent.reference_price else None
        leg = legs.setdefault(iid, {
            "instrument_id": iid, "symbol": part["symbol"], "multiplier": part["multiplier"],
            "entry_side": "BUY" if qty > 0 else "SELL", "entry_qty": ZERO, "entry_notional": ZERO,
            "exit_qty": ZERO, "exit_notional": ZERO, "realized": ZERO, "commission": ZERO,
            "entry_slip_w": ZERO, "entry_slip_q": ZERO, "exit_slip_w": ZERO, "exit_slip_q": ZERO,
            "entry_ref": None, "exit_ref": None, "first_at": part["time"], "last_at": part["time"]})
        leg["realized"] += realized
        leg["commission"] += part["commission"]
        leg["last_at"] = part["time"]
        is_entry = after_abs > prior_abs
        side = "BUY" if qty > 0 else "SELL"
        slip = None
        if ref:
            slip = ((price - ref) / ref if side == "BUY" else (ref - price) / ref) * D(10000)
        if is_entry:
            leg["entry_qty"] += abs(qty)
            leg["entry_notional"] += abs(qty) * price
            if leg["entry_ref"] is None:
                leg["entry_ref"] = ref
            if slip is not None:
                leg["entry_slip_w"] += slip * abs(qty)
                leg["entry_slip_q"] += abs(qty)
        else:
            leg["exit_qty"] += abs(qty)
            leg["exit_notional"] += abs(qty) * price
            leg["exit_ref"] = ref
            if slip is not None:
                leg["exit_slip_w"] += slip * abs(qty)
                leg["exit_slip_q"] += abs(qty)
    first, last = parts[0], parts[-1]
    first_intent, last_intent = _intent_for(ctx, first), _intent_for(ctx, last)
    attribution = ctx["attributions"].get(first_intent.pk) if first_intent else None
    version_number = (attribution.strategy_version.version if attribution and attribution.strategy_version_id
                      else instance.version)
    currency = currencies[0] if currencies else instance.instrument.currency
    flags = set()
    if len(set(currencies)) > 1:
        flags.add(MIXED_CURRENCY)
    entry_notional = sum((l["entry_notional"] * l["multiplier"] for l in legs.values()), ZERO)
    if family == "STOCK":
        direction = "LONG" if first["quantity"] > 0 else "SHORT"
    else:
        direction = "NET_CREDIT" if _entry_cash(parts) > 0 else "NET_DEBIT"
    return {
        "family": family, "parts": parts, "closed": closed, "legs": legs, "gross": gross,
        "commissions": commissions, "net": gross - commissions, "currency": currency,
        "version_number": version_number, "direction": direction, "entry_notional": entry_notional,
        "first_intent": first_intent, "last_intent": last_intent, "flags": flags,
        "opened_at": first["time"], "closed_at": last["time"] if closed else None,
        "first_exec": first["execution_id"], "last_exec": last["execution_id"],
        "attribution": attribution,
    }


def _entry_cash(parts):
    """Net cash of the opening fills (positive = credit). Opening = the fills before exposure first returns
    to its peak, approximated as every fill that moves its instrument away from zero."""
    pos = defaultdict(lambda: ZERO)
    cash = ZERO
    for p in parts:
        before = abs(pos[p["instrument_id"]])
        pos[p["instrument_id"]] += p["quantity"]
        if abs(pos[p["instrument_id"]]) > before:
            cash += -p["quantity"] * p["price"] * p["multiplier"]
    return cash


# ------------------------------------------------------------------------------------------------
# exit reason and entry linkage (heuristics, documented in DESIGN.md)
# ------------------------------------------------------------------------------------------------
def resolve_exit(instance, summary):
    """Return (exit_reason, subtype, exit_run)."""
    from apps.strategies.models import StrategyRun, StrategySignal

    intent = summary["last_intent"]
    if intent is None:
        return "UNKNOWN", "", None
    if intent.origin == "MANUAL":
        return "MANUAL", "", None
    if intent.origin == "BROKER_IMPORT":
        return "BROKER_IMPORT", "", None
    run = (StrategyRun.objects.filter(strategy_instance=instance, started_at__lte=intent.created_at)
           .select_related("signal").order_by("-started_at", "-pk").first())
    if run is None:
        return "UNKNOWN", "", None
    if (run.context_snapshot or {}).get("operator_action") == "FLATTEN":
        return "FLATTEN", "", run
    signal = getattr(run, "signal", None)
    reason = (signal.reason if signal else "") or ""
    stype = signal.signal_type if signal else ""
    if reason == "Contract expiry exit":
        return "EXPIRY_RULE", "", run
    if reason == "Strategy hold-period exit":
        return "HOLD_PERIOD", "", run
    if reason.startswith("Approved soft exit:"):
        codes = reason.split(":", 1)[1].strip()
        return "SOFT_EXIT", codes.split(";")[0].strip()[:64], run
    if stype.startswith("EXIT"):
        return "SIGNAL", "", run
    if instance.state == "KILLED" or instance.kill_switch:
        return "KILL_SWITCH", "", run
    return "UNKNOWN", "", run


def resolve_entry(instance, summary):
    """Return (entry_run, snapshot, linked) for the first entry intent."""
    from apps.strategies.models import OptionSelectionSnapshot, StrategyRun

    intent = summary["first_intent"]
    if intent is None:
        return None, None, False
    run = (StrategyRun.objects.filter(strategy_instance=instance, started_at__lte=intent.created_at,
                                      signal__signal_type__in=ENTRY_SIGNALS)
           .select_related("signal").order_by("-started_at", "-pk").first())
    snapshot, linked = None, False
    if summary["family"] == "OPTION":
        sid = None
        if run is not None:
            context = run.context_snapshot or {}
            sid = (((context.get("bar") or {}).get("option_context") or {}).get("selection_snapshot_id")
                   or (context.get("strategy_state_data") or {}).get("selection_snapshot_id"))
        if sid:
            snapshot = OptionSelectionSnapshot.objects.filter(pk=int(sid)).first()
            linked = snapshot is not None
        if snapshot is None:
            snapshot = (OptionSelectionSnapshot.objects.filter(strategy_instance=instance,
                                                              created_at__lte=intent.created_at)
                        .order_by("-created_at", "-pk").first())
    return run, snapshot, linked


# ------------------------------------------------------------------------------------------------
# data-quality flags
# ------------------------------------------------------------------------------------------------
def quality_flags(instance, summary, snapshot, linked):
    from apps.reconciliation.models import ReconciliationBreak
    from apps.risk.models import RiskCheckResult
    from apps.oms.models import OrderIntent

    flags = set(summary["flags"])
    session = getattr(instance.portfolio, "gateway_session", None)
    if instance.execution_mode == "PAPER" and session is not None and \
            str(getattr(session, "market_data_type", "")).upper() == "DELAYED":
        flags.add(PAPER_DELAYED_DATA)
    intents = [i for i in {summary["first_intent"], summary["last_intent"]} if i is not None]
    if RiskCheckResult.objects.filter(order_intent__in=intents, decision="RESIZED").exists():
        flags.add(ATTRIBUTION_DRIFT_POSSIBLE)
    opened, closed = summary["opened_at"], summary["closed_at"] or timezone.now()
    if ReconciliationBreak.objects.filter(material=True, created_at__gte=opened, created_at__lte=closed).exists():
        flags.add(RECONCILIATION_BREAK_DURING_TRADE)
    instrument_ids = list(summary["legs"].keys())
    if OrderIntent.objects.filter(portfolio_id=instance.portfolio_id, instrument_id__in=instrument_ids,
                                  origin__in=["MANUAL", "BROKER_IMPORT"],
                                  created_at__gte=opened, created_at__lte=closed).exists():
        flags.add(MANUAL_INTERVENTION)
    executions_by_order = defaultdict(set)
    for part in summary["parts"]:
        executions_by_order[part.get("order_id")].add(part.get("origin_execution_id") or part["execution_id"])
    if any(len(ids) > 1 for order_id, ids in executions_by_order.items() if order_id):
        flags.add(PARTIAL_FILLS)
    if summary["family"] == "OPTION":
        if not getattr(settings, "REMOTE_FEATURE_DB_URL", ""):
            flags.add(MISSING_REMOTE_FEATURES)
        if not getattr(settings, "OPTION_PRETRADE_RISK_ENABLED", True):
            flags.add(OPTION_RISK_BYPASS)
        flags.add(OPTION_PATH_UNAVAILABLE)  # no live snapshots until Phase 4
        if not linked:
            flags.add(OPTION_SNAPSHOT_UNLINKED)
    return flags


# ------------------------------------------------------------------------------------------------
# persistence
# ------------------------------------------------------------------------------------------------
def _fingerprint(parts, closed):
    return _hash([[p["execution_id"], str(p["quantity"]), str(p["price"]), str(p["commission"])] for p in parts]
                 + [closed])


def _persist(instance, summary, ctx, exit_info, entry_info, flags, metrics, metrics_version, pack_builder):
    reason, subtype, exit_run = exit_info if summary["closed"] else ("", "", None)
    entry_run, snapshot, _linked = entry_info
    key = f"episode:{instance.pk}:{summary['version_number']}:{summary['first_exec']}"
    status = TradeEpisode.Status.CLOSED if summary["closed"] else TradeEpisode.Status.OPEN
    hold = None
    if summary["closed"]:
        hold = int((summary["closed_at"] - summary["opened_at"]).total_seconds())
    entry_notional = summary["entry_notional"]
    risk_basis = metrics.get("outcome.risk_basis")
    ror = None
    basis = _dec(risk_basis, None) if risk_basis else (entry_notional or None)
    if basis:
        ror = (summary["net"] / basis).quantize(D("0.000001"))
    defaults = dict(
        strategy_key=instance.definition.key, instance_id=instance.pk, instance_name=instance.name[:128],
        strategy_version=summary["version_number"], portfolio_id=instance.portfolio_id,
        instrument_id=instance.instrument_id, instrument_symbol=instance.instrument.symbol,
        asset_family=summary["family"], execution_mode=instance.execution_mode, currency=summary["currency"],
        direction=summary["direction"], strategy_instance=instance, entry_run=entry_run,
        option_selection_snapshot=snapshot,
        strategy_version_ref=summary["attribution"].strategy_version if summary["attribution"] else None,
        status=status, opened_at=summary["opened_at"], closed_at=summary["closed_at"],
        gross_pnl=summary["gross"], commissions=summary["commissions"], net_pnl=summary["net"],
        return_on_risk=ror, hold_seconds=hold, exit_reason=reason, exit_subtype=subtype,
        first_fill_execution_id=summary["first_exec"], last_fill_execution_id=summary["last_exec"],
        fill_count=len(summary["parts"]), fills_fingerprint=_fingerprint(summary["parts"], summary["closed"]),
        metrics_version_built=metrics_version,
    )
    from .capture import snapshot_for

    run_config = snapshot_for(instance.pk, summary["version_number"])
    defaults["run_config_snapshot_id"] = run_config.pk if run_config is not None else None
    with transaction.atomic():
        episode = TradeEpisode.objects.filter(idempotency_key=key).first()
        merged_flags = sorted(set(flags) | set((episode.data_quality_flags if episode else []) or []))
        defaults["data_quality_flags"] = merged_flags
        if episode is None:
            episode = TradeEpisode.objects.create(idempotency_key=key, **defaults)
        else:
            for field, value in defaults.items():
                setattr(episode, field, value)
            episode.save()
        episode.legs.all().delete()
        TradeEpisodeLeg.objects.bulk_create([_leg_row(episode, leg, instance, ctx) for leg in summary["legs"].values()])
        EpisodeMetrics.objects.update_or_create(
            episode=episode, defaults={"metrics": metrics, "metrics_version": metrics_version})
        if summary["closed"] and not EpisodeEvidenceSnapshot.objects.filter(episode=episode).exists():
            pack, digest = pack_builder(episode)
            EpisodeEvidenceSnapshot.objects.create(episode=episode, pack=pack, evidence_hash=digest,
                                                   builder_version=BUILDER_VERSION)
            try:  # freeze which stored context items surrounded this trade; never affects the episode
                from .context_view import link

                with transaction.atomic():
                    link(episode, pack.get("context"))
            except Exception:
                logger.exception("intelligence: could not link context for episode %s", episode.pk)
            from .reviews import schedule_review

            schedule_review(episode.pk)
            from .learning import schedule_learning

            schedule_learning(episode.pk)
    return episode


def _leg_row(episode, leg, instance, ctx):
    instrument = ctx["instruments"].get(leg["instrument_id"])
    option = _option_details(instrument) if instrument is not None else None
    role = ""
    entry_avg = leg["entry_notional"] / leg["entry_qty"] if leg["entry_qty"] else ZERO
    exit_avg = leg["exit_notional"] / leg["exit_qty"] if leg["exit_qty"] else ZERO
    q8 = D("0.0001")
    return TradeEpisodeLeg(
        episode=episode, instrument_id=leg["instrument_id"], symbol=leg["symbol"][:64], role=role,
        right=getattr(option, "right", "") or "", strike=getattr(option, "strike", None),
        expiration=getattr(option, "expiration", None), multiplier=leg["multiplier"],
        entry_side=leg["entry_side"], entry_quantity=leg["entry_qty"], entry_avg_price=entry_avg,
        exit_quantity=leg["exit_qty"], exit_avg_price=exit_avg, realized_pnl=leg["realized"],
        commission=leg["commission"], entry_reference_price=leg["entry_ref"], exit_reference_price=leg["exit_ref"],
        entry_slippage_bps=(leg["entry_slip_w"] / leg["entry_slip_q"]).quantize(q8) if leg["entry_slip_q"] else None,
        exit_slippage_bps=(leg["exit_slip_w"] / leg["exit_slip_q"]).quantize(q8) if leg["exit_slip_q"] else None,
        first_fill_at=leg["first_at"], last_fill_at=leg["last_at"],
    )


# ------------------------------------------------------------------------------------------------
# public entry points
# ------------------------------------------------------------------------------------------------
def build_or_update_episodes(instance_id, *, force=False, dry_run=False):
    """Rebuild this instance's episodes from its attributed fills. Safe to call any number of times, in any
    order, from any worker. Returns the list of episodes it touched (created, updated or re-checked)."""
    from apps.strategies.models import StrategyInstance
    from apps.strategies.reporting import attributed_fills

    from . import evidence, metrics as metrics_module

    instance = (StrategyInstance.objects.select_related("definition", "instrument", "portfolio__gateway_session")
                .filter(pk=instance_id).first())
    if instance is None:
        return []
    events = attributed_fills(instance)
    if not events:
        return []
    ctx = _load_context(instance, events)
    single = not any(getattr(ctx["instruments"].get(e["instrument_id"]), "asset_class", "") == "OPT" for e in events)
    touched = []
    for parts, closed in segment_events(events, single):
        summary = summarize(instance, parts, closed, ctx)
        key = f"episode:{instance.pk}:{summary['version_number']}:{summary['first_exec']}"
        existing = TradeEpisode.objects.filter(idempotency_key=key).first()
        fingerprint = _fingerprint(parts, closed)
        if (existing is not None and not force and existing.fills_fingerprint == fingerprint
                and existing.metrics_version_built == metrics_module.METRICS_VERSION):
            touched.append(existing)
            continue
        if existing is not None and existing.status == TradeEpisode.Status.EXPIRED:
            touched.append(existing)  # expiry-closed episodes are owned by the sweeper
            continue
        if dry_run:
            touched.append(existing if existing is not None else {"idempotency_key": key, "closed": closed})
            continue
        exit_info = resolve_exit(instance, summary) if closed else ("", "", None)
        entry_info = resolve_entry(instance, summary)
        flags = quality_flags(instance, summary, entry_info[1], entry_info[2])
        try:
            computed = metrics_module.compute_metrics(instance, summary, ctx, entry_info, exit_info, flags)
        except Exception:
            logger.exception("intelligence metrics failed for instance %s", instance.pk)
            computed = {"error.metrics": True}
        pack_builder = lambda ep: evidence.build_pack(ep, instance, summary, ctx, entry_info, exit_info, computed)  # noqa: E731
        try:
            episode = _persist(instance, summary, ctx, exit_info, entry_info, flags, computed,
                               metrics_module.METRICS_VERSION, pack_builder)
        except IntegrityError:
            # A concurrent worker created it first; the next sweep reconciles any difference.
            episode = TradeEpisode.objects.filter(idempotency_key=key).first()
        if episode is not None:
            touched.append(episode)
    return touched


def instances_for_fill(fill_id):
    """Strategy instance ids whose attribution includes this fill (empty for manual / unattributed fills)."""
    from apps.allocation.models import OrderIntentAttribution
    from apps.execution.models import Fill

    fill = Fill.objects.filter(pk=fill_id).select_related("order").first()
    if fill is None:
        return []
    return list(OrderIntentAttribution.objects.filter(order_intent_id=fill.order.intent_id,
                                                      strategy_instance__isnull=False)
                .values_list("strategy_instance_id", flat=True).distinct())

# flags set by the expiry sweeper (tasks.sweep_expired)
EXPIRED_BROKER_STILL_HOLDS = "EXPIRED_BROKER_STILL_HOLDS"
PNL_ESTIMATED_AT_EXPIRY = "PNL_ESTIMATED_AT_EXPIRY"
EXPIRY_SETTLEMENT_UNKNOWN = "EXPIRY_SETTLEMENT_UNKNOWN"
