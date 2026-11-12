from decimal import Decimal
import pytest
from django.core.exceptions import ValidationError
from django.test import override_settings
from apps.accounts.models import BrokerAccount
from apps.allocation.models import OrderIntentAttribution, RebalancePolicy
from apps.instruments.models import BrokerContract, Instrument
from apps.broker_gateway.client import GatewayError
from apps.broker_gateway.sync import process_snapshot
from apps.oms.models import Order, OrderIntent
from apps.oms.services import apply_execution
from apps.portfolios.models import TradingPortfolio
from apps.rebalancing.coordinator import build_portfolio_target_snapshot
from apps.rebalancing.services import plan_rebalance
from apps.market_streams.models import IndicatorValue, MarketBar, MarketDataSubscription, StrategyEvaluationJob
from apps.market_streams.services import coordinate_bar_readiness, persist_bar
from apps.strategies.evaluation_jobs import process_strategy_evaluation_jobs
from apps.strategies.framework import create_instance, enable_instance, evaluate_instance, pause_instance, update_instance
from apps.strategies.models import StrategyAttributedPosition, StrategyDefinition, StrategyTarget, StrategyVersion
from tests.managed_gateway import bind_gateway_mode
from tests.strategy_activation import activate_strategy

pytestmark=pytest.mark.django_db


@pytest.fixture
def portfolio():
    account=BrokerAccount.objects.create(account_id="DU-STRATEGIES",net_liquidation=100000,available_cash=100000,buying_power=200000)
    portfolio=TradingPortfolio.objects.create(name="Strategy paper",account=account,minimum_notional=1)
    bind_gateway_mode(portfolio)
    return portfolio


def instrument(symbol,conid):
    item=Instrument.objects.create(symbol=symbol,exchange="SMART",currency="USD")
    BrokerContract.objects.create(instrument=item,conid=conid,primary_exchange="NASDAQ",local_symbol=symbol)
    return item


def make(portfolio,item,key,name,parameters,target="0.05",mode="PAPER"):
    instance,_=create_instance(name=name,definition_key=key,portfolio=portfolio,instrument_id=item.pk,timeframe="5m",
        parameters=parameters,target_configuration={"target_weight":target},execution_mode=mode,qualify=False)
    return instance


def test_definitions_default_paper_and_immutable_version(portfolio):
    tsla=instrument("TSLA",76792991)
    instance=make(portfolio,tsla,"RSI_MEAN_REVERSION","TSLA_RSI_5M",{"window":14,"entry_threshold":30,
        "exit_threshold":65,"entry_rule":"CROSS_ABOVE","exit_rule":"CROSS_ABOVE","direction":"LONG"})
    assert StrategyDefinition.objects.count()==14
    assert instance.execution_mode=="PAPER" and not instance.enabled and instance.version==1
    version=instance.versions.get();version.configuration_snapshot={}
    with pytest.raises(ValidationError):version.save()
    old_hash=instance.versions.get().parameter_hash
    update_instance(instance,{"parameters":{**instance.parameters,"window":10}})
    assert instance.version==2 and instance.versions.count()==2 and instance.versions.get(version=1).parameter_hash==old_hash


def test_rsi_plugin_is_ticker_portable_and_replay_safe(portfolio):
    cfg={"window":14,"entry_threshold":30,"exit_threshold":65,"entry_rule":"CROSS_ABOVE","exit_rule":"CROSS_ABOVE","direction":"LONG"}
    instances=[make(portfolio,instrument("TSLA",1),"RSI_MEAN_REVERSION","TSLA_RSI",cfg),
        make(portfolio,instrument("AAPL",2),"RSI_MEAN_REVERSION","AAPL_RSI",cfg)]
    for instance in instances:
        activate_strategy(instance)
        run=evaluate_instance(instance,bar={"bar_id":f"bar-{instance.instrument.symbol}","close":"100","is_final":True},
            indicators={"rsi":"31"},previous_indicators={"rsi":"29"},event_id=f"event-{instance.instrument.symbol}")
        replay=evaluate_instance(instance,bar={"bar_id":f"bar-{instance.instrument.symbol}","close":"100","is_final":True},
            indicators={"rsi":"31"},previous_indicators={"rsi":"29"},event_id=f"event-{instance.instrument.symbol}")
        assert replay.pk==run.pk and run.targets.get().target_weight==Decimal("0.05")
    assert {x.instrument.symbol for x in StrategyTarget.objects.all()}=={"TSLA","AAPL"}


def test_tsla_rsi_14_five_minute_paper_example_enters_shared_execution_path(portfolio):
    tsla=instrument("TSLA",8)
    item=make(portfolio,tsla,"RSI_MEAN_REVERSION","TSLA_RSI_14_5M_PAPER",{"window":14,"entry_threshold":30,
        "exit_threshold":65,"entry_rule":"CROSS_ABOVE","exit_rule":"CROSS_ABOVE","direction":"LONG"},"0.05","PAPER")
    activate_strategy(item)
    run=evaluate_instance(item,bar={"bar_id":"tsla-5m-final","interval":"5m","close":"100","is_final":True},
        indicators={"rsi":"31"},previous_indicators={"rsi":"29"},event_id="tsla-5m-final:1")
    target=run.targets.get();assert target.target_type=="WEIGHT" and target.target_weight==Decimal("0.05")
    RebalancePolicy.objects.create(portfolio=portfolio,minimum_trade_notional=1,maximum_turnover=1,mode="PAPER")
    rebalance=plan_rebalance(portfolio,"STRATEGY_TARGET","tsla-rsi-paper",prices={tsla.pk:100},mode="PAPER",strict_market_state=False)
    intent=OrderIntent.objects.get(rebalance=rebalance)
    assert intent.instrument==tsla and intent.side=="BUY" and intent.quantity==50 and intent.mode=="PAPER"
    assert intent.attributions.get().strategy_instance==item and intent.strategy_version_snapshot==[item.versions.get().pk]


def test_hold_run_keeps_last_changed_target_for_rebalancing(portfolio):
    tsla=instrument("TSLA",9);item=make(portfolio,tsla,"RSI_MEAN_REVERSION","PERSIST_TARGET",{"window":14,
        "entry_threshold":30,"exit_threshold":65,"direction":"LONG"})
    activate_strategy(item)
    evaluate_instance(item,bar={"bar_id":"entry","close":"100","is_final":True},indicators={"rsi":"31"},
        previous_indicators={"rsi":"29"},event_id="entry")
    hold=evaluate_instance(item,bar={"bar_id":"hold","close":"101","is_final":True},indicators={"rsi":"40"},
        previous_indicators={"rsi":"39"},event_id="hold")
    snapshot=build_portfolio_target_snapshot(portfolio,prices={tsla.pk:100})
    assert not hold.targets.exists() and Decimal(snapshot.net_targets[str(tsla.pk)])==Decimal("0.05")


def test_strategy_portability_separate_state_and_shared_indicator(portfolio):
    tsla=instrument("TSLA",3)
    rsi1=make(portfolio,tsla,"RSI_MEAN_REVERSION","TSLA_RSI_A",{"window":14,"entry_threshold":30,"exit_threshold":65,"direction":"LONG"})
    rsi2=make(portfolio,tsla,"RSI_MEAN_REVERSION","TSLA_RSI_B",{"window":14,"entry_threshold":25,"exit_threshold":70,"direction":"LONG"})
    sma=make(portfolio,tsla,"SMA_CROSSOVER","TSLA_SMA",{"fast_window":20,"slow_window":50,"direction":"LONG"})
    activate_strategy(rsi1);activate_strategy(rsi2)
    shared=rsi1.input_bindings.get(requirement__name="rsi").requirement
    assert shared.active_ref_count==2
    assert rsi2.input_bindings.get(requirement__name="rsi").requirement_id==shared.pk
    activate_strategy(sma)
    evaluate_instance(rsi1,bar={"bar_id":"r","close":"100","is_final":True},indicators={"rsi":"31"},previous_indicators={"rsi":"29"},event_id="r")
    evaluate_instance(sma,bar={"bar_id":"s","close":"100","is_final":True},indicators={"sma_fast":"11","sma_slow":"10"},previous_indicators={"sma_fast":"9","sma_slow":"10"},event_id="s")
    rsi1.refresh_from_db();sma.refresh_from_db();rsi2.refresh_from_db()
    assert rsi1.state=="LONG" and sma.state=="LONG" and rsi2.state=="READY_WAITING_FOR_LIVE_BAR"


def test_multi_strategy_targets_net_to_one_paper_intent_with_attribution(portfolio):
    tsla=instrument("TSLA",4)
    long=make(portfolio,tsla,"FIXED_WEIGHT_REBALANCE","TSLA_FIXED_LONG",{"direction":"BOTH"},"0.05","PAPER")
    short=make(portfolio,tsla,"FIXED_WEIGHT_REBALANCE","TSLA_FIXED_SHORT",{"direction":"BOTH"},"-0.02","PAPER")
    for item,event in [(long,"long"),(short,"short")]:
        activate_strategy(item);evaluate_instance(item,bar={"bar_id":event,"close":"100","is_final":True},indicators={},event_id=event)
    RebalancePolicy.objects.create(portfolio=portfolio,minimum_trade_notional=1,maximum_turnover=1,mode="PAPER")
    snapshot=build_portfolio_target_snapshot(portfolio,prices={tsla.pk:100})
    assert Decimal(snapshot.net_targets[str(tsla.pk)])==Decimal("0.03")
    assert len([row for row in snapshot.target_contributions if row["instrument_id"]==tsla.pk])==2
    rebalance=plan_rebalance(portfolio,"MANUAL","strategy-netting",prices={tsla.pk:100},mode="PAPER",strict_market_state=False)
    intent=OrderIntent.objects.get(rebalance=rebalance)
    assert intent.side=="BUY" and intent.quantity==30
    attrs=OrderIntentAttribution.objects.filter(order_intent=intent)
    assert attrs.count()==2 and all(x.strategy_instance_id and x.strategy_version_id for x in attrs)
    order=Order.objects.create(intent=intent,internal_id="net-tsla",status="ACKNOWLEDGED",quantity=30)
    apply_execution(order,{"execution_id":"net-partial","quantity":"10","price":"100","commission":"1","currency":"USD"})
    order.refresh_from_db();assert order.status=="PARTIALLY_FILLED" and order.filled_quantity==10
    attributed=list(StrategyAttributedPosition.objects.filter(instrument=tsla).values_list("quantity",flat=True))
    assert len(attributed)==2 and sum(attributed)==Decimal("10") and min(attributed)<0<max(attributed)
    apply_execution(order,{"execution_id":"net-final","quantity":"20","price":"101","commission":"2","currency":"USD"})
    order.refresh_from_db();assert order.status=="FILLED" and sum(StrategyAttributedPosition.objects.filter(instrument=tsla).values_list("quantity",flat=True))==Decimal("30")


def test_plugin_failure_isolated_to_its_version(portfolio,monkeypatch):
    tsla=instrument("TSLA",5);item=make(portfolio,tsla,"FIXED_WEIGHT_REBALANCE","FAIL_ONLY_THIS",{"direction":"LONG"})
    activate_strategy(item)
    class Broken:
        def evaluate(self,context):raise RuntimeError("plugin exploded")
    monkeypatch.setattr("apps.strategies.framework.get_plugin",lambda definition:Broken())
    run=evaluate_instance(item,bar={"bar_id":"bad","close":"100","is_final":True},indicators={},event_id="bad")
    item.refresh_from_db()
    assert run.status=="ERROR" and "plugin exploded" in run.error and item.state=="ERROR" and not run.targets.exists()
    from apps.strategies.plugins import get_plugin as registered_plugin
    monkeypatch.setattr("apps.strategies.framework.get_plugin",registered_plugin)
    retried=evaluate_instance(item,bar={"bar_id":"bad","close":"100","is_final":True},indicators={},event_id="bad",
        retry_failed=True)
    item.refresh_from_db()
    assert retried.pk==run.pk and retried.status=="COMPLETED" and item.state!="ERROR"


def test_persisted_final_inputs_trigger_once_and_corrected_bar_gets_new_namespace(portfolio):
    from django.utils import timezone
    tsla=instrument("TSLA",6);item=make(portfolio,tsla,"SMA_CROSSOVER","STREAMING_SMA",{"fast_window":2,"slow_window":3,"direction":"LONG"})
    activate_strategy(item);now=timezone.now()
    def bar(version):
        return MarketBar.objects.create(instrument=tsla,bar_id="stable",interval="5m",window_start=now,window_end=now,
            open=100,high=101,low=99,close=101,volume=10,version=version,is_final=True,source_event_count=1,produced_at=now)
    first=bar(1);requirements={x.requirement.parameters["role"]:x.requirement for x in item.input_bindings.filter(requirement__input_type="INDICATOR")}
    IndicatorValue.objects.create(instrument=tsla,indicator="sma_fast",indicator_name="sma",indicator_role="fast",
        implementation_version=1,requirement_identity_hash=requirements["fast"].identity_hash,
        value=11,previous_value=9,parameters=requirements["fast"].parameters,timeframe="5m",
        source_bar_id="stable",source_bar_version=1,event_time=now,source_key="fast-1")
    assert coordinate_bar_readiness(first)==0
    IndicatorValue.objects.create(instrument=tsla,indicator="sma_slow",indicator_name="sma",indicator_role="slow",
        implementation_version=1,requirement_identity_hash=requirements["slow"].identity_hash,
        value=10,previous_value=10,parameters=requirements["slow"].parameters,timeframe="5m",
        source_bar_id="stable",source_bar_version=1,event_time=now,source_key="slow-1")
    assert coordinate_bar_readiness(first)==1 and coordinate_bar_readiness(first)==0 and item.runs.count()==0
    assert process_strategy_evaluation_jobs()["completed"]==1 and item.runs.count()==1
    assert StrategyEvaluationJob.objects.get(bar=first).status=="COMPLETED"
    corrected=bar(2)
    for role,value in [("fast",12),("slow",10)]:
        requirement=requirements[role]
        IndicatorValue.objects.create(instrument=tsla,indicator=f"sma_{role}",indicator_name="sma",
            indicator_role=role,implementation_version=1,requirement_identity_hash=requirement.identity_hash,
            value=value,previous_value=value,parameters=requirement.parameters,timeframe="5m",source_bar_id="stable",
            source_bar_version=2,event_time=now,source_key=f"{role}-2")
    assert coordinate_bar_readiness(corrected)==1
    assert process_strategy_evaluation_jobs()["completed"]==1 and item.runs.count()==2


def test_strategy_management_api_create_patch_and_gateway_mode_enforcement(client,portfolio):
    item=instrument("MSFT",7)
    definitions=client.get("/api/v1/strategy-definitions/").json()
    assert definitions["ok"] and len(definitions["data"])==14
    payload={"name":"MSFT_FIXED","definition_key":"FIXED_WEIGHT_REBALANCE","instrument_id":item.pk,
        "portfolio_id":portfolio.pk,"timeframe":"1d","parameters":{"direction":"LONG"},
        "target_configuration":{"target_weight":"0.10"},"qualify":False}
    created=client.post("/api/v1/strategy-instances/",payload,data_type="json",content_type="application/json")
    assert created.status_code==201 and created.json()["data"]["execution_mode"]=="PAPER"
    instance_id=created.json()["data"]["id"]
    patched=client.patch(f"/api/v1/strategy-instances/{instance_id}/",{"target_configuration":{"target_weight":"0.08"}},content_type="application/json")
    assert patched.status_code==200 and patched.json()["data"]["version"]==2
    blocked=client.patch(f"/api/v1/strategy-instances/{instance_id}/",{"execution_mode":"LIVE"},content_type="application/json")
    assert blocked.status_code==400


def test_async_ibkr_qualification_event_records_canonical_contract():
    item=Instrument.objects.create(symbol="NVDA",asset_class="STK",exchange="SMART",currency="USD")
    process_snapshot({"event_type":"command.qualify.completed","payload":{"command_id":12,"qualified":True,
        "conid":4815747,"symbol":"NVDA","asset_class":"STK","exchange":"SMART","primary_exchange":"NASDAQ","currency":"USD"}})
    item.refresh_from_db();assert item.broker_contract.conid==4815747 and item.broker_contract.primary_exchange=="NASDAQ"


def test_shared_strategies_reuse_and_reference_count_market_subscription(portfolio):
    class Gateway:
        def __init__(self):self.subscribes=[];self.cancels=[]
        def health(self):return {"connected":True,"connection_generation":"session-1"}
        def subscribe_market_data(self,payload,key):self.subscribes.append((payload,key));return {"command_id":10,"status":"PENDING"}
        def cancel_market_data(self,payload,key):self.cancels.append((payload,key));return {"command_id":11,"status":"PENDING"}
    gateway=Gateway();item=instrument("SHARED",321)
    first=make(portfolio,item,"FIXED_WEIGHT_REBALANCE","SHARED_A",{"direction":"LONG"})
    second=make(portfolio,item,"FIXED_WEIGHT_REBALANCE","SHARED_B",{"direction":"LONG"})
    activate_strategy(first,gateway=gateway);activate_strategy(second,gateway=gateway)
    subscription=MarketDataSubscription.objects.get();assert subscription.consumer_count==2 and len(gateway.subscribes)==1
    # Only releases count here; the cancel queued ahead of the one subscribe
    # just clears a stale Gateway entry for that key.
    preflight=[item for item in gateway.cancels if item[0].get("resubscribe")]
    assert len(preflight)==1 and preflight[0][0]["subscription_key"]==gateway.subscribes[0][0]["subscription_key"]
    releases=lambda:[item for item in gateway.cancels if not item[0].get("resubscribe")]
    pause_instance(first,gateway);assert MarketDataSubscription.objects.get().consumer_count==1 and not releases()
    pause_instance(second,gateway);assert MarketDataSubscription.objects.get().consumer_count==0 and len(releases())==1


def test_strategy_activation_rolls_back_when_subscription_setup_fails(portfolio):
    class FailingGateway:
        def health(self):
            return {"connected": True, "connection_generation": "failed-setup"}

        def subscribe_market_data(self, payload, key):
            raise GatewayError("subscription rejected")

    item=instrument("ACTFAIL",322)
    instance=make(
        portfolio,
        item,
        "FIXED_WEIGHT_REBALANCE",
        "ACTIVATION_FAILURE",
        {"direction":"LONG"},
    )

    with override_settings(KAFKA_ENABLED=True):
        with pytest.raises(ValueError, match="subscription rejected"):
            enable_instance(instance, FailingGateway())

    instance.refresh_from_db()
    assert instance.enabled is False
    assert instance.versions.get().activated_at is None
    assert instance.input_bindings.exists()
    assert not instance.input_bindings.filter(active=True).exists()
    subscription=MarketDataSubscription.objects.get()
    assert subscription.state=="ERROR" and subscription.consumer_count==0


def test_real_final_bar_advances_strategy_warmup(portfolio):
    item=instrument("WARM",654);instance=make(portfolio,item,"FIXED_WEIGHT_REBALANCE","WARMUP_FINAL",{"direction":"LONG"})
    activate_strategy(instance,ready=False)
    persist_bar({"produced_at":"2026-07-13T00:01:01+00:00","payload":{"bar_id":"warm-1","instrument_id":item.pk,
        "interval":"5m","window_start":"2026-07-13T00:00:00+00:00","window_end":"2026-07-13T00:05:00+00:00",
        "open":"10","high":"11","low":"9","close":"10.5","volume":"100","is_final":True,"version":1,
        "processing_mode":"WARMUP"}})
    persist_bar({"produced_at":"2026-07-13T00:01:02+00:00","payload":{"bar_id":"warm-1","instrument_id":item.pk,
        "interval":"5m","window_start":"2026-07-13T00:00:00+00:00","window_end":"2026-07-13T00:05:00+00:00",
        "open":"10","high":"11","low":"9","close":"10.5","volume":"100","is_final":True,"version":1,
        "processing_mode":"WARMUP"}})
    instance.refresh_from_db()
    assert instance.warmup_progress==1 and instance.warmup_last_progress_at is not None
    assert instance.state=="READY_WAITING_FOR_LIVE_BAR" and instance.runs.count()==0
    persist_bar({"produced_at":"2026-07-13T00:06:01+00:00","payload":{"bar_id":"live-1","instrument_id":item.pk,
        "interval":"5m","window_start":"2026-07-13T00:05:00+00:00","window_end":"2026-07-13T00:10:00+00:00",
        "open":"10.5","high":"11","low":"10","close":"10.75","volume":"100","is_final":True,"version":1,
        "processing_mode":"LIVE"}})
    assert process_strategy_evaluation_jobs()["completed"]==1
    instance.refresh_from_db()
    assert instance.state!="WARMING_UP"
