"""Warm-up counting when a re-sent history overlaps windows already stored.

A bar id is (instrument, timeframe, window_start) only. When the current
subscription re-sends history for windows an earlier subscription already
stored (yesterday's live bars), persist_bar keeps the stored row and the
re-sent copy never exists under the current provider generation. Only the
windows that were new got counted, so warm-up stalled short (46 of 75).
"""
import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.utils import timezone

from apps.market_streams.models import MarketBar
from apps.market_streams.services import _warmup_bars
from tests.test_manual_quote_leases import _case

pytestmark=pytest.mark.django_db

FIVE=timedelta(minutes=5)


def _bar(instrument, start, *, generation, mode, received, provider="IBKR", data_type="LIVE"):
    return MarketBar.objects.create(
        instrument=instrument,bar_id=f"{instrument.pk}:5m:{start.isoformat()}",interval="5m",
        window_start=start,window_end=start+FIVE,open=Decimal("1"),high=Decimal("2"),low=Decimal("1"),
        close=Decimal("2"),version=1,is_final=True,provider=provider,provider_generation=generation,
        market_data_type=data_type,received_at=received,processing_mode=mode,produced_at=received)


def _scenario(settings, *, today=46, yesterday=40, **older):
    _,session,instrument=_case(settings,"NIFTY50")
    now=timezone.now().replace(second=0,microsecond=0)
    generation=uuid.uuid4()
    subscription=SimpleNamespace(pk=7,provider_generation=generation,active_provider="IBKR",
        requested_at=now-timedelta(minutes=2))
    instance=SimpleNamespace(pk=9,instrument=instrument,timeframe="5m")
    first_today=now-today*FIVE
    for index in range(today):
        _bar(instrument,first_today+index*FIVE,generation=str(generation),mode="WARMUP",received=now)
    # Yesterday's windows, stored by an earlier subscription's live stream.
    yesterday_close=first_today-timedelta(hours=18)
    for index in range(yesterday):
        _bar(instrument,yesterday_close-(index+1)*FIVE,generation=str(uuid.uuid4()),mode="LIVE",
            received=now-timedelta(hours=20),**older)
    return instance,subscription


def test_stored_earlier_windows_complete_a_warmup_the_resent_history_could_not(settings):
    instance,subscription=_scenario(settings)

    bars=_warmup_bars(instance,subscription,75)

    assert len(bars)==80
    starts=[bar.window_start for bar in bars]
    assert starts==sorted(starts)
    # Every recent window still comes from the current subscription.
    assert all(bar.provider_generation==str(subscription.provider_generation) for bar in bars[-46:])


def test_a_different_market_data_type_is_never_counted(settings):
    instance,subscription=_scenario(settings,data_type="DELAYED")

    assert len(_warmup_bars(instance,subscription,75))==46


def test_a_different_provider_is_never_counted(settings):
    instance,subscription=_scenario(settings,provider="FINNHUB")

    assert len(_warmup_bars(instance,subscription,75))==46


def test_stored_windows_outside_the_lookback_are_not_counted(settings):
    settings.WARMUP_BACKFILL_LOOKBACK_DAYS=0
    instance,subscription=_scenario(settings)

    assert len(_warmup_bars(instance,subscription,75))==46


def test_nothing_is_counted_until_the_current_subscription_delivered_history(settings):
    instance,subscription=_scenario(settings,today=0)

    assert _warmup_bars(instance,subscription,75)==[]
