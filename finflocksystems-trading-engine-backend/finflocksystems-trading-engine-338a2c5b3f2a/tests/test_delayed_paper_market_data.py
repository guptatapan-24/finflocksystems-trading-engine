from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.instruments.models import Instrument
from apps.market_data.fallback import (
    _canonical_outbox_key,
    _provider_market_data_type,
)
from apps.market_streams.models import InstrumentMarketState
from apps.risk.pricing import OrderPriceUnavailable, trusted_market_price
from streaming.flink.jobs.processing import normalize_market_event


pytestmark = pytest.mark.django_db


@pytest.fixture
def instrument():
    return Instrument.objects.create(
        symbol="HDFCBANK",
        asset_class="STK",
        exchange="NSE",
        currency="INR",
    )


def test_delayed_ibkr_price_is_usable_only_for_paper(instrument):
    now = timezone.now()
    state = InstrumentMarketState.objects.create(
        instrument=instrument,
        status="FRESH",
        reference_price=Decimal("725.85"),
        latest_event_at=now,
        market_event_at=now - timedelta(minutes=15),
        reference_price_provider="IBKR",
        reference_price_source="ibkr_delayed",
        market_data_type="DELAYED",
    )

    assert state.is_execution_usable(now, execution_mode="PAPER")
    assert trusted_market_price(instrument, execution_mode="PAPER") == Decimal("725.85")
    assert not state.is_execution_usable(now, execution_mode="LIVE")
    assert not state.is_execution_usable(now)
    with pytest.raises(OrderPriceUnavailable):
        trusted_market_price(instrument, execution_mode="LIVE")


def test_delayed_label_cannot_disguise_a_non_ibkr_source(instrument):
    now = timezone.now()
    state = InstrumentMarketState.objects.create(
        instrument=instrument,
        status="FRESH",
        reference_price=Decimal("725.85"),
        latest_event_at=now,
        market_event_at=now - timedelta(minutes=15),
        reference_price_provider="OTHER",
        reference_price_source="delayed",
        market_data_type="DELAYED",
    )

    assert not state.is_execution_usable(now, execution_mode="PAPER")


def test_warmup_bar_outbox_identity_changes_with_provider_generation():
    payload = {
        "event_kind": "BAR",
        "processing_mode": "WARMUP",
        "instrument_id": 123,
        "timeframe": "1m",
        "window_start": "2026-08-24T09:15:00+00:00",
        "window_end": "2026-08-24T09:16:00+00:00",
        "provider": "IBKR",
        "provider_generation": 1,
    }
    first = _canonical_outbox_key(payload)
    payload["provider_generation"] = 2

    assert _canonical_outbox_key(payload) != first


def test_legacy_ibkr_delayed_event_cannot_be_mislabeled_live(instrument):
    received_at = "2026-08-25T12:00:05+00:00"
    raw = {
        "source_event_id": "ibkr-delayed:265598:2026-08-25T11:45:00+00:00",
        "symbol": instrument.symbol,
        "instrument_id": instrument.pk,
        "event_kind": "TICK",
        "event_time": "2026-08-25T11:45:00+00:00",
        "received_at": received_at,
        "price": "645.12",
        "source": "ibkr_delayed",
        # Legacy Gateway events either omitted this field or supplied the
        # historical LIVE default despite using the delayed IBKR source.
        "market_data_type": "LIVE",
    }

    assert _provider_market_data_type(raw) == "DELAYED"
    canonical = normalize_market_event(raw, {})
    assert canonical["market_data_type"] == "DELAYED"
    assert canonical["received_at"] == received_at


def test_non_delayed_source_rejects_unknown_market_data_type():
    with pytest.raises(ValueError, match="market_data_type"):
        _provider_market_data_type(
            {"source": "ibkr_live", "market_data_type": "UNKNOWN"}
        )
