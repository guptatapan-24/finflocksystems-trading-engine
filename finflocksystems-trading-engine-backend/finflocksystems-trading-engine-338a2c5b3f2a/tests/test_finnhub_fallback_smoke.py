import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.audit.models import OutboxEvent
from apps.instruments.models import BrokerContract, Instrument, InstrumentProviderMapping
from apps.market_data.fallback import failover_subscription, publish_provider_event
from apps.market_data.providers.base import ProviderCandle, ProviderErrorCode, ProviderQuote
from apps.market_streams.models import MarketDataProviderTransition, MarketDataSubscription


pytestmark = pytest.mark.django_db


class SmokeProvider:
    def __init__(self, now):
        self.now = now

    def capabilities(self, asset_class, timeframe):
        return asset_class == "STK" and timeframe == "1m"

    def historical_candles(self, symbol, timeframe, start, end):
        return [ProviderCandle(self.now - timedelta(minutes=1), self.now, Decimal("99"), Decimal("101"),
            Decimal("98"), Decimal("100"), Decimal("1000"))]

    def quote(self, symbol):
        return ProviderQuote(Decimal("100"), self.now)


def payload(item, provider, generation, start):
    end=start+timedelta(seconds=5)
    return {"source_event_id":f"{provider}:{generation}:{start.isoformat()}",
        "subscription_key":f"{item.instrument_id}:{item.timeframe}","instrument_id":item.instrument_id,
        "conid":item.conid,"symbol":item.instrument.symbol,"exchange":"SMART","currency":"USD",
        "event_kind":"BAR","timeframe":"5s","event_time":start.isoformat(),"window_start":start.isoformat(),
        "window_end":end.isoformat(),"open":"100","high":"101","low":"99","close":"100",
        "volume":"10","is_final":True,"source":"finnhub_live" if provider=="FINNHUB" else "ibkr_live",
        "provider":provider,"provider_symbol":"AAPL" if provider=="FINNHUB" else None,
        "provider_generation":str(generation)}


def test_mocked_ibkr_failure_finnhub_takeover_and_confirmed_recovery_smoke(settings):
    settings.MARKET_DATA_FALLBACK_ENABLED=True
    settings.FINNHUB_HISTORICAL_FALLBACK_ENABLED=True
    settings.FINNHUB_LIVE_FALLBACK_ENABLED=True
    settings.PRIMARY_RECOVERY_CONFIRMATION_EVENTS=3
    now=timezone.now().replace(microsecond=0,second=0)
    instrument=Instrument.objects.create(symbol="AAPL",exchange="SMART",primary_exchange="NASDAQ",currency="USD")
    contract=BrokerContract.objects.create(instrument=instrument,conid=265598,primary_exchange="NASDAQ",
        local_symbol="AAPL",qualified_at=now)
    InstrumentProviderMapping.objects.create(instrument=instrument,provider="FINNHUB",provider_symbol="AAPL",
        exchange_mic="XNAS",provider_exchange="NASDAQ",currency="USD",status="VERIFIED",
        verification_method="AUTOMATIC",verified_at=now)
    item=MarketDataSubscription.objects.create(instrument=instrument,conid=contract.conid,timeframe="1m",
        state="ACTIVE",consumer_count=1,required_history_bars=1,last_primary_event_at=now)

    item=failover_subscription(item.pk,ProviderErrorCode.IBKR_ENTITLEMENT,historical=True,client=SmokeProvider(now))
    assert item.active_provider=="FINNHUB"
    live_start=now+timedelta(seconds=5)
    assert publish_provider_event(payload(item,"FINNHUB",item.provider_generation,live_start))["accepted"] is True

    fallback_generation=item.provider_generation;probe_generation=uuid.uuid4()
    MarketDataSubscription.objects.filter(pk=item.pk).update(fallback_state="RECOVERING",
        primary_probe_generation=probe_generation,primary_probe_started_at=timezone.now(),primary_probe_event_count=0)
    item.refresh_from_db()
    for offset in (10,15):
        event_start=now+timedelta(seconds=offset)
        assert publish_provider_event(payload(item,"IBKR",probe_generation,event_start),
            received_at=event_start+timedelta(seconds=5))["accepted"] is False
    event_start=now+timedelta(seconds=20)
    assert publish_provider_event(payload(item,"IBKR",probe_generation,event_start),
        received_at=event_start+timedelta(seconds=5))["accepted"] is True
    item.refresh_from_db()
    assert item.active_provider=="IBKR" and item.provider_generation==probe_generation
    assert publish_provider_event(payload(item,"FINNHUB",fallback_generation,now+timedelta(seconds=25)))["accepted"] is False

    canonical_keys=list(OutboxEvent.objects.filter(topic="market.raw.v1").values_list("idempotency_key",flat=True))
    assert len(canonical_keys)==len(set(canonical_keys))
    assert MarketDataProviderTransition.objects.filter(new_provider="FINNHUB").exists()
    assert MarketDataProviderTransition.objects.filter(new_provider="IBKR",reason="IBKR_RECOVERED").exists()
