from datetime import timedelta
from decimal import Decimal
import pytest
from django.utils import timezone
from apps.accounts.models import BrokerAccount
from apps.allocation.models import RebalancePolicy
from apps.broker_gateway.models import BrokerGatewaySession
from apps.instruments.models import Instrument
from apps.market_streams.models import IndicatorValue
from apps.oms.models import Order, OrderIntent
from apps.portfolios.models import PortfolioPosition, TradingPortfolio
from apps.rebalancing.services import (
    _execution_adv,
    advance_rebalance,
    plan_rebalance,
    recover_incomplete,
)
from apps.execution.modes import RunType
from apps.strategies.models import (
    StrategyAllocation,
    StrategyDefinition,
    StrategyInstance,
    StrategyRun,
    StrategyTarget,
    StrategyVersion,
)
from tests.managed_gateway import bind_gateway_mode

pytestmark=pytest.mark.django_db


def strategy_target(portfolio,instrument,name,weight,input_hash,mode="PAPER"):
    if portfolio.gateway_session_id is None:
        bind_gateway_mode(portfolio, mode=mode.lower())
    instance=StrategyInstance.objects.create(name=name,definition=StrategyDefinition.objects.get(key="FIXED_WEIGHT_REBALANCE"),
        portfolio=portfolio,instrument=instrument,timeframe="1d",parameters={"direction":"LONG"},enabled=True,
        execution_mode=mode,state="LONG")
    StrategyAllocation.objects.create(portfolio=portfolio,strategy_instance=instance,weight=1)
    version=StrategyVersion.objects.create(strategy_instance=instance,version=instance.version,
        configuration_snapshot={},parameter_hash=f"hash-{instance.pk}")
    run=StrategyRun.objects.create(strategy_instance=instance,strategy_version=version,input_hash=input_hash,
        status="COMPLETED",completed_at=timezone.now())
    StrategyTarget.objects.create(run=run,strategy_instance=instance,strategy_version=version,portfolio=portfolio,
        instrument=instrument,target_weight=weight,signal_time=timezone.now(),execution_mode=mode)
    return instance


def setup_case():
    account=BrokerAccount.objects.create(account_id="DU1",net_liquidation=1000,available_cash=0)
    portfolio=TradingPortfolio.objects.create(name="P",account=account)
    old=Instrument.objects.create(symbol="OLD"); new=Instrument.objects.create(symbol="NEW")
    PortfolioPosition.objects.create(portfolio=portfolio,instrument=old,quantity=10,market_price=100)
    strategy_target(portfolio,new,"S","0.5","x")
    policy=RebalancePolicy.objects.create(portfolio=portfolio,maximum_turnover="2",mode="PAPER",partial_fill_threshold="0.95")
    return account,portfolio,old,new,policy


def test_netting_lot_rounding_suppression_and_sell_before_buy():
    account,portfolio,old,new,policy=setup_case()
    run=plan_rebalance(portfolio,"MANUAL","reb-1",prices={old.pk:100,new.pk:100},nav=1000,mode="PAPER",strict_market_state=False)
    sell=OrderIntent.objects.get(rebalance=run,side="SELL"); buy=OrderIntent.objects.get(rebalance=run,side="BUY")
    assert sell.quantity==10 and sell.eligible and buy.quantity==5 and not buy.eligible and run.phase=="SELLS"
    assert run.targets.get(instrument=new).drift==Decimal("0.5")


def test_partial_fill_recalculation_and_restart_do_not_duplicate():
    account,portfolio,old,new,policy=setup_case()
    run=plan_rebalance(portfolio,"MANUAL","reb-2",prices={old.pk:100,new.pk:100},nav=1000,mode="PAPER",strict_market_state=False)
    sell=OrderIntent.objects.get(rebalance=run,side="SELL")
    order=Order.objects.create(intent=sell,internal_id="sell",status="PARTIALLY_FILLED",quantity=10,filled_quantity=5)
    advance_rebalance(run); assert not OrderIntent.objects.get(rebalance=run,side="BUY").eligible
    order.status="FILLED";order.filled_quantity=10;order.save();account.available_cash=1000;account.save()
    position=PortfolioPosition.objects.get(portfolio=portfolio,instrument=old);position.quantity=0;position.save()
    advance_rebalance(run); assert OrderIntent.objects.get(rebalance=run,side="BUY").eligible
    count=OrderIntent.objects.count();recover_incomplete();recover_incomplete();assert OrderIntent.objects.count()==count


def test_recovery_advances_terminal_rebalance_without_synthesizing_replacement_intents():
    account,portfolio,old,new,policy=setup_case()
    PortfolioPosition.objects.filter(portfolio=portfolio,instrument=old).update(quantity=0)
    account.available_cash=1000;account.save(update_fields=["available_cash"])
    run=plan_rebalance(portfolio,"MANUAL","recover-existing-only",prices={old.pk:100,new.pk:100},
        nav=1000,mode="PAPER",strict_market_state=False)
    intent=OrderIntent.objects.get(rebalance=run,side="BUY")
    Order.objects.create(intent=intent,internal_id="cancelled-buy",status="CANCELLED",
        quantity=intent.quantity,filled_quantity=0)
    count=OrderIntent.objects.count()

    assert recover_incomplete()==1
    run.refresh_from_db()

    assert OrderIntent.objects.count()==count
    assert run.phase=="BLOCKED"
    assert run.status=="FAILED"


@pytest.mark.parametrize("status,filled,expected_phase,expected_status",[
    ("REJECTED",0,"BLOCKED","FAILED"),
    ("CANCELLED",5,"BLOCKED","PARTIALLY_COMPLETED"),
    ("EXPIRED",9,"BLOCKED","PARTIALLY_COMPLETED"),
])
def test_terminal_sells_below_threshold_never_unlock_buys(status,filled,expected_phase,expected_status):
    account,portfolio,old,new,policy=setup_case()
    run=plan_rebalance(portfolio,"MANUAL",f"terminal-{status}",prices={old.pk:100,new.pk:100},nav=1000,mode="PAPER",strict_market_state=False)
    sell=OrderIntent.objects.get(rebalance=run,side="SELL")
    Order.objects.create(intent=sell,internal_id=f"sell-{status}",status=status,quantity=10,filled_quantity=filled)
    advance_rebalance(run);run.refresh_from_db()
    assert run.phase==expected_phase and run.status==expected_status
    assert not OrderIntent.objects.get(rebalance=run,side="BUY").eligible
    count=OrderIntent.objects.count();recover_incomplete();recover_incomplete();assert OrderIntent.objects.count()==count


def test_partial_fill_at_threshold_recalculates_from_actual_cash_and_unlocks_buy():
    account,portfolio,old,new,policy=setup_case()
    run=plan_rebalance(portfolio,"MANUAL","threshold",prices={old.pk:100,new.pk:100},nav=1000,mode="PAPER",strict_market_state=False)
    sell=OrderIntent.objects.get(rebalance=run,side="SELL")
    Order.objects.create(intent=sell,internal_id="sell-threshold",status="CANCELLED",quantity=10,filled_quantity="9.5")
    account.available_cash=950;account.save(update_fields=["available_cash"])
    position=PortfolioPosition.objects.get(portfolio=portfolio,instrument=old);position.quantity="0.5";position.save(update_fields=["quantity"])
    advance_rebalance(run);run.refresh_from_db()
    assert run.phase=="BUYS" and run.status=="EXECUTING"
    assert OrderIntent.objects.get(rebalance=run,side="BUY").eligible


def test_nonterminal_partial_fill_below_threshold_keeps_sell_phase():
    account,portfolio,old,new,policy=setup_case()
    run=plan_rebalance(portfolio,"MANUAL","partial-open",prices={old.pk:100,new.pk:100},nav=1000,mode="PAPER",strict_market_state=False)
    sell=OrderIntent.objects.get(rebalance=run,side="SELL")
    Order.objects.create(intent=sell,internal_id="sell-open",status="PARTIALLY_FILLED",quantity=10,filled_quantity=5)
    advance_rebalance(run);run.refresh_from_db()
    assert run.phase=="SELLS" and not OrderIntent.objects.get(rebalance=run,side="BUY").eligible


def test_mixed_filled_and_rejected_sells_below_threshold_blocks_buys():
    account,portfolio,old,new,policy=setup_case()
    other=Instrument.objects.create(symbol="OTHER")
    PortfolioPosition.objects.create(portfolio=portfolio,instrument=other,quantity=10,market_price=100)
    run=plan_rebalance(portfolio,"MANUAL","mixed-sells",prices={old.pk:100,other.pk:100,new.pk:100},nav=2000,
        mode="PAPER",strict_market_state=False)
    sells=list(OrderIntent.objects.filter(rebalance=run,side="SELL").order_by("instrument_id"))
    assert len(sells)==2
    Order.objects.create(intent=sells[0],internal_id="mixed-filled",status="FILLED",quantity=sells[0].quantity,
        filled_quantity=sells[0].quantity)
    Order.objects.create(intent=sells[1],internal_id="mixed-rejected",status="REJECTED",quantity=sells[1].quantity,
        filled_quantity=0)
    advance_rebalance(run);run.refresh_from_db()
    assert run.phase=="BLOCKED" and run.status=="PARTIALLY_COMPLETED"
    assert not OrderIntent.objects.get(rebalance=run,side="BUY").eligible


def test_preview_never_creates_intents():
    account,portfolio,old,new,policy=setup_case()
    run=plan_rebalance(portfolio,"MANUAL","reb-preview",prices={old.pk:100,new.pk:100},nav=1000,
        mode="PAPER",run_type=RunType.PREVIEW,strict_market_state=False)
    assert run.phase=="PREVIEW_COMPLETE" and not OrderIntent.objects.filter(rebalance=run).exists()


@pytest.mark.parametrize(("execution_mode", "gateway_mode"), [
    ("PAPER", "paper"),
    ("LIVE", "live"),
])
def test_preview_never_creates_intents_in_either_execution_mode(
    execution_mode, gateway_mode
):
    account=BrokerAccount.objects.create(
        account_id=f"DU-PREVIEW-{execution_mode}",
        net_liquidation=1000,
        available_cash=1000,
    )
    session=BrokerGatewaySession.objects.create(
        display_name=f"{execution_mode} preview",
        username_hint="preview",
        mode=gateway_mode,
        child_container_name=f"{gateway_mode}-preview",
        encrypted_gateway_token="test",
        encrypted_novnc_password="test",
    )
    portfolio=TradingPortfolio.objects.create(
        name=f"{execution_mode} preview",
        account=account,
        gateway_session=session,
    )
    instrument=Instrument.objects.create(symbol=f"PREV-{execution_mode}")
    strategy_target(
        portfolio,
        instrument,
        f"{execution_mode} preview strategy",
        "0.50",
        f"preview-{execution_mode}",
        mode=execution_mode,
    )
    policy=RebalancePolicy.objects.create(
        portfolio=portfolio,
        maximum_turnover="2",
        minimum_trade_notional="1",
        fee_buffer="0",
        mode=execution_mode,
    )

    run=plan_rebalance(
        portfolio,
        "MANUAL",
        f"preview-{execution_mode}",
        prices={instrument.pk:100},
        nav=1000,
        mode=execution_mode,
        policy=policy,
        run_type=RunType.PREVIEW,
        strict_market_state=False,
    )

    assert run.mode == execution_mode
    assert run.run_type == "PREVIEW"
    assert run.targets.get().trade_quantity
    assert not OrderIntent.objects.filter(rebalance=run).exists()


def test_execution_adv_uses_only_fresh_daily_configured_final_live_value(settings):
    settings.EXECUTION_AVERAGE_VOLUME_WINDOW = 20
    instrument=Instrument.objects.create(symbol="ADV")
    account=BrokerAccount.objects.create(account_id="DU-ADV")
    portfolio=TradingPortfolio.objects.create(name="ADV",account=account)
    policy=RebalancePolicy.objects.create(
        portfolio=portfolio,
        price_staleness_limit=300,
    )
    now=timezone.now()

    def indicator(source_key, value, *, timeframe="1d", window=20,
                  event_time=None, is_final=True, processing_mode="LIVE"):
        return IndicatorValue.objects.create(
            instrument=instrument,
            indicator="average_volume",
            indicator_name="average_volume",
            requirement_identity_hash=source_key,
            value=value,
            parameters={"window": window},
            timeframe=timeframe,
            source_bar_id=source_key,
            is_final=is_final,
            processing_mode=processing_mode,
            event_time=event_time or now,
            source_key=source_key,
        )

    indicator("adv-correct", 123, event_time=now-timedelta(seconds=5))
    indicator("adv-wrong-timeframe", 999, timeframe="1m")
    indicator("adv-wrong-window", 888, window=10)
    indicator("adv-stale", 777, event_time=now-timedelta(hours=1))
    indicator("adv-warmup", 666, processing_mode="WARMUP")
    indicator("adv-nonfinal", 555, is_final=False)

    assert _execution_adv(instrument, None, policy) == Decimal("123")


def test_turnover_uses_cash_constrained_executable_quantity_before_later_trades():
    account=BrokerAccount.objects.create(account_id="DU-TURNOVER",net_liquidation=1000,available_cash=150)
    portfolio=TradingPortfolio.objects.create(name="Turnover",account=account)
    large=Instrument.objects.create(symbol="LARGE",lot_size=1)
    later=Instrument.objects.create(symbol="LATER",lot_size=1)
    strategy_target(portfolio,large,"Turnover large","1.00","turnover-large")
    strategy_target(portfolio,later,"Turnover later","0.04","turnover-later")
    policy=RebalancePolicy.objects.create(
        portfolio=portfolio,
        maximum_turnover="1.02",
        cash_buffer_percent="0",
        fee_buffer="0",
        minimum_trade_notional="1",
        mode="PAPER",
    )

    run=plan_rebalance(
        portfolio,
        "MANUAL",
        "rebalance-final-quantity-turnover",
        prices={large.pk:100,later.pk:10},
        nav=1000,
        mode="PAPER",
        policy=policy,
        strict_market_state=False,
    )

    large_target=run.targets.get(instrument=large)
    later_target=run.targets.get(instrument=later)
    assert large_target.target_quantity == Decimal("10")
    assert large_target.trade_quantity == Decimal("1")
    assert later_target.trade_quantity == Decimal("4")
    assert not later_target.suppressed
    assert run.planned_turnover == Decimal("0.1400000000")
