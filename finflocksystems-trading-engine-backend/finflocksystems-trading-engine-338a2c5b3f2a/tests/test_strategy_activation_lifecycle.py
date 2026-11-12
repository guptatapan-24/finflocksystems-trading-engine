from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.accounts.models import BrokerAccount
from apps.audit.models import OutboxEvent
from apps.broker_gateway.sync import process_snapshot
from apps.broker_gateway.models import BrokerSessionAccount
from apps.instruments.models import BrokerContract,Instrument
from apps.market_streams.models import MarketDataSubscription,StrategyEvaluationJob
from apps.market_streams.services import persist_bar
from apps.market_streams.tasks import check_warmup_timeouts
from apps.portfolio_construction.services import apply_construction_run
from apps.portfolio_construction.models import PortfolioConstructionPlan
from apps.portfolios.models import TradingPortfolio
from apps.strategies.evaluation_jobs import process_strategy_evaluation_jobs
from apps.strategies.framework import (
    AUTOMATIC_EXECUTION_STREAMING_DISABLED,
    StrategyActivationError,
    create_instance,
    enable_instance,
    update_instance,
)
from apps.strategies.models import (
    StrategyAction,StrategyInputBinding,StrategyInstance,StrategyRun,StrategyTarget,
)
from tests.managed_gateway import bind_gateway_mode
from tests.strategy_activation import FakeActivationGateway
from tests.test_portfolio_construction import (
    add_goal,add_stock,construction_case,create_construction_run,run_construction,
)


pytestmark=pytest.mark.django_db


@pytest.fixture
def lifecycle():
    account=BrokerAccount.objects.create(
        account_id="DU-ACTIVATION",net_liquidation=100000,
        available_cash=100000,buying_power=200000,
    )
    portfolio=TradingPortfolio.objects.create(
        name="Activation lifecycle",account=account,minimum_notional=1)
    bind_gateway_mode(portfolio)
    instrument=Instrument.objects.create(
        symbol="LIFE",exchange="SMART",currency="USD")
    BrokerContract.objects.create(
        instrument=instrument,conid=880001,primary_exchange="NASDAQ",
        local_symbol="LIFE",
    )
    instance,_=create_instance(
        name="Lifecycle fixed",
        definition_key="FIXED_WEIGHT_REBALANCE",
        portfolio=portfolio,
        instrument_id=instrument.pk,
        timeframe="1m",
        parameters={"direction":"LONG"},
        target_configuration={"target_weight":"0.05"},
        execution_mode="PAPER",
        qualify=False,
    )
    return portfolio,instrument,instance


def confirm_subscription(portfolio,subscription):
    process_snapshot({
        "event_type":"command.subscribe_market_data.completed",
        "payload":{
            "subscription_key":(
                f"{portfolio.gateway_session_id}:"
                f"{subscription.instrument_id}:{subscription.timeframe}"
            ),
            "provider_generation":str(subscription.provider_generation),
        },
    },gateway_session=portfolio.gateway_session)
    subscription.refresh_from_db()


def bar_envelope(instrument,bar_id,minute,mode):
    start=f"2026-07-27T00:{minute:02d}:00+00:00"
    end=f"2026-07-27T00:{minute+1:02d}:00+00:00"
    return {
        "event_id":f"{bar_id}:1",
        "produced_at":end,
        "payload":{
            "bar_id":bar_id,
            "instrument_id":instrument.pk,
            "interval":"1m",
            "window_start":start,
            "window_end":end,
            "open":"100",
            "high":"102",
            "low":"99",
            "close":"101",
            "volume":"1000",
            "source_event_count":2,
            "version":1,
            "is_final":True,
            "processing_mode":mode,
        },
    }


def test_new_strategy_is_disabled_not_warming(lifecycle):
    _,_,instance=lifecycle
    assert instance.enabled is False
    assert instance.state=="DISABLED"
    assert not instance.input_bindings.filter(active=True).exists()
    assert not OutboxEvent.objects.filter(topic="strategy.inputs.v1").exists()


def test_kafka_disabled_blocks_activation_without_subscription(lifecycle):
    _,_,instance=lifecycle
    with pytest.raises(
            StrategyActivationError,match=AUTOMATIC_EXECUTION_STREAMING_DISABLED):
        enable_instance(instance,FakeActivationGateway())
    instance.refresh_from_db()
    assert instance.enabled is False
    assert instance.state=="BLOCKED"
    assert instance.block_reason==AUTOMATIC_EXECUTION_STREAMING_DISABLED
    assert not MarketDataSubscription.objects.exists()


def test_manual_activation_registers_inputs_and_creates_subscription(
        lifecycle,settings):
    portfolio,_,instance=lifecycle
    settings.KAFKA_ENABLED=True
    gateway=FakeActivationGateway()
    instance=enable_instance(instance,gateway)
    subscription=MarketDataSubscription.objects.get()
    event=OutboxEvent.objects.get(topic="strategy.inputs.v1")
    assert instance.enabled is True and instance.state=="SUBSCRIBING"
    assert instance.input_bindings.filter(
        active=True,strategy_version__version=instance.version).exists()
    assert event.payload["requirements"]
    assert subscription.gateway_session==portfolio.gateway_session
    assert subscription.consumer_count==1
    assert subscription.required_history_bars==6
    assert subscription.conid==880001
    assert subscription.state=="SUBSCRIBING"
    assert gateway.subscribes[0][0]["provider_generation"]==str(
        subscription.provider_generation)
    assert gateway.subscribes[0][0]["subscription_key"]==(
        f"{portfolio.gateway_session_id}:{instance.instrument_id}:1m")
    confirm_subscription(portfolio,subscription)
    instance.refresh_from_db()
    assert instance.state=="WARMING_UP"


def test_builder_apply_activates_and_reports_each_strategy(
        settings,monkeypatch,django_capture_on_commit_callbacks):
    settings.KAFKA_ENABLED=True
    portfolio,instruments=construction_case(("AAA",))
    BrokerSessionAccount.objects.create(
        session=portfolio.gateway_session,broker_account=portfolio.account,available=True)
    plan=PortfolioConstructionPlan.objects.create(portfolio=portfolio)
    goal=add_goal(plan,"Activation goal","1","GROW",5)
    add_stock(goal,instruments[0])
    run=run_construction(
        create_construction_run(plan,"activation-builder",refresh_history=False),
        refresh_history=False,
    )
    gateway=FakeActivationGateway("builder-generation")
    monkeypatch.setattr(
        "apps.market_streams.subscriptions.GatewayClient",
        lambda session,require_commands=True:gateway,
    )
    from apps.strategies import tasks as strategy_tasks
    monkeypatch.setattr(
        strategy_tasks.activate_strategy_instance,
        "delay",
        lambda *args:strategy_tasks.activate_strategy_instance.run(*args),
    )
    with django_capture_on_commit_callbacks(execute=True):
        applied,_,_=apply_construction_run(run,"activation-builder-apply")
    applied.refresh_from_db()
    rows=applied.metrics["strategy_instances"]
    assert rows and applied.application_status=="ACTIVATING"
    for subscription in MarketDataSubscription.objects.all():
        confirm_subscription(portfolio,subscription)
    applied.refresh_from_db()
    assert applied.application_status=="ACTIVATING"
    assert applied.metrics["application"]["strategy_activation"]=="SUBSCRIPTION_READY"
    assert applied.metrics["application"]["market_subscription"]=="ACTIVE"
    assert all(row["activation_status"]=="WARMING_UP" for row in applied.metrics["strategy_instances"])
    assert all(row["market_subscription"]=="ACTIVE" for row in applied.metrics["strategy_instances"])
    assert all(row["subscription_ready"] for row in applied.metrics["strategy_instances"])
    assert all(not row["warmup_complete"] for row in applied.metrics["strategy_instances"])


def test_builder_activation_failure_is_not_full_success(
        django_capture_on_commit_callbacks):
    portfolio,instruments=construction_case(("AAA",))
    plan=PortfolioConstructionPlan.objects.create(portfolio=portfolio)
    goal=add_goal(plan,"Blocked activation","1","GROW",5)
    add_stock(goal,instruments[0])
    run=run_construction(
        create_construction_run(plan,"blocked-builder",refresh_history=False),
        refresh_history=False,
    )
    with django_capture_on_commit_callbacks(execute=True):
        applied,_,_=apply_construction_run(run,"blocked-builder-apply")
    applied.refresh_from_db()
    assert applied.application_status=="PARTIALLY_APPLIED"
    assert applied.metrics["application"]["strategy_activation"]=="FAILED"
    assert applied.metrics["application"]["market_subscription"]=="FAILED"
    assert applied.last_error==AUTOMATIC_EXECUTION_STREAMING_DISABLED
    assert all(
        row["block_reason"]==AUTOMATIC_EXECUTION_STREAMING_DISABLED
        for row in applied.metrics["strategy_instances"]
    )


def test_warmup_bar_reaches_ready_without_trading(lifecycle,settings):
    portfolio,instrument,instance=lifecycle
    settings.KAFKA_ENABLED=True
    instance=enable_instance(instance,FakeActivationGateway())
    subscription=MarketDataSubscription.objects.get()
    confirm_subscription(portfolio,subscription)
    persist_bar(bar_envelope(instrument,"warmup-only",0,"WARMUP"))
    instance.refresh_from_db()
    assert instance.warmup_progress==1
    assert instance.state=="READY_WAITING_FOR_LIVE_BAR"
    assert not StrategyEvaluationJob.objects.exists()
    assert not StrategyRun.objects.exists()
    assert not StrategyTarget.objects.exists()


def test_first_live_final_bar_schedules_and_evaluates(lifecycle,settings):
    portfolio,instrument,instance=lifecycle
    settings.KAFKA_ENABLED=True
    enable_instance(instance,FakeActivationGateway())
    subscription=MarketDataSubscription.objects.get()
    confirm_subscription(portfolio,subscription)
    persist_bar(bar_envelope(instrument,"warmup-before-live",0,"WARMUP"))
    persist_bar(bar_envelope(instrument,"first-live",1,"LIVE"))
    job=StrategyEvaluationJob.objects.get()
    assert job.status=="PENDING"
    assert process_strategy_evaluation_jobs()["completed"]==1
    instance.refresh_from_db()
    run=StrategyRun.objects.get()
    target=StrategyTarget.objects.get()
    assert run.status=="COMPLETED"
    assert target.run==run and target.target_weight==Decimal("0.05")
    assert instance.state=="LONG"


def test_activation_is_idempotent_for_bindings_subscriptions_and_commands(
        lifecycle,settings):
    _,_,instance=lifecycle
    settings.KAFKA_ENABLED=True
    gateway=FakeActivationGateway()
    enable_instance(instance,gateway)
    binding_count=StrategyInputBinding.objects.count()
    outbox_count=OutboxEvent.objects.filter(topic="strategy.inputs.v1").count()
    enable_instance(instance,gateway)
    assert StrategyInputBinding.objects.count()==binding_count
    assert MarketDataSubscription.objects.count()==1
    assert MarketDataSubscription.objects.get().consumer_count==1
    assert OutboxEvent.objects.filter(topic="strategy.inputs.v1").count()==outbox_count
    assert len(gateway.subscribes)==1


def test_reactivation_republishes_inputs_after_deactivation(
        lifecycle,settings):
    _,_,instance=lifecycle
    settings.KAFKA_ENABLED=True
    gateway=FakeActivationGateway()
    instance=enable_instance(instance,gateway)
    first_effective_from=instance.effective_from
    first_event=OutboxEvent.objects.get(topic="strategy.inputs.v1")

    from apps.strategies.framework import pause_instance
    instance=pause_instance(instance,gateway)
    assert OutboxEvent.objects.filter(
        topic="strategy.inputs.v1",
        payload__requirements=[],
    ).exists()

    instance=enable_instance(instance,gateway)
    activation_events=OutboxEvent.objects.filter(
        topic="strategy.inputs.v1",
    ).exclude(payload__requirements=[]).order_by("created_at")
    assert activation_events.count()==2
    assert activation_events.last().idempotency_key!=first_event.idempotency_key
    assert instance.effective_from>first_effective_from
    assert instance.warmup_started_at>=instance.effective_from
    assert len(gateway.subscribes)==2


def test_new_version_reconciles_existing_subscription_without_duplicate_command(
        lifecycle,settings):
    portfolio,_,instance=lifecycle
    settings.KAFKA_ENABLED=True
    gateway=FakeActivationGateway()
    instance=enable_instance(instance,gateway)
    subscription=MarketDataSubscription.objects.get()
    confirm_subscription(portfolio,subscription)
    instance=update_instance(instance,{
        "target_configuration":{
            "target_weight":"0.10",
            "construction_run_id":"new-builder-run",
        },
    })
    assert instance.enabled is True
    assert instance.versions.get(version=instance.version).activated_at is None
    instance=enable_instance(instance,gateway)
    assert instance.versions.get(version=instance.version).activated_at is not None
    assert MarketDataSubscription.objects.count()==1
    assert len(gateway.subscribes)==2


def test_repeated_api_activation_request_queues_one_task(
        lifecycle,settings,client,django_capture_on_commit_callbacks):
    _,_,instance=lifecycle
    settings.KAFKA_ENABLED=True
    path=f"/api/v1/strategy-instances/{instance.pk}/enable/"
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        first=client.post(
            path,{},content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="one-activation-task",
        )
        second=client.post(
            path,{},content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="one-activation-task",
        )
    assert first.status_code==202 and second.status_code==202
    assert len(callbacks)==1
    assert StrategyAction.objects.count()==1


def test_subscription_timeout_identifies_pending_command(
        lifecycle,settings):
    _,_,instance=lifecycle
    settings.KAFKA_ENABLED=True
    settings.WARMUP_TIMEOUT_SECONDS=30
    instance=enable_instance(instance,FakeActivationGateway())
    old=timezone.now()-timedelta(minutes=2)
    StrategyInstance.objects.filter(pk=instance.pk).update(
        warmup_started_at=old,warmup_last_progress_at=old,updated_at=old)
    assert check_warmup_timeouts()==1
    instance.refresh_from_db()
    subscription=MarketDataSubscription.objects.get()
    assert instance.state=="BLOCKED"
    assert instance.block_reason=="Warm-up timeout: subscription command is still pending"
    assert subscription.state=="SUBSCRIBING"
