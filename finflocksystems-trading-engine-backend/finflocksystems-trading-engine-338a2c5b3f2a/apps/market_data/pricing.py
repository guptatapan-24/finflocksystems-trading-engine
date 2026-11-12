from decimal import Decimal

from django.conf import settings
from django.utils import timezone


def effective_position_price(position, at=None):
    at = at or timezone.now()
    broker_price = Decimal(position.market_price)
    broker_fresh = bool(
        broker_price > 0 and position.updated_at
        and (at - position.updated_at).total_seconds() <= settings.MARKET_PRICE_STALE_SECONDS
    )
    if broker_fresh:
        return broker_price, "IBKR", "ibkr_position_snapshot"
    state = getattr(position.instrument, "market_state", None)
    if state and state.is_usable(at) and state.reference_price is not None:
        return Decimal(state.reference_price), state.reference_price_provider or "CANONICAL", state.reference_price_source
    return broker_price, "IBKR" if broker_price > 0 else "NONE", "ibkr_position_snapshot" if broker_price > 0 else ""
