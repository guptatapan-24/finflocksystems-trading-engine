"""Real Docker Compose paper pipeline proof.

This test deliberately uses the configured PostgreSQL database and running
Kafka/Flink/Gateway services. It is skipped in the normal SQLite unit suite.
"""

import json
import os
import time
import uuid

import pytest
from django.conf import settings
from django.test import Client


pytestmark = pytest.mark.docker_integration


def _enabled():
    return os.getenv("RUN_DOCKER_PAPER_PIPELINE", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _wait(label, callback, *, timeout=180, interval=1):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = callback()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"Timed out waiting for {label}; last observation={last!r}")


@pytest.fixture
def live_postgres(django_db_blocker):
    if not _enabled():
        pytest.skip("set RUN_DOCKER_PAPER_PIPELINE=1 to run the Docker paper pipeline")
    if settings.DATABASES["default"]["ENGINE"] != "django.db.backends.postgresql":
        pytest.fail("Docker paper pipeline proof requires PostgreSQL")
    if settings.ALLOW_LIVE_TRADING:
        pytest.fail("Docker paper pipeline proof requires ALLOW_LIVE_TRADING=false")
    with django_db_blocker.unblock():
        yield


def test_frontend_to_controlled_paper_fill_pipeline(live_postgres):
    from django.db import connection
    from apps.allocation.models import (
        PortfolioTargetCoordination,
        PortfolioTargetSnapshot,
        RebalanceRun,
    )
    from apps.audit.models import OutboxEvent
    from apps.broker_gateway.client import GatewayClient
    from apps.broker_gateway.services import synchronize_local_paper_gateway
    from apps.event_bus.tasks import check_stream_health, publish_outbox_events
    from apps.execution.models import BrokerCommand, Fill
    from apps.execution.readiness import collect_execution_readiness
    from apps.execution.tasks import dispatch_broker_commands, execute_order_intents
    from apps.market_streams.models import (
        IndicatorValue,
        InstrumentMarketState,
        MarketBar,
        MarketDataSubscription,
    )
    from apps.oms.models import Order, OrderIntent
    from apps.portfolios.models import PortfolioPosition, TradingPortfolio
    from apps.position_sizing.models import PositionSizingDecision
    from apps.rebalancing.tasks import coordinate_portfolio_targets
    from apps.reconciliation.services import reconcile
    from apps.risk.models import RiskCheckResult
    from apps.strategies.models import (
        StrategyInstance,
        StrategyRun,
        StrategyTarget,
        StrategyWarmupReadiness,
    )
    from apps.strategies.tasks import execute_strategy_evaluation_jobs

    if not settings.LOCAL_PAPER_GATEWAY_SERVICE_TOKEN:
        pytest.fail("LOCAL_PAPER_GATEWAY_SERVICE_TOKEN is required")
    if "paper-ibkr-gateway" not in settings.LOCAL_PAPER_GATEWAY_URL:
        pytest.fail("The test must use the Docker paper Gateway route")
    assert connection.get_autocommit(), "live pipeline proof requires PostgreSQL autocommit"
    assert not connection.in_atomic_block, (
        "live pipeline proof cannot run inside a pytest transaction because "
        "Kafka consumers and workers need to observe committed control-plane rows"
    )

    session = _wait(
        "connected local paper Gateway",
        lambda: (
            candidate
            if (
                (candidate := synchronize_local_paper_gateway())
                and candidate.status == candidate.Status.CONNECTED
                and candidate.commands_enabled
                and candidate.last_gateway_state.get("mode") == "paper"
                and candidate.last_gateway_state.get("reconciled") is True
            )
            else None
        ),
        timeout=60,
    )
    mapping = session.session_accounts.filter(available=True).select_related(
        "broker_account"
    ).first()
    assert mapping is not None, "paper Gateway did not expose an account"
    account = mapping.broker_account
    prior_instances = StrategyInstance.objects.filter(
        name__startswith="docker-paper-sma-"
    )
    prior_instances.update(
        enabled=False,
        state="DISABLED",
        kill_switch=True,
        block_reason="Prior Docker paper integration run retired",
    )

    suffix = uuid.uuid4().hex[:10].upper()
    portfolio = TradingPortfolio.objects.filter(
        account=account,
        gateway_session=session,
    ).order_by("pk").first()
    assert portfolio is not None
    portfolio.kill_switch = False
    portfolio.save(update_fields=["kill_switch"])
    account.kill_switch = False
    account.save(update_fields=["kill_switch", "updated_at"])

    reconciliation = reconcile(
        trigger="docker-paper-pipeline",
        broker_account=account,
        gateway_session=session,
    )
    assert reconciliation.status == "COMPLETED"
    account.refresh_from_db()
    assert account.is_reconciled and account.net_liquidation > 0

    def runtime_tick():
        synchronize_local_paper_gateway()
        publish_outbox_events()
        execute_strategy_evaluation_jobs(limit=20)
        coordinate_portfolio_targets()
        execute_order_intents(limit=20)
        dispatch_broker_commands(limit=20)

    def preflight():
        runtime_tick()
        check_stream_health()
        result = collect_execution_readiness()
        return result if result["ready"] else None

    readiness = _wait("execution readiness preflight", preflight, timeout=150, interval=2)
    assert readiness["signals"]["kafka"]["missing_topics"] == []
    assert readiness["signals"]["flink"]["status"] == "HEALTHY"
    assert readiness["signals"]["outbox_publisher"]["status"] == "HEALTHY"
    assert readiness["signals"]["kafka_consumer"]["status"] == "HEALTHY"

    initial_intent_count = OrderIntent.objects.filter(portfolio=portfolio).count()
    api = Client()
    created_response = api.post(
        "/api/v1/strategy-instances/",
        data=json.dumps(
            {
                "name": f"docker-paper-sma-{suffix}",
                "definition_key": "SMA_CROSSOVER",
                "portfolio_id": portfolio.pk,
                "ticker": f"DP{suffix}",
                "exchange": "SMART",
                "currency": "USD",
                "timeframe": "1m",
                "parameters": {
                    "fast_window": 2,
                    "slow_window": 3,
                    "entry_rule": "CROSS_ABOVE",
                    "exit_rule": "CROSS_BELOW",
                    "direction": "LONG",
                },
                "target_configuration": {
                    "target_weight": "0.01",
                    "capital_share": "1",
                    "priority": 10,
                },
                "execution_mode": "PAPER",
                "qualify": True,
            }
        ),
        content_type="application/json",
    )
    assert created_response.status_code == 201, created_response.json()
    created = created_response.json()["data"]
    instance_id = int(created["id"])
    assert created["qualification_command"]
    assert created["state"] == "DISABLED" and created["enabled"] is False
    assert OrderIntent.objects.filter(portfolio=portfolio).count() == initial_intent_count

    def qualified_contract():
        synchronize_local_paper_gateway()
        publish_outbox_events()
        instance = StrategyInstance.objects.select_related("instrument").get(pk=instance_id)
        return getattr(instance.instrument, "broker_contract", None)

    contract = _wait(
        "paper IBKR contract qualification",
        qualified_contract,
        timeout=60,
    )
    assert contract.conid > 0 and contract.qualified_at is not None

    activation_response = api.post(
        f"/api/v1/strategy-instances/{instance_id}/enable/",
        data=b"{}",
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=f"docker-paper-enable-{suffix}",
    )
    assert activation_response.status_code in {200, 202}, activation_response.json()
    activation = activation_response.json()["data"]
    assert activation["activation_status"] in {
        "ACTIVATING",
        "SUBSCRIBING",
        "WARMING_UP",
        "READY_WAITING_FOR_LIVE_BAR",
    }

    pipeline_observation = {}

    def completed_pipeline():
        runtime_tick()
        instance = StrategyInstance.objects.get(pk=instance_id)
        target = StrategyTarget.objects.filter(
            strategy_instance=instance, status="ACTIVE"
        ).order_by("-pk").first()
        run = (
            target.run
            if target is not None
            else StrategyRun.objects.filter(
                strategy_instance=instance, status="COMPLETED"
            ).order_by("-pk").first()
        )
        intent = OrderIntent.objects.filter(
            attributions__strategy_instance=instance
        ).select_related("order").order_by("-pk").first()
        order = getattr(intent, "order", None) if intent else None
        fill = Fill.objects.filter(order=order).first() if order else None
        coordination = PortfolioTargetCoordination.objects.filter(
            portfolio=instance.portfolio
        ).first()
        pipeline_observation.clear()
        pipeline_observation.update(
            {
                "state": instance.state,
                "block_reason": instance.block_reason,
                "run_id": getattr(run, "pk", None),
                "target_id": getattr(target, "pk", None),
                "coordination_status": getattr(coordination, "status", None),
                "coordination_error": getattr(coordination, "last_error", ""),
                "intent_id": getattr(intent, "pk", None),
                "order_id": getattr(order, "pk", None),
                "order_status": getattr(order, "status", None),
                "fill_id": getattr(fill, "pk", None),
            }
        )
        return (
            (instance, run, target, intent, order, fill)
            if all((run, target, intent, order, fill))
            and order.status == "FILLED"
            else None
        )

    try:
        instance, run, target, intent, order, fill = _wait(
            "first live strategy target through synchronized paper fill",
            completed_pipeline,
            timeout=180,
            interval=1,
        )
    except AssertionError as exc:
        raise AssertionError(
            f"{exc}; pipeline observation={pipeline_observation!r}"
        ) from exc
    runtime_tick()
    check_stream_health()

    subscription = MarketDataSubscription.objects.get(
        gateway_session=session,
        instrument=instance.instrument,
        timeframe=instance.timeframe,
    )
    evidence = StrategyWarmupReadiness.objects.get(
        strategy_instance=instance, is_current=True
    )
    warmup_bars = MarketBar.objects.filter(
        instrument=instance.instrument,
        interval="1m",
        processing_mode="WARMUP",
        is_final=True,
    )
    live_bars = MarketBar.objects.filter(
        instrument=instance.instrument,
        interval="1m",
        processing_mode="LIVE",
        is_final=True,
    )
    indicators = IndicatorValue.objects.filter(
        instrument=instance.instrument,
        timeframe="1m",
        is_final=True,
    )
    market_state = InstrumentMarketState.objects.get(instrument=instance.instrument)

    assert subscription.state in {"ACTIVE", "DEGRADED"}
    assert instance.subscription_ready_at is not None
    assert instance.warmup_completed_at is not None
    assert instance.first_evaluation_completed_at is not None
    assert instance.execution_active_at is not None
    assert evidence.completed_at <= instance.first_evaluation_completed_at
    assert len(evidence.bar_ids) >= settings.EXECUTION_AVERAGE_VOLUME_WINDOW
    assert set(evidence.bar_ids).issubset(set(warmup_bars.values_list("bar_id", flat=True)))
    assert all(row["processing_mode"] == "WARMUP" for row in evidence.bar_timestamps)
    assert evidence.provider == "IBKR"
    assert evidence.provider_generation == str(subscription.provider_generation)
    assert evidence.strategy_version_id == run.strategy_version_id
    assert evidence.requirement_hashes

    assert warmup_bars.count() >= settings.EXECUTION_AVERAGE_VOLUME_WINDOW
    assert live_bars.exists()
    assert indicators.filter(indicator_name="sma", value__isnull=False).exists()
    assert indicators.filter(indicator_name="average_volume", value__isnull=False).exists()
    assert market_state.is_execution_usable()
    assert market_state.reference_price_source == "ibkr_live"

    raw_events = OutboxEvent.objects.filter(
        topic="market.raw.v1",
        aggregate_id=str(instance.instrument_id),
    )
    assert raw_events.filter(status="PUBLISHED").exists()
    assert OutboxEvent.objects.filter(
        topic="strategy.inputs.v1",
        aggregate_id=str(instance.pk),
        status="PUBLISHED",
    ).exists()

    snapshot = PortfolioTargetSnapshot.objects.filter(
        portfolio=portfolio,
        source_strategy_runs__contains=[run.pk],
        status="READY",
    ).order_by("-pk").first()
    assert snapshot is not None
    assert any(row.get("target_id") == target.pk for row in snapshot.target_contributions)
    rebalance = RebalanceRun.objects.get(target_snapshot=snapshot)
    assert rebalance.automatic and rebalance.run_type == "EXECUTION"
    assert intent.rebalance_id == rebalance.pk
    assert intent.origin == OrderIntent.Origin.REBALANCE
    assert intent.created_at >= target.created_at
    assert intent.requires_fresh_price

    sizing = PositionSizingDecision.objects.get(order_intent=intent)
    assert sizing.approved_quantity > 0
    assert RiskCheckResult.objects.filter(
        order_intent=intent, decision__in=["APPROVED", "RESIZED"]
    ).exists()
    command = BrokerCommand.objects.get(order=order, command_type="PLACE")
    assert command.status == BrokerCommand.Status.ACKNOWLEDGED
    assert command.mode == "PAPER" and command.gateway_command_id
    assert (
        command.response_payload.get("status")
        or command.response_payload.get("broker_status")
    ) in {"Submitted", "Filled"}
    gateway_order = GatewayClient(session).order_state(order.internal_id)
    assert any(
        row["command_type"] == "PLACE_ORDER" and row["status"] == "COMPLETED"
        for row in gateway_order["commands"]
    )

    assert fill.execution_id.startswith(f"{session.pk}:mock-fill:")
    assert fill.quantity > 0 and fill.price > 0
    order.refresh_from_db()
    assert order.status == "FILLED" and order.filled_quantity == order.quantity
    position = PortfolioPosition.objects.get(
        portfolio=portfolio, instrument=instance.instrument
    )
    assert position.quantity > 0
    assert position.market_price == fill.price

    final_readiness = collect_execution_readiness()
    assert final_readiness["ready"], [
        blocker["code"] for blocker in final_readiness["blockers"]
    ]
