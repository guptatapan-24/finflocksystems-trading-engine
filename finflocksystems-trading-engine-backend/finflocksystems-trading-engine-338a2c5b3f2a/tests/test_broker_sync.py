import pytest
from apps.accounts.models import BrokerAccount
from apps.broker_gateway.models import BrokerGatewaySession, BrokerSyncCursor
from apps.broker_gateway.models import BrokerPositionSnapshot
from apps.broker_gateway.sync import sync_events, sync_positions
from apps.execution.models import Fill
from apps.instruments.models import Instrument
from apps.oms.models import Order
from apps.portfolios.models import PortfolioPosition, TradingPortfolio

pytestmark=pytest.mark.django_db

class FakeGateway:
    def __init__(self, events): self._events=events; self.acked=0; self.requested_after=[]
    def events(self, after=0):
        self.requested_after.append(after)
        # Mirrors the Gateway endpoint: an `after` beyond its latest id means
        # its sequence restarted, so it pages from the beginning.
        latest=max((event["id"] for event in self._events),default=0)
        if after>latest and latest>0:after=0
        return [event for event in self._events if event["id"]>after]
    def ack_events(self, sequence): self.acked=sequence

def event(sequence, kind, rows): return {"id":sequence,"event_type":f"snapshot.{kind}","payload":{"value":rows}}

def position_event(sequence, account, rows, *, complete=True, snapshot_id=None):
    return {"id":sequence,"event_type":"snapshot.positions","payload":{"value":rows,"account":account,
        "complete":complete,"snapshot_id":snapshot_id or f"snapshot-{sequence}-{account}"}}

def test_gateway_snapshots_create_broker_truth_projections_and_ledgers():
    contract={"account":"DU123","conid":265598,"symbol":"AAPL","local_symbol":"AAPL","asset_class":"STK","exchange":"SMART","primary_exchange":"NASDAQ","currency":"USD"}
    events=[
        event(1,"accounts",[{"account_id":"DU123"}]),
        event(2,"account_summary",[
            {"account":"DU123","tag":"NetLiquidation","value":"125000","currency":"USD"},
            {"account":"DU123","tag":"AvailableFunds","value":"40000","currency":"USD"},
            {"account":"DU123","tag":"BuyingPower","value":"160000","currency":"USD"},
        ]),
        event(3,"open_orders",[{**contract,"internal_id":"","broker_order_id":"77","permanent_id":"9001","side":"BUY","quantity":"5","order_type":"LMT","limit_price":"100","stop_price":None,"time_in_force":"DAY","status":"Submitted","filled_quantity":"0","average_fill_price":"0"}]),
        event(4,"executions",[{**contract,"execution_id":"E-1","broker_order_id":"77","permanent_id":"9001","side":"BOT","quantity":"5","price":"99.50","commission":"1.25","executed_at":"2026-07-11T09:00:00+00:00"}]),
        event(5,"positions",[{**contract,"quantity":"5","average_cost":"99.75","market_price":"101.00"}]),
    ]
    session=BrokerGatewaySession.objects.create(
        display_name="IBKR",
        username_hint="test",
        mode="paper",
        status=BrokerGatewaySession.Status.CONNECTED,
        child_container_name="broker-sync-test",
        encrypted_gateway_token="test",
        encrypted_novnc_password="test",
        commands_enabled=True,
        last_gateway_state={"connected":True,"reconciled":True,"mode":"paper"},
    )
    gateway=FakeGateway(events)
    gateway.gateway_session=session
    assert sync_events(gateway)==5 and gateway.acked==5
    account=BrokerAccount.objects.get(account_id="DU123")
    assert account.net_liquidation==125000 and account.available_cash==40000
    assert TradingPortfolio.objects.get(account=account).name=="IBKR · DU123"
    assert PortfolioPosition.objects.get(portfolio__account=account).quantity==5
    assert Order.objects.get(broker_permanent_id="9001").status=="FILLED"
    assert Fill.objects.get(execution_id=f"{session.pk}:E-1").commission==pytest.approx(1.25)
    assert BrokerSyncCursor.objects.get().last_sequence==5
    assert sync_events(gateway)==0 and Fill.objects.count()==1


def test_gateway_generation_change_replays_a_restarted_event_sequence():
    session=BrokerGatewaySession.objects.create(
        display_name="Static paper",
        username_hint="test",
        mode="paper",
        status=BrokerGatewaySession.Status.CONNECTED,
        child_container_name="paper-ibkr-gateway",
        encrypted_gateway_token="test",
        encrypted_novnc_password="test",
        commands_enabled=True,
    )
    first=FakeGateway([event(9,"accounts",[{"account_id":"DU-A"}])])
    second=FakeGateway([event(1,"accounts",[{"account_id":"DU-B"}])])

    assert sync_events(first,session,connection_generation="generation-a")==1
    assert sync_events(second,session,connection_generation="generation-b")==1

    cursor=BrokerSyncCursor.objects.get(session=session)
    assert first.requested_after==[0] and second.requested_after==[9]
    assert cursor.connection_generation=="generation-b"
    assert cursor.last_sequence==1 and second.acked==1
    assert BrokerAccount.objects.filter(account_id="DU-B").exists()


def test_gateway_reconnect_on_the_same_event_log_does_not_replay_history():
    session=BrokerGatewaySession.objects.create(
        display_name="Reconnect",username_hint="test",mode="paper",
        status=BrokerGatewaySession.Status.CONNECTED,child_container_name="reconnect-gateway",
        encrypted_gateway_token="test",encrypted_novnc_password="test",commands_enabled=True,
    )
    gateway=FakeGateway([event(1,"accounts",[{"account_id":"DU-R"}]),event(2,"accounts",[{"account_id":"DU-R"}])])
    assert sync_events(gateway,session,connection_generation="generation-a")==2
    gateway._events.append(event(3,"accounts",[{"account_id":"DU-R"}]))

    assert sync_events(gateway,session,connection_generation="generation-b")==1

    cursor=BrokerSyncCursor.objects.get(session=session)
    assert gateway.requested_after==[0,2]
    assert cursor.connection_generation=="generation-b" and cursor.last_sequence==3


def test_replayed_disconnect_does_not_fail_a_subscription_requested_after_it(monkeypatch):
    from datetime import timedelta
    from django.utils import timezone
    from apps.broker_gateway.sync import process_snapshot
    from apps.market_streams.models import MarketDataSubscription
    from apps.instruments.models import BrokerContract
    import apps.market_data.fallback as fallback

    session=BrokerGatewaySession.objects.create(
        display_name="Stale",username_hint="test",mode="paper",
        status=BrokerGatewaySession.Status.CONNECTED,child_container_name="stale-gateway",
        encrypted_gateway_token="test",encrypted_novnc_password="test",commands_enabled=True,
    )
    instrument=Instrument.objects.create(symbol="NIFTY50",asset_class="IND",exchange="NSE",currency="INR")
    BrokerContract.objects.create(instrument=instrument,conid=51497778,local_symbol="NIFTY50")
    now=timezone.now()
    fresh=MarketDataSubscription.objects.create(gateway_session=session,instrument=instrument,timeframe="5m",
        conid=51497778,consumer_count=1,active_provider="IBKR",state="SUBSCRIBING",requested_at=now)
    failed=[]
    monkeypatch.setattr(fallback,"handle_ibkr_failure",lambda subscription,*args,**kwargs:failed.append(subscription.pk))

    old={"id":10,"event_type":"session.disconnected","created_at":(now-timedelta(hours=20)).isoformat(),
        "payload":{"error":"Socket disconnect","occurred_at":(now-timedelta(hours=20)).isoformat()}}
    process_snapshot(old,session)
    assert failed==[]

    current={"id":11,"event_type":"session.disconnected","created_at":(now+timedelta(seconds=5)).isoformat(),
        "payload":{"error":"Socket disconnect","occurred_at":(now+timedelta(seconds=5)).isoformat()}}
    process_snapshot(current,session)
    assert failed==[fresh.pk]


def _position(account, quantity, *, conid=265598, symbol="AAPL"):
    return {"account":account,"conid":conid,"symbol":symbol,"local_symbol":symbol,"asset_class":"STK",
        "exchange":"SMART","primary_exchange":"NASDAQ","currency":"USD","quantity":str(quantity),
        "average_cost":"100","market_price":"101"}


def test_complete_snapshot_is_scoped_to_one_account_with_same_contract():
    account_a=BrokerAccount.objects.create(account_id="DU-A")
    account_b=BrokerAccount.objects.create(account_id="DU-B")
    portfolio_a=TradingPortfolio.objects.create(account=account_a,name="A")
    portfolio_b=TradingPortfolio.objects.create(account=account_b,name="B")
    instrument=Instrument.objects.create(symbol="AAPL",primary_exchange="NASDAQ")
    from apps.instruments.models import BrokerContract
    BrokerContract.objects.create(instrument=instrument,conid=265598)
    PortfolioPosition.objects.create(portfolio=portfolio_a,instrument=instrument,quantity=1,average_cost=80)
    PortfolioPosition.objects.create(portfolio=portfolio_b,instrument=instrument,quantity=9,average_cost=90)

    sync_events(FakeGateway([position_event(1,"DU-A",[_position("DU-A",3)])]))

    assert PortfolioPosition.objects.get(portfolio=portfolio_a,instrument=instrument).quantity==3
    untouched=PortfolioPosition.objects.get(portfolio=portfolio_b,instrument=instrument)
    assert untouched.quantity==9 and untouched.average_cost==90


def test_empty_complete_snapshot_zeros_only_the_target_account():
    account_a=BrokerAccount.objects.create(account_id="DU-A")
    account_b=BrokerAccount.objects.create(account_id="DU-B")
    portfolio_a=TradingPortfolio.objects.create(account=account_a,name="A")
    portfolio_b=TradingPortfolio.objects.create(account=account_b,name="B")
    instrument=Instrument.objects.create(symbol="MSFT")
    PortfolioPosition.objects.create(portfolio=portfolio_a,instrument=instrument,quantity=4,average_cost=10,market_price=11)
    PortfolioPosition.objects.create(portfolio=portfolio_b,instrument=instrument,quantity=7,average_cost=20,market_price=21)

    sync_events(FakeGateway([position_event(1,"DU-A",[])]))

    target=PortfolioPosition.objects.get(portfolio=portfolio_a,instrument=instrument)
    other=PortfolioPosition.objects.get(portfolio=portfolio_b,instrument=instrument)
    assert (target.quantity,target.average_cost,target.market_price)==(0,0,0)
    assert (other.quantity,other.average_cost,other.market_price)==(7,20,21)


def test_partial_snapshot_is_audited_but_not_applied():
    account=BrokerAccount.objects.create(account_id="DU-A")
    portfolio=TradingPortfolio.objects.create(account=account,name="A")
    instrument=Instrument.objects.create(symbol="AAPL",primary_exchange="NASDAQ")
    from apps.instruments.models import BrokerContract
    BrokerContract.objects.create(instrument=instrument,conid=265598)
    PortfolioPosition.objects.create(portfolio=portfolio,instrument=instrument,quantity=8,average_cost=75)

    sync_events(FakeGateway([position_event(1,"DU-A",[_position("DU-A",2)],complete=False)]))

    current=PortfolioPosition.objects.get(portfolio=portfolio,instrument=instrument)
    snapshot=BrokerPositionSnapshot.objects.get()
    assert (current.quantity,current.average_cost)==(8,75)
    assert snapshot.status=="INCOMPLETE" and snapshot.complete is False


def test_duplicate_complete_snapshot_is_applied_once():
    rows=[_position("DU-A",5)]
    gateway=FakeGateway([
        position_event(1,"DU-A",rows,snapshot_id="same-snapshot"),
        position_event(2,"DU-A",rows,snapshot_id="same-snapshot"),
    ])
    assert sync_events(gateway)==2
    snapshot=BrokerPositionSnapshot.objects.get(snapshot_key="same-snapshot")
    assert snapshot.status=="COMPLETED" and snapshot.attempt_count==1
    assert PortfolioPosition.objects.get(portfolio__account__account_id="DU-A").quantity==5


def test_snapshot_failure_rolls_back_all_position_changes_and_is_audited(monkeypatch):
    account=BrokerAccount.objects.create(account_id="DU-A")
    portfolio=TradingPortfolio.objects.create(account=account,name="A")
    instrument=Instrument.objects.create(symbol="AAPL",primary_exchange="NASDAQ")
    from apps.instruments.models import BrokerContract
    BrokerContract.objects.create(instrument=instrument,conid=265598)
    PortfolioPosition.objects.create(portfolio=portfolio,instrument=instrument,quantity=9,average_cost=90)
    from apps.broker_gateway import sync as sync_module
    real_ensure=sync_module.ensure_instrument
    def fail_second(row):
        if row.get("symbol")=="FAIL":
            raise RuntimeError("mid-snapshot failure")
        return real_ensure(row)
    monkeypatch.setattr(sync_module,"ensure_instrument",fail_second)
    rows=[_position("DU-A",2),_position("DU-A",1,conid=999,symbol="FAIL")]

    with pytest.raises(RuntimeError,match="mid-snapshot failure"):
        sync_positions(rows,account_id="DU-A",complete=True,snapshot_key="failed-snapshot")

    current=PortfolioPosition.objects.get(portfolio=portfolio,instrument=instrument)
    snapshot=BrokerPositionSnapshot.objects.get(snapshot_key="failed-snapshot")
    assert (current.quantity,current.average_cost)==(9,90)
    assert snapshot.status=="FAILED" and "mid-snapshot failure" in snapshot.last_error


def test_position_row_without_contract_details_keeps_the_qualified_option_identity():
    from apps.broker_gateway.sync import ensure_instrument
    from apps.instruments.models import BrokerContract
    from apps.instruments.services import record_qualified_contract

    index=Instrument.objects.create(symbol="NIFTY50",asset_class="IND",exchange="NSE",currency="INR")
    BrokerContract.objects.create(instrument=index,conid=51497778,local_symbol="NIFTY50")
    option=Instrument.objects.create(symbol="NIFTY26SEP22950CE",asset_class="OPT",exchange="NSE",currency="INR")
    contract={"conid":897478542,"symbol":"NIFTY","local_symbol":"NIFTY26SEP22950CE","asset_class":"OPT",
        "exchange":"NSE","primary_exchange":"","currency":"INR","expiration":"20260929","strike":"22950.0",
        "right":"C","multiplier":"1","trading_class":"NIFTY"}
    record_qualified_contract(option,{**contract,"underlying_conid":51497778,"style":"EUROPEAN","settlement":"CASH"})

    # A position/execution/order row: the gateway serializes these without
    # contract details, so underConId, style and settlement are absent.
    assert ensure_instrument({**contract,"account":"DU123","quantity":"65","underlying_conid":None})==option

    option.option_contract.refresh_from_db()
    assert option.option_contract.underlying_conid==51497778
    assert option.option_contract.underlying==index
    assert (option.option_contract.style,option.option_contract.settlement)==("EUROPEAN","CASH")
    assert option.option_contract.trading_class=="NIFTY"


def test_execution_times_are_utc_and_a_re_reported_time_corrects_the_fill():
    from apps.broker_gateway.sync import sync_executions
    from datetime import datetime, timezone

    contract={"account":"DU123","conid":265598,"symbol":"AAPL","local_symbol":"AAPL","asset_class":"STK",
        "exchange":"SMART","primary_exchange":"NASDAQ","currency":"USD"}
    execution={**contract,"execution_id":"E-TZ","broker_order_id":"78","permanent_id":"9002","side":"BOT",
        "quantity":"5","price":"99.50","commission":"1.25"}
    session=BrokerGatewaySession.objects.create(
        display_name="IBKR",username_hint="test",mode="paper",status=BrokerGatewaySession.Status.CONNECTED,
        child_container_name="broker-sync-tz-test",encrypted_gateway_token="test",encrypted_novnc_password="test",
        commands_enabled=True,last_gateway_state={"connected":True,"reconciled":True,"mode":"paper"},
    )
    # A zone-less time is UTC, whatever the process zone is.
    sync_executions([{**execution,"executed_at":"2026-09-28T11:36:34"}],gateway_session=session)
    fill=Fill.objects.get(execution_id=f"{session.pk}:E-TZ")
    assert fill.executed_at==datetime(2026,9,28,11,36,34,tzinfo=timezone.utc)

    # The fixed Gateway re-reports the true time of the same execution.
    sync_executions([{**execution,"executed_at":"2026-09-28T06:36:34+00:00"}],gateway_session=session)
    fill.refresh_from_db()
    assert fill.executed_at==datetime(2026,9,28,6,36,34,tzinfo=timezone.utc)
    assert Fill.objects.filter(execution_id=f"{session.pk}:E-TZ").count()==1
    assert fill.order.filled_quantity==5


def test_sync_lock_is_released_without_lua_so_back_to_back_syncs_both_run(monkeypatch):
    import redis

    class AclRestrictedRedis:
        """Managed Redis whose ACL user may SET/GET/DEL but not run scripts."""
        def __init__(self): self.store={}
        def set(self, key, value, nx=False, ex=None):
            if nx and key in self.store:return None
            self.store[key]=value.encode() if isinstance(value,str) else value;return True
        def get(self, key): return self.store.get(key)
        def delete(self, key): self.store.pop(key,None)
        def eval(self, *args): raise redis.exceptions.NoPermissionError("NOPERM no permissions to run the 'eval' command")

    fake=AclRestrictedRedis()
    monkeypatch.setattr(redis.Redis,"from_url",classmethod(lambda cls,*args,**kwargs:fake))
    session=BrokerGatewaySession.objects.create(
        display_name="Lock",username_hint="test",mode="paper",
        status=BrokerGatewaySession.Status.CONNECTED,child_container_name="lock-gateway",
        encrypted_gateway_token="test",encrypted_novnc_password="test",commands_enabled=True,
    )
    gateway=FakeGateway([event(1,"accounts",[{"account_id":"DU-L"}])])
    assert sync_events(gateway,session)==1
    assert fake.store=={}
    gateway._events.append(event(2,"accounts",[{"account_id":"DU-L"}]))
    assert sync_events(gateway,session)==1
    assert BrokerSyncCursor.objects.get(session=session).last_sequence==2
