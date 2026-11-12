"""Read models for the strategy detail screen: decision ledger, trade points,
ATR boundary and strategy-scoped running P&L.

Everything here is derived from facts that are already persisted -- each
`StrategyRun` keeps the exact option context it was evaluated against, every
entry keeps its `OptionSelectionSnapshot`, and every fill is attributed to the
strategies that asked for it. Nothing is recomputed from live market data, so
the ledger shows what the engine actually saw on each bar.
"""
from collections import defaultdict
from datetime import timezone as datetime_timezone
from decimal import Decimal, InvalidOperation

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import OptionSelectionSnapshot

LEDGER_LIMIT = 500
ENTRY_SIGNALS = {"ENTER_OPTIONS"}
EXIT_SIGNALS = {"EXIT_OPTIONS"}
DEFAULT_IV_SCORE_THRESHOLD = "60"
DEFAULT_TQS_THRESHOLD = "60"

# The refusing gate for each option-context status, in the order the backend
# applies them, so the ledger can say where a bar stopped.
GATE_LABELS = {
    "ENTRY_WINDOW_CLOSED": "Entry window",
    "NO_EXPIRY": "DTE window",
    "MISSING_BOUNDARY": "RPS inputs",
    "ENTRY_GATE_CLOSED": "RPS floor",
    "NO_LIQUID_STRUCTURE": "Liquidity",
    "UNSUPPORTED_PROVIDER": "Chain provider",
    "NO_ELIGIBLE_STRUCTURE": "IV / POP / EV / TQS",
    "NO_IBKR_STRUCTURE": "IBKR contracts",
    "EXPIRY_ALREADY_ENTERED": "One entry per expiry",
}


def _decimal(value):
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() else None


def _text(value, places=2):
    number = _decimal(value)
    if number is None:
        return None
    return str(number.quantize(Decimal(1).scaleb(-places)))


def _money(value):
    return format(Decimal(value).quantize(Decimal("0.00000001")), "f")


def _passed(value, threshold):
    value, threshold = _decimal(value), _decimal(threshold)
    if value is None or threshold is None:
        return None
    return value >= threshold


def _label(key):
    return str(key or "").replace("_", " ").strip()


def _when(value):
    if value is None:
        return None
    parsed = value if hasattr(value, "isoformat") else parse_datetime(str(value))
    if parsed is not None and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, datetime_timezone.utc)
    return parsed


def _iv_score(signal):
    signal = signal or {}
    return signal.get("iv_score", signal.get("score"))


def _candidate(row):
    signal = row.get("entry_signal") or {}
    return {
        "strategy_key": row.get("strategy_key"),
        "eligible": bool(row.get("eligible")),
        "rejection_reason": row.get("rejection_reason"),
        "iv_score": _iv_score(signal),
        "tqs": row.get("tqs"),
        "pop": row.get("pop"),
        "expected_pnl": row.get("expected_pnl"),
        "max_loss": row.get("max_loss"),
    }


def _best(rankings, selected_key=None):
    """The candidate a row is described by: the selected structure when one was
    entered, otherwise the best-ranked one (rankings arrive best first)."""
    if selected_key:
        for row in rankings:
            if row.get("strategy_key") == selected_key:
                return row
    return rankings[0] if rankings else None


def _max_tqs(rankings):
    values = [_decimal(row.get("tqs")) for row in rankings]
    values = [value for value in values if value is not None]
    return str(max(values)) if values else None


def _shared_rejections(rankings):
    """Refusal reasons every candidate shares -- the real holdup -- or else each
    candidate's own reason."""
    reasons = []
    for row in rankings:
        reason = row.get("rejection_reason")
        if reason:
            reasons.append(f"{_label(row.get('strategy_key'))}: {reason}")
    return reasons


def _thresholds(parameters):
    return {
        "rps_threshold": str(parameters.get("rps_threshold")) if parameters.get("rps_threshold") is not None else None,
        "iv_score_threshold": str(parameters.get("rps_iv_score_threshold", DEFAULT_IV_SCORE_THRESHOLD)),
        "tqs_threshold": str(parameters.get("rps_tqs_threshold", DEFAULT_TQS_THRESHOLD)),
    }


def _entry_reason(row):
    parts = [f"Entered {_label(row['selected_strategy']) or 'option structure'}"]
    checks = (
        ("RPS", row["rps"], row["rps_threshold"]),
        ("IV score", row["iv_score"], row["iv_score_threshold"]),
        ("TQS", row["tqs"], row["tqs_threshold"]),
    )
    for name, value, threshold in checks:
        if value is None:
            continue
        comparison = "≥" if _passed(value, threshold) else "vs"
        parts.append(f"{name} {_text(value)} {comparison} {_text(threshold) or '—'}")
    return "; ".join(parts)


def _decision(signal_type, next_state, has_targets):
    if signal_type in ENTRY_SIGNALS:
        return "ENTERED"
    if signal_type in EXIT_SIGNALS:
        return "EXITED"
    if signal_type == "HOLD_OPTIONS":
        return "HOLDING"
    if signal_type == "HOLD" and next_state in {"ENTRY_PENDING", "EXIT_PENDING"}:
        return "ORDERS_WORKING"
    if signal_type == "NO_ACTION":
        return "NO_TRADE"
    return "TARGET_SET" if has_targets else "NO_TRADE"


def _snapshot_evidence(snapshot):
    source = snapshot.source_snapshot or {}
    return {
        "rps_observation": source.get("rps_observation") or {},
        "selection_evidence": source.get("selection_evidence") or {},
        "entry_threshold": source.get("entry_threshold"),
    }


def _run_row(run, parameters, snapshots):
    context = run.context_snapshot or {}
    bar = context.get("bar") or {}
    option = bar.get("option_context") or {}
    signal = getattr(run, "signal", None)
    signal_type = signal.signal_type if signal else None
    prior_state = context.get("strategy_state_data") or {}
    targets = list(run.targets.all())
    snapshot = snapshots.get(option.get("selection_snapshot_id"))
    if snapshot is None and option.get("status") in {"HOLDING", "SOFT_EXIT", "LOCKED_POSITION"}:
        snapshot = snapshots.get(prior_state.get("selection_snapshot_id"))
    evidence = _snapshot_evidence(snapshot) if snapshot else {}
    measured = option.get("rps_observation") or {}
    observation = measured or evidence.get("rps_observation") or {}
    selection = option.get("selection_evidence") or evidence.get("selection_evidence") or {}
    rankings = list(selection.get("rankings") or option.get("candidate_rankings") or [])
    selected_key = option.get("selected_strategy") or (snapshot.selected_strategy if snapshot else None)
    best = _best(rankings, selected_key if signal_type in ENTRY_SIGNALS else None)
    best_signal = (best or {}).get("entry_signal") or selection.get("entry_signal") or {}
    thresholds = _thresholds(parameters)
    if option.get("entry_threshold") is not None:
        thresholds["rps_threshold"] = str(option["entry_threshold"])
    elif evidence.get("entry_threshold") is not None:
        thresholds["rps_threshold"] = str(evidence["entry_threshold"])

    status = option.get("status")
    if signal_type == "NO_ACTION" and signal and "already entered this contract expiry" in (signal.reason or ""):
        status = "EXPIRY_ALREADY_ENTERED"
    decision = "ERROR" if run.status == "ERROR" else _decision(signal_type, None, bool(targets))
    if signal_type == "HOLD" and signal and "still active" in (signal.reason or ""):
        decision = "ORDERS_WORKING"

    rps = option.get("entry_score")
    if rps is None and snapshot is not None:
        rps = str(snapshot.entry_score)
    row = {
        "id": f"run:{run.pk}",
        "run_id": run.pk,
        "time": _when(bar.get("window_end")) or (signal.signal_time if signal else run.started_at),
        "evaluated_at": run.completed_at or run.started_at,
        "bar_close": bar.get("close"),
        # A bar measured on its own observation reports that spot; any other
        # bar (a hold, an exit) reports its own close, never the entry spot.
        "spot": measured.get("spot") if measured.get("spot") is not None else (
            bar.get("close") if bar.get("close") is not None and signal_type not in ENTRY_SIGNALS
            else (str(snapshot.spot) if snapshot else None)
        ),
        "decision": decision,
        "signal_type": signal_type,
        "status": status or (run.status if run.status == "ERROR" else signal_type),
        "gate": GATE_LABELS.get(status),
        "rps": rps,
        "iv_score": _iv_score(best_signal),
        "iv_rank": best_signal.get("iv_rank"),
        "iv_percentile": best_signal.get("iv_percentile"),
        "tqs": (best or {}).get("tqs") if signal_type in ENTRY_SIGNALS else _max_tqs(rankings),
        **thresholds,
        "atr_support": observation.get("atr_support") if observation.get("atr_support") is not None else (str(snapshot.atr_support) if snapshot else None),
        "atr_resistance": observation.get("atr_resistance") if observation.get("atr_resistance") is not None else (str(snapshot.atr_resistance) if snapshot else None),
        "oi_support": observation.get("oi_support"),
        "oi_resistance": observation.get("oi_resistance"),
        "selected_strategy": selected_key if signal_type in ENTRY_SIGNALS | EXIT_SIGNALS | {"HOLD_OPTIONS", "HOLD"} else None,
        "candidates": [_candidate(item) for item in rankings],
        "orders": [],
        "target_count": len(targets),
    }
    row["rps_passed"] = _passed(row["rps"], row["rps_threshold"])
    row["iv_passed"] = _passed(row["iv_score"], row["iv_score_threshold"])
    row["tqs_passed"] = _passed(row["tqs"], row["tqs_threshold"])

    if run.status == "ERROR":
        reason, details = run.error or "Strategy evaluation failed", []
    elif decision == "ENTERED":
        reason = _entry_reason(row)
        details = [option.get("reason")] if option.get("reason") else []
        if best and best.get("pop") is not None:
            details.append(f"POP {_text(best.get('pop'), 4)} · expected P&L {_text(best.get('expected_pnl')) or '—'}")
    elif decision == "EXITED":
        reason = (signal.reason if signal else "") or "Exit"
        details = [option.get("reason")] if option.get("reason") and option.get("reason") != reason else []
    elif decision == "HOLDING":
        reason = f"Holding {_label(selected_key) or 'position'}: {option.get('reason') or (signal.reason if signal else '')}".rstrip(": ")
        details = ["RPS, IV score, TQS and the ATR band are the entry values this structure is locked to"] if snapshot else []
    else:
        reason = option.get("reason") or (signal.reason if signal else "") or "No action"
        if status == "EXPIRY_ALREADY_ENTERED" and signal:
            reason = signal.reason
        if status == "ENTRY_WINDOW_CLOSED":
            opens = parameters.get("rps_market_open_time")
            delay = parameters.get("rps_entry_delay_minutes")
            if opens is not None:
                reason = f"{reason} (opens {opens}{f' + {delay} min' if delay else ''})"
        details = _shared_rejections(rankings) if status in {"NO_ELIGIBLE_STRUCTURE", "NO_IBKR_STRUCTURE"} else []
        if selection.get("reason") and selection.get("reason") != reason:
            details.insert(0, selection["reason"])
    row["reason"] = reason
    row["reason_details"] = [item for item in details if item]
    return row


def _job_row(job):
    details = job.error_details or {}
    waiting_quotes = job.status == "WAITING_FOR_INPUT" and details.get("input_kind") == "OPTION_QUOTES"
    message = details.get("message") or job.error_code or job.status.replace("_", " ").lower()
    return {
        "id": f"job:{job.pk}",
        "run_id": None,
        "time": job.event_time,
        "evaluated_at": job.updated_at,
        "bar_close": None,
        "spot": None,
        "decision": "AWAITING_QUOTES" if waiting_quotes else "NOT_EVALUATED",
        "signal_type": None,
        "status": job.error_code or job.status,
        "gate": "Option quotes" if waiting_quotes else None,
        "rps": None, "iv_score": None, "iv_rank": None, "iv_percentile": None, "tqs": None,
        "rps_threshold": None, "iv_score_threshold": None, "tqs_threshold": None,
        "rps_passed": None, "iv_passed": None, "tqs_passed": None,
        "atr_support": None, "atr_resistance": None, "oi_support": None, "oi_resistance": None,
        "selected_strategy": None,
        "candidates": [],
        "orders": [],
        "target_count": 0,
        "reason": (
            f"Entry selected; waiting for executable IBKR quotes on every leg ({message})"
            if waiting_quotes else f"Bar not evaluated: {message}"
        ),
        "reason_details": [f"Attempt {job.attempt_count}", f"Job status {job.status}"],
    }


def _attach_orders(rows, instance):
    """Attach the orders a trade decision produced: every strategy order created
    after that decision and before the next one."""
    from apps.allocation.models import OrderIntentAttribution

    trades = sorted((row for row in rows if row["decision"] in {"ENTERED", "EXITED", "TARGET_SET"}),
                    key=lambda row: row["evaluated_at"] or row["time"])
    if not trades:
        return
    attributions = list(OrderIntentAttribution.objects.filter(strategy_instance=instance)
        .select_related("order_intent__order", "order_intent__instrument").order_by("order_intent__created_at"))
    for index, row in enumerate(trades):
        start = row["evaluated_at"] or row["time"]
        end = trades[index + 1]["evaluated_at"] if index + 1 < len(trades) else None
        for attribution in attributions:
            intent = attribution.order_intent
            order = getattr(intent, "order", None)
            if order is None or intent.created_at < start or (end and intent.created_at >= end):
                continue
            row["orders"].append({
                "id": order.internal_id, "symbol": intent.instrument.symbol, "side": intent.side,
                "quantity": str(order.quantity), "filled_quantity": str(order.filled_quantity),
                "average_fill_price": str(order.average_fill_price), "status": order.status,
            })


def strategy_ledger(instance, limit=LEDGER_LIMIT):
    """One row per evaluated bar, newest first, saying what was decided and why."""
    runs = list(instance.runs.select_related("signal").prefetch_related("targets")
        .order_by("-started_at", "-pk")[:limit])
    snapshot_ids = set()
    for run in runs:
        option = ((run.context_snapshot or {}).get("bar") or {}).get("option_context") or {}
        prior = (run.context_snapshot or {}).get("strategy_state_data") or {}
        for value in (option.get("selection_snapshot_id"), prior.get("selection_snapshot_id")):
            if value:
                snapshot_ids.add(int(value))
    snapshots = {item.pk: item for item in OptionSelectionSnapshot.objects.filter(pk__in=snapshot_ids)}
    parameters = instance.parameters or {}
    rows = [_run_row(run, parameters, snapshots) for run in runs]
    oldest = rows[-1]["time"] if len(rows) >= limit else None
    jobs = instance.evaluation_jobs.filter(
        strategy_run__isnull=True, status__in=["WAITING_FOR_INPUT", "RETRY", "FAILED"],
    ).order_by("-event_time")[:limit]
    rows.extend(_job_row(job) for job in jobs if oldest is None or job.event_time >= oldest)
    _attach_orders(rows, instance)
    rows.sort(key=lambda row: (row["time"] or timezone.now(), row["id"]), reverse=True)
    return rows[:limit]


def atr_bands(ledger, bars):
    """The ATR boundary for every chart bar.

    The boundary is frozen from completed prior data, so it is one value per
    session: every bar of a date takes the band measured on that date.
    """
    by_date = {}
    for row in sorted(ledger, key=lambda item: item["time"]):
        support, resistance = _decimal(row["atr_support"]), _decimal(row["atr_resistance"])
        if support is None or resistance is None or row["time"] is None:
            continue
        by_date.setdefault(row["time"].date(), (support, resistance))
    return [
        {"time": bar.window_end, "support": str(by_date[bar.window_end.date()][0]),
         "resistance": str(by_date[bar.window_end.date()][1])}
        for bar in bars if bar.window_end.date() in by_date
    ]


def attributed_fills(instance):
    """This strategy's share of every fill, exactly as `apply_execution`
    attributes it to `StrategyAttributedPosition`."""
    from apps.allocation.models import OrderIntentAttribution

    attributions = list(OrderIntentAttribution.objects.filter(strategy_instance=instance)
        .select_related("order_intent__order", "order_intent__instrument")
        .prefetch_related("order_intent__order__fills", "order_intent__attributions"))
    events = []
    for attribution in attributions:
        intent = attribution.order_intent
        order = getattr(intent, "order", None)
        if order is None or not order.quantity:
            continue
        gross = sum((abs(Decimal(item.allocated_quantity)) for item in intent.attributions.all()), Decimal(0)) or Decimal(1)
        share = abs(Decimal(attribution.allocated_quantity)) / gross
        multiplier = Decimal(intent.instrument.multiplier or 1)
        for fill in order.fills.all():
            quantity = Decimal(attribution.allocated_quantity) * Decimal(fill.quantity) / Decimal(order.quantity)
            if not quantity:
                continue
            events.append({
                "time": fill.executed_at, "execution_id": fill.execution_id,
                "instrument_id": intent.instrument_id, "symbol": intent.instrument.symbol,
                "quantity": quantity, "price": Decimal(fill.price), "multiplier": multiplier,
                "commission": Decimal(fill.commission) * share, "currency": fill.currency,
                "order_id": order.internal_id, "side": intent.side,
            })
    events.sort(key=lambda item: (item["time"], item["execution_id"]))
    return events


def _apply(book, event):
    """Weighted-average accounting, identical to the portfolio ledger."""
    quantity, average = book["quantity"], book["average"]
    signed = event["quantity"]
    updated = quantity + signed
    realized = Decimal(0)
    if quantity == 0 or quantity * signed > 0:
        if updated:
            book["average"] = (abs(quantity) * average + abs(signed) * event["price"]) / abs(updated)
    else:
        closed = min(abs(quantity), abs(signed))
        direction = Decimal(1) if quantity > 0 else Decimal(-1)
        realized = (event["price"] - average) * closed * direction * event["multiplier"]
        if updated == 0:
            book["average"] = Decimal(0)
        elif quantity * updated < 0:
            book["average"] = event["price"]
    book["quantity"] = updated
    book["last_price"] = event["price"]
    book["multiplier"] = event["multiplier"]
    return realized


def _unrealized(books, marks=None):
    total = Decimal(0)
    for instrument_id, book in books.items():
        if not book["quantity"]:
            continue
        mark = (marks or {}).get(instrument_id, book["last_price"])
        total += (mark - book["average"]) * book["quantity"] * book["multiplier"]
    return total


def _marks(instrument_ids, portfolio):
    from apps.market_streams.models import InstrumentMarketState
    from apps.portfolios.models import PortfolioPosition

    marks = {}
    for state in InstrumentMarketState.objects.filter(instrument_id__in=instrument_ids, reference_price__isnull=False):
        marks[state.instrument_id] = {
            "price": Decimal(state.reference_price), "source": state.reference_price_source or "MARKET_STATE",
            "fresh": state.is_usable(), "as_of": state.latest_event_at,
        }
    for position in PortfolioPosition.objects.filter(portfolio=portfolio, instrument_id__in=instrument_ids):
        if position.instrument_id not in marks and Decimal(position.market_price):
            marks[position.instrument_id] = {
                "price": Decimal(position.market_price), "source": "PORTFOLIO_POSITION",
                "fresh": False, "as_of": position.updated_at,
            }
    return marks


def trade_points(events):
    """Entry and exit points: the strategy's gross exposure leaving and
    returning to flat. A multi-leg structure is one entry, not one per leg."""
    points = []
    positions = defaultdict(Decimal)
    current = None
    for event in events:
        was_flat = not any(positions.values())
        positions[event["instrument_id"]] += event["quantity"]
        is_flat = not any(positions.values())
        if was_flat and not is_flat:
            current = {"time": event["time"], "type": "ENTRY", "fills": 0, "legs": set(), "status": "FILLED"}
            points.append(current)
        elif not was_flat and is_flat:
            current = {"time": event["time"], "type": "EXIT", "fills": 0, "legs": set(), "status": "FILLED"}
            points.append(current)
        if current is not None:
            current["fills"] += 1
            current["legs"].add(event["symbol"])
            current["last_time"] = event["time"]
    return points


def strategy_pnl(instance):
    """Running P&L of this strategy alone: realized plus open mark-to-market on
    the quantity attributed to it, net of its share of commissions."""
    events = attributed_fills(instance)
    books = defaultdict(lambda: {"quantity": Decimal(0), "average": Decimal(0), "last_price": Decimal(0),
                                 "multiplier": Decimal(1), "symbol": "", "realized": Decimal(0), "commission": Decimal(0)})
    realized = commission = Decimal(0)
    series = []
    currency = instance.instrument.currency
    for event in events:
        book = books[event["instrument_id"]]
        book["symbol"] = event["symbol"]
        gain = _apply(book, event)
        book["realized"] += gain
        book["commission"] += event["commission"]
        realized += gain
        commission += event["commission"]
        currency = event["currency"] or currency
        unrealized = _unrealized(books)
        series.append({"time": event["time"], "realized": _money(realized - commission),
                       "unrealized": _money(unrealized), "total": _money(realized - commission + unrealized),
                       "mark_basis": "LAST_FILL"})
    marks = _marks(list(books), instance.portfolio)
    legs = []
    unrealized = Decimal(0)
    complete = True
    for instrument_id, book in books.items():
        mark = marks.get(instrument_id)
        if book["quantity"]:
            if mark is None:
                complete = False
            price = mark["price"] if mark else book["last_price"]
            leg_unrealized = (price - book["average"]) * book["quantity"] * book["multiplier"]
            unrealized += leg_unrealized
        else:
            price, leg_unrealized = (mark["price"] if mark else None), Decimal(0)
        legs.append({
            "instrument_id": instrument_id, "symbol": book["symbol"], "quantity": _money(book["quantity"]),
            "average_price": _money(book["average"]), "mark_price": None if price is None else _money(price),
            "mark_source": mark["source"] if mark else ("LAST_FILL" if book["quantity"] else None),
            "mark_fresh": bool(mark and mark["fresh"]), "multiplier": _money(book["multiplier"]),
            "realized_pnl": _money(book["realized"] - book["commission"]),
            "unrealized_pnl": _money(leg_unrealized), "open": bool(book["quantity"]),
        })
    now = timezone.now()
    total = realized - commission + unrealized
    if events:
        series.append({"time": now, "realized": _money(realized - commission), "unrealized": _money(unrealized),
                       "total": _money(total), "mark_basis": "CURRENT_MARK"})
    open_legs = [leg for leg in legs if leg["open"]]
    return {
        "currency": currency,
        "running_pnl": _money(total),
        "realized_pnl": _money(realized - commission),
        "gross_realized_pnl": _money(realized),
        "unrealized_pnl": _money(unrealized),
        "commissions": _money(commission),
        "open_position": bool(open_legs),
        "marks_complete": complete,
        "marks_fresh": all(leg["mark_fresh"] for leg in open_legs) if open_legs else True,
        "fill_count": len(events),
        "legs": sorted(legs, key=lambda leg: (not leg["open"], leg["symbol"])),
        "series": series,
        "as_of": now,
        "scope": "STRATEGY_ATTRIBUTED_FILLS",
    }


def chart_trade_markers(instance, ledger):
    """Entry/exit markers: executed points from fills, plus the latest entry or
    exit decision that has not filled yet."""
    points = trade_points(attributed_fills(instance))
    markers = [{
        "time": point["time"], "type": point["type"], "status": "FILLED",
        "label": f"{'Entry' if point['type'] == 'ENTRY' else 'Exit'} · {point['fills']} fill{'s' if point['fills'] != 1 else ''}",
    } for point in points]
    last_fill = points[-1]["last_time"] if points else None
    for row in ledger:
        if row["decision"] not in {"ENTERED", "EXITED"}:
            continue
        if last_fill is not None and row["time"] <= last_fill:
            break
        kind = "ENTRY" if row["decision"] == "ENTERED" else "EXIT"
        markers.append({
            "time": row["time"], "type": kind, "status": "PENDING",
            "label": f"{'Entry' if kind == 'ENTRY' else 'Exit'} signal · awaiting fill",
        })
        break
    return sorted(markers, key=lambda item: item["time"])
