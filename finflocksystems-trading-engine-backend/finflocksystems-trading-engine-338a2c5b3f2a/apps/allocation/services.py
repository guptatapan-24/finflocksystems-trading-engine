from decimal import Decimal, ROUND_DOWN
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from apps.audit.models import OperationAttempt, OutboxEvent
from apps.core.idempotency import canonical_request_hash, require_matching_request
from apps.execution.modes import RunType, execution_mode_for_portfolio
from apps.strategies.models import StrategyAllocation
from .models import AllocationDecision, AllocationRun, PortfolioFlow, StrategyCapitalSnapshot

D = Decimal
CENT = D("0.01")


def _money(value):
    return D(str(value)).quantize(CENT, rounding=ROUND_DOWN)


def allocate_deposit(amount, nav, strategies):
    amount, nav = _money(amount), D(str(nav))
    if amount <= 0:
        raise ValueError("Deposit amount must be positive")
    eligible = [dict(item) for item in strategies if item.get("enabled", True)]
    desired_nav = nav + amount
    for item in eligible:
        current = D(str(item.get("current", 0)))
        share = max(D(str(item["target_share"])), D(str(item.get("minimum_share", 0))))
        item["deficit"] = max(share*desired_nav-current, D(0))
        share_cap = max(D(str(item.get("maximum_share", 1)))*desired_nav-current, D(0))
        capacity_cap = max(D(str(item.get("capacity", desired_nav)))-current, D(0))
        item["cap"] = min(share_cap, capacity_cap)
    weights = [item["deficit"] for item in eligible]
    if sum(weights) == 0:
        weights = [D(str(item["target_share"])) for item in eligible]
    remaining, allocations = amount, {str(item["id"]): D(0) for item in eligible}
    active = list(range(len(eligible)))
    while remaining >= CENT and active:
        denom = sum(weights[i] for i in active)
        if denom <= 0:
            break
        progressed = D(0)
        for i in list(active):
            item, key = eligible[i], str(eligible[i]["id"])
            room = item["cap"] - allocations[key]
            proposed = _money(remaining * weights[i] / denom)
            add = min(room, proposed)
            if add > 0:
                allocations[key] += add; progressed += add
            if room-add < CENT:
                active.remove(i)
        remaining -= progressed
        if progressed < CENT:
            break
    for i in sorted(active, key=lambda x: (eligible[x].get("priority", 100), str(eligible[x]["id"]))):
        key, room = str(eligible[i]["id"]), eligible[i]["cap"]-allocations[str(eligible[i]["id"])]
        add = min(room, remaining)
        allocations[key] += add; remaining -= add
        if remaining < CENT:
            break
    for item in eligible:
        key, minimum = str(item["id"]), D(str(item.get("minimum_allocation", 0)))
        if 0 < allocations[key] < minimum:
            remaining += allocations[key]; allocations[key] = D(0)
    return allocations, _money(remaining), eligible


def allocate_withdrawal(amount, nav, portfolio_cash, strategies, liquidation_policy="PROPORTIONAL"):
    amount, remaining = _money(amount), _money(amount)
    if amount <= 0 or amount >= D(str(nav)):
        raise ValueError("Withdrawal must be positive and less than NAV")
    decisions = []
    cash_take = min(remaining, max(D(str(portfolio_cash)), D(0)))
    if cash_take:
        decisions.append({"strategy_id": None, "source": "PORTFOLIO_CASH", "amount": _money(cash_take)})
        remaining -= cash_take
    ordered = sorted((dict(x) for x in strategies), key=lambda x: (x.get("priority", 100), str(x["id"])))
    for item in ordered:
        take = min(remaining, max(D(str(item.get("idle_cash", 0))), D(0)))
        if take:
            decisions.append({"strategy_id": item["id"], "source": "STRATEGY_CASH", "amount": _money(take)})
            remaining -= take
        if remaining <= 0: break
    desired_nav = D(str(nav))-amount
    surpluses = [(item, max(D(str(item.get("current", 0)))-D(str(item["target_share"]))*desired_nav, D(0))) for item in ordered]
    total = sum(value for _, value in surpluses)
    if remaining > 0 and total > 0:
        original = remaining
        for item, surplus in surpluses:
            take = min(surplus, _money(original*surplus/total), remaining)
            if take:
                decisions.append({"strategy_id": item["id"], "source": "STRATEGY_SURPLUS", "amount": take})
                remaining -= take
    if remaining > 0:
        candidates=[item for item in ordered if D(str(item.get("current",0)))>0]
        if liquidation_policy=="PROPORTIONAL":
            total_capital=sum(D(str(item.get("current",0))) for item in candidates)
            original=remaining
            for item in candidates:
                take=min(remaining,_money(original*D(str(item.get("current",0)))/total_capital)) if total_capital else D(0)
                if take:
                    decisions.append({"strategy_id":item["id"],"source":"POSITION_LIQUIDATION","amount":take,"liquidation_required":True})
                    remaining-=take
            if remaining and candidates:
                decisions[-1]["amount"]+=remaining;remaining=D(0)
        else:
            # Conviction/liquidity/cost inputs are optional policy metadata; priority is the stable fallback.
            metric={"LOWEST_CONVICTION_FIRST":"conviction","MOST_LIQUID_FIRST":"liquidity",
                    "LOWEST_COST_FIRST":"estimated_cost","PRIORITY_ORDER":"priority"}.get(liquidation_policy,"priority")
            reverse=liquidation_policy=="MOST_LIQUID_FIRST"
            candidates.sort(key=lambda item:(D(str(item.get(metric,item.get("priority",100)))),str(item["id"])),reverse=reverse)
            for item in candidates:
                take=min(remaining,D(str(item.get("current",0))))
                if take:
                    decisions.append({"strategy_id":item["id"],"source":"POSITION_LIQUIDATION","amount":_money(take),"liquidation_required":True})
                    remaining-=take
                if remaining<=0:break
        if remaining>0:
            decisions.append({"strategy_id":None,"source":"UNFUNDED","amount":_money(remaining),"liquidation_required":True})
            remaining=D(0)
    return decisions


def _strategy_rows(portfolio):
    rows = []
    for allocation in StrategyAllocation.objects.filter(portfolio=portfolio).select_related("strategy_instance"):
        instance = allocation.strategy_instance
        rows.append({"id": instance.pk, "enabled": instance.enabled and not instance.kill_switch,
            "target_share": allocation.weight, "current": instance.allocated_capital,
            "minimum_share": allocation.minimum_share, "maximum_share": allocation.maximum_share,
            "capacity": allocation.capacity if allocation.capacity is not None else Decimal("Infinity"),
            "minimum_allocation": allocation.minimum_allocation, "priority": allocation.priority,
            "idle_cash": allocation.idle_cash})
    return rows


def resolve_allocation_mode(portfolio, allocation_mode):
    requested = str(allocation_mode or "AUTO").upper()
    if requested not in {"AUTO", "STRATEGY_ALLOCATION", "PORTFOLIO_OPTIMIZATION"}:
        raise ValueError("Allocation mode must be AUTO, STRATEGY_ALLOCATION, or PORTFOLIO_OPTIMIZATION")
    from apps.portfolio_optimization.models import PortfolioOptimizationPolicy, PortfolioUniverse

    optimization_configured = (
        PortfolioOptimizationPolicy.objects.filter(portfolio=portfolio, enabled=True).exists()
        and PortfolioUniverse.objects.filter(portfolio=portfolio, enabled=True).exists()
    )
    if requested == "PORTFOLIO_OPTIMIZATION" and not optimization_configured:
        raise ValueError("Portfolio optimization mode requires an enabled universe and policy")
    if requested == "AUTO":
        return "PORTFOLIO_OPTIMIZATION" if optimization_configured else "STRATEGY_ALLOCATION"
    return requested


@transaction.atomic
def _create_flow_run(portfolio, flow_type, amount, idempotency_key, effective_at, liquidation_policy,
                     allocation_mode, nav, cash, request_hash):
    from apps.accounts.models import BrokerAccount
    from apps.risk.models import CapitalReservation
    account=BrokerAccount.objects.select_for_update().get(pk=portfolio.account_id)
    flow = PortfolioFlow.objects.select_for_update().filter(idempotency_key=idempotency_key).first()
    if flow:
        stored_hash=flow.request_hash or canonical_request_hash("portfolio_flow",{
            "portfolio_id":flow.portfolio_id,"flow_type":flow.flow_type,"amount":flow.amount,
            "effective_at":None,"liquidation_policy":flow.allocation_run.liquidation_policy,
            "allocation_mode":flow.allocation_run.allocation_mode,"nav":None})
        require_matching_request(stored_hash,request_hash)
        if not flow.request_hash:
            flow.request_hash=stored_hash;flow.save(update_fields=["request_hash"])
        return flow.allocation_run, False
    flow = PortfolioFlow.objects.create(
        portfolio=portfolio,
        flow_type=flow_type,
        amount=amount,
        currency=portfolio.account.base_currency,
        effective_at=effective_at or timezone.now(),
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    run = AllocationRun.objects.create(
        flow=flow,
        portfolio_nav_before=nav,
        portfolio_cash_before=cash,
        approved_amount=amount,
        liquidation_policy=liquidation_policy,
        allocation_mode=allocation_mode,
        snapshot={
            "nav": str(nav),
            "cash": str(cash),
            "mode": execution_mode_for_portfolio(portfolio),
            "resolved_allocation_mode": allocation_mode,
        },
    )
    if flow_type in {"WITHDRAWAL","INTERNAL_TRANSFER_OUT"}:
        reserved=CapitalReservation.objects.filter(account=account,status__in=["ACTIVE","CONSUMED"]).aggregate(
            total=Sum("amount"))["total"] or D(0)
        cash_reservation=min(amount,max(D(account.available_cash)-D(reserved),D(0)))
        if cash_reservation:
            CapitalReservation.objects.create(account=account,portfolio=portfolio,reference_type="PORTFOLIO_FLOW",
                reference_id=str(flow.pk),amount=cash_reservation,estimated_fees=0,
                idempotency_key=f"capital:portfolio-flow:{flow.pk}")
    OperationAttempt.objects.create(operation_type="PORTFOLIO_FLOW",operation_id=str(flow.pk),
        attempt_number=flow.attempt_count,request_hash=flow.request_hash)
    return run, True


@transaction.atomic
def create_strategy_flow_allocation(run):
    run = AllocationRun.objects.select_for_update().select_related("flow__portfolio").get(pk=run.pk)
    flow = run.flow
    portfolio = flow.portfolio
    amount = D(flow.amount)
    nav = D(run.portfolio_nav_before)
    cash = D(run.portfolio_cash_before)
    rows = _strategy_rows(portfolio)
    post_nav = nav + amount if flow.flow_type in {"DEPOSIT", "INTERNAL_TRANSFER_IN"} else nav - amount
    for row in rows:
        target = D(str(row["target_share"]))*post_nav
        current = D(str(row["current"]))
        StrategyCapitalSnapshot.objects.create(allocation_run=run, strategy_instance_id=row["id"], capital_before=current,
            target_capital=target, deficit=max(target-current, 0), surplus=max(current-target, 0), idle_cash=row["idle_cash"])
    if flow.flow_type in {"DEPOSIT", "INTERNAL_TRANSFER_IN"}:
        required_reserve = max(D(portfolio.cash_buffer_pct)*(nav+amount)-cash, D(0))
        investable = max(amount-required_reserve, D(0))
        values, remainder, computed = allocate_deposit(investable, nav, rows) if investable else ({}, D(0), [])
        remainder += amount-investable
        rank = 0
        for row in sorted(computed, key=lambda x: (x.get("priority",100), str(x["id"]))):
            approved = values.get(str(row["id"]), D(0))
            if approved:
                AllocationDecision.objects.create(run=run, strategy_instance_id=row["id"], source="CAPITAL_DEFICIT",
                    requested_amount=amount, approved_amount=approved, rank=rank,
                    binding_constraint="CAPACITY_OR_MAXIMUM" if approved >= row["cap"] else "DEFICIT_WEIGHT",
                    details={"deficit": str(row["deficit"]), "cap": str(row["cap"])})
                rank += 1
        run.unallocated_amount = remainder
    else:
        decisions = allocate_withdrawal(amount, nav, cash, rows, run.liquidation_policy)
        for rank, item in enumerate(decisions):
            approved=D(0) if item["source"]=="UNFUNDED" else item["amount"]
            if item["source"]=="UNFUNDED":run.unallocated_amount+=item["amount"]
            AllocationDecision.objects.create(run=run, strategy_instance_id=item.get("strategy_id"), source=item["source"],
                requested_amount=item["amount"] if item["source"]=="UNFUNDED" else amount, approved_amount=approved, rank=rank,
                binding_constraint="LIQUIDATION_POLICY" if item.get("liquidation_required") else "AVAILABLE_CAPITAL",
                liquidation_required=item.get("liquidation_required", False))
    strategy_changes={}
    direction=D(1) if flow.flow_type in {"DEPOSIT","INTERNAL_TRANSFER_IN"} else D(-1)
    for decision in run.decisions.exclude(strategy_instance__isnull=True):
        strategy_changes[decision.strategy_instance_id]=strategy_changes.get(decision.strategy_instance_id,D(0))+direction*D(decision.approved_amount)
    from apps.strategies.models import StrategyInstance
    for instance in StrategyInstance.objects.select_for_update().filter(pk__in=strategy_changes):
        instance.allocated_capital=max(D(instance.allocated_capital)+strategy_changes[instance.pk],D(0))
        instance.save(update_fields=["allocated_capital"])
    run.approved_amount=amount-run.unallocated_amount
    run.snapshot.update({"post_flow_nav": str(post_nav)})
    run.save(update_fields=["approved_amount", "unallocated_amount", "snapshot"])
    return run


def _optimized_flow_unallocated(flow, run, optimization, rebalance):
    amount = D(flow.amount)
    cash = D(run.portfolio_cash_before)
    if flow.flow_type in {"WITHDRAWAL", "INTERNAL_TRANSFER_OUT"}:
        cash_funding = min(max(cash, D(0)), amount)
        fee_buffer = D(rebalance.policy.fee_buffer) if rebalance.policy_id else D(0)
        sell_proceeds = D(0)
        for target in rebalance.targets.select_related("instrument"):
            notional = (
                max(-D(target.trade_quantity), D(0)) * D(target.reference_price)
                * D(target.instrument.multiplier or 1)
            )
            if notional:
                sell_proceeds += max(notional-fee_buffer-notional*D("0.0005"), D(0))
        return _money(min(max(amount - cash_funding - sell_proceeds, D(0)), amount))

    optimized_values = {
        target.instrument_id: D(target.target_value)
        for target in optimization.targets.all()
    }
    unallocated = D(0)
    cash_or_size_reasons = {
        "CASH_OR_FEE_BUFFER",
        "BELOW_MINIMUM_QUANTITY",
        "BELOW_MINIMUM_NOTIONAL",
    }
    for target in rebalance.targets.all():
        price = D(target.reference_price)
        if price <= 0:
            continue
        current = D(target.current_quantity)
        rounded_desired = max(D(target.target_quantity) - current, D(0))
        executable = max(D(target.trade_quantity), D(0)) if not target.suppressed else D(0)
        raw_desired = max(optimized_values.get(target.instrument_id, D(0)) / price - current, D(0))
        # Lot-size residuals are always real unallocated cash. Rounded quantities
        # are counted only when cash, fee, or minimum-size rules prevented them.
        unallocated += max(raw_desired - rounded_desired, D(0)) * price
        if executable < rounded_desired and (
            not target.suppression_reason or target.suppression_reason in cash_or_size_reasons
        ):
            unallocated += (rounded_desired - executable) * price
    return _money(min(max(unallocated, D(0)), amount))


def create_optimized_flow_allocation(run):
    flow = run.flow
    portfolio = flow.portfolio
    amount = D(flow.amount)
    nav = D(run.portfolio_nav_before)
    cash = D(run.portfolio_cash_before)
    post_nav = nav + amount if flow.flow_type in {"DEPOSIT", "INTERNAL_TRANSFER_IN"} else nav - amount
    if post_nav <= 0:
        raise ValueError("Withdrawal must be positive and less than NAV")
    post_cash = cash + amount if flow.flow_type in {"DEPOSIT", "INTERNAL_TRANSFER_IN"} else max(cash - amount, D(0))

    from apps.portfolio_optimization.services import plan_optimized_rebalance, run_optimization

    optimization = run_optimization(
        portfolio,
        f"optimization:flow:{flow.pk}",
        trigger=flow.flow_type,
        nav=post_nav,
        available_cash=post_cash,
        refresh_history=True,
        flow_reference=f"flow:{flow.pk}",
        retry_failed=run.flow.attempt_count>1,
    )
    rebalance = plan_optimized_rebalance(
        optimization,
        f"rebalance:flow:{flow.pk}:optimization:{optimization.pk}",
        mode=execution_mode_for_portfolio(portfolio),
        run_type=RunType.EXECUTION,
        strict_market_state=True,
        available_cash=post_cash,
    )
    unallocated = _optimized_flow_unallocated(flow, run, optimization, rebalance)
    with transaction.atomic():
        run = AllocationRun.objects.select_for_update().get(pk=run.pk)
        run.optimization_run = optimization
        run.unallocated_amount = unallocated
        run.approved_amount = amount - unallocated
        run.snapshot.update({
            "post_flow_nav": str(post_nav),
            "post_flow_cash": str(post_cash),
            "optimization_run_id": optimization.pk,
            "rebalance_run_id": rebalance.pk,
        })
        run.save(update_fields=["optimization_run", "approved_amount", "unallocated_amount", "snapshot"])
    return run


@transaction.atomic
def _complete_flow(run):
    run = AllocationRun.objects.select_for_update().select_related("flow").get(pk=run.pk)
    flow = run.flow
    portfolio = flow.portfolio
    run.status = "COMPLETED" if run.unallocated_amount==0 else "PARTIALLY_ALLOCATED"
    run.completed_at = timezone.now(); run.save(update_fields=["status", "completed_at", "approved_amount", "unallocated_amount",
        "optimization_run", "allocation_mode", "snapshot"])
    flow.status = "ALLOCATED"; flow.save(update_fields=["status"])
    OutboxEvent.objects.create(topic="portfolio.flow.allocated.v1", event_type="portfolio.flow.allocated",
        aggregate_type="portfolio", aggregate_id=str(portfolio.pk), partition_key=str(portfolio.pk),
        payload={"flow_id": flow.pk, "allocation_run_id": run.pk, "approved_amount": str(run.approved_amount),
                 "unallocated_amount": str(run.unallocated_amount), "allocation_mode":run.allocation_mode,
                 "optimization_run_id":run.optimization_run_id}, idempotency_key=f"flow:{flow.pk}:allocated")
    return run


def execute_flow_allocation(run):
    run=AllocationRun.objects.select_related("flow__portfolio__account","optimization_run").get(pk=run.pk)
    if run.status not in {"QUEUED","CALCULATING"}:return run
    if run.status=="QUEUED":
        AllocationRun.objects.filter(pk=run.pk,status="QUEUED").update(status="CALCULATING")
        OperationAttempt.objects.filter(operation_type="PORTFOLIO_FLOW",operation_id=str(run.flow_id),
            attempt_number=run.flow.attempt_count,status="QUEUED").update(status="PROCESSING")
        run.status="CALCULATING"
    try:
        if run.allocation_mode == "PORTFOLIO_OPTIMIZATION":
            run = create_optimized_flow_allocation(run)
            run = _complete_flow(run)
        else:
            with transaction.atomic():
                run = create_strategy_flow_allocation(run)
                run = _complete_flow(run)
        OperationAttempt.objects.filter(operation_type="PORTFOLIO_FLOW",operation_id=str(run.flow_id),
            attempt_number=run.flow.attempt_count).update(status="COMPLETED",result={"allocation_run_id":run.pk},
            completed_at=timezone.now())
        return run
    except Exception as exc:
        retryable=not isinstance(exc,ValueError)
        if run.optimization_run_id:
            retryable=retryable or bool(run.optimization_run.retryable)
        AllocationRun.objects.filter(pk=run.pk).update(status="FAILED", completed_at=timezone.now())
        PortfolioFlow.objects.filter(pk=run.flow_id).update(status="FAILED",retryable=retryable,last_error=str(exc)[:1000])
        OperationAttempt.objects.filter(operation_type="PORTFOLIO_FLOW",operation_id=str(run.flow_id),
            attempt_number=run.flow.attempt_count).update(status="FAILED",retryable=retryable,
            error=str(exc)[:1000],completed_at=timezone.now())
        raise


def create_flow(portfolio, flow_type, amount, idempotency_key, *, nav=None, effective_at=None,
                liquidation_policy="PROPORTIONAL", allocation_mode="AUTO", retry_failed=False,defer_optimized=False):
    if flow_type not in {value for value, _ in PortfolioFlow.TYPES}:
        raise ValueError("Unsupported flow type")
    amount = _money(amount)
    if amount <= 0:
        raise ValueError("Flow amount must be positive")
    resolved_mode = resolve_allocation_mode(portfolio, allocation_mode)
    nav = D(str(nav if nav is not None else portfolio.account.net_liquidation))
    cash = D(portfolio.account.available_cash)
    request_hash=canonical_request_hash("portfolio_flow",{
        "portfolio_id":portfolio.pk,"flow_type":flow_type,"amount":amount,
        "effective_at":effective_at.isoformat() if hasattr(effective_at,"isoformat") else effective_at,
        "liquidation_policy":liquidation_policy,"allocation_mode":allocation_mode,"nav":nav})
    run, created = _create_flow_run(
        portfolio,
        flow_type,
        amount,
        idempotency_key,
        effective_at,
        liquidation_policy,
        resolved_mode,
        nav,
        cash,
        request_hash,
    )
    if not created:
        if run.status!="FAILED":
            return run
        flow=run.flow
        if not retry_failed or not flow.retryable:
            return run
        with transaction.atomic():
            flow=PortfolioFlow.objects.select_for_update().get(pk=flow.pk)
            run=AllocationRun.objects.select_for_update().get(pk=run.pk)
            run.decisions.all().delete();run.capital_snapshots.all().delete()
            flow.status="REQUESTED";flow.retryable=False;flow.last_error="";flow.attempt_count+=1
            flow.save(update_fields=["status","retryable","last_error","attempt_count"])
            run.status="CALCULATING";run.completed_at=None;run.unallocated_amount=0;run.optimization_run=None
            run.save(update_fields=["status","completed_at","unallocated_amount","optimization_run"])
            OperationAttempt.objects.create(operation_type="PORTFOLIO_FLOW",operation_id=str(flow.pk),
                attempt_number=flow.attempt_count,request_hash=flow.request_hash)
    if resolved_mode=="PORTFOLIO_OPTIMIZATION" and defer_optimized:
        AllocationRun.objects.filter(pk=run.pk).update(status="QUEUED")
        OperationAttempt.objects.filter(operation_type="PORTFOLIO_FLOW",operation_id=str(run.flow_id),
            attempt_number=run.flow.attempt_count).update(status="QUEUED")
        run.status="QUEUED";return run
    return execute_flow_allocation(run)
