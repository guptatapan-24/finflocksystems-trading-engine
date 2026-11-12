from decimal import Decimal, ROUND_FLOOR
from django.db import transaction
from .models import PositionSizingDecision


D = Decimal


def floor_lot(quantity, lot):
    quantity, lot = D(str(quantity)), D(str(lot))
    if lot <= 0:
        raise ValueError("Lot size must be positive")
    return (quantity / lot).to_integral_value(rounding=ROUND_FLOOR) * lot


def calculate_limits(*, target_quantity, entry_price, stop_price, nav, available_cash, adv,
                     lot_size=1, multiplier=1, max_loss_fraction="0.005", max_weight="0.10",
                     participation="0.01", broker_max_quantity=None, minimum_order=0,
                     fractional_supported=True, short_available=True, side="BUY", minimum_stop_fraction="0.001"):
    q, price, nav, cash = map(lambda x: D(str(x)), [target_quantity, entry_price, nav, available_cash])
    stop = D(str(stop_price)) if stop_price is not None else None
    lot, multiplier, adv = D(str(lot_size)), D(str(multiplier)), D(str(adv or 0))
    if price <= 0 or nav <= 0 or q < 0:
        raise ValueError("Target quantity, entry price and NAV must be valid")
    if side == "SELL" and not short_available:
        broker = D(0)
    else:
        broker = D(str(broker_max_quantity if broker_max_quantity is not None else q))
    risk_budget = D(str(max_loss_fraction)) * nav
    rejected = ""
    if stop is None:
        risk = q
    else:
        distance = abs(price-stop) * multiplier
        if distance <= 0 or distance/price < D(str(minimum_stop_fraction)):
            risk, rejected = D(0), "Stop distance is invalid or too small"
        else:
            risk = floor_lot(risk_budget/distance, lot)
    limits = {"target": q, "risk": risk,
        "weight": floor_lot(D(str(max_weight))*nav/(price*multiplier), lot),
        "liquidity": floor_lot(D(str(participation))*adv, lot),
        "cash": floor_lot(cash/(price*multiplier), lot) if side == "BUY" else q,
        "broker": floor_lot(broker, lot)}
    approved = floor_lot(min(limits.values()), lot)
    if approved < D(str(minimum_order)):
        approved, rejected = D(0), rejected or "Approved quantity is below broker minimum"
    if not fractional_supported:
        approved = approved.to_integral_value(rounding=ROUND_FLOOR)
    order = ["target", "risk", "weight", "liquidity", "cash", "broker"]
    binding = next(name for name in order if limits[name] == min(limits.values()))
    return {**limits, "approved": approved, "binding": binding.upper(), "risk_budget": risk_budget, "rejected_reason": rejected}


@transaction.atomic
def size_and_record(policy, instrument, side, target_quantity, entry_price, stop_price, nav,
                    available_cash, adv, broker_limits=None, order_intent=None, idempotency_key=None,
                    strategy_limits=None):
    if idempotency_key:
        existing = PositionSizingDecision.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            return existing
    broker_limits = broker_limits or {}
    strategy_limits = strategy_limits or {}
    result = calculate_limits(target_quantity=target_quantity, entry_price=entry_price, stop_price=stop_price,
        nav=nav, available_cash=available_cash, adv=adv, lot_size=instrument.lot_size,
        multiplier=instrument.multiplier,max_loss_fraction=strategy_limits.get("max_loss_fraction",policy.max_loss_fraction),
        max_weight=min(D(policy.max_instrument_weight),D(str(strategy_limits.get("max_weight",policy.max_instrument_weight)))),
        participation=policy.max_participation_rate, side=side,
        minimum_stop_fraction=policy.minimum_stop_fraction, **broker_limits)
    defaults={"policy":policy,"order_intent":order_intent,"instrument":instrument,"side":side,
        "target_quantity":target_quantity,"risk_quantity":result["risk"],"weight_quantity":result["weight"],
        "liquidity_quantity":result["liquidity"],"cash_quantity":result["cash"],"broker_quantity":result["broker"],
        "approved_quantity":result["approved"],"entry_price":entry_price,"stop_price":stop_price,
        "risk_budget":result["risk_budget"],"binding_constraint":result["binding"],
        "calculation_version":policy.calculation_version,"rejected_reason":result["rejected_reason"],
        "limits":{key:str(value) for key,value in result.items() if isinstance(value,Decimal)}}
    if idempotency_key:
        decision,_=PositionSizingDecision.objects.get_or_create(idempotency_key=idempotency_key,defaults=defaults)
    else:
        decision=PositionSizingDecision.objects.create(**defaults)
    return decision


def volatility_scaled_candidates(candidates, instrument_cap, gross_limit=1, net_limit=1):
    raw = {key: D(str(item["score"]))/D(str(item["volatility"])) for key, item in candidates.items() if D(str(item["volatility"])) > 0}
    denominator = sum(abs(value) for value in raw.values())
    weights = {key: value/denominator for key, value in raw.items()} if denominator else {key:D(0) for key in raw}
    cap = D(str(instrument_cap))
    capped = {key: max(-cap, min(cap, value)) for key, value in weights.items()}
    target_gross = min(D(str(gross_limit)), sum(abs(x) for x in weights.values()))
    for _ in range(len(capped)+1):
        gross = sum(abs(x) for x in capped.values())
        remainder = target_gross-gross
        free = [key for key,value in capped.items() if abs(value)<cap and raw[key] != 0]
        if remainder <= D("0.0000000001") or not free:
            break
        denom = sum(abs(raw[key]) for key in free)
        for key in free:
            addition = remainder*abs(raw[key])/denom
            capped[key] = (D(1) if raw[key]>0 else D(-1))*min(cap,abs(capped[key])+addition)
    gross = sum(abs(x) for x in capped.values())
    if gross > D(str(gross_limit)):
        scale = D(str(gross_limit))/gross
        capped = {key:value*scale for key,value in capped.items()}
    net = sum(capped.values())
    if abs(net) > D(str(net_limit)) and net:
        scale = D(str(net_limit))/abs(net)
        capped = {key:value*scale for key,value in capped.items()}
    return capped
