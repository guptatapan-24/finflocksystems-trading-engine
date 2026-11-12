"""Entry-time RPS selection and IBKR contract qualification for options.

The decision rules are the Aryan_Options backend's, in its order:

1. the causal daily ATR/OI RPS V2 score must clear `rps_threshold`
   (``option_analytics.rps``);
2. every candidate structure is priced and put through the approved
   range/IV/breach entry gate against its **own** boundaries
   (``option_analytics.entry_signal`` -> ``option_analytics.range_entry``);
3. survivors clear the causal bounded-loss, POP and EV pre-filters, the approved
   market-state routing, the hard gates and the 14-factor TQS, and the
   risk-adjusted winner is entered (``option_analytics.runtime``).

Open positions are held to expiry.  The only discretionary close is one of the
three profit/regime soft exits in ``option_analytics.exit_policy``; the range is
reported, never gated on.
"""

from datetime import date, timedelta
from zoneinfo import ZoneInfo
from decimal import Decimal

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.instruments.services import option_chain, qualify_option_contract
from apps.market_data.dhan_options import DhanOptionChainClient
from apps.broker_gateway.client import GatewayError
from apps.market_data.ibkr_options import IBKROptionChainClient, IBKROptionDataUnavailable
from apps.market_data.remote_features import (
    RemoteFeatureProvider, RemoteFeatureUnavailable, remote_features_enabled,
    remote_symbol, decision_day,
)
from apps.market_streams.models import InstrumentMarketState, MarketBar
from apps.market_streams.subscriptions import acquire_consumer_lease

from .models import OptionSelectionSnapshot, StrategyAttributedPosition
from .option_analytics import rps as rps_engine
from .option_analytics.dynamic_exit import DynamicExitController, entry_baseline
from .option_analytics.runtime import rank_candidates
from .option_templates import AUTO_CANDIDATES, AUTO_OPTION_STRATEGY_KEY, OPTION_TEMPLATES


D = Decimal
MODEL_VERSION = f"{rps_engine.MODEL_VERSION}/{rps_engine.BOUNDARY_VERSION}"
# The source's `rps_feature_warmup_days`. The 252-session windows behind
# `atr_realized_vol`, `trend_adx` and `bollinger_compression` need materially
# more history than the streaming warm-up buffer carries, so the RPS features
# are read straight from completed daily bars rather than from indicators.
FEATURE_HISTORY_SESSIONS = 500


def _daily_history(instrument, observed_at):
    """Completed IBKR daily bars strictly before the decision timestamp.

    De-duplicated newest-version-first per `bar_id`, the same way the rest of
    this runtime reads a bar series, and cut at `observed_at` so a feature can
    never be built from a bar the decision could not have seen.
    """
    rows = MarketBar.objects.filter(
        instrument=instrument, interval="1d", is_final=True,
        window_end__lte=observed_at, provider="IBKR",
    ).order_by("-window_end", "-version").values(
        "bar_id", "window_end", "open", "high", "low", "close", "volume",
    )[:FEATURE_HISTORY_SESSIONS * 2]
    bars, seen = [], set()
    for row in rows:
        if row["bar_id"] in seen:
            continue
        seen.add(row["bar_id"])
        bars.append(row)
        if len(bars) == FEATURE_HISTORY_SESSIONS:
            break
    return list(reversed(bars))


def _eligible(row, parameters):
    """Whether a quote is executable enough to be entered."""
    return (
        row.get("price") is not None and row["price"] > 0
        and row.get("oi") is not None and row["oi"] >= int(parameters["minimum_open_interest"])
        and row.get("volume") is not None and row["volume"] >= int(parameters.get("minimum_volume", 0))
    )


def _priced(row):
    return row.get("price") is not None and row["price"] > 0


def strike_ladder(rows, right, spot):
    """The priced strikes of one right in ascending order, and the ATM index.

    The ladder is built on *priced* quotes only, matching the source's
    price-only candidate filter, so an offset counts the same strikes the source
    would count.  Screening it by open interest first would silently renumber
    every offset whenever a thin strike dropped out; the liquidity requirement
    is applied to the chosen legs instead, where it rejects the structure rather
    than shifting it.
    """
    ladder = sorted(
        (row for row in rows if row["right"] == right and _priced(row)),
        key=lambda row: row["strike"],
    )
    if not ladder:
        return None, None
    atm = min(range(len(ladder)), key=lambda index: abs(ladder[index]["strike"] - spot))
    return ladder, atm


def _at_offset(rows, right, offset, spot):
    ladder, atm = strike_ladder(rows, right, spot)
    if ladder is None:
        return None
    index = atm + int(offset)
    return ladder[index] if 0 <= index < len(ladder) else None


def select_template_legs(strategy_key, chains, parameters, *, spot):
    """Resolve one source template to liquid point-in-time observations.

    Strikes are chosen by ATM offset on each right's ladder -- the source's
    `ATMOffsetSelector` -- so a structure's shape is fixed by the template and
    never moved by where an open-interest wall happens to sit.  Open interest is
    an RPS *factor* and an independent confirmation; it does not select strikes.
    """
    template = OPTION_TEMPLATES[strategy_key]

    def leg(schema, quote):
        group = schema["expiry_group"]
        return None if quote is None else {
            **quote, "role": schema["role"], "side": schema["side"], "ratio": 1,
            "expiration": chains[group]["expiration"], "expiry_group": group,
        }

    if strategy_key == "DELTA_PUT_CREDIT_SPREAD":
        main = chains["main"]["rows"]
        puts = [
            row for row in main
            if row["right"] == "P" and row.get("delta") is not None and _eligible(row, parameters)
        ]
        if not puts:
            return []
        short = min(puts, key=lambda row: abs(row["delta"] - D(str(parameters["target_delta"]))))
        target = short["strike"] - D(str(parameters["strike_width"]))
        below = [row for row in puts if row["strike"] < short["strike"]]
        long_put = min(below, key=lambda row: abs(row["strike"] - target)) if below else None
        schemas = {item["role"]: item for item in template["legs"]}
        result = [leg(schemas["long_put"], long_put), leg(schemas["short_put"], short)]
    else:
        result = [
            leg(schema, _at_offset(chains[schema["expiry_group"]]["rows"], schema["right"], schema["offset"], spot))
            for schema in template["legs"]
        ]
    if any(item is None for item in result):
        return []
    # One contract per leg: an offset that collapses two legs onto the same
    # strike and right is not the structure the template describes.
    if len({(item["right"], item["strike"], item["expiration"]) for item in result}) != len(result):
        return []
    if any(not _eligible(item, parameters) for item in result):
        return []
    return result


def _context_from_snapshot(instance, snapshot):
    legs = list(snapshot.selected_legs)
    instrument_ids = [int(leg["instrument_id"]) for leg in legs]
    states = {
        state.instrument_id: state
        for state in InstrumentMarketState.objects.filter(instrument_id__in=instrument_ids)
    }
    ready = len(states) == len(instrument_ids) and all(
        states[instrument_id].is_execution_usable(execution_mode=instance.execution_mode)
        for instrument_id in instrument_ids
    )
    state_data = {
        "selected_strategy": snapshot.selected_strategy,
        "selection_snapshot_id": snapshot.pk,
        "expiration": snapshot.expiration.isoformat(),
        "legs": legs,
        "exit_baseline": (snapshot.source_snapshot or {}).get("exit_baseline"),
    }
    return {
        "status": "READY" if ready else "WAITING_FOR_OPTION_QUOTES",
        "reason": (
            f"RPS {snapshot.entry_score} selected {snapshot.selected_strategy}"
            if ready else "Selected IBKR option contracts are waiting for fresh executable quotes"
        ),
        "selected_strategy": snapshot.selected_strategy,
        "entry_score": str(snapshot.entry_score),
        "selection_snapshot_id": snapshot.pk,
        "legs": legs,
        "exit_baseline": state_data["exit_baseline"],
        "state_data": state_data,
    }


def _ensure_option_leases(instance, legs):
    from apps.instruments.models import Instrument

    instrument_ids = {int(leg["instrument_id"]) for leg in legs}
    instruments = {item.pk: item for item in Instrument.objects.filter(pk__in=instrument_ids)}
    for instrument_id in instrument_ids:
        acquire_consumer_lease(
            gateway_session=instance.portfolio.gateway_session,
            instrument=instruments[instrument_id],
            timeframe=settings.OPTION_STRATEGY_MARKET_DATA_TIMEFRAME,
            consumer_type="STRATEGY",
            lease_key=f"strategy:{instance.pk}:leg:{instrument_id}",
            ttl_seconds=settings.OPTION_STRATEGY_MARKET_DATA_LEASE_SECONDS,
        )


def _chain_for_expiry(ibkr, expiration, strike):
    matches = [
        row for row in ibkr["chains"]
        if expiration.isoformat() in row["expirations"]
        and D(str(strike)) in {D(value) for value in row["strikes"]}
    ]
    if not matches:
        raise ValueError(f"IBKR option chain has no exact {expiration} strike {strike}")
    return sorted(matches, key=lambda row: (row["exchange"] != "SMART", row["trading_class"]))[0]


def _chain_client(instance, *, dhan_client, gateway):
    if dhan_client is not None or settings.OPTION_CHAIN_PROVIDER == "DHAN":
        return (dhan_client or DhanOptionChainClient()), "DHAN"
    if settings.OPTION_CHAIN_PROVIDER == "IBKR":
        return IBKROptionChainClient(instance.portfolio.gateway_session, gateway=gateway), "IBKR"
    raise ValueError("OPTION_CHAIN_PROVIDER must be IBKR or DHAN")


def _feature_history(instrument, observed_at):
    """The daily feature frame, from the remote feature source when configured.

    The source reads its `feature_candles` from the remote database at a fixed
    granularity regardless of the replay timeframe, so the RPS score does not
    move when the execution timeframe does. Without a remote source this falls
    back to the daily bars the strategy's own subscription persisted, which
    only exist when the strategy itself runs daily.
    """
    if not remote_features_enabled():
        return rps_engine.daily_features(_daily_history(instrument, observed_at))
    candles = RemoteFeatureProvider().underlying_candles(
        remote_symbol(instrument),
        sessions=int(settings.REMOTE_FEATURE_SESSIONS),
        before=observed_at,
    )
    if not candles:
        raise RemoteFeatureUnavailable(
            f"Remote feature source returned no candles for {remote_symbol(instrument)}"
        )
    return rps_engine.daily_features(candles)


def _chain_features(instrument, event_date):
    """The prior session's precomputed chain features, T-1 aligned like every
    other daily factor. None when no remote source is configured."""
    if not remote_features_enabled():
        return None
    return RemoteFeatureProvider().chain_features(
        remote_symbol(instrument), before=decision_day(event_date),
    )


def _observation(instance, bar, event_date, *, spot, chain_rows, observed_at):
    features = _feature_history(instance.instrument, observed_at)
    return rps_engine.build_observation(
        features=features, session_date=event_date, spot=float(spot),
        chain_rows=chain_rows, parameters=instance.parameters,
        chain_features=_chain_features(instance.instrument, event_date),
    )


def prepare_exit_context(instance, bar, *, gateway=None):
    """Evaluate the approved soft-exit policy for an open structure.

    Returns the controller's decision alongside a refreshed baseline, or a
    status explaining why the policy could not be evaluated -- in which case the
    position is held, which is the source's failure mode too.
    """
    state_data = instance.state_data or {}
    legs = list(state_data.get("legs") or [])
    positions = {
        item.instrument_id: item
        for item in StrategyAttributedPosition.objects.filter(
            strategy_instance=instance, portfolio=instance.portfolio,
        ).exclude(quantity=0)
    }
    controller = DynamicExitController(instance.parameters)
    baseline = state_data.get("exit_baseline")
    if not controller.enabled or not legs or not baseline:
        return {"status": "LOCKED_POSITION", "reason": "Open option structure remains locked"}
    if settings.OPTION_CHAIN_PROVIDER != "IBKR":
        return {"status": "LOCKED_POSITION", "reason": "The soft-exit policy requires the IBKR chain provider"}
    if {int(leg["instrument_id"]) for leg in legs} != set(positions):
        return {"status": "LOCKED_POSITION", "reason": "Held option legs do not match the recorded structure"}

    client = IBKROptionChainClient(instance.portfolio.gateway_session, gateway=gateway)
    try:
        spot = client.underlying_price(instance.instrument)
        quotes = client.contract_quotes([{"conid": leg["conid"]} for leg in legs])
    except (IBKROptionDataUnavailable, GatewayError) as exc:
        # A quote outage must not fail the evaluation and put a strategy that is
        # holding a position into a retry loop. The soft exits only ever bank
        # profit, so not being able to evaluate them is a reason to hold.
        return {"status": "LOCKED_POSITION", "reason": f"Soft-exit quotes unavailable: {exc}"}
    if spot is None or not all(str(leg["conid"]) in quotes for leg in legs):
        return {"status": "LOCKED_POSITION", "reason": "Fresh executable quotes for every held leg are required"}

    observed_at = parse_datetime(str(bar.get("window_end"))) or timezone.now()
    observation = _observation(
        instance, bar, observed_at.date(), spot=spot, chain_rows=[], observed_at=observed_at,
    ) or {}
    decision = controller.evaluate(
        positions=[
            {
                "instrument_id": int(leg["instrument_id"]), "right": leg["right"],
                "strike": leg["strike"], "multiplier": leg["multiplier"],
                "quantity": positions[int(leg["instrument_id"])].quantity,
                "average_cost": positions[int(leg["instrument_id"])].average_cost,
            }
            for leg in legs
        ],
        quotes={int(leg["instrument_id"]): quotes[str(leg["conid"])] for leg in legs},
        baseline=baseline, observation=observation, spot=spot,
        session_date=observed_at.date(),
        expiry=date.fromisoformat(state_data["expiration"]),
    )
    if decision is None:
        return {"status": "LOCKED_POSITION", "reason": "The soft-exit policy could not be evaluated"}
    return {
        "status": "SOFT_EXIT" if decision["decision"] == "SOFT_EXIT" else "HOLDING",
        "reason": "; ".join(decision["explanations"]) or "No soft-exit trigger fired",
        "exit_decision": decision,
        "exit_baseline": decision["baseline"],
        "state_data": {"exit_baseline": decision["baseline"], "exit_evaluation": decision["metrics"]},
    }


def _entry_window_open(parameters, event_stamp):
    """The source's entry-window gate (`trading/rps.py`).

    A decision is refused until the session has been open for
    `rps_entry_delay_minutes`. The source compares in exchange-local time --
    it localises its session index to Asia/Kolkata -- and nothing in this
    runtime records IBKR's `timeZoneId`, so the zone is a parameter defaulting
    to the source's own.

    At a daily timeframe the bar lands after the close and this always passes;
    it is what lets an intraday timeframe sample the window the way the
    source's replay does.
    """
    raw = str(parameters.get("rps_market_open_time", "09:15") or "09:15")
    hour, _, minute = raw.partition(":")
    zone = ZoneInfo(str(parameters.get("rps_market_timezone", "Asia/Kolkata") or "Asia/Kolkata"))
    local = event_stamp.astimezone(zone)
    opens = local.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
    delay = timedelta(minutes=int(parameters.get("rps_entry_delay_minutes", 0) or 0))
    return local >= opens + delay


def _entry_in_progress(instance, legs):
    """Whether the locked entry still has work in the OMS or at the broker.

    The selection is locked while its legs are being entered, so a bar that
    arrives mid-entry targets the same contracts. Once every leg's order is
    terminal, and every unsent intent belongs to a finished rebalance, the entry
    is over. A strategy that is still flat then (a leg was rejected, or a
    partial structure was unwound) must select again from the current chain;
    reusing the old snapshot would re-enter on its stale spot, quotes and scores.
    """
    from django.db.models import Q

    from apps.oms.models import OrderIntent
    from apps.rebalancing.coordinator import ACTIVE_ORDER_STATUSES, ACTIVE_REBALANCE_STATUSES
    from apps.rebalancing.services import TERMINAL_INTENT_STATUSES

    unsent = Q(order__isnull=True) & (
        Q(rebalance__isnull=True) | Q(rebalance__status__in=ACTIVE_REBALANCE_STATUSES)
    ) & ~Q(operation_status__in={*TERMINAL_INTENT_STATUSES, "COMPLETED", "USER_CANCELLED", "BROKER_REJECTED"})
    return OrderIntent.objects.filter(
        attributions__strategy_instance=instance,
        instrument_id__in={int(leg["instrument_id"]) for leg in legs},
    ).filter(Q(order__status__in=ACTIVE_ORDER_STATUSES) | unsent).exists()


def prepare_option_context(instance, version, bar, indicators, *, dhan_client=None, gateway=None):
    """Prepare an auditable, executable option selection for one final bar."""
    held_ids = list(StrategyAttributedPosition.objects.filter(
        strategy_instance=instance, portfolio=instance.portfolio,
    ).exclude(quantity=0).values_list("instrument_id", flat=True))
    if held_ids:
        _ensure_option_leases(instance, [{"instrument_id": instrument_id} for instrument_id in held_ids])
        return prepare_exit_context(instance, bar, gateway=gateway)

    locked_snapshot_id = (instance.state_data or {}).get("selection_snapshot_id")
    locked_legs = (instance.state_data or {}).get("legs")
    if locked_legs and locked_snapshot_id and _entry_in_progress(instance, locked_legs):
        locked = OptionSelectionSnapshot.objects.filter(
            pk=locked_snapshot_id, strategy_instance=instance,
        ).first()
        if locked:
            _ensure_option_leases(instance, locked.selected_legs)
            return _context_from_snapshot(instance, locked)

    market_bar_id = str(bar.get("bar_id") or bar.get("event_id"))
    market_bar_version = int(bar.get("version", 1))
    existing = OptionSelectionSnapshot.objects.filter(
        strategy_instance=instance, strategy_version=version,
        market_bar_id=market_bar_id, market_bar_version=market_bar_version,
    ).first()
    if existing:
        _ensure_option_leases(instance, existing.selected_legs)
        return _context_from_snapshot(instance, existing)

    parameters = instance.parameters
    event_stamp = parse_datetime(str(bar.get("window_end"))) or timezone.now()
    event_date = event_stamp.date()
    # Refused before the chain is fetched, as in the source, so an intraday
    # timeframe does not spend an IBKR chain request on every bar that precedes
    # the window.
    if not _entry_window_open(parameters, event_stamp):
        return {"status": "ENTRY_WINDOW_CLOSED", "reason": "RPS entry window has not opened"}
    client, provider = _chain_client(instance, dhan_client=dhan_client, gateway=gateway)
    expiries = [
        expiry for expiry in client.expiries(instance.instrument, parameters)
        if int(parameters["entry_min_dte"]) <= (expiry - event_date).days <= int(parameters["entry_max_dte"])
    ]
    if not expiries:
        return {"status": "NO_EXPIRY", "reason": f"No {provider} expiry is inside the configured entry DTE window"}
    if instance.definition.key == "CALENDAR_CALL":
        near_candidates = [expiry for expiry in expiries if 1 <= (expiry - event_date).days <= 24]
        far_candidates = [expiry for expiry in expiries if 25 <= (expiry - event_date).days <= 90]
        if not near_candidates or not far_candidates:
            return {"status": "NO_EXPIRY", "reason": "Calendar call requires a 1-24 DTE near expiry and a 25-90 DTE far expiry"}
        near_expiry, far_expiry = near_candidates[0], far_candidates[0]
    else:
        near_expiry = far_expiry = expiries[0]
    main_chain = client.chain(instance.instrument, parameters, near_expiry)
    chains = {"main": main_chain, "near": main_chain}
    chains["far"] = client.chain(instance.instrument, parameters, far_expiry) if far_expiry != near_expiry else main_chain

    spot = main_chain["spot"]
    observation = _observation(
        instance, bar, event_date, spot=spot,
        chain_rows=main_chain["rows"], observed_at=main_chain["observed_at"],
    )
    if observation is None or observation["rps_base"] is None:
        return {"status": "MISSING_BOUNDARY", "reason": "RPS is unavailable from causal daily inputs"}
    score = D(str(observation["rps_base"]))
    threshold = D(str(parameters["rps_threshold"]))
    # Carried on every refusal from here on, so the operator sees the score and
    # the floor it was measured against whichever later gate refused.
    measured = {"entry_score": str(score), "entry_threshold": str(threshold), "rps_observation": observation}
    if score < threshold:
        return {
            "status": "ENTRY_GATE_CLOSED",
            "reason": f"RPS {score.quantize(D('0.01'))} below {threshold}",
            **measured,
            "state_data": {"rps_observation": observation},
        }

    candidate_keys = AUTO_CANDIDATES if instance.definition.key == AUTO_OPTION_STRATEGY_KEY else (instance.definition.key,)
    legs_by_candidate = {}
    for key in candidate_keys:
        legs = select_template_legs(key, chains, parameters, spot=spot)
        if legs:
            legs_by_candidate[key] = legs
    if not legs_by_candidate:
        return {
            "status": "NO_LIQUID_STRUCTURE",
            "reason": "No option template passed OI, volume, and price requirements",
            **measured,
        }

    if provider != "IBKR":
        return {
            "status": "UNSUPPORTED_PROVIDER",
            "reason": "The approved entry, selection and exit engines require measured IBKR IV, bid/ask and Greeks",
            **measured,
        }
    chain_rows = {near_expiry: main_chain["rows"]}
    if far_expiry != near_expiry:
        chain_rows[far_expiry] = chains["far"]["rows"]
    candidates, selection_evidence = rank_candidates(
        instance, legs_by_candidate, spot=spot, observation=observation, chains=chain_rows,
        observed_at=main_chain["observed_at"], expiration=near_expiry,
    )
    if not candidates:
        return {
            "status": "NO_ELIGIBLE_STRUCTURE", "reason": selection_evidence["reason"],
            **measured,
            "selection_evidence": selection_evidence,
            "state_data": {"selection_evidence": selection_evidence, "rps_observation": observation},
        }

    ibkr = getattr(client, "definitions", None) or option_chain(
        underlying_instrument=instance.instrument,
        gateway=gateway,
        gateway_session=instance.portfolio.gateway_session,
    )
    selected_key = None
    selected = []
    selected_definitions = []
    for profile in candidates:
        candidate_key = profile["strategy_key"]
        candidate_legs = legs_by_candidate[candidate_key]
        try:
            definitions = [
                leg.get("chain_definition") or _chain_for_expiry(ibkr, leg["expiration"], leg["strike"])
                for leg in candidate_legs
            ]
        except ValueError as exc:
            profile["ibkr_feasible"] = False
            profile["ibkr_rejection"] = str(exc)
            continue
        profile["ibkr_feasible"] = True
        selected_key = candidate_key
        selected = candidate_legs
        selected_definitions = definitions
        break
    if selected_key is None:
        return {
            "status": "NO_IBKR_STRUCTURE",
            "reason": "No selected structure has exact matching strikes and expiries in the IBKR chain",
            **measured,
            "selection_evidence": selection_evidence,
            "candidate_rankings": selection_evidence["rankings"],
        }
    qualified_legs = []
    for leg, definition in zip(selected, selected_definitions):
        instrument, _, option = qualify_option_contract(
            underlying_instrument=instance.instrument,
            expiration=leg["expiration"].isoformat(), strike=str(leg["strike"]),
            right=leg["right"], multiplier=definition["multiplier"],
            trading_class=definition["trading_class"], exchange=definition["exchange"],
            gateway=gateway, gateway_session=instance.portfolio.gateway_session,
        )
        qualified_legs.append({
            "instrument_id": instrument.pk, "conid": instrument.broker_contract.conid,
            "role": leg["role"], "side": leg["side"], "ratio": leg["ratio"],
            "right": option.right, "strike": str(option.strike),
            "expiration": option.expiration.isoformat(), "multiplier": str(option.multiplier),
            "lot_size": str(instrument.lot_size),
            "selection_provider": provider,
            "oi": leg["oi"], "volume": leg["volume"], "selection_price": str(leg["price"]),
        })

    baseline = entry_baseline(selected, observation=observation, spot=float(spot))
    snapshot = OptionSelectionSnapshot.objects.create(
        strategy_instance=instance, strategy_version=version,
        market_bar_id=market_bar_id, market_bar_version=market_bar_version,
        observed_at=main_chain["observed_at"], provider=provider, model_version=MODEL_VERSION,
        atr_reference=D(str(observation["spot"])),
        atr_value=D(str(observation["atr14_daily"])),
        atr_support=D(str(observation["atr_support"])),
        atr_resistance=D(str(observation["atr_resistance"])),
        oi_support=None if observation["oi_support"] is None else D(str(observation["oi_support"])),
        oi_resistance=None if observation["oi_resistance"] is None else D(str(observation["oi_resistance"])),
        spot=spot, entry_score=score, selected_strategy=selected_key,
        expiration=near_expiry, candidate_rankings=selection_evidence["rankings"],
        selected_legs=qualified_legs,
        source_snapshot={
            "observed_at": main_chain["observed_at"].isoformat(),
            "rps_observation": observation,
            "selection_evidence": selection_evidence,
            "exit_baseline": baseline,
            "gateway_mode": instance.portfolio.gateway_session.mode,
            "oi_top_k": rps_engine.OI_TOP_K,
            "atr_multiplier": str(rps_engine.ATR_MULTIPLIER),
            "entry_threshold": str(threshold),
            "entry_score_weights": rps_engine.WEIGHTS,
            "decision_order": [
                "RPS_FLOOR", "APPROVED_RANGE_IV_BREACH_ENTRY", "BOUNDED_LOSS_POP_EV",
                "APPROVED_ROUTING", "HARD_GATES", "TQS_14_FACTOR", "RISK_ADJUSTED_RETURN",
            ],
        },
    )
    _ensure_option_leases(instance, qualified_legs)
    return _context_from_snapshot(instance, snapshot)
