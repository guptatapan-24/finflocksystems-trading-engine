from collections import deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from django.test import override_settings

from broker.ib_async_adapter import IBAsyncBrokerAdapter


@pytest.mark.parametrize("count,seconds,expected", [(75, 86400, "122 D"), (252, 86400, "2 Y"), (20, 60, "4 D"), (100, 3600, "28 D")])
def test_historical_duration_counts_sessions_and_uses_ibkr_year_units(count, seconds, expected):
    assert IBAsyncBrokerAdapter._warmup_duration(count, seconds) == expected


@override_settings(IBKR_MARKET_DATA_TYPE="LIVE")
def test_daily_subscription_requests_valid_duration_and_emits_warmup():
    adapter = IBAsyncBrokerAdapter.__new__(IBAsyncBrokerAdapter)
    contract = SimpleNamespace(conId=51497778)
    bar = SimpleNamespace(date=datetime(2026, 9, 18, tzinfo=timezone.utc), open=25000, high=25100, low=24900, close=25050, volume=0)
    class Event:
        def __iadd__(self, callback):
            return self
    class IB:
        def qualifyContracts(self, requested):
            return [contract]
        def reqMarketDataType(self, data_type):
            assert data_type == 1
        def reqHistoricalData(self, requested, **kwargs):
            assert kwargs["durationStr"] == "122 D"
            assert kwargs["barSizeSetting"] == "1 day"
            return [bar]
        def reqRealTimeBars(self, *args):
            return SimpleNamespace(reqId=10, updateEvent=Event())
    adapter.ib = IB()
    adapter._contract = lambda payload: contract
    adapter.contracts = {}
    adapter.market_subscriptions = {}
    adapter.market_request_ids = {}
    adapter.recent_errors = deque()
    adapter.market_events = deque()
    result = adapter.subscribe_market_data({"subscription_key": "1:1d", "instrument_id": 1,
        "conid": 51497778, "symbol": "NIFTY50", "timeframe": "1d", "historical_bars": 75})
    assert result["historical_bar_count"] == 1
    assert adapter.market_events[0]["processing_mode"] == "WARMUP"
    assert adapter.market_events[0]["market_data_type"] == "LIVE"


@override_settings(IBC_TRADING_MODE="paper",IBKR_MARKET_DATA_TYPE="DELAYED")
def test_ibkr_paper_subscription_uses_delayed_historical_updates():
    class Event:
        def __init__(self):self.handlers=[]
        def __iadd__(self,handler):self.handlers.append(handler);return self

    class Bars(list):
        def __init__(self,*values):super().__init__(values);self.reqId=44;self.updateEvent=Event()

    first=SimpleNamespace(date=datetime(2026,8,24,8,54,0,tzinfo=timezone.utc),open=725,high=726,low=724,close=725.5,volume=10)
    second=SimpleNamespace(date=datetime(2026,8,24,8,54,5,tzinfo=timezone.utc),open=725.5,high=727,low=725,close=726,volume=12)
    live=Bars(first,second);live.last=726;live.lastSize=12;live.close=725;live.bid=725.9;live.ask=726.0
    live.marketPrice=lambda:live.last
    contract=SimpleNamespace(conId=382750910)

    class FakeIB:
        def __init__(self):self.market_data_types=[];self.cancelled=[]
        def qualifyContracts(self,_contract):return [contract]
        def reqMarketDataType(self,value):self.market_data_types.append(value)
        def reqMktData(self,value,*_args):assert value is contract;return live
        def cancelMktData(self,value):self.cancelled.append(value)

    adapter=IBAsyncBrokerAdapter.__new__(IBAsyncBrokerAdapter);adapter.ib=FakeIB();adapter.contracts={}
    adapter.market_subscriptions={};adapter.market_events=deque();adapter.market_request_ids={};adapter.recent_errors=deque(maxlen=50)
    adapter._contract=lambda payload:contract
    result=adapter.subscribe_market_data({"subscription_key":"session:7:1m","gateway_subscription_key":"probe-7",
        "instrument_id":7,"conid":382750910,"symbol":"HDFCBANK","exchange":"NSE","currency":"INR",
        "timeframe":"1m","historical_bars":0,"provider_generation":"12345678-1234-5678-1234-567812345678","probe":True})
    assert result["market_data_type"]=="DELAYED" and adapter.ib.market_data_types==[3]
    live.updateEvent.handlers[0](live)
    current=adapter.market_subscriptions["probe-7"]
    adapter._capture_delayed_ticker(current,current["bucket_received_at"]+timedelta(seconds=5))
    event=adapter.drain_market_events()[0]
    assert event["source"]=="ibkr_delayed" and event["market_data_type"]=="DELAYED"
    assert event["received_at"] and event["delay_seconds"]>=900
    adapter.cancel_market_data({"subscription_key":"session:7:1m"})
    assert adapter.ib.cancelled==[contract]

