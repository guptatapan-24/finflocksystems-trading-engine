from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist


class OrderPriceUnavailable(ValueError):
    """Raised when durable, trusted pricing cannot support pre-trade risk."""


def trusted_market_price(instrument, execution_mode=None):
    try:
        state = instrument.market_state
    except ObjectDoesNotExist as exc:
        raise OrderPriceUnavailable(
            "A fresh persisted market price is required for this order"
        ) from exc
    if not state.is_execution_usable(execution_mode=execution_mode) or state.reference_price is None:
        raise OrderPriceUnavailable(
            "A fresh persisted market price is required for this order"
        )
    price = Decimal(state.reference_price)
    if not price.is_finite() or price <= 0:
        raise OrderPriceUnavailable(
            "The persisted market price is invalid for pre-trade risk"
        )
    return price


def resolve_order_risk_price(
    instrument, order_type, side, *, limit_price=None, stop_price=None, execution_mode=None
):
    """Return the conservative price used by the common pre-trade risk service."""
    order_type = str(order_type or "MKT").upper()
    limit_price = Decimal(limit_price) if limit_price is not None else None
    stop_price = Decimal(stop_price) if stop_price is not None else None

    if order_type == "MKT":
        return trusted_market_price(instrument, execution_mode)
    if order_type == "LMT":
        if limit_price is None:
            raise OrderPriceUnavailable("LMT orders require limit_price")
        return limit_price
    if order_type == "STP":
        if stop_price is None:
            raise OrderPriceUnavailable("STP orders require stop_price")
        return max(stop_price, trusted_market_price(instrument, execution_mode))
    if order_type == "STP_LMT":
        if stop_price is None or limit_price is None:
            raise OrderPriceUnavailable(
                "STP_LMT orders require both stop_price and limit_price"
            )
        return max(stop_price, limit_price)
    raise OrderPriceUnavailable(f"Unsupported order_type: {order_type}")
