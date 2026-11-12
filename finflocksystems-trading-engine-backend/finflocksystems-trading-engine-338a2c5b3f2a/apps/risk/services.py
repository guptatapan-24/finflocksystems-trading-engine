from decimal import Decimal, ROUND_FLOOR

from django.conf import settings
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.audit.models import OutboxEvent
from apps.execution.modes import (
    LiveTradingDisabled,
    execution_mode_for_gateway_mode,
    require_live_trading_allowed,
    require_portfolio_execution_mode,
)
from apps.reconciliation.models import ReconciliationBreak

from .models import CapitalReservation, KillSwitch, PreTradeRiskPolicy, RiskCheckResult


ACTIVE_ORDER_STATUSES = {
    "CREATED", "RISK_APPROVED", "QUEUED", "SUBMITTED", "ACKNOWLEDGED",
    "PARTIALLY_FILLED", "CANCEL_PENDING", "UNKNOWN",
}
RESERVED_SELL_INTENT_STATUSES = {
    "PENDING", "CLAIMED", "RISK_APPROVED", "SUBMITTING", "QUEUED", "BROKER_BLOCKED",
}


def _contract_multiplier(instrument):
    """Return the cash-value multiplier for one unit/contract."""
    if str(instrument.asset_class).upper() != "OPT":
        return Decimal(1)
    option = getattr(instrument, "option_contract", None)
    multiplier = Decimal(option.multiplier if option is not None else instrument.multiplier or 1)
    return multiplier if multiplier > 0 else Decimal(1)


def order_quantity_error(portfolio, instrument, quantity):
    quantity = Decimal(quantity)
    minimum = Decimal(portfolio.minimum_quantity)
    if quantity < minimum:
        return f"quantity must be at least the portfolio minimum of {minimum}"
    lot_size = Decimal(instrument.lot_size or 1)
    if lot_size <= 0:
        return "Instrument lot_size must be positive"
    if not instrument.fractional_support and quantity % lot_size != 0:
        return (
            f"quantity must be a whole multiple of lot_size {lot_size} "
            "because fractional trading is disabled"
        )
    return ""


def _reserved_sell_quantity(intent):
    from apps.oms.models import OrderIntent

    candidates = (
        OrderIntent.objects.filter(
            portfolio=intent.portfolio,
            instrument=intent.instrument,
            side="SELL",
        )
        .exclude(pk=intent.pk)
        .select_related("order")
    )
    reserved = Decimal(0)
    for candidate in candidates:
        if hasattr(candidate, "order"):
            if candidate.order.status in ACTIVE_ORDER_STATUSES:
                reserved += max(
                    Decimal(candidate.order.quantity)
                    - Decimal(candidate.order.filled_quantity),
                    Decimal(0),
                )
            continue
        if candidate.operation_status in RESERVED_SELL_INTENT_STATUSES:
            reserved += Decimal(candidate.quantity)
    return reserved


def _matching_kill_switches(intent):
    account = intent.portfolio.account
    account_ids = [str(account.pk), account.account_id]
    strategy_ids = []
    if intent.strategy_instance_id:
        strategy_ids.append(str(intent.strategy_instance_id))
    query = Q(scope__iexact="GLOBAL", scope_id__in=["", "*"])
    query |= Q(scope__iexact="ACCOUNT", scope_id__in=account_ids)
    query |= Q(scope__iexact="PORTFOLIO", scope_id=str(intent.portfolio_id))
    query |= Q(scope__iexact="INSTRUMENT", scope_id=str(intent.instrument_id))
    if strategy_ids:
        query |= Q(scope__iexact="STRATEGY", scope_id__in=strategy_ids)
        query |= Q(scope__iexact="STRATEGY_INSTANCE", scope_id__in=strategy_ids)
    return KillSwitch.objects.filter(query, enabled=True)


def _unreserved_committed_capital(intent, policy):
    from apps.oms.models import OrderIntent

    reserved_intent_ids = CapitalReservation.objects.filter(
        account=intent.portfolio.account,
        status__in=["ACTIVE", "CONSUMED"],
        order_intent__isnull=False,
    ).values("order_intent_id")
    candidates = OrderIntent.objects.filter(
        portfolio__account=intent.portfolio.account,
        side="BUY",
    ).exclude(pk=intent.pk).exclude(pk__in=reserved_intent_ids).filter(
        Q(operation_status__in=["RISK_APPROVED", "SUBMITTING", "QUEUED"])
        | Q(order__status__in=ACTIVE_ORDER_STATUSES)
    )
    committed = Decimal(0)
    for candidate in candidates:
        price = Decimal(candidate.reference_price or candidate.limit_price or 0)
        if price <= 0:
            continue
        notional = Decimal(candidate.quantity) * price * _contract_multiplier(candidate.instrument)
        committed += notional + notional * Decimal(policy.estimated_commission_rate) + Decimal(policy.estimated_fixed_fee)
    return committed


def _automatic_option_short_coverage(intent):
    """Return staged hedge/collateral evidence for an automatic option short.

    Manual sells and ordinary stock shorts deliberately never enter this path.
    The immutable target snapshot must identify an opening option leg, and each
    defined-risk short must already have its strategy-attributed long hedge.
    """
    if (
        intent.origin != intent.Origin.REBALANCE
        or intent.instrument.asset_class != "OPT"
        or not intent.rebalance_id
        or not intent.rebalance.target_snapshot_id
    ):
        return None
    snapshot = intent.rebalance.target_snapshot
    short_rows = [
        row for row in snapshot.target_contributions
        if int(row["instrument_id"]) == intent.instrument_id
        and (row.get("target_metadata") or {}).get("option_strategy")
        and not (row.get("target_metadata") or {}).get("exit")
        and Decimal(str(row.get("target_quantity") or 0)) < 0
    ]
    if not short_rows:
        return None

    from apps.instruments.models import Instrument
    from apps.portfolios.models import PortfolioPosition
    from apps.strategies.models import StrategyAttributedPosition

    short_contract = intent.instrument.option_contract
    candidate_rows = [
        row for row in snapshot.target_contributions
        if Decimal(str(row.get("target_quantity") or 0)) > 0
        and (row.get("target_metadata") or {}).get("option_strategy")
    ]
    instruments = {
        item.pk: item
        for item in Instrument.objects.filter(
            pk__in={int(row["instrument_id"]) for row in candidate_rows}
        ).select_related("option_contract")
    }
    covered_quantity = Decimal(0)
    collateral_per_contract = Decimal(0)
    coverage = []
    for short_row in short_rows:
        metadata = short_row.get("target_metadata") or {}
        strategy_id = int(short_row["strategy_instance_id"])
        required = abs(Decimal(str(short_row.get("target_quantity") or 0)))
        current_short = StrategyAttributedPosition.objects.filter(
            strategy_instance_id=strategy_id,
            portfolio=intent.portfolio,
            instrument=intent.instrument,
        ).first()
        existing_short = abs(min(
            Decimal(current_short.quantity) if current_short else Decimal(0),
            Decimal(0),
        ))
        remaining_required = max(required - existing_short, Decimal(0))
        if metadata.get("option_strategy") == "CASH_SECURED_PUT":
            covered_quantity += remaining_required
            collateral_per_contract = max(
                collateral_per_contract,
                Decimal(short_contract.strike) * _contract_multiplier(intent.instrument),
            )
            coverage.append({
                "strategy_instance_id": strategy_id,
                "coverage": "CASH_SECURED_PUT",
                "quantity": str(remaining_required),
            })
            continue

        matches = []
        for row in candidate_rows:
            if int(row["strategy_instance_id"]) != strategy_id:
                continue
            hedge = instruments.get(int(row["instrument_id"]))
            if hedge is None or not hasattr(hedge, "option_contract"):
                continue
            contract = hedge.option_contract
            if contract.underlying_conid != short_contract.underlying_conid:
                continue
            same_expiry_hedge = (
                contract.expiration == short_contract.expiration
                and (
                    (short_contract.right == "P" and contract.right == "P" and contract.strike < short_contract.strike)
                    or (short_contract.right == "C" and contract.right == "C" and contract.strike > short_contract.strike)
                )
            )
            calendar_hedge = (
                short_contract.right == "C"
                and contract.right == "C"
                and contract.expiration > short_contract.expiration
                and contract.strike <= short_contract.strike
            )
            if same_expiry_hedge or calendar_hedge:
                matches.append((hedge, contract, same_expiry_hedge))
        if not matches:
            return None
        hedge, hedge_contract, same_expiry = min(
            matches,
            key=lambda item: (
                not item[2],
                abs(item[1].strike - short_contract.strike),
                item[1].expiration,
            ),
        )
        attributed = StrategyAttributedPosition.objects.filter(
            strategy_instance_id=strategy_id,
            portfolio=intent.portfolio,
            instrument=hedge,
        ).first()
        portfolio_position = PortfolioPosition.objects.filter(
            portfolio=intent.portfolio,
            instrument=hedge,
        ).first()
        available = min(
            max(Decimal(attributed.quantity), Decimal(0)) if attributed else Decimal(0),
            max(Decimal(portfolio_position.quantity), Decimal(0)) if portfolio_position else Decimal(0),
        )
        protected = min(
            remaining_required,
            max(available - existing_short, Decimal(0)),
        )
        if protected <= 0:
            return None
        covered_quantity += protected
        per_contract = (
            abs(Decimal(hedge_contract.strike) - Decimal(short_contract.strike))
            if same_expiry else Decimal(short_contract.strike)
        ) * _contract_multiplier(intent.instrument)
        collateral_per_contract = max(collateral_per_contract, per_contract)
        coverage.append({
            "strategy_instance_id": strategy_id,
            "coverage": "DEFINED_RISK_HEDGE" if same_expiry else "CALENDAR_HEDGE",
            "hedge_instrument_id": hedge.pk,
            "quantity": str(protected),
        })
    return {
        "covered_quantity": covered_quantity,
        "collateral_per_contract": collateral_per_contract,
        "coverage": coverage,
    }


@transaction.atomic
def option_risk_bypassed(intent):
    """Whether pre-trade risk evaluation is switched off for this order.

    `OPTION_PRETRADE_RISK_ENABLED=false` removes the pre-trade risk engine for
    option orders entirely: no policy limits, no notional or exposure caps, no
    capital reservation, and no `available_position` check -- which is the one
    that rejects the short legs of a credit structure, because selling an
    option you do not already hold is a short sale and short selling is off.

    Kill switches are deliberately still honoured. They are an operator's stop,
    not a risk measurement, and an order that ignores an explicit stop is a
    different thing from an order that skips a limit check.
    """
    return (
        intent.instrument.asset_class == "OPT"
        and not getattr(settings, "OPTION_PRETRADE_RISK_ENABLED", True)
    )


def evaluate_intent(intent, gateway_state=None):
    from apps.accounts.models import BrokerAccount
    from apps.oms.models import OrderIntent

    gateway_state = gateway_state or {}
    intent = OrderIntent.objects.select_for_update(of=("self",)).select_related(
        "portfolio__account", "portfolio__gateway_session", "instrument", "strategy_instance"
    ).get(pk=intent.pk)
    account = BrokerAccount.objects.select_for_update().get(pk=intent.portfolio.account_id)
    intent.portfolio.account = account
    policy, _ = PreTradeRiskPolicy.objects.get_or_create(portfolio=intent.portfolio)
    policy = PreTradeRiskPolicy.objects.select_for_update().get(pk=policy.pk)
    requested = Decimal(intent.quantity)
    approved = requested
    checks = []

    def add(name, decision, reason, qty=None, details=None):
        nonlocal approved
        if qty is not None:
            approved = min(approved, Decimal(qty))
        check = RiskCheckResult.objects.create(
            order_intent=intent,
            check_name=name,
            decision=decision,
            reason=reason,
            requested_quantity=requested,
            approved_quantity=approved,
            details=details or {},
        )
        checks.append(check)
        OutboxEvent.objects.create(
            topic="risk.decisions.v1",
            event_type="risk.decision.recorded",
            aggregate_type="order_intent",
            aggregate_id=str(intent.pk),
            partition_key=intent.idempotency_key,
            payload={
                "risk_check_id":check.pk,"check":name,"decision":decision,
                "requested_quantity":str(requested),"approved_quantity":str(approved),
                "reason":reason,"origin":intent.origin,
            },
            idempotency_key=f"risk-check:{check.pk}:recorded",
        )

    model_killed = (
        settings.GLOBAL_KILL_SWITCH
        or intent.portfolio.kill_switch
        or account.kill_switch
        or bool(intent.strategy_instance_id and intent.strategy_instance.kill_switch)
    )
    matched_switches = list(_matching_kill_switches(intent).values("scope", "scope_id", "reason"))
    if model_killed or matched_switches:
        add("kill_switch", "REJECTED", "A matching kill switch is active", 0, {"matches": matched_switches})
        return "REJECTED", Decimal(0), checks
    if option_risk_bypassed(intent):
        # Still refuse to send into a disconnected gateway: that is an
        # execution precondition, not a risk limit, and an order placed blind
        # leaves the account in an uncertain state.
        if not gateway_state.get("connected", False):
            add("gateway", "HELD", "Gateway is disconnected", 0)
            return "HELD", Decimal(0), checks
        # The approved path marks the intent RISK_APPROVED before returning and
        # the dispatcher only picks up intents in that state, so the bypass has
        # to do the same or the order is silently never sent. No capital
        # reservation is written, which is the point: `settle_order_reservation`
        # releases by filter and is a no-op when there is nothing reserved.
        intent.operation_status = "RISK_APPROVED"
        intent.save(update_fields=["operation_status"])
        add(
            "option_risk_bypass",
            "APPROVED",
            "Pre-trade risk evaluation is disabled for option orders",
            requested,
            {
                "asset_class": intent.instrument.asset_class,
                "side": intent.side,
                "setting": "OPTION_PRETRADE_RISK_ENABLED=false",
            },
        )
        return "APPROVED", requested, checks
    if not policy.enabled:
        add("risk_policy", "REJECTED", "Pre-trade risk policy is disabled", 0)
        return "REJECTED", Decimal(0), checks
    if not gateway_state.get("connected", False):
        add("gateway", "HELD", "Gateway is disconnected", 0)
        return "HELD", Decimal(0), checks
    try:
        intent_mode = require_portfolio_execution_mode(
            intent.portfolio,
            intent.mode,
        )
        health_mode = execution_mode_for_gateway_mode(gateway_state.get("mode"))
        if health_mode != intent_mode:
            raise ValueError(
                "Intent, Gateway session, and Gateway health modes must match"
            )
        require_live_trading_allowed(intent_mode)
    except LiveTradingDisabled as exc:
        add("live_trading", "REJECTED", str(exc), 0)
        return "REJECTED", Decimal(0), checks
    except ValueError as exc:
        add("gateway_mode", "REJECTED", str(exc), 0)
        return "REJECTED", Decimal(0), checks
    account_breaks = ReconciliationBreak.objects.filter(
        run__broker_account=account, material=True, resolved=False
    ).filter(
        Q(run__gateway_session=intent.portfolio.gateway_session)
        | Q(run__gateway_session__isnull=True)
    ).exists()
    if not gateway_state.get("reconciled", False) or account_breaks:
        add("reconciliation", "HELD", "Broker account state is not reconciled", 0)
        return "HELD", Decimal(0), checks
    if not intent.instrument.tradable:
        add("instrument", "REJECTED", "Instrument is not tradable", 0)
        return "REJECTED", Decimal(0), checks
    if not intent.eligible:
        add("execution_sequence", "HELD", "Intent is waiting for sell-stage completion", 0)
        return "HELD", Decimal(0), checks
    quantity_error = order_quantity_error(intent.portfolio, intent.instrument, requested)
    if quantity_error:
        add("order_quantity", "REJECTED", quantity_error, 0)
        return "REJECTED", Decimal(0), checks

    from apps.risk.pricing import (
        OrderPriceUnavailable,
        resolve_order_risk_price,
        trusted_market_price,
    )

    try:
        if intent.requires_fresh_price:
            trusted_market_price(intent.instrument, intent_mode)
        if intent.origin == OrderIntent.Origin.MANUAL or intent.requires_fresh_price:
            price = resolve_order_risk_price(
                intent.instrument,
                intent.order_type,
                intent.side,
                limit_price=intent.limit_price,
                stop_price=intent.stop_price,
                execution_mode=intent_mode,
            )
            if intent.reference_price != price:
                intent.reference_price = price
                intent.save(update_fields=["reference_price"])
        else:
            price = Decimal(intent.reference_price or intent.limit_price or 0)
    except OrderPriceUnavailable as exc:
        add("market_freshness", "REJECTED", str(exc), 0)
        return "REJECTED", Decimal(0), checks

    instrument_currency = str(intent.instrument.currency or "").strip().upper()
    account_currency = str(account.base_currency or "").strip().upper()
    if instrument_currency != account_currency:
        add(
            "currency_conversion",
            "REJECTED",
            "Cross-currency risk conversion is unavailable; instrument and account base currencies must match",
            0,
            {"instrument_currency": instrument_currency, "account_currency": account_currency},
        )
        return "REJECTED", Decimal(0), checks

    if hasattr(intent, "sizing_decision"):
        sized = Decimal(intent.sizing_decision.approved_quantity)
        if sized <= 0:
            add("position_sizing", "REJECTED", intent.sizing_decision.rejected_reason or "Position sizing approved zero quantity", 0)
            return "REJECTED", Decimal(0), checks
        approved = min(approved, sized)

    approved = min(approved, Decimal(policy.maximum_order_quantity))
    if price > 0:
        unit_notional = price * _contract_multiplier(intent.instrument)
        approved = min(approved, Decimal(policy.maximum_order_notional) / unit_notional)
    if not intent.instrument.fractional_support:
        lot_size = Decimal(intent.instrument.lot_size or 1)
        approved = (approved / lot_size).to_integral_value(rounding=ROUND_FLOOR) * lot_size
    if approved <= 0:
        add("policy_limits", "REJECTED", "Persisted risk policy approved zero quantity", 0)
        return "REJECTED", Decimal(0), checks

    if intent.side == "SELL":
        from apps.portfolios.models import PortfolioPosition

        position = (
            PortfolioPosition.objects.select_for_update()
            .filter(portfolio=intent.portfolio, instrument=intent.instrument)
            .first()
        )
        long_position = max(
            Decimal(position.quantity) if position is not None else Decimal(0),
            Decimal(0),
        )
        reserved_sell = _reserved_sell_quantity(intent)
        available_position = max(long_position - reserved_sell, Decimal(0))
        if requested > available_position:
            shortfall = requested - available_position
            coverage = _automatic_option_short_coverage(intent)
            if coverage is None:
                add(
                    "available_position",
                    "REJECTED",
                    "SELL quantity exceeds the available unreserved long position; short selling is disabled",
                    0,
                    {
                        "long_position": str(long_position),
                        "reserved_sell_quantity": str(reserved_sell),
                        "available_position_quantity": str(available_position),
                        "short_selling_enabled": False,
                    },
                )
                return "REJECTED", Decimal(0), checks
            collateral_unit = coverage["collateral_per_contract"]
            coverage_details = {
                **coverage,
                "covered_quantity": str(coverage["covered_quantity"]),
                "collateral_per_contract": str(collateral_unit),
            }
            if collateral_unit <= 0:
                add("option_short_coverage", "REJECTED", "Option short collateral is invalid", 0)
                return "REJECTED", Decimal(0), checks
            collateral_limit = (
                Decimal(policy.maximum_order_notional) / collateral_unit
            ).to_integral_value(rounding=ROUND_FLOOR)
            approved = min(
                approved,
                available_position + min(
                    shortfall,
                    coverage["covered_quantity"],
                    collateral_limit,
                ),
            )
            if approved <= available_position:
                add(
                    "option_short_coverage", "REJECTED",
                    "Option short collateral exceeds the portfolio order-notional limit", 0,
                    coverage_details,
                )
                return "REJECTED", Decimal(0), checks
            opening_quantity = approved - available_position
            collateral = opening_quantity * collateral_unit
            already_reserved = CapitalReservation.objects.filter(
                account=account, status__in=["ACTIVE", "CONSUMED"]
            ).exclude(order_intent=intent).aggregate(models_sum=Sum("amount"))["models_sum"] or Decimal(0)
            available_cash = Decimal(account.available_cash) * (
                Decimal(1) - Decimal(intent.portfolio.cash_buffer_pct)
            )
            if collateral + Decimal(already_reserved) > available_cash:
                add(
                    "option_short_collateral", "HELD",
                    "Automatic option short exceeds cash available after active reservations", 0,
                    {
                        **coverage_details,
                        "required_collateral": str(collateral),
                        "available_cash": str(available_cash),
                        "already_reserved": str(already_reserved),
                    },
                )
                return "HELD", Decimal(0), checks
            CapitalReservation.objects.update_or_create(
                idempotency_key=f"option-short-collateral:{intent.pk}",
                defaults={
                    "account": account,
                    "portfolio": intent.portfolio,
                    "order_intent": intent,
                    "reference_type": "OPTION_SHORT_COLLATERAL",
                    "reference_id": str(intent.pk),
                    "amount": collateral,
                    "estimated_fees": 0,
                    "status": "ACTIVE",
                    "released_at": None,
                },
            )
            add(
                "option_short_coverage",
                "RESIZED" if approved < requested else "APPROVED",
                "Automatic option short is covered by a filled hedge or reserved assignment cash",
                approved,
                {**coverage_details, "required_collateral": str(collateral)},
            )

    decision = "RESIZED" if approved < requested else "APPROVED"

    if intent.side == "BUY" and price > 0:
        notional = approved * price * _contract_multiplier(intent.instrument)
        fees = notional * Decimal(policy.estimated_commission_rate) + Decimal(policy.estimated_fixed_fee)
        already_reserved = CapitalReservation.objects.filter(
            account=account, status__in=["ACTIVE", "CONSUMED"]
        ).exclude(order_intent=intent).aggregate(models_sum=Sum("amount"))["models_sum"] or Decimal(0)
        short_collateral = Decimal(0)
        if intent.instrument.asset_class == "OPT":
            short_collateral = CapitalReservation.objects.filter(
                portfolio=intent.portfolio,
                order_intent__instrument=intent.instrument,
                reference_type="OPTION_SHORT_COLLATERAL",
                status__in=["ACTIVE", "CONSUMED"],
            ).aggregate(models_sum=Sum("amount"))["models_sum"] or Decimal(0)
        reserved_for_cash_check = max(
            Decimal(already_reserved) - short_collateral,
            Decimal(0),
        )
        committed_without_reservation = _unreserved_committed_capital(intent, policy)
        available = Decimal(account.available_cash) * (Decimal(1) - Decimal(intent.portfolio.cash_buffer_pct))
        required = notional + fees
        if required + reserved_for_cash_check + committed_without_reservation > available:
            add("available_cash", "HELD", "Order exceeds cash after active reservations, fees, and pending withdrawals", 0,
                {"available":str(available),"reserved":str(reserved_for_cash_check),
                 "offset_option_short_collateral":str(short_collateral),
                 "unreserved_commitments":str(committed_without_reservation),"required":str(required)})
            return "HELD", Decimal(0), checks
        CapitalReservation.objects.update_or_create(
            idempotency_key=f"capital:order-intent:{intent.pk}",
            defaults={
                "account":account,"portfolio":intent.portfolio,"order_intent":intent,
                "reference_type":"ORDER_INTENT","reference_id":str(intent.pk),"amount":required,
                "estimated_fees":fees,"status":"ACTIVE","released_at":None,
            },
        )

    intent.operation_status = "RISK_APPROVED"
    intent.save(update_fields=["operation_status"])
    add("pre_trade", decision, "Persisted policy and capital checks passed", approved,
        {"policy_id":policy.pk,"policy_version":policy.version})
    return decision, approved, checks


@transaction.atomic
def settle_order_reservation(order, terminal_status):
    reservation = CapitalReservation.objects.select_for_update().filter(order_intent=order.intent).first()
    if not reservation:
        return None
    if terminal_status == "FILLED":
        reservation.status = "CONSUMED"
        reservation.released_at = None
    elif terminal_status in {"CANCELLED", "REJECTED", "EXPIRED"}:
        reservation.status = "RELEASED"
        reservation.released_at = timezone.now()
    else:
        return reservation
    reservation.save(update_fields=["status", "released_at"])
    if (
        terminal_status == "FILLED"
        and order.intent.side == "BUY"
        and order.intent.instrument.asset_class == "OPT"
    ):
        from apps.strategies.models import StrategyAttributedPosition

        remaining_short = StrategyAttributedPosition.objects.filter(
            portfolio=order.intent.portfolio,
            instrument=order.intent.instrument,
            quantity__lt=0,
        ).exists()
        if not remaining_short:
            CapitalReservation.objects.filter(
                portfolio=order.intent.portfolio,
                order_intent__instrument=order.intent.instrument,
                reference_type="OPTION_SHORT_COLLATERAL",
                status__in=["ACTIVE", "CONSUMED"],
            ).update(status="RELEASED", released_at=timezone.now())
    return reservation
