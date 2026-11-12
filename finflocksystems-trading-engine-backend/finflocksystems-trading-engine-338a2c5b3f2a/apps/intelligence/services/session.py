"""Session Record (Phase 9): what happened in one market session, per exchange zone.

Built only from rows that are already stored (decisions from the engine's own `strategy_ledger`, episodes, alerts, the
operator journal, risk results, reconciliation breaks). It calls no broker, no market-data feed and no LLM; the
optional narrative is produced separately (`session_narrative`). One zone is one currency: money is never summed across
zones (R5). Regular hours are the standard ones; holidays and early closes are not modelled until calendar data exists.
"""
import hashlib
import json
import logging
from collections import Counter, OrderedDict
from datetime import datetime, time, timedelta, timezone as dt_utc
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from apps.intelligence import conf
from apps.intelligence.models import LiveAlert, OperatorEvent, SessionRecord, TradeEpisode, TradeReview

from .evidence import _event_line, sanitize_text, sanitize_tree
from .facts import flatten
from .metrics import _zone

logger = logging.getLogger(__name__)
RECORD_VERSION = "1"
SESSIONS = {
    "XNSE": {"label": "NSE", "currency": "INR", "open": time(9, 15), "close": time(15, 30)},
    "XNYS": {"label": "NYSE", "currency": "USD", "open": time(9, 30), "close": time(16, 0)},
}
NOT_TAKEN = {"NO_TRADE", "AWAITING_QUOTES", "NOT_EVALUATED", "ERROR"}
TIMELINE_LIMIT = 300
GROUP_LIMIT = 60
ROW_LIMIT = 100
LOW_N = 8
GATES = (("rps", "rps_threshold", "RPS"), ("iv_score", "iv_score_threshold", "IV score"), ("tqs", "tqs_threshold", "TQS"))


def finalize_after_minutes():
    from django.conf import settings

    return int(getattr(settings, "INTELLIGENCE_SESSION_FINALIZE_MINUTES", 30))


def zone_of(instance):
    """The exchange zone an instance trades in (same INR/XNSE rule the episode code uses)."""
    instrument = instance.instrument
    if instrument.currency == "INR":
        return "XNSE"
    return instrument.trading_calendar if instrument.trading_calendar in SESSIONS else "XNYS"


def local_date(zone, now=None):
    return (now or timezone.now()).astimezone(_zone(zone)).date()


def window(zone, day):
    """(start, end, open, close) as UTC datetimes for the exchange-local calendar `day`."""
    tz = _zone(zone)
    cfg = SESSIONS[zone]
    at = lambda d, t: datetime.combine(d, t, tzinfo=tz).astimezone(dt_utc.utc)  # noqa: E731
    return at(day, time.min), at(day + timedelta(days=1), time.min), at(day, cfg["open"]), at(day, cfg["close"])


def _num(value):
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def _d2(value):
    """Money as it is shown and quoted: two decimals (the stored episode values keep full precision)."""
    return str(Decimal(value).quantize(Decimal("0.01")))


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def _margin_text(row):
    """The failing gate with its margin, e.g. 'RPS 58 vs 60 (-2)'. None when no gate values are available."""
    for value_key, threshold_key, label in GATES:
        value, threshold = _num(row.get(value_key)), _num(row.get(threshold_key))
        passed = row.get(f"{value_key}_passed")
        if value is not None and threshold is not None and passed is False:
            return label, value, threshold, round(value - threshold, 4)
    return None


def _decision_rows(instances, start, end, open_dt, close_dt):
    """Compact ledger rows inside the window, plus the per-instance idle list and a truncation flag."""
    from apps.strategies.reporting import LEDGER_LIMIT, strategy_ledger

    rows, idle, truncated = [], [], False
    for instance in instances:
        evaluated = (instance.runs.filter(started_at__gte=start, started_at__lt=end).exists()
                     or instance.evaluation_jobs.filter(event_time__gte=start, event_time__lt=end).exists())
        if not evaluated:
            idle.append({"instance": instance.name, "strategy": instance.definition.key, "state": instance.state,
                         "enabled": instance.enabled})
            continue
        ledger = strategy_ledger(instance, limit=LEDGER_LIMIT)
        if len(ledger) >= LEDGER_LIMIT and ledger[-1].get("time") and ledger[-1]["time"] > start:
            truncated = True  # the engine ledger holds only its newest 500 rows
        for row in ledger:
            when = row.get("time")
            if not isinstance(when, datetime) or not (start <= when < end):
                continue
            rows.append({**{k: row.get(k) for k in (
                "decision", "status", "gate", "rps", "rps_threshold", "iv_score", "iv_score_threshold", "tqs",
                "tqs_threshold", "rps_passed", "iv_passed", "tqs_passed")},
                "time": when, "instance": instance.name, "strategy": instance.definition.key,
                "reason": sanitize_text(row.get("reason") or "", 200), "in_session": open_dt <= when < close_dt})
    return rows, idle, truncated


def _group_not_taken(rows):
    """Collapse the many 'no trade' evaluations into a few groups: how often, first / last time, closest gate."""
    groups = OrderedDict()
    for row in sorted(rows, key=lambda r: r["time"]):
        if row["decision"] not in NOT_TAKEN:
            continue
        key = (row["instance"], row["decision"], row["gate"] or row["status"], row["reason"])
        group = groups.setdefault(key, {
            "instance": row["instance"], "strategy": row["strategy"], "decision": row["decision"],
            "gate": row["gate"] or row["status"], "reason": row["reason"], "count": 0,
            "first": row["time"], "last": row["time"], "closest": None})
        group["count"] += 1
        group["last"] = row["time"]
        margin = _margin_text(row)
        if margin and (group["closest"] is None or abs(margin[3]) < abs(group["closest"]["margin"])):
            group["closest"] = {"gate": margin[0], "value": margin[1], "threshold": margin[2], "margin": margin[3],
                                "at": row["time"]}
    out = []
    for group in list(groups.values())[:GROUP_LIMIT]:
        group = dict(group)
        for key in ("first", "last"):
            group[key] = _iso(group[key])
        if group["closest"]:
            group["closest"] = {**group["closest"], "at": _iso(group["closest"]["at"])}
        out.append(group)
    return out


def _episode_row(episode, review=None):
    return {"episode_id": episode.pk, "instrument": episode.instrument_symbol, "instance": episode.instance_name,
            "strategy": episode.strategy_key, "direction": episode.direction, "status": episode.status,
            "opened_at": _iso(episode.opened_at), "closed_at": _iso(episode.closed_at), "currency": episode.currency,
            "gross_pnl": _d2(episode.gross_pnl), "commissions": _d2(episode.commissions), "net_pnl": _d2(episode.net_pnl),
            "pnl_basis": episode.pnl_basis, "exit_reason": episode.exit_reason or None,
            "flags": sorted(episode.data_quality_flags or []),
            "review_grade": getattr(review, "process_grade", None) or None,
            "review_outcome": getattr(review, "outcome_class", None) or None}


def build_content(zone, day, now=None):
    """Everything the Session Record says about (zone, day), as one JSON-safe dict. Pure reads."""
    from apps.oms.models import OrderIntent  # noqa: F401  (relation used through RiskCheckResult filters)
    from apps.reconciliation.models import ReconciliationBreak
    from apps.risk.models import KillSwitch, RiskCheckResult
    from apps.strategies.models import StrategyInstance

    now = now or timezone.now()
    cfg = SESSIONS[zone]
    start, end, open_dt, close_dt = window(zone, day)
    asof = min(close_dt, now)  # "open positions" means open at the close (or now, while the session is running)
    phase = "NOT_STARTED" if now < open_dt else "IN_SESSION" if now < close_dt else "CLOSED"
    instances = [i for i in StrategyInstance.objects.select_related("definition", "instrument", "portfolio")
                 if zone_of(i) == zone]
    ids = [i.pk for i in instances]
    symbols = sorted({i.instrument.symbol for i in instances})
    notes = [f"Regular hours assumed {cfg['open']:%H:%M}-{cfg['close']:%H:%M} local; holidays and early closes are not modelled."]

    rows, idle, truncated = _decision_rows(instances, start, end, open_dt, close_dt)
    decision_counts = Counter(r["decision"] for r in rows)
    not_taken = _group_not_taken(rows)

    episodes = TradeEpisode.objects.filter(instance_id__in=ids)
    reviews = {}
    for review in TradeReview.objects.filter(episode__in=episodes, scope="TRADE").order_by("created_at"):
        reviews[review.episode_id] = review
    opened = list(episodes.filter(opened_at__gte=start, opened_at__lt=end).order_by("opened_at"))
    closed = list(episodes.filter(closed_at__gte=start, closed_at__lt=end, status__in=["CLOSED", "EXPIRED"]).order_by("closed_at"))
    still_open = list(episodes.filter(opened_at__lt=asof).filter(Q(closed_at__isnull=True) | Q(closed_at__gte=asof)).order_by("opened_at"))
    open_rows = []
    for episode in still_open[:ROW_LIMIT]:
        row = _episode_row(episode, reviews.get(episode.pk))
        snap = episode.live_snapshots.filter(observed_at__lte=asof).order_by("-observed_at").first()
        row["latest_mark"] = None if snap is None else {
            "observed_at": _iso(snap.observed_at), "mark_pnl": None if snap.mark_pnl is None else _d2(snap.mark_pnl),
            "pct_of_max_loss": None if snap.pct_of_max_loss is None else _d2(snap.pct_of_max_loss),
            "quote_freshness": snap.quote_freshness}
        open_rows.append(row)
    closed_rows = [_episode_row(e, reviews.get(e.pk)) for e in closed[:ROW_LIMIT]]
    net_by_currency = {}
    wins = 0
    for episode in closed:
        net_by_currency[episode.currency] = net_by_currency.get(episode.currency, Decimal("0")) + episode.net_pnl
        wins += 1 if episode.net_pnl > 0 else 0

    alerts = list(LiveAlert.objects.filter(episode__instance_id__in=ids, first_seen__gte=start, first_seen__lt=end)
                  .select_related("episode").order_by("first_seen")[:ROW_LIMIT])
    alert_rows = [{"time": _iso(a.first_seen), "severity": a.severity, "rule": a.rule_id, "text": a.text,
                   "instrument": a.episode.instrument_symbol, "occurrences": a.occurrences} for a in alerts]

    operator = list(OperatorEvent.objects.filter(occurred_at__gte=start, occurred_at__lt=end).exclude(event_type="REQUEST")
                    .filter(Q(instance_id__in=ids) | Q(instance_id__isnull=True, instrument_symbol__in=symbols))
                    .order_by("occurred_at", "pk")[:ROW_LIMIT])
    operator_rows = [_event_line(e) for e in operator]

    risk = list(RiskCheckResult.objects.filter(created_at__gte=start, created_at__lt=end,
                                               order_intent__attributions__strategy_instance_id__in=ids)
                .exclude(decision="APPROVED").select_related("order_intent__instrument").distinct().order_by("created_at")[:ROW_LIMIT])
    risk_rows = [{"time": _iso(r.created_at), "instrument": r.order_intent.instrument.symbol, "check": r.check_name,
                  "decision": r.decision, "reason": sanitize_text(r.reason, 200), "requested": str(r.requested_quantity),
                  "approved": str(r.approved_quantity)} for r in risk]

    account_ids = {i.portfolio.account_id for i in instances if i.portfolio_id}
    breaks = list(ReconciliationBreak.objects.filter(created_at__gte=start, created_at__lt=end, material=True,
                                                     run__broker_account_id__in=account_ids).order_by("created_at")[:ROW_LIMIT])
    break_rows = [{"time": _iso(b.created_at), "category": b.category, "severity": b.severity, "resolved": b.resolved}
                  for b in breaks]
    kills = list(KillSwitch.objects.filter(enabled=True, updated_at__gte=start, updated_at__lt=end))
    kill_rows = [{"time": _iso(k.updated_at), "scope": k.scope, "reason": sanitize_text(k.reason, 200)} for k in kills]

    touched = {e.pk: e for e in (opened + closed + still_open)}  # one trade can be opened, closed and open: count it once
    flags = Counter(f for e in touched.values() for f in (e.data_quality_flags or []))
    if truncated:
        flags["LEDGER_TRUNCATED"] += 1
        notes.append("The engine's decision ledger keeps only its newest 500 rows per strategy; earlier decisions of this day may be missing.")
    if decision_counts.get("AWAITING_QUOTES") or decision_counts.get("NOT_EVALUATED"):
        flags["EVALUATIONS_WITHOUT_INPUT"] += 1
    if len(closed) < LOW_N:
        notes.append(f"Only {len(closed)} trade(s) closed in this session: too few to conclude anything about the strategies.")
    if phase != "CLOSED":
        notes.append("The session is still open (or has not started): this is a live view, not the final record.")

    try:  # outside context of the day, from stored items only (Phase 10); a failure here never blocks the record
        from . import daily_brief

        context = daily_brief.build_content(zone, day, now)
    except Exception:
        logger.exception("intelligence: session context failed for %s %s", zone, day)
        context = {"available": False, "sources": [], "notes": ["Outside context could not be read."]}

    timeline = []

    def add(when, kind, instance, text, ref=None):
        timeline.append({"time": _iso(when), "kind": kind, "instance": instance, "text": text, "ref": ref})

    for r in rows:
        if r["decision"] in {"ENTERED", "EXITED", "ORDERS_WORKING", "TARGET_SET", "ERROR"}:
            add(r["time"], f"decision:{r['decision'].lower()}", r["instance"], r["reason"] or r["decision"], r["strategy"])
    for e in opened:
        add(e.opened_at, "trade:opened", e.instance_name, f"{e.instrument_symbol} {e.direction.replace('_', ' ').lower()} opened", f"episode:{e.pk}")
    for e in closed:
        add(e.closed_at, "trade:closed", e.instance_name,
            f"{e.instrument_symbol} closed, net {_d2(e.net_pnl)} {e.currency}, exit {(e.exit_reason or 'unknown').replace('_', ' ')}", f"episode:{e.pk}")
    for a in alert_rows:
        add(a["time"], f"alert:{a['severity'].lower()}", a["instrument"], a["text"], a["rule"])
    for o in operator_rows:
        add(o["at"], f"operator:{o['type'].lower()}", None, o["type"].replace("_", " ").lower() + (f": {o.get('action')}" if o.get("action") else ""), o.get("actor"))
    for r in risk_rows:
        add(r["time"], f"risk:{r['decision'].lower()}", r["instrument"], f"{r['check']}: {r['reason']}", r["check"])
    for b in break_rows:
        add(b["time"], "reconciliation:break", None, f"{b['category']} ({b['severity']})", None)
    for k in kill_rows:
        add(k["time"], "kill_switch", None, f"{k['scope']}: {k['reason']}", None)
    timeline.sort(key=lambda item: (str(item["time"]), item["kind"]))

    headline = {
        "currency": cfg["currency"], "phase": phase,
        "closed_trades": len(closed), "winning_trades": wins,
        "net_pnl": _d2(net_by_currency.get(cfg["currency"], Decimal("0"))),
        "net_pnl_by_currency": {k: _d2(v) for k, v in net_by_currency.items()},
        "opened_trades": len(opened), "open_at_asof": len(still_open),
        "decisions_total": len(rows), "decisions": dict(decision_counts),
        "strategies": len(instances), "strategies_without_evaluation": len(idle),
        "alerts": dict(Counter(a["severity"] for a in alert_rows)),
        "operator_actions": len(operator_rows), "risk_interventions": dict(Counter(r["decision"] for r in risk_rows)),
        "material_reconciliation_breaks": len(break_rows), "kill_switches": len(kill_rows)}
    content = {
        "record_version": RECORD_VERSION, "zone": zone, "label": cfg["label"], "session_date": day.isoformat(),
        "currency": cfg["currency"], "regular_hours": f"{cfg['open']:%H:%M}-{cfg['close']:%H:%M}",
        "window": {"start": _iso(start), "end": _iso(end), "open": _iso(open_dt), "close": _iso(close_dt)},
        "asof": _iso(asof), "phase": phase, "headline": headline,
        "timeline": timeline[:TIMELINE_LIMIT], "timeline_truncated": len(timeline) > TIMELINE_LIMIT,
        "not_taken": not_taken, "idle_strategies": idle, "closed_trades": closed_rows, "open_positions": open_rows,
        "alerts": alert_rows, "operator": operator_rows, "risk": risk_rows, "reconciliation": break_rows,
        "kill_switches": kill_rows, "data_quality": dict(sorted(flags.items())), "notes": notes, "context": context}
    # Strategy and instrument names are operator free text: neutralise control characters, code fences and our own data
    # markers everywhere in the record, once, so nothing downstream (report, model prompt) can carry an instruction (R8).
    return sanitize_tree(content, 300)


def content_hash(content):
    stable = {k: v for k, v in content.items() if k not in ("asof",)}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def facts_for(content):
    """Flat, citable fact map for the narrative: aggregates and per-trade / per-group figures only (no free text)."""
    facts = {}
    flatten({k: v for k, v in content["headline"].items() if k != "net_pnl_by_currency"}, "session.headline", facts)
    facts["session.zone"], facts["session.date"] = content["zone"], content["session_date"]
    flatten([{k: t[k] for k in ("instrument", "direction", "net_pnl", "gross_pnl", "commissions", "exit_reason", "review_grade")}
             for t in content["closed_trades"]], "session.closed", facts)
    flatten([{k: g.get(k) for k in ("instance", "decision", "gate", "count")} | (
        {"closest_gate": g["closest"]["gate"], "closest_value": g["closest"]["value"], "closest_threshold": g["closest"]["threshold"],
         "closest_margin": g["closest"]["margin"]} if g.get("closest") else {}) for g in content["not_taken"]], "session.not_taken", facts)
    flatten(content["data_quality"], "session.data_quality", facts)
    return facts


def upsert(zone, day, now=None, narrative_fn=None):
    """Build and store the record for (zone, day). Returns the row. Finalises it once the session is over (plus a
    settling delay), freezing the content and, if `narrative_fn` is given, its narrative. A FINAL row is never changed."""
    now = now or timezone.now()
    existing = SessionRecord.objects.filter(zone=zone, session_date=day).first()
    if existing is not None and existing.status == SessionRecord.Status.FINAL:
        return existing
    content = build_content(zone, day, now)
    digest = content_hash(content)
    _, _, _, close_dt = window(zone, day)
    due = now >= close_dt + timedelta(minutes=finalize_after_minutes())
    narrative = {}
    if due and narrative_fn is not None:
        try:
            narrative = narrative_fn(zone, day, content, digest) or {}
        except Exception:
            logger.exception("intelligence: session narrative failed for %s %s", zone, day)
            narrative = {"status": "UNAVAILABLE", "reason": "narrative generation failed"}
    values = dict(currency=SESSIONS[zone]["currency"], record_version=RECORD_VERSION, content=content, content_hash=digest,
                  built_at=now)
    if due:
        values.update(status=SessionRecord.Status.FINAL, finalized_at=now, narrative=narrative)
    record, _ = SessionRecord.objects.update_or_create(zone=zone, session_date=day, defaults=values)
    return record


def zones_with_strategies():
    from apps.strategies.models import StrategyInstance

    return sorted({zone_of(i) for i in StrategyInstance.objects.select_related("instrument")})


def run_all(now=None, narrative_fn=None):
    """Beat entry: refresh today's and yesterday's record for every zone that has strategies. Never raises."""
    if not conf.session_records_enabled():
        return {"skipped": "disabled"}
    now = now or timezone.now()
    done = {}
    for zone in zones_with_strategies():
        for day in {local_date(zone, now), local_date(zone, now) - timedelta(days=1)}:
            try:
                row = upsert(zone, day, now, narrative_fn)
                done[f"{zone}:{day}"] = row.status
            except Exception:
                logger.exception("intelligence: session record failed for %s %s", zone, day)
                done[f"{zone}:{day}"] = "error"
    return done
