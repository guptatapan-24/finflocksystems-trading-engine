import hashlib
import json
import uuid
from decimal import Decimal
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.utils import timezone
from apps.audit.models import AuditEvent, OutboxEvent
from apps.execution.models import Fill
from apps.portfolios.models import CashLedgerEntry, PortfolioPosition, PositionLedgerEntry
from .models import Order, OrderStatusHistory

ALLOWED = {
 "CREATED": {"RISK_APPROVED", "REJECTED", "BROKER_BLOCKED"}, "RISK_APPROVED": {"QUEUED"},
 "QUEUED": {"SUBMITTED", "BROKER_BLOCKED", "REJECTED", "CANCEL_PENDING", "UNKNOWN"}, "BROKER_BLOCKED": {"QUEUED", "SUBMITTED", "REJECTED", "UNKNOWN"},
 "SUBMITTED": {"ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED", "BROKER_BLOCKED", "REJECTED", "UNKNOWN", "CANCEL_PENDING"},
 "ACKNOWLEDGED": {"PARTIALLY_FILLED", "FILLED", "BROKER_BLOCKED", "CANCEL_PENDING", "CANCELLED", "EXPIRED", "UNKNOWN"},
 "PARTIALLY_FILLED": {"PARTIALLY_FILLED", "FILLED", "CANCEL_PENDING", "CANCELLED", "UNKNOWN"},
 "CANCEL_PENDING": {"QUEUED", "SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED", "CANCEL_PENDING", "CANCELLED", "FILLED", "UNKNOWN"},
 "UNKNOWN": {"QUEUED", "BROKER_BLOCKED", "SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED", "CANCEL_PENDING", "CANCELLED"},
 "CANCELLED": {"CANCELLED", "FILLED"},
}


def _apply_weighted_average_fill(position, signed_quantity, price, multiplier=Decimal(1)):
    """Apply average-cost accounting and return gross realized P&L for this fill."""
    prior_quantity = Decimal(position.quantity)
    prior_average = Decimal(position.average_cost)
    new_quantity = prior_quantity + signed_quantity
    realized_pnl = Decimal(0)

    if prior_quantity == 0 or prior_quantity * signed_quantity > 0:
        if new_quantity:
            position.average_cost = (
                abs(prior_quantity) * prior_average + abs(signed_quantity) * price
            ) / abs(new_quantity)
    elif signed_quantity:
        closed_quantity = min(abs(prior_quantity), abs(signed_quantity))
        direction = Decimal(1) if prior_quantity > 0 else Decimal(-1)
        realized_pnl = (price - prior_average) * closed_quantity * direction * multiplier
        if new_quantity == 0:
            position.average_cost = 0
        elif prior_quantity * new_quantity < 0:
            position.average_cost = price

    position.quantity = new_quantity
    position.realized_pnl = Decimal(position.realized_pnl) + realized_pnl
    return realized_pnl

@transaction.atomic
def create_order(intent, quantity=None):
    if not intent.eligible:
        raise ValueError("Order intent is not eligible for OMS processing")
    order, created = Order.objects.get_or_create(intent=intent, defaults={"internal_id": str(uuid.uuid4()), "quantity": quantity or intent.quantity})
    if created:
        transition(order, "RISK_APPROVED", "risk", f"order:{order.internal_id}:approved")
        OutboxEvent.objects.create(topic="orders.events.v1",event_type="order.created",aggregate_type="order",
            aggregate_id=order.internal_id,partition_key=order.internal_id,payload={"order_id":order.internal_id},
            idempotency_key=f"outbox:order:{order.internal_id}:created")
    return order

@transaction.atomic
def transition(order, new_status, source, event_key, reason="", *, broker_status="", reason_code="", details=None,
               occurred_at=None, operator_requested=False):
    order = Order.objects.select_for_update().get(pk=order.pk)
    history, created = OrderStatusHistory.objects.get_or_create(event_key=event_key, defaults={"order": order,
        "from_status": order.status, "to_status": new_status, "source": source,"broker_status":broker_status,
        "reason_code":reason_code,"reason":reason,"details":details or {},"occurred_at":occurred_at or timezone.now(),
        "operator_requested":operator_requested})
    if not created: return order
    if new_status not in ALLOWED.get(order.status, set()): raise ValueError(f"Invalid order transition {order.status} -> {new_status}")
    order.status = new_status; order.save(update_fields=["status", "updated_at"])
    return order


def _apply_late_commission(fill, raw_event, incoming_commission):
    """Apply a broker commission correction without replaying fill quantity."""
    current = Decimal(fill.commission)
    incoming = Decimal(incoming_commission)
    if incoming == current or (current != 0 and incoming == 0):
        return fill
    delta = incoming - current
    fill.commission = incoming
    fill.raw_event = raw_event
    fill.save(update_fields=["commission", "raw_event"])

    cash_entry = CashLedgerEntry.objects.select_for_update().get(
        idempotency_key=f"cash:{fill.execution_id}"
    )
    cash_entry.amount = Decimal(cash_entry.amount) - delta
    cash_entry.save(update_fields=["amount"])

    attributions = list(
        fill.order.intent.attributions.select_for_update(of=("self",))
    )
    gross_allocated = sum(
        (abs(Decimal(item.allocated_quantity)) for item in attributions),
        Decimal(0),
    ) or Decimal(1)
    for attribution in attributions:
        cost_share = abs(Decimal(attribution.allocated_quantity)) / gross_allocated
        attribution.allocated_cost = Decimal(attribution.allocated_cost) + (
            delta * cost_share
        )
        attribution.save(update_fields=["allocated_cost"])

    revision = hashlib.sha256(
        f"{fill.execution_id}:{incoming}".encode()
    ).hexdigest()
    AuditEvent.objects.get_or_create(
        idempotency_key=f"audit:fill-commission:{revision}",
        defaults={
            "event_type": "fill.commission.updated",
            "actor": "broker",
            "aggregate_type": "order",
            "aggregate_id": fill.order.internal_id,
            "data": {
                "execution_id": fill.execution_id,
                "previous_commission": str(current),
                "commission": str(incoming),
            },
        },
    )
    OutboxEvent.objects.get_or_create(
        idempotency_key=f"outbox:fill-commission:{revision}",
        defaults={
            "topic": "executions.events.v1",
            "event_type": "execution.commission.updated",
            "aggregate_type": "account",
            "aggregate_id": fill.order.intent.portfolio.account.account_id,
            "partition_key": fill.order.intent.portfolio.account.account_id,
            "payload": {
                "execution_id": fill.execution_id,
                "order_id": fill.order.internal_id,
                "previous_commission": str(current),
                "commission": str(incoming),
            },
        },
    )
    return fill


@transaction.atomic
def apply_execution(order, execution):
    order = Order.objects.select_for_update().select_related("intent__portfolio", "intent__instrument").get(pk=order.pk)
    raw_event = json.loads(json.dumps(execution, cls=DjangoJSONEncoder))
    quantity = Decimal(str(execution["quantity"]))
    price = Decimal(str(execution["price"]))
    commission = Decimal(str(execution.get("commission", 0)))
    currency = execution.get("currency") or order.intent.instrument.currency
    fill, created = Fill.objects.get_or_create(execution_id=execution["execution_id"], defaults={"order": order, "quantity": quantity, "price": price, "commission": commission, "currency": currency, "executed_at": execution.get("executed_at", timezone.now()), "raw_event": raw_event})
    if not created:
        fill = Fill.objects.select_for_update().select_related(
            "order__intent__portfolio__account"
        ).get(pk=fill.pk)
        if (
            fill.order_id != order.pk
            or Decimal(fill.quantity) != quantity
            or Decimal(fill.price) != price
            or fill.currency != currency
        ):
            raise ValueError(
                "Conflicting broker execution payload for an existing execution ID"
            )
        return _apply_late_commission(fill, raw_event, commission)
    old_qty = order.filled_quantity; new_qty = old_qty + fill.quantity
    order.average_fill_price = ((order.average_fill_price * old_qty) + (fill.price * fill.quantity)) / new_qty
    order.filled_quantity = new_qty; order.save(update_fields=["filled_quantity", "average_fill_price", "updated_at"])
    status = "FILLED" if new_qty >= order.quantity else (
        "CANCELLED" if order.status == "CANCELLED" else
        "CANCEL_PENDING" if order.status == "CANCEL_PENDING" else "PARTIALLY_FILLED"
    )
    transition(order, status, "execution", f"execution:{fill.execution_id}")
    signed_qty = fill.quantity if order.intent.side == "BUY" else -fill.quantity
    multiplier = Decimal(order.intent.instrument.multiplier or 1)
    cash = -(signed_qty * fill.price * multiplier) - fill.commission
    position, _ = PortfolioPosition.objects.select_for_update().get_or_create(portfolio=order.intent.portfolio, instrument=order.intent.instrument)
    realized_pnl = _apply_weighted_average_fill(position, signed_qty, fill.price, multiplier)
    position.market_price = fill.price
    position.save(update_fields=["quantity", "average_cost", "realized_pnl", "market_price", "updated_at"])
    PositionLedgerEntry.objects.create(portfolio=order.intent.portfolio, instrument=order.intent.instrument,
        quantity_delta=signed_qty, price=fill.price, realized_pnl=realized_pnl, kind="FILL",
        reference=fill.execution_id, idempotency_key=f"position:{fill.execution_id}")
    CashLedgerEntry.objects.create(portfolio=order.intent.portfolio, amount=cash, currency=fill.currency,
        kind="FILL", reference=fill.execution_id, idempotency_key=f"cash:{fill.execution_id}")
    attributions=list(order.intent.attributions.select_for_update(of=("self",)).select_related("strategy_instance"))
    if attributions and order.quantity:
        from apps.strategies.models import StrategyAttributedPosition
        gross_allocated=sum((abs(Decimal(x.allocated_quantity)) for x in attributions),Decimal(0)) or Decimal(1)
        for attribution in attributions:
            fill_fraction=fill.quantity/Decimal(order.quantity)
            attributed_signed=Decimal(attribution.allocated_quantity)*fill_fraction
            cost_share=abs(Decimal(attribution.allocated_quantity))/gross_allocated
            attribution.allocated_value+=abs(attributed_signed)*fill.price*multiplier
            attribution.allocated_cost+=fill.commission*cost_share
            attribution.save(update_fields=["allocated_value","allocated_cost"])
            if attribution.strategy_instance_id:
                strategy_position,_=StrategyAttributedPosition.objects.select_for_update().get_or_create(
                    strategy_instance=attribution.strategy_instance,portfolio=order.intent.portfolio,instrument=order.intent.instrument)
                prior=Decimal(strategy_position.quantity);updated=prior+attributed_signed
                if attributed_signed>0 and updated>0:
                    strategy_position.average_cost=((Decimal(strategy_position.average_cost)*max(prior,Decimal(0)))+
                        fill.price*attributed_signed)/updated
                elif updated==0:
                    strategy_position.average_cost=0
                strategy_position.quantity=updated
                strategy_position.save(update_fields=["quantity","average_cost","updated_at"])
    AuditEvent.objects.create(event_type="fill.recorded", actor="broker", aggregate_type="order", aggregate_id=order.internal_id, data={"execution_id": fill.execution_id}, idempotency_key=f"audit:fill:{fill.execution_id}")
    OutboxEvent.objects.create(topic="executions.events.v1",event_type="execution.recorded",aggregate_type="account",
        aggregate_id=order.intent.portfolio.account.account_id,partition_key=order.intent.portfolio.account.account_id,
        payload={"execution_id":fill.execution_id,"order_id":order.internal_id,"instrument_id":order.intent.instrument_id,
                 "quantity":str(fill.quantity),"price":str(fill.price),"commission":str(fill.commission)},
        idempotency_key=f"outbox:execution:{fill.execution_id}")
    if order.intent.rebalance_id:
        from apps.rebalancing.services import advance_rebalance
        advance_rebalance(order.intent.rebalance)
    if status == "FILLED":
        from apps.risk.services import settle_order_reservation
        settle_order_reservation(order, status)
    return fill
