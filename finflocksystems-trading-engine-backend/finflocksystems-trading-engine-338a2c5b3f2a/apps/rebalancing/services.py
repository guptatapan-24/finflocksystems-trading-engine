from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from apps.audit.models import OutboxEvent
from apps.core.idempotency import canonical_request_hash, require_matching_request
from apps.execution.modes import (
    ExecutionMode,
    RunType,
    normalize_run_type,
    require_portfolio_execution_mode,
)
from apps.allocation.models import (
    OrderIntentAttribution,
    PortfolioTargetSnapshot,
    RebalancePolicy,
    RebalanceRun,
    TargetPortfolioPosition,
)
from apps.oms.models import OrderIntent
from apps.portfolios.models import PortfolioPosition
from apps.strategies.models import StrategyAllocation

D = Decimal
VARIABLE_FEE_RATE = D("0.0005")
TERMINAL_ORDER_STATUSES = {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"}
TERMINAL_INTENT_STATUSES = {"RISK_REJECTED", "FAILED"}


def _round_lot(quantity, lot):
    return (quantity/lot).to_integral_value(rounding=ROUND_HALF_UP)*lot


def _floor_lot(quantity, lot):
    from decimal import ROUND_DOWN
    return max(D(0), (quantity/lot).to_integral_value(rounding=ROUND_DOWN)*lot)


def _reference_prices(portfolio, prices, strict, execution_mode=ExecutionMode.LIVE):
    from apps.market_streams.models import InstrumentMarketState
    result, unusable = {}, set()
    if prices:
        result.update({key:D(str(value)) for key,value in prices.items() if not str(key).startswith("lot:")})
    for state in InstrumentMarketState.objects.filter(instrument_id__in=set(result) | set(
            PortfolioPosition.objects.filter(portfolio=portfolio).values_list("instrument_id", flat=True))):
        if state.is_execution_usable(execution_mode=execution_mode) and state.reference_price:
            result[state.instrument_id] = D(state.reference_price)
        elif strict:
            unusable.add(state.instrument_id)
    return result, unusable


def _net_order_policy(snapshot, instrument_id, reference_price, side):
    """Apply the immutable portfolio-level policy selected in the target snapshot."""
    policy = (snapshot.portfolio_order_policy or {}).get(str(instrument_id), {})
    order_type = policy.get("order_type", "MKT")
    time_in_force = policy.get("time_in_force", "DAY")
    limit = None
    if order_type == "LMT":
        offset = D(str(policy.get("limit_offset_bps", 0))) / D(10000)
        limit = reference_price * (D(1) + offset if side == "BUY" else D(1) - offset)
    return order_type, time_in_force, limit


def _snapshot_attribution(snapshot):
    attribution = {}
    contribution_rows = {}
    for row in snapshot.target_contributions:
        instrument_id = int(row["instrument_id"])
        strategy_instance_id = int(row["strategy_instance_id"])
        contribution = D(str(row["effective_weight"]))
        attribution.setdefault(instrument_id, {})[strategy_instance_id] = contribution
        contribution_rows[(instrument_id, strategy_instance_id)] = row
    return attribution, contribution_rows


def _option_execution_stage(snapshot, instrument_id, delta):
    if snapshot is None:
        return None
    rows = [
        row for row in snapshot.target_contributions
        if int(row["instrument_id"]) == int(instrument_id)
        and (row.get("target_metadata") or {}).get("option_strategy")
    ]
    if not rows:
        return None
    stages = []
    for row in rows:
        metadata = row.get("target_metadata") or {}
        if metadata.get("exit"):
            # Cover short contracts before selling long protection.
            stages.append(0 if delta > 0 else 1)
        else:
            # Establish long protection before opening a short contract.
            stages.append(0 if metadata.get("hedge_first") else 1)
    return min(stages)


def _portfolio_order_limit(snapshot, quantity, notional):
    limits = snapshot.portfolio_risk_limits or {}
    maximum_quantity = D(str(limits.get("maximum_order_quantity", "Infinity")))
    maximum_notional = D(str(limits.get("maximum_order_notional", "Infinity")))
    if abs(quantity) > maximum_quantity:
        return "PORTFOLIO_MAXIMUM_ORDER_QUANTITY"
    if abs(notional) > maximum_notional:
        return "PORTFOLIO_MAXIMUM_ORDER_NOTIONAL"
    return ""


def _execution_adv(instrument, target_snapshot, policy, execution_mode=ExecutionMode.LIVE):
    """Resolve only a registered, final, fresh live execution ADV."""
    from django.conf import settings
    from apps.market_streams.models import IndicatorValue

    window = int(getattr(settings, "EXECUTION_AVERAGE_VOLUME_WINDOW", 20))
    cutoff = timezone.now() - timedelta(seconds=int(policy.price_staleness_limit))
    query = IndicatorValue.objects.filter(
        instrument=instrument,
        is_final=True,
        processing_mode="LIVE",
        parameters={"window": window},
        value__isnull=False,
    ).filter(
        Q(indicator_name="average_volume") | Q(indicator="average_volume")
    )
    timeframe=str(getattr(settings,"EXECUTION_ADV_TIMEFRAME","RUNTIME") or "").strip()
    if timeframe.upper()!="RUNTIME":
        query=query.filter(timeframe=timeframe)
    if execution_mode==ExecutionMode.PAPER:
        query=query.filter(Q(event_time__gte=cutoff) | Q(
            bar__market_data_type="DELAYED",bar__produced_at__gte=cutoff,
        ))
    else:
        query=query.filter(event_time__gte=cutoff).exclude(bar__market_data_type="DELAYED")
    row=query.order_by("-event_time", "-pk").first()
    return D(row.value) if row is not None else D(0)


@transaction.atomic
def plan_rebalance(portfolio, trigger, idempotency_key, *, prices=None, nav=None, mode=None,
                   policy=None, strict_market_state=True, optimization_run=None, construction_run=None,
                   target_snapshot=None, available_cash=None, defer=False, retry_failed=False,
                   automatic=False, run_type=RunType.EXECUTION):
    explicit_sources = sum(bool(value) for value in (optimization_run, construction_run, target_snapshot))
    if explicit_sources > 1:
        raise ValueError("A rebalance may use only one immutable target source")
    if not optimization_run and not construction_run and not target_snapshot:
        existing_run = RebalanceRun.objects.filter(
            idempotency_key=idempotency_key
        ).select_related("target_snapshot").first()
        if existing_run and existing_run.target_snapshot_id:
            target_snapshot = existing_run.target_snapshot
        else:
            from apps.rebalancing.coordinator import build_portfolio_target_snapshot
            target_snapshot = build_portfolio_target_snapshot(
                portfolio,
                logical_time=timezone.now(),
                prices=(
                    None
                    if strict_market_state and portfolio.gateway_session_id
                    else prices
                ),
            )
    if target_snapshot:
        target_snapshot = PortfolioTargetSnapshot.objects.get(pk=target_snapshot.pk)
        if target_snapshot.portfolio_id != portfolio.pk:
            raise ValueError("Portfolio target snapshot belongs to another portfolio")
        if target_snapshot.status != "READY":
            raise ValueError("Rejected portfolio target snapshots cannot create a rebalance")
    mode = require_portfolio_execution_mode(
        portfolio,
        mode,
    )
    run_type = normalize_run_type(run_type)
    policy = policy or RebalancePolicy.objects.filter(portfolio=portfolio).first()
    if policy is None:
        policy = RebalancePolicy.objects.create(portfolio=portfolio, mode=mode)
    elif policy.mode != mode:
        policy.mode = mode
        policy.save(update_fields=["mode", "updated_at"])
    if target_snapshot and target_snapshot.execution_mode != mode:
        raise ValueError("Target snapshot mode must match the portfolio Gateway session")
    request_hash=canonical_request_hash("rebalance",{
        "portfolio_id":portfolio.pk,"trigger":trigger,"prices":prices,"nav":nav,"mode":mode,
        "run_type":run_type,
        "policy_id":policy.pk,"strict_market_state":strict_market_state,
        "optimization_run_id":optimization_run.pk if optimization_run else None,
        "construction_run_id":construction_run.pk if construction_run else None,
        "target_snapshot_id":target_snapshot.pk if target_snapshot else None,
        "available_cash":available_cash,"automatic":automatic})
    run, created = RebalanceRun.objects.get_or_create(idempotency_key=idempotency_key, defaults={
        "portfolio": portfolio, "policy": policy, "trigger": trigger, "mode": mode,
        "run_type": run_type,
        "request_hash":request_hash,
        "optimization_run": optimization_run,
        "construction_run": construction_run,
        "target_snapshot": target_snapshot,
        "automatic": automatic,
        "target_source": "PORTFOLIO_OPTIMIZATION" if optimization_run else (
            "GOAL_CONSTRUCTION" if construction_run else "PORTFOLIO_TARGET_SNAPSHOT"
        )})
    if not created:
        require_matching_request(run.request_hash,request_hash)
        if not run.request_hash:
            run.request_hash=request_hash;run.save(update_fields=["request_hash"])
        if run.status=="FAILED" and retry_failed and run.retryable:
            run.targets.all().delete();run.status="QUEUED" if defer else "CALCULATING";run.last_error="";run.retryable=False
            run.attempt_count+=1;run.completed_at=None
            run.save(update_fields=["status","last_error","retryable","attempt_count","completed_at"])
            if defer:return run
        elif run.status!="QUEUED" or defer:return run
        elif run.status=="QUEUED":
            run.status="CALCULATING";run.save(update_fields=["status"])
    elif defer:
        run.status="QUEUED";run.save(update_fields=["status"]);return run
    nav = D(str(nav if nav is not None else (
        target_snapshot.portfolio_nav if target_snapshot else portfolio.account.net_liquidation
    )))
    available_cash = D(str(available_cash if available_cash is not None else (
        target_snapshot.available_cash if target_snapshot else portfolio.account.available_cash
    )))
    if nav <= 0:
        raise ValueError("Portfolio NAV must be positive")
    current_rows = {x.instrument_id:x for x in PortfolioPosition.objects.filter(portfolio=portfolio).select_related("instrument")}
    if optimization_run:
        if optimization_run.portfolio_id != portfolio.pk or optimization_run.status != "COMPLETED":
            raise ValueError("Optimization run is not a completed target set for this portfolio")
        target_weights = {item.instrument_id: D(item.optimized_weight) for item in optimization_run.targets.all()}
        target_quantities = {}
        attribution = {}
    elif construction_run:
        if construction_run.plan.portfolio_id != portfolio.pk or construction_run.status != "COMPLETED":
            raise ValueError("Construction run is not a completed target set for this portfolio")
        target_weights = {item.instrument_id: D(item.target_weight) for item in construction_run.targets.all()}
        target_quantities = {}
        attribution = {}
    else:
        target_weights = {
            int(instrument_id): D(str(weight))
            for instrument_id, weight in target_snapshot.net_targets.items()
        }
        target_quantities = {
            int(instrument_id): D(str(quantity))
            for instrument_id, quantity in target_snapshot.net_target_quantities.items()
        }
        attribution, contribution_rows = _snapshot_attribution(target_snapshot)
    # A strategy target snapshot owns only its contributed instruments. Broker
    # positions without strategy attribution are external/manual holdings and
    # must remain HOLD; treating their absent target as zero would turn an
    # unrelated automatic strategy into an account-wide liquidation.
    instrument_ids = (
        set(target_weights) | set(target_quantities)
        if target_snapshot and automatic
        else set(target_weights) | set(current_rows)
    )
    if target_snapshot:
        no_op_instruments = {
            instrument_id
            for instrument_id in instrument_ids
            if target_weights.get(instrument_id, D(0)) == 0
            and target_quantities.get(instrument_id, D(0)) == 0
            and instrument_id not in current_rows
            and D(
                str(
                    target_snapshot.current_positions.get(
                        str(instrument_id), {}
                    ).get("projected_quantity", 0)
                )
            )
            == 0
        }
        instrument_ids -= no_op_instruments
        reference = {
            int(instrument_id): D(str(price))
            for instrument_id, price in target_snapshot.reference_prices.items()
        }
        unusable = set()
    else:
        reference, unusable = _reference_prices(portfolio, prices or {}, strict_market_state, mode)
    missing = instrument_ids-set(reference)
    if missing:
        raise ValueError(f"No auditable reference price for instruments: {sorted(missing)}")
    run.nav = nav
    run.snapshot = {"nav":str(nav), "cash":str(available_cash), "target_source":run.target_source,
        "optimization_run_id":optimization_run.pk if optimization_run else None,
        "construction_run_id":construction_run.pk if construction_run else None,
        "target_snapshot_id":target_snapshot.pk if target_snapshot else None,
        "positions":target_snapshot.current_positions if target_snapshot else {
            str(key):str(row.quantity) for key,row in current_rows.items()
        },
        "prices":{str(key):str(reference[key]) for key in instrument_ids}}
    candidates = []
    for instrument_id in instrument_ids:
        row = current_rows.get(instrument_id)
        instrument = row.instrument if row else __import__("apps.instruments.models", fromlist=["Instrument"]).Instrument.objects.get(pk=instrument_id)
        price = reference[instrument_id]
        if target_snapshot:
            position_snapshot = target_snapshot.current_positions.get(str(instrument_id), {})
            current = D(str(position_snapshot.get("projected_quantity", 0)))
        else:
            current = D(row.quantity if row else 0)
        multiplier = D(str(instrument.multiplier or 1))
        exact_quantity = instrument_id in target_quantities
        weight = D(target_weights.get(instrument_id, 0)); current_weight = current*price*multiplier/nav
        drift = weight-current_weight
        lot = D(str((prices or {}).get(f"lot:{instrument_id}", instrument.lot_size)))
        target_qty = _round_lot(
            target_quantities[instrument_id] if exact_quantity else weight*nav/(price*multiplier),
            lot,
        )
        if exact_quantity:
            weight = target_qty*price*multiplier/nav
            drift = weight-current_weight
        delta = target_qty-current
        notional = abs(delta*price*multiplier)
        reason = ""
        if instrument_id in unusable: reason = "STALE_OR_UNAVAILABLE_PRICE"
        elif not exact_quantity and abs(drift) < D(policy.instrument_drift_threshold) and trigger not in {"MANUAL", "SCHEDULED", "DEPOSIT", "WITHDRAWAL"}: reason = "BELOW_DRIFT_THRESHOLD"
        elif abs(delta) < D(policy.minimum_trade_quantity): reason = "BELOW_MINIMUM_QUANTITY"
        elif notional < D(policy.minimum_trade_notional): reason = "BELOW_MINIMUM_NOTIONAL"
        elif strict_market_state and not hasattr(instrument, "broker_contract"): reason = "UNQUALIFIED_CONTRACT"
        elif target_snapshot:
            reason = _portfolio_order_limit(target_snapshot, delta, notional)
        estimated_cost = D(policy.fee_buffer) + notional*VARIABLE_FEE_RATE
        benefit = abs(drift)*nav
        if not reason and estimated_cost >= benefit: reason = "COST_EXCEEDS_BENEFIT"
        option_stage = _option_execution_stage(target_snapshot, instrument_id, delta)
        candidates.append({"instrument":instrument, "weight":weight, "current":current, "current_weight":current_weight,
            "drift":drift, "target":target_qty, "delta":delta, "price":price, "lot":lot,
            "multiplier":multiplier,"exact_quantity":exact_quantity,"option_stage":option_stage,
            "estimated_cost":estimated_cost, "reason":reason})
    has_option_stages=any(item["option_stage"] is not None and not item["reason"] for item in candidates)
    has_option_hedge_stage=any(item["option_stage"] == 0 and not item["reason"] for item in candidates)
    candidates.sort(key=lambda x: (
        x["option_stage"] if has_option_stages and x["option_stage"] is not None else (0 if x["delta"]<0 else 1),
        -abs(x["drift"]),x["estimated_cost"],x["instrument"].pk,
    ))
    turnover_used = D(0)
    buy_cash = max(available_cash*(D(1)-D(policy.cash_buffer_percent)), D(0))
    for rank, item in enumerate(candidates):
        item["desired_delta"] = item["delta"]
        if not item["reason"] and item["delta"] > 0:
            affordable = _floor_lot(
                max(buy_cash-D(policy.fee_buffer), D(0))/(item["price"]*item["multiplier"]*(D(1)+VARIABLE_FEE_RATE)),
                item["lot"],
            )
            if affordable < item["delta"]:
                item["delta"] = affordable
            if item["delta"] <= 0:
                item["reason"] = "CASH_OR_FEE_BUFFER"
        trade_turnover = abs(item["delta"]*item["price"]*item["multiplier"])/nav if not item["reason"] else D(0)
        if not item["reason"] and turnover_used+trade_turnover > D(policy.maximum_turnover):
            item["reason"] = "TURNOVER_LIMIT"
        if not item["reason"]:
            turnover_used += trade_turnover
            if item["delta"] < 0 and policy.sell_before_buy:
                sell_notional = abs(item["delta"])*item["price"]*item["multiplier"]
                buy_cash += max(sell_notional-D(policy.fee_buffer)-sell_notional*VARIABLE_FEE_RATE, D(0))
            elif item["delta"] > 0:
                buy_notional = item["delta"]*item["price"]*item["multiplier"]
                buy_cash -= buy_notional+D(policy.fee_buffer)+buy_notional*VARIABLE_FEE_RATE
            item["estimated_cost"] = D(policy.fee_buffer) + abs(item["delta"]*item["price"]*item["multiplier"])*VARIABLE_FEE_RATE
        item["target_row"] = TargetPortfolioPosition.objects.create(rebalance=run, instrument=item["instrument"],
            target_weight=item["weight"], target_quantity=item["target"], trade_quantity=D(0) if item["reason"] else item["delta"],
            reference_price=item["price"], current_quantity=item["current"], current_weight=item["current_weight"],
            drift=item["drift"], lot_size=item["lot"], estimated_cost=item["estimated_cost"],
            suppressed=bool(item["reason"]), suppression_reason=item["reason"], rank=rank)
    planned_has_sells = any(x["delta"] < 0 and not x["reason"] for x in candidates)
    run.total_drift = sum(abs(x["drift"]) for x in candidates)
    run.planned_turnover = turnover_used
    run.phase = (
        "PREVIEW_COMPLETE"
        if run_type == RunType.PREVIEW
        else (
            ("OPTION_HEDGES" if has_option_hedge_stage else "OPTION_SHORTS") if has_option_stages
            else ("SELLS" if policy.sell_before_buy and planned_has_sells else "BUYS")
        )
    )
    run.status = "PLANNED" if run_type == RunType.PREVIEW else "INTENTS_CREATED"
    if has_option_stages:
        run.snapshot={
            **run.snapshot,
            "option_execution_stages": {
                "0": [item["instrument"].pk for item in candidates if item["option_stage"] == 0 and not item["reason"]],
                "1": [item["instrument"].pk for item in candidates if item["option_stage"] == 1 and not item["reason"]],
                "rule": "LONG_PROTECTION_BEFORE_SHORT_ENTRY;SHORT_COVER_BEFORE_LONG_EXIT",
            },
        }
    run.last_recalculated_at = timezone.now()
    run.save(update_fields=["nav","snapshot","total_drift","planned_turnover","phase","status","last_recalculated_at"])
    if run_type == RunType.EXECUTION:
        for item in candidates:
            if item["reason"] or not item["delta"]:
                continue
            is_buy = item["delta"] > 0
            eligible = (
                item["option_stage"] != 1 if run.phase == "OPTION_HEDGES"
                else not (policy.sell_before_buy and is_buy and run.phase == "SELLS")
            )
            contributions = attribution.get(item["instrument"].pk, {})
            order_type,tif,limit_price=_net_order_policy(
                target_snapshot, item["instrument"].pk, item["price"], "BUY" if is_buy else "SELL"
            ) if target_snapshot else ("MKT", "DAY", None)
            version_ids = sorted({
                contribution_rows[(item["instrument"].pk, strategy_instance_id)].get("strategy_version_id")
                for strategy_instance_id in contributions
                if contribution_rows[(item["instrument"].pk, strategy_instance_id)].get("strategy_version_id")
            }) if target_snapshot else []
            contributing_allocations = {
                allocation.strategy_instance_id: allocation
                for allocation in StrategyAllocation.objects.filter(
                    portfolio=portfolio, strategy_instance_id__in=contributions
                ).select_related("strategy_instance")
            }
            intent = OrderIntent.objects.create(rebalance=run, portfolio=portfolio, instrument=item["instrument"],
                side="BUY" if is_buy else "SELL", quantity=abs(item["delta"]), reference_price=item["price"],
                order_type=order_type,time_in_force=tif,limit_price=limit_price,strategy_version_snapshot=sorted(version_ids),
                idempotency_key=f"rebalance:{run.pk}:instrument:{item['instrument'].pk}:v1",
                request_hash=canonical_request_hash("strategy_order_intent",{
                    "rebalance_id":run.pk,"portfolio_id":portfolio.pk,"instrument_id":item["instrument"].pk,
                    "side":"BUY" if is_buy else "SELL","quantity":abs(item["delta"]),"order_type":order_type,
                    "time_in_force":tif,"limit_price":limit_price,"strategy_versions":sorted(version_ids)}), source="REBALANCE",
                origin=OrderIntent.Origin.REBALANCE, mode=mode, requires_fresh_price=True,
                execution_priority=(item["option_stage"] or 0)*10000+item["target_row"].rank, eligible=eligible)
            # Exact multi-leg option lots are already bounded by the strategy,
            # portfolio, quote-freshness, and pre-trade option-collateral checks.
            # The generic single-instrument ADV/weight sizer would independently
            # resize legs and can destroy a defined-risk hedge ratio.
            if strict_market_state and not (
                item["exact_quantity"] and item["instrument"].asset_class == "OPT"
            ):
                from apps.position_sizing.models import PositionSizingPolicy
                from apps.position_sizing.services import size_and_record
                sizing_policy = PositionSizingPolicy.objects.filter(portfolio=portfolio, enabled=True).first() or PositionSizingPolicy.objects.create(portfolio=portfolio)
                limits = target_snapshot.portfolio_risk_limits if target_snapshot else {}
                broker_limits = {
                    "broker_max_quantity": D(str(limits.get("maximum_order_quantity", intent.quantity))),
                    "short_available": item["weight"] >= 0,
                }
                size_and_record(sizing_policy, item["instrument"], intent.side, intent.quantity, item["price"], None, nav,
                    available_cash, _execution_adv(item["instrument"], target_snapshot, policy, mode),
                    broker_limits=broker_limits,strategy_limits={},order_intent=intent,
                    idempotency_key=f"sizing:rebalance:{run.pk}:instrument:{item['instrument'].pk}:v1")
            net_contribution = sum(contributions.values(), D(0))
            attribution_total = sum((abs(value) for value in contributions.values()), D(0)) or D(1)
            strategy_trade_deltas = {
                strategy_instance_id: D(str(
                    contribution_rows[(item["instrument"].pk, strategy_instance_id)].get(
                        "strategy_trade_delta", 0
                    )
                ))
                for strategy_instance_id in contributions
            }
            net_strategy_trade_delta = sum(strategy_trade_deltas.values(), D(0))
            for strategy_instance_id, contribution in contributions.items():
                instance = contributing_allocations[strategy_instance_id].strategy_instance
                contribution_row = contribution_rows[(item["instrument"].pk, strategy_instance_id)]
                allocated_quantity = (
                    item["delta"]
                    * strategy_trade_deltas[strategy_instance_id]
                    / net_strategy_trade_delta
                    if net_strategy_trade_delta
                    else (
                        item["delta"] * contribution / net_contribution
                        if net_contribution
                        else item["delta"] * abs(contribution) / attribution_total
                    )
                )
                OrderIntentAttribution.objects.create(order_intent=intent, strategy_instance=instance,
                    strategy_version_id=contribution_row.get("strategy_version_id"),
                    target_delta=contribution,
                    allocated_quantity=allocated_quantity)
    OutboxEvent.objects.create(topic="portfolio.rebalance.planned.v1", event_type="portfolio.rebalance.planned",
        aggregate_type="portfolio", aggregate_id=str(portfolio.pk), partition_key=str(portfolio.pk),
        payload={"rebalance_run_id":run.pk,"mode":mode,"run_type":run_type,
                 "phase":run.phase,"turnover":str(turnover_used),
                 "target_source":run.target_source,"optimization_run_id":optimization_run.pk if optimization_run else None,
                 "construction_run_id":construction_run.pk if construction_run else None,
                 "target_snapshot_id":target_snapshot.pk if target_snapshot else None},
        idempotency_key=f"rebalance:{run.pk}:planned")
    if run_type == RunType.EXECUTION and run.phase not in {"SELLS","OPTION_HEDGES"}:
        run = _finish_rebalance_at_safe_boundary(run)
    return run


@transaction.atomic
def advance_rebalance(run):
    run = RebalanceRun.objects.select_for_update().get(pk=run.pk)
    if run.run_type != RunType.EXECUTION:
        return run
    if run.phase == "OPTION_HEDGES":
        stage_ids=set((run.snapshot.get("option_execution_stages") or {}).get("0",[]))
        stage_intents=list(OrderIntent.objects.filter(rebalance=run,instrument_id__in=stage_ids))
        requested=sum((D(intent.quantity) for intent in stage_intents),D(0))
        filled=sum((D(intent.order.filled_quantity) for intent in stage_intents if hasattr(intent,"order")),D(0))
        threshold=D(run.policy.partial_fill_threshold if run.policy else "0.95")
        terminal=all(
            (hasattr(intent,"order") and intent.order.status in TERMINAL_ORDER_STATUSES)
            or (not hasattr(intent,"order") and intent.operation_status in TERMINAL_INTENT_STATUSES)
            for intent in stage_intents
        )
        fill_ratio=filled/requested if requested else D(1)
        if terminal and fill_ratio < threshold:
            run.phase="BLOCKED";run.status="PARTIALLY_COMPLETED" if filled else "FAILED"
            run.last_error=(f"Option protection stage ended at fill ratio {fill_ratio}; required threshold is {threshold}. Short-leg intents remain ineligible.")
            run.last_recalculated_at=timezone.now();run.completed_at=timezone.now()
            run.save(update_fields=["phase","status","last_error","last_recalculated_at","completed_at"])
            from apps.rebalancing.coordinator import release_portfolio_coordination
            release_portfolio_coordination(run)
            return run
        if requested == 0 or fill_ratio >= threshold:
            stage_one_ids=set((run.snapshot.get("option_execution_stages") or {}).get("1",[]))
            OrderIntent.objects.filter(rebalance=run,instrument_id__in=stage_one_ids).update(eligible=True)
            run.phase="OPTION_SHORTS";run.status="EXECUTING";run.last_error=""
            run.last_recalculated_at=timezone.now()
            run.save(update_fields=["phase","status","last_error","last_recalculated_at"])
        return _finish_rebalance_at_safe_boundary(run)
    if run.phase != "SELLS":
        return _finish_rebalance_at_safe_boundary(run)
    sell_intents = OrderIntent.objects.filter(rebalance=run, side="SELL")
    requested = sum((D(x.quantity) for x in sell_intents), D(0))
    filled = sum((D(x.order.filled_quantity) for x in sell_intents if hasattr(x, "order")), D(0))
    threshold = D(run.policy.partial_fill_threshold if run.policy else "0.95")
    terminal = all(hasattr(x,"order") and x.order.status in {"FILLED","CANCELLED","REJECTED","EXPIRED"} for x in sell_intents)
    fill_ratio=filled/requested if requested else D(1)
    if terminal and fill_ratio < threshold:
        run.phase="BLOCKED"
        run.status="PARTIALLY_COMPLETED" if filled else "FAILED"
        run.last_error=(f"Sell stage ended at fill ratio {fill_ratio}; required threshold is {threshold}. "
            "Buy intents remain ineligible.")
        run.last_recalculated_at=timezone.now()
        run.completed_at=timezone.now()
        run.save(update_fields=["phase","status","last_error","last_recalculated_at","completed_at"])
        from apps.rebalancing.coordinator import release_portfolio_coordination
        release_portfolio_coordination(run)
        return run
    if requested == 0 or fill_ratio >= threshold:
        portfolio = run.portfolio
        cash = max(D(portfolio.account.available_cash)*(D(1)-D(run.policy.cash_buffer_percent)), D(0))
        positions = {x.instrument_id:D(x.quantity) for x in PortfolioPosition.objects.filter(portfolio=portfolio)}
        turnover_used = sum(
            abs(D(target.trade_quantity) * D(target.reference_price) * D(target.instrument.multiplier or 1)) / D(run.nav)
            for target in run.targets.filter(trade_quantity__lt=0, suppressed=False).select_related("instrument")
        )
        for target in run.targets.filter(trade_quantity__gt=0).order_by("rank"):
            intent = OrderIntent.objects.filter(rebalance=run, instrument=target.instrument, side="BUY").first()
            if not intent or hasattr(intent, "order"):
                continue
            desired = max(D(target.target_quantity)-positions.get(target.instrument_id,D(0)),D(0))
            affordable = _floor_lot(
                max(cash-D(run.policy.fee_buffer),D(0))/(D(target.reference_price)*D(target.instrument.multiplier or 1)*(D(1)+VARIABLE_FEE_RATE)),
                D(target.lot_size),
            )
            turnover_quantity = _floor_lot(
                max(D(run.policy.maximum_turnover)-turnover_used, D(0))*D(run.nav)/(D(target.reference_price)*D(target.instrument.multiplier or 1)),
                D(target.lot_size),
            )
            quantity = min(desired, affordable, turnover_quantity)
            if quantity > 0:
                intent.quantity=quantity;intent.eligible=True;intent.save(update_fields=["quantity","eligible"])
                target.trade_quantity=quantity;target.suppressed=False;target.suppression_reason=""
                target.save(update_fields=["trade_quantity","suppressed","suppression_reason"])
                buy_notional = quantity*D(target.reference_price)*D(target.instrument.multiplier or 1)
                cash -= buy_notional+D(run.policy.fee_buffer)+buy_notional*VARIABLE_FEE_RATE
                turnover_used += quantity*D(target.reference_price)*D(target.instrument.multiplier or 1)/D(run.nav)
            else:
                target.trade_quantity=0;target.suppressed=True;target.suppression_reason="CASH_OR_FEE_BUFFER"
                if affordable > 0 and turnover_quantity <= 0:
                    target.suppression_reason="TURNOVER_LIMIT"
                target.save(update_fields=["trade_quantity","suppressed","suppression_reason"])
        run.phase = "BUYS"
        run.status = "EXECUTING"
        run.last_error = ""
        run.planned_turnover = turnover_used
        run.last_recalculated_at = timezone.now()
        run.save(update_fields=["phase","status","last_error","planned_turnover","last_recalculated_at"])
    return _finish_rebalance_at_safe_boundary(run)


def _finish_rebalance_at_safe_boundary(run):
    intents = list(OrderIntent.objects.filter(rebalance=run).select_related("order"))
    terminal = all(
        (
            hasattr(intent, "order")
            and intent.order.status in TERMINAL_ORDER_STATUSES
        )
        or (
            not hasattr(intent, "order")
            and intent.operation_status in TERMINAL_INTENT_STATUSES
        )
        for intent in intents
    )
    if not terminal:
        return run
    filled = sum(
        (
            D(intent.order.filled_quantity)
            for intent in intents
            if hasattr(intent, "order")
        ),
        D(0),
    )
    failed = any(
        (
            hasattr(intent, "order")
            and intent.order.status in {"CANCELLED", "REJECTED", "EXPIRED"}
        )
        or intent.operation_status in TERMINAL_INTENT_STATUSES
        for intent in intents
    )
    run.phase = "COMPLETE" if not failed else "BLOCKED"
    run.status = "PARTIALLY_COMPLETED" if failed and filled else (
        "FAILED" if failed else "COMPLETED"
    )
    run.completed_at = timezone.now()
    run.last_recalculated_at = timezone.now()
    run.save(
        update_fields=[
            "phase",
            "status",
            "completed_at",
            "last_recalculated_at",
        ]
    )
    if run.status == "COMPLETED" and run.target_snapshot_id:
        flatten_ids = {
            int(row["strategy_instance_id"])
            for row in run.target_snapshot.target_contributions
            if row.get("lifecycle") == "FLATTEN_REQUESTED"
        }
        if flatten_ids:
            from apps.strategies.models import (
                StrategyAttributedPosition,
                StrategyInstance,
            )
            for strategy_instance_id in flatten_ids:
                if not StrategyAttributedPosition.objects.filter(
                    strategy_instance_id=strategy_instance_id
                ).exclude(quantity=0).exists():
                    StrategyInstance.objects.filter(
                        pk=strategy_instance_id,
                        state="FLATTEN_REQUESTED",
                    ).update(state="FLAT", updated_at=timezone.now())
    from apps.rebalancing.coordinator import release_portfolio_coordination
    release_portfolio_coordination(run)
    return run


def recover_incomplete():
    recovered = 0
    for run in RebalanceRun.objects.filter(
        status__in=["INTENTS_CREATED", "EXECUTING"],
        mode__in=ExecutionMode.values,
        run_type=RunType.EXECUTION,
    ):
        advance_rebalance(run)
        recovered += 1
    return recovered
