import pytest
from collections import deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from broker.base import BrokerAdapter
from broker.ib_async_adapter import IBAsyncBrokerAdapter
from broker.mock import MockBrokerAdapter
from django.utils import timezone as django_timezone
from django.test import override_settings
from gateway_service.models import GatewayCommand, GatewayCommandAttempt, GatewayEvent, GatewayHealthSnapshot
from gateway_service.services import (
    claim_command,
    claim_next_command,
    compact_gateway_operational_records,
    enqueue,
    process_command,
    recover_expired_commands,
)

pytestmark=pytest.mark.django_db


@override_settings(GATEWAY_EVENT_RETENTION_DAYS=1,GATEWAY_HEALTH_RETENTION_DAYS=1,GATEWAY_COMPACTION_BATCH_SIZE=100)
def test_gateway_compaction_keeps_unacknowledged_events():
    old=django_timezone.now()-timedelta(days=2)
    acknowledged=GatewayEvent.objects.create(event_key="old-ack",event_type="snapshot",acknowledged=True)
    pending=GatewayEvent.objects.create(event_key="old-pending",event_type="snapshot",acknowledged=False)
    health=GatewayHealthSnapshot.objects.create(connected=True,reconciled=True)
    GatewayEvent.objects.filter(pk__in=[acknowledged.pk,pending.pk]).update(created_at=old)
    GatewayHealthSnapshot.objects.filter(pk=health.pk).update(created_at=old)
    compact_gateway_operational_records()
    assert not GatewayEvent.objects.filter(pk=acknowledged.pk).exists()
    assert GatewayEvent.objects.filter(pk=pending.pk).exists()
    assert not GatewayHealthSnapshot.objects.filter(pk=health.pk).exists()

def test_mock_implements_adapter_and_order_lifecycle():
    broker=MockBrokerAdapter(); assert isinstance(broker,BrokerAdapter)
    broker.connect(); matches=broker.search_contracts("BHP"); qualified=broker.qualify_contract(matches[0])
    placed=broker.place_order({"internal_id":"I1","symbol":"AAPL","side":"BUY","quantity":1})
    assert len(matches)==2 and matches[1]["primary_exchange"]=="ASX"
    assert qualified["qualified"] and qualified["conid"]==matches[0]["conid"] and placed["status"]=="Submitted"
    assert broker.modify_order({"internal_id":"I1","quantity":2})["quantity"]==2
    assert broker.cancel_order({"internal_id":"I1"})["status"]=="Cancelled"
    cancellation=broker.drain_order_events()[0]
    assert cancellation["broker_status"]=="Cancelled" and cancellation["operator_requested"] is True
    assert broker.refresh_state()["accounts"] == []

def test_command_processing_persists_callback():
    broker=MockBrokerAdapter(); broker.connect()
    command=GatewayCommand.objects.create(command_type="PLACE_ORDER",idempotency_key="k",payload={"internal_id":"I1","symbol":"AAPL","side":"BUY","quantity":1})
    process_command(command,broker); command.refresh_from_db()
    event=GatewayEvent.objects.get()
    assert command.status=="COMPLETED"
    assert event.payload["command_id"]==command.pk
    assert event.payload["command_type"]=="PLACE_ORDER"
    assert event.payload["payload"]["internal_id"]=="I1"

def test_search_command_returns_multiple_exact_contracts():
    broker=MockBrokerAdapter();broker.connect()
    command=GatewayCommand.objects.create(command_type="SEARCH_CONTRACTS",idempotency_key="search:BHP",payload={"query":"BHP"})
    process_command(command,broker);command.refresh_from_db()
    assert command.status=="COMPLETED" and len(command.result["results"])==2


@override_settings(GATEWAY_CONTRACT_SEARCH_MAX_RESULTS=2)
def test_ibkr_search_prioritizes_exact_stocks_without_unbounded_detail_requests():
    def contract(conid, symbol, sec_type):
        return SimpleNamespace(
            conId=conid,
            symbol=symbol,
            localSymbol=symbol,
            secType=sec_type,
            exchange="SMART",
            primaryExchange="NASDAQ",
            currency="USD",
        )

    contracts = [
        contract(1, "AAPL", "OPT"),
        contract(2, "AAPLX", "STK"),
        contract(3, "AAPL", "STK"),
        contract(4, "AAP", "STK"),
        contract(5, "AAPL", "STK"),
    ]

    class FakeIB:
        RequestTimeout=0

        def reqMatchingSymbols(self, query):
            assert query == "AAPL"
            assert self.RequestTimeout == 12
            return [SimpleNamespace(contract=value) for value in contracts]

        def reqContractDetails(self, value):
            raise AssertionError("symbol search must not expand every match with reqContractDetails")

    adapter=IBAsyncBrokerAdapter.__new__(IBAsyncBrokerAdapter)
    adapter.ib=FakeIB()
    results=adapter.search_contracts("aapl")
    assert [row["conid"] for row in results]==[3,5]
    assert all(row["asset_class"]=="STK" for row in results)
    assert adapter.ib.RequestTimeout==0


def test_ibkr_qualification_uses_operation_deadline_and_restores_client_timeout():
    contract=SimpleNamespace(
        conId=265598,symbol="AAPL",localSymbol="AAPL",secType="STK",
        exchange="SMART",primaryExchange="NASDAQ",currency="USD",
    )

    class FakeIB:
        RequestTimeout=0

        def qualifyContracts(self,value):
            assert value is contract
            assert self.RequestTimeout==15
            return [value]
        def reqContractDetails(self,value):
            return [SimpleNamespace(contract=value,underConId=0,longName="Apple Inc",marketName="AAPL")]

    adapter=IBAsyncBrokerAdapter.__new__(IBAsyncBrokerAdapter)
    adapter.ib=FakeIB()
    adapter.contracts={}
    adapter._contract=lambda payload:contract
    result=adapter.qualify_contract({"conid":contract.conId})
    assert result["qualified"] is True
    assert result["conid"]==contract.conId
    assert adapter.ib.RequestTimeout==0


def test_ibkr_option_chain_uses_qualified_underlying_and_security_definition_parameters():
    underlying=SimpleNamespace(conId=1234,symbol="NIFTY",localSymbol="NIFTY 50",secType="IND",
        exchange="NSE",primaryExchange="NSE",currency="INR")
    details=SimpleNamespace(contract=underlying,underConId=0,longName="NIFTY 50",marketName="NIFTY")
    parameter=SimpleNamespace(exchange="NFO",tradingClass="NIFTY",multiplier="75",
        expirations={"20260826","20260930"},strikes={25100.0,25000.0})
    class FakeIB:
        RequestTimeout=0
        def reqContractDetails(self,contract):
            assert contract.conId==1234 and self.RequestTimeout==30
            return [details]
        def reqSecDefOptParams(self,symbol,fut_exchange,sec_type,conid):
            assert (symbol,fut_exchange,sec_type,conid)==("NIFTY","","IND",1234)
            assert self.RequestTimeout==30
            return [parameter]
    adapter=IBAsyncBrokerAdapter.__new__(IBAsyncBrokerAdapter);adapter.ib=FakeIB()
    adapter._contract=lambda payload:SimpleNamespace(conId=int(payload["conid"]))
    result=adapter.option_chain({"underlying_conid":1234,"symbol":"NIFTY","asset_class":"IND","exchange":"NSE","currency":"INR"})
    assert result["chains"]==[{"exchange":"NFO","trading_class":"NIFTY","multiplier":"75",
        "expirations":["20260826","20260930"],"strikes":["25000.0","25100.0"]}]
    assert adapter.ib.RequestTimeout==0


def test_mock_option_order_requires_exact_option_contract():
    broker=MockBrokerAdapter();broker.connect()
    result=broker.place_option_order({"internal_id":"OPT-1","account":"DU1","symbol":"NIFTY",
        "asset_class":"OPT","conid":7654321,"side":"BUY","quantity":"1"})
    assert result["status"]=="Submitted" and result["asset_class"]=="OPT"
    with pytest.raises(ValueError,match="whole number|asset_class"):
        broker.place_option_order({"internal_id":"BAD","asset_class":"STK","conid":1,"side":"BUY","quantity":"1"})


def test_market_subscription_commands_are_idempotent():
    broker=MockBrokerAdapter();broker.connect();payload={"subscription_key":"1:1m","instrument_id":1,"conid":123,
        "symbol":"AAPL","timeframe":"1m","historical_bars":20}
    first=GatewayCommand.objects.create(command_type="SUBSCRIBE_MARKET_DATA",idempotency_key="sub:1",payload=payload)
    process_command(first,broker)
    assert first.result["state"]=="ACTIVE" and len(broker.subscriptions)==1
    cancel=GatewayCommand.objects.create(command_type="CANCEL_MARKET_DATA",idempotency_key="cancel:1",payload={"subscription_key":"1:1m"})
    process_command(cancel,broker)
    assert cancel.result["state"]=="INACTIVE" and not broker.subscriptions


def test_market_subscription_preserves_provider_epoch_and_probe_runtime_key():
    broker=MockBrokerAdapter();broker.connect();generation="12345678-1234-5678-1234-567812345678"
    payload={"subscription_key":"1:1m","gateway_subscription_key":f"1:1m:probe:{generation}",
        "instrument_id":1,"conid":123,"symbol":"AAPL","timeframe":"1m","historical_bars":0,
        "provider":"IBKR","provider_generation":generation,"probe":True}
    result=broker.subscribe_market_data(payload)
    assert result["subscription_key"]=="1:1m" and result["provider_generation"]==generation
    assert result["probe"] is True and list(broker.subscriptions)==[payload["gateway_subscription_key"]]
    cancelled=broker.cancel_market_data({"subscription_key":"1:1m"})
    assert cancelled["cancelled"]==1 and not broker.subscriptions


def test_ibkr_market_payload_carries_canonical_identity_and_provider_epoch():
    adapter=IBAsyncBrokerAdapter.__new__(IBAsyncBrokerAdapter)
    generation="12345678-1234-5678-1234-567812345678"
    bar=SimpleNamespace(date=datetime(2026,7,15,0,0,tzinfo=timezone.utc),open=100,high=102,low=99,close=101,volume=7)
    payload={"subscription_key":"7:1m","instrument_id":7,"conid":265598,"symbol":"AAPL",
        "exchange":"SMART","currency":"USD","provider_generation":generation}
    event=adapter._market_payload(bar,payload,"ibkr_live","5s",5)
    assert event["instrument_id"]==7 and event["conid"]==265598 and event["provider"]=="IBKR"
    assert event["provider_generation"]==generation and event["window_end"]=="2026-07-15T00:00:05+00:00"

def test_kill_switch_blocks_submission():
    broker=MockBrokerAdapter(); broker.connect(); broker.killed=True
    with pytest.raises(RuntimeError): broker.place_order({"internal_id":"I1"})

def test_ibkr_order_event_preserves_exact_rejection_diagnostics():
    adapter=IBAsyncBrokerAdapter.__new__(IBAsyncBrokerAdapter);adapter.operator_cancellations=set()
    contract=SimpleNamespace(conId=265598,symbol="AAPL",localSymbol="AAPL",secType="STK",exchange="SMART",
        primaryExchange="NASDAQ",currency="USD")
    order=SimpleNamespace(orderRef="internal-1",account="DU1",orderId=881,permId=9901)
    status=SimpleNamespace(status="Inactive",whyHeld="locate pending")
    log=SimpleNamespace(time=datetime(2026,7,13,1,0,tzinfo=timezone.utc),status="Inactive",
        message="Order rejected - insufficient equity",errorCode=201)
    trade=SimpleNamespace(order=order,orderStatus=status,contract=contract,log=[log],advancedError='{"errorCode":201}')
    event=adapter._order_event(trade)
    assert event["error_code"]=="201" and event["error_message"]=="Order rejected - insufficient equity"
    assert event["why_held"]=="locate pending" and event["advanced_reject"]=={"errorCode":201}
    assert event["trade_log"][0]["status"]=="Inactive" and event["occurred_at"]=="2026-07-13T01:00:00+00:00"

def test_ibkr_async_market_error_retains_exact_permission_reason():
    adapter=IBAsyncBrokerAdapter.__new__(IBAsyncBrokerAdapter)
    adapter.ib=SimpleNamespace(trades=lambda:[]);adapter.order_events=deque();adapter.market_events=deque()
    adapter.recent_errors=deque(maxlen=50);adapter.market_subscriptions={"5:1m":{"request_id":42}};adapter.market_request_ids={42:{"subscription_key":"5:1m",
        "instrument_id":5,"conid":265598,"symbol":"AAPL","timeframe":"1m"}}
    adapter._on_error(42,354,"Requested market data is not subscribed",SimpleNamespace(conId=265598))
    event=adapter.drain_market_events()[0]
    assert event["event_kind"]=="ERROR" and event["error_code"]=="354"
    assert event["error_message"]=="Requested market data is not subscribed" and event["subscription_key"]=="5:1m"
    assert not adapter.market_request_ids and not adapter.market_subscriptions


def _expire(command):
    GatewayCommand.objects.filter(pk=command.pk).update(
        lease_expires_at=django_timezone.now()-timedelta(seconds=1)
    )


def test_active_command_lease_cannot_be_claimed_by_another_worker():
    command=enqueue("PLACE_ORDER",{"internal_id":"I1","side":"BUY","quantity":1},"lease")
    first=claim_next_command("worker-a",lease_seconds=60)
    second=claim_next_command("worker-b",lease_seconds=60)
    assert first.pk==command.pk and first.claimed_by=="worker-a"
    assert second is None and GatewayCommandAttempt.objects.count()==1


@pytest.mark.parametrize("command_type",["PLACE_ORDER","MODIFY_ORDER","CANCEL_ORDER"])
def test_worker_crash_before_broker_submission_is_safely_retried(command_type):
    broker=MockBrokerAdapter();broker.connect()
    broker.place_order({"internal_id":"I1","side":"BUY","quantity":1})
    payload={"internal_id":"I1"}
    if command_type=="PLACE_ORDER":
        payload={"internal_id":"I2","side":"BUY","quantity":1}
    elif command_type=="MODIFY_ORDER":
        payload={"internal_id":"I1","quantity":2}
    command=enqueue(command_type,payload,f"before:{command_type}")
    claimed=claim_command(command,"worker-a",lease_seconds=1)
    _expire(claimed)
    assert recover_expired_commands(broker)==1
    claimed=claim_next_command("worker-b")
    process_command(claimed,broker)
    command.refresh_from_db()
    assert command.status=="COMPLETED" and command.attempt_count==2
    states=list(command.attempt_history.order_by("attempt_number").values_list("submission_state",flat=True))
    assert states==["EXPIRED_BEFORE_SUBMISSION","COMPLETED"]


@pytest.mark.parametrize("command_type",["PLACE_ORDER","MODIFY_ORDER","CANCEL_ORDER"])
def test_worker_crash_after_broker_submission_recovers_without_resubmission(command_type):
    broker=MockBrokerAdapter();broker.connect()
    broker.place_order({"internal_id":"I1","side":"BUY","quantity":1})
    if command_type=="PLACE_ORDER":
        payload={"internal_id":"I2","side":"BUY","quantity":1}
    elif command_type=="MODIFY_ORDER":
        payload={"internal_id":"I1","quantity":2}
    else:
        payload={"internal_id":"I1"}
    command=enqueue(command_type,payload,f"after:{command_type}")
    claimed=claim_command(command,"worker-a",lease_seconds=1)
    attempt=claimed.attempt_history.get(attempt_number=1)
    attempt.submission_state="SUBMITTING";attempt.save(update_fields=["submission_state"])
    if command_type=="PLACE_ORDER":
        broker.place_order(payload)
    elif command_type=="MODIFY_ORDER":
        broker.modify_order(payload)
    else:
        broker.cancel_order(payload)
    order_count=len(broker.orders);next_order_id=broker.next_order_id
    _expire(claimed)
    assert recover_expired_commands(broker)==1
    command.refresh_from_db()
    assert command.status=="COMPLETED" and command.attempt_count==1
    assert len(broker.orders)==order_count and broker.next_order_id==next_order_id
    assert command.attempt_history.get().submission_state=="RECOVERED"


def test_uncertain_placement_without_broker_reference_is_not_resubmitted():
    broker=MockBrokerAdapter();broker.connect()
    command=enqueue("PLACE_ORDER",{"internal_id":"I1","side":"BUY","quantity":1},"uncertain")
    claimed=claim_command(command,"worker-a",lease_seconds=1)
    attempt=claimed.attempt_history.get()
    attempt.submission_state="SUBMITTING";attempt.save(update_fields=["submission_state"])
    _expire(claimed)
    recover_expired_commands(broker)
    command.refresh_from_db()
    assert command.status=="UNKNOWN" and not broker.orders
    assert "not resubmitted" in command.last_error


def test_zone_less_execution_time_is_read_as_the_gateway_zone_not_the_process_zone():
    import os
    import time
    from ib_async.decoder import Decoder

    previous = os.environ.get("TZ")
    # Django defaults the process zone to America/Chicago when TIME_ZONE is unset.
    os.environ["TZ"] = "America/Chicago"
    time.tzset()
    try:
        adapter = IBAsyncBrokerAdapter()
        fields = [
            "11", "-1", "109", "897478542", "NIFTY", "OPT", "20260929", "22950", "C", "1", "NSE", "INR",
            "NIFTY26SEP22950CE", "NIFTY", "0001b26d.6ab9964d.01.01", "20260928 06:36:34",
            "DUQ275858", "NSE", "BOT", "65", "56.40", "404093046", "17", "0", "65", "56.40", "",
            "", "", "", "0",
        ]
        Decoder(adapter.ib.wrapper, 170).execDetails(fields)
        [execution] = adapter.refresh_state()["executions"]
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()
    assert execution["executed_at"] == "2026-09-28T06:36:34+00:00"


class _Event(list):
    def __iadd__(self, handler):
        self.append(handler);return self


class _ReconnectingIB:
    """Just enough of ib_async.IB for the market-data subscription path."""
    def __init__(self):
        self.history_requests=0;self.realtime_requests=0;self.cancelled=[];self.connected=False;self._next=40
    def connect(self,*args,**kwargs):self.connected=True
    def isConnected(self):return self.connected
    def qualifyContracts(self,contract):return [SimpleNamespace(conId=51497778)]
    def reqMarketDataType(self,value):pass
    def reqHistoricalData(self,*args,**kwargs):
        self.history_requests+=1
        return [SimpleNamespace(date=datetime(2026,11,6,4,0,tzinfo=timezone.utc),open=1,high=2,low=1,close=2,volume=0)]
    def reqRealTimeBars(self,*args,**kwargs):
        self.realtime_requests+=1;self._next+=1
        return SimpleNamespace(reqId=self._next,updateEvent=_Event())
    def cancelRealTimeBars(self,live):self.cancelled.append(live.reqId)


def _subscription_adapter():
    adapter=IBAsyncBrokerAdapter.__new__(IBAsyncBrokerAdapter)
    adapter.ib=_ReconnectingIB();adapter.contracts={};adapter.market_subscriptions={};adapter.market_events=deque()
    adapter.market_request_ids={};adapter.recent_errors=deque(maxlen=50)
    adapter._contract=lambda payload:SimpleNamespace()
    return adapter


def _index_payload(generation):
    return {"subscription_key":"s:7:5m","instrument_id":7,"conid":51497778,"symbol":"NIFTY50",
        "asset_class":"IND","exchange":"NSE","currency":"INR","timeframe":"5m","historical_bars":1,
        "provider":"IBKR","provider_generation":generation}


@override_settings(IBKR_MARKET_DATA_TYPE="LIVE")
def test_reconnect_drops_dead_streams_so_the_next_subscribe_is_full():
    adapter=_subscription_adapter();adapter.connect()
    adapter.subscribe_market_data(_index_payload("g-1"))
    assert adapter.ib.history_requests==1 and list(adapter.market_subscriptions)==["s:7:5m"]

    adapter.connect()  # the broker loop reconnects after a lost TWS socket
    assert adapter.market_subscriptions=={} and adapter.market_request_ids=={}
    adapter.market_events.clear()

    result=adapter.subscribe_market_data(_index_payload("g-1"))
    assert "reused" not in result and adapter.ib.history_requests==2 and adapter.ib.realtime_requests==2
    assert [event["source"] for event in adapter.market_events]==["ibkr_historical"]


@override_settings(IBKR_MARKET_DATA_TYPE="LIVE")
def test_a_new_provider_generation_replaces_the_held_stream():
    adapter=_subscription_adapter();adapter.connect()
    adapter.subscribe_market_data(_index_payload("g-1"));adapter.market_events.clear()

    same=adapter.subscribe_market_data(_index_payload("g-1"))
    assert same["reused"] is True and adapter.ib.history_requests==1

    result=adapter.subscribe_market_data(_index_payload("g-2"))
    assert "reused" not in result and result["provider_generation"]=="g-2"
    assert adapter.ib.cancelled==[41] and adapter.ib.history_requests==2
    assert adapter.market_subscriptions["s:7:5m"]["payload"]["provider_generation"]=="g-2"
    assert [event["provider_generation"] for event in adapter.market_events]==["g-2"]
