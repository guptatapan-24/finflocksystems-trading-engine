"""End-to-end contract tests for the one automatic PAPER execution path.

The fast test harness deliberately round-trips every market event through the
versioned Kafka envelope JSON contract while using Flink's deterministic pure
processing functions.  Container-level Kafka/Flink process recovery remains the
responsibility of the automatic-execution smoke script; these tests exercise the
durable PostgreSQL boundaries without introducing timing-dependent services.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from django.test import override_settings
from django.utils import timezone


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from streaming.flink.jobs.events import envelope as flink_envelope
from streaming.flink.jobs.identity import (
    bar_event_key,
    canonical_event_key,
    indicator_event_key,
    market_quality_event_key,
    raw_event_key,
)
from streaming.flink.jobs.processing import (
    advance_bar_version,
    aggregate_bars,
    compute_indicator,
    normalize_market_event,
)

from apps.accounts.models import BrokerAccount
from apps.allocation.models import (
    PortfolioTargetCoordination,
    PortfolioTargetSnapshot,
    RebalancePolicy,
    RebalanceRun,
)
from apps.audit.models import OperationAttempt
from apps.broker_gateway.client import GatewayClient, GatewayTransportError
from apps.broker_gateway.sync import process_snapshot, sync_executions
from apps.event_bus.schemas import validate_envelope
from apps.execution import dispatch as execution_dispatch
from apps.execution.dispatch import (
    claim_next_broker_command,
    claim_next_order_intent,
    dispatch_broker_command,
    process_order_intents,
    recover_stuck_broker_commands,
)
from apps.execution.models import BrokerCommand, Fill
from apps.instruments.models import BrokerContract, Instrument
from apps.market_streams.models import (
    IndicatorValue,
    InstrumentMarketState,
    MarketBar,
    StrategyEvaluationJob,
)
from apps.market_streams.services import consume_market_event
from apps.oms.models import Order, OrderIntent
from apps.portfolios.models import (
    CashLedgerEntry,
    PortfolioPosition,
    PositionLedgerEntry,
    TradingPortfolio,
)
from apps.rebalancing import coordinator as target_coordinator
from apps.rebalancing.coordinator import process_target_coordination
from apps.reconciliation.services import reconcile
from apps.risk.models import RiskCheckResult
from apps.strategies import evaluation_jobs
from apps.strategies.evaluation_jobs import (
    process_strategy_evaluation_jobs,
    recover_stuck_strategy_evaluation_jobs,
)
from apps.strategies.framework import create_instance
from apps.strategies.models import StrategyRun, StrategyTarget
from tests.managed_gateway import bind_managed_gateway
from tests.strategy_activation import activate_strategy


pytestmark = pytest.mark.django_db


READY_GATEWAY = {"connected": True, "reconciled": True, "mode": "paper"}


@dataclass
class AutomaticDomain:
    account: BrokerAccount
    portfolio: TradingPortfolio
    session: object
    instrument: Instrument
    strategy: object
    base_time: object
    history: list[dict]


class PaperGateway:
    def __init__(self, *, place_error=None, order_state=None):
        self.place_error = place_error
        self.state = order_state or {
            "commands": [],
            "reference": {},
            "broker_order": {},
            "non_submission_established": False,
        }
        self.place_calls = 0

    def health(self):
        return dict(READY_GATEWAY)

    def place_order(self, payload, key):
        self.place_calls += 1
        assert payload["internal_id"]
        assert key
        if self.place_error is not None:
            raise self.place_error
        return {"command_id": 7001, "status": "PENDING"}

    def order_state(self, internal_id):
        return self.state


class ReconciledBroker:
    def __init__(self, *, position, execution):
        self.position = position
        self.execution = execution

    def health(self):
        return dict(READY_GATEWAY)

    def positions(self):
        return [self.position]

    def executions(self):
        return [self.execution]


def _kafka_round_trip(value):
    validate_envelope(value)
    return json.loads(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _domain(settings, suffix):
    # The test drives Kafka/Flink contracts explicitly and uses a mock Gateway;
    # do not start a real market-data subscription as a side effect of enable.
    settings.KAFKA_ENABLED = False
    settings.EXECUTION_AVERAGE_VOLUME_WINDOW = 3
    account = BrokerAccount.objects.create(
        account_id=f"DU-AUTO-{suffix}",
        net_liquidation=Decimal("10000"),
        available_cash=Decimal("10000"),
        buying_power=Decimal("20000"),
        is_reconciled=True,
    )
    portfolio = TradingPortfolio.objects.create(
        name=f"Automatic {suffix}",
        account=account,
        cash_buffer_pct=0,
        minimum_notional=1,
    )
    session = bind_managed_gateway(portfolio, settings)
    instrument = Instrument.objects.create(
        symbol=f"A{suffix}"[:32],
        exchange="SMART",
        primary_exchange="NASDAQ",
        currency="USD",
    )
    IndicatorValue.objects.create(
        instrument=instrument,
        indicator="average_volume",
        indicator_name="average_volume",
        requirement_identity_hash=f"execution-adv-{suffix}",
        value=100000,
        parameters={"window": 3},
        timeframe="1d",
        source_bar_id=f"execution-adv-{suffix}",
        is_final=True,
        processing_mode="LIVE",
        event_time=timezone.now(),
        source_key=f"execution-adv-{suffix}",
    )
    BrokerContract.objects.create(
        instrument=instrument,
        conid=8_000_000 + instrument.pk,
        primary_exchange="NASDAQ",
        local_symbol=instrument.symbol,
        qualified_at=timezone.now(),
    )
    RebalancePolicy.objects.create(
        portfolio=portfolio,
        mode="PAPER",
        instrument_drift_threshold=0,
        portfolio_drift_threshold=0,
        minimum_trade_notional=1,
        minimum_trade_quantity=1,
        cash_buffer_percent=0,
        fee_buffer=0,
        maximum_turnover=1,
        sell_before_buy=False,
    )
    strategy, _ = create_instance(
        name=f"SMA {suffix}",
        definition_key="SMA_CROSSOVER",
        portfolio=portfolio,
        instrument_id=instrument.pk,
        timeframe="1m",
        parameters={"fast_window": 2, "slow_window": 3, "direction": "LONG"},
        target_configuration={"target_weight": "0.10", "capital_share": "1"},
        execution_mode="PAPER",
        qualify=False,
    )
    activate_strategy(strategy)
    return AutomaticDomain(
        account=account,
        portfolio=portfolio,
        session=session,
        instrument=instrument,
        strategy=strategy,
        base_time=timezone.now().replace(second=0, microsecond=0)
        - timedelta(minutes=4),
        history=[],
    )


def _derive_market(domain, index, close, mode):
    start = domain.base_time + timedelta(minutes=index)
    end = start + timedelta(minutes=1)
    raw = {
        "source_event_id": f"{domain.instrument.symbol}:{mode}:{index}",
        "instrument_id": domain.instrument.pk,
        "conid": domain.instrument.broker_contract.conid,
        "symbol": domain.instrument.symbol,
        "event_kind": "BAR",
        "timeframe": "1m",
        "event_time": (start + timedelta(seconds=1)).isoformat(),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "open": str(close),
        "high": str(Decimal(str(close)) + 1),
        "low": str(Decimal(str(close)) - 1),
        "close": str(close),
        "price": str(close),
        "volume": "1000",
        "is_final": True,
        "provider": "IBKR",
        "provider_generation": "automatic-e2e-generation",
        "source": "synthetic_test",
        "processing_mode": mode,
    }
    raw_key = raw_event_key(raw)
    raw_message = _kafka_round_trip(
        flink_envelope(
            "market.raw",
            "instrument",
            domain.instrument.pk,
            raw,
            raw_key,
            occurred_at=raw["event_time"],
        )
    )
    canonical = normalize_market_event(
        raw_message["payload"],
        {str(domain.instrument.broker_contract.conid): domain.instrument.pk},
    )
    canonical_key = canonical_event_key(raw_key, domain.instrument.pk)
    canonical_message = _kafka_round_trip(
        flink_envelope(
            "market.canonical",
            "instrument",
            domain.instrument.pk,
            canonical,
            canonical_key,
            raw_message,
            canonical["event_time"],
        )
    )
    bar = aggregate_bars([canonical], interval="1m", seconds=60)[0]
    bar_message = _kafka_round_trip(
        flink_envelope(
            "market.bar",
            "instrument",
            domain.instrument.pk,
            bar,
            bar_event_key(bar["bar_id"], bar["version"]),
            canonical_message,
            bar["window_end"],
        )
    )
    candidate_history = [*domain.history, bar]
    indicators = []
    bindings = (
        domain.strategy.input_bindings.filter(
            active=True,
            strategy_version__version=domain.strategy.version,
            requirement__input_type="INDICATOR",
        )
        .select_related("requirement")
        .order_by("requirement__role", "requirement__identity_hash")
    )
    for binding in bindings:
        requirement = binding.requirement
        value = compute_indicator(
            candidate_history, requirement.name, requirement.parameters
        )
        previous = compute_indicator(
            candidate_history[:-1], requirement.name, requirement.parameters
        )
        output_name = (
            f"{requirement.name}_{requirement.role}"
            if requirement.role
            else requirement.name
        )
        source_key = indicator_event_key(
            bar["bar_id"],
            bar["version"],
            requirement.identity_hash,
            requirement.implementation_version,
        )
        indicators.append(
            _kafka_round_trip(
                flink_envelope(
                    "market.indicator",
                    "instrument",
                    domain.instrument.pk,
                    {
                        "instrument_id": str(domain.instrument.pk),
                        "timeframe": "1m",
                        "indicator": output_name,
                        "indicator_name": requirement.name,
                        "indicator_role": requirement.role,
                        "implementation_version": requirement.implementation_version,
                        "value": str(value) if value is not None else None,
                        "previous_value": (
                            str(previous) if previous is not None else None
                        ),
                        "event_time": bar["window_end"],
                        "parameters": requirement.parameters,
                        "requirement_identity_hash": requirement.identity_hash,
                        "source_bar_id": bar["bar_id"],
                        "source_bar_version": bar["version"],
                        "is_final": True,
                        "source_key": source_key,
                        "processing_mode": mode,
                    },
                    source_key,
                    canonical_message,
                    bar["window_end"],
                )
            )
        )
    quality_key = market_quality_event_key(canonical_message["event_id"], "FRESH")
    quality = _kafka_round_trip(
        flink_envelope(
            "market.quality",
            "instrument",
            domain.instrument.pk,
            {
                "instrument_id": str(domain.instrument.pk),
                "status": "FRESH",
                "reference_price": str(close),
                "latest_event_at": bar["window_end"],
                "source_event_id": canonical_message["event_id"],
                "stale_after_seconds": 3600,
                "provider": "IBKR",
                "source": "synthetic_test",
                "processing_mode": mode,
            },
            quality_key,
            canonical_message,
            bar["window_end"],
        )
    )
    return {
        "raw": raw_message,
        "canonical": canonical_message,
        "bar": bar_message,
        "indicators": indicators,
        "quality": quality,
        "bar_payload": bar,
    }


def _persist_derived(domain, derived, *, indicators=True):
    consume_market_event("automatic-e2e-market", derived["bar"])
    if indicators:
        for indicator in derived["indicators"]:
            consume_market_event("automatic-e2e-market", indicator)
    consume_market_event("automatic-e2e-market", derived["quality"])
    domain.history.append(derived["bar_payload"])


def _schedule_live_decision(domain):
    for index, close in enumerate(("100", "100", "100")):
        derived = _derive_market(domain, index, close, "WARMUP")
        _persist_derived(domain, derived)
    live = _derive_market(domain, 3, "110", "LIVE")
    _persist_derived(domain, live)
    return live


def _complete_strategy(domain):
    _schedule_live_decision(domain)
    result = process_strategy_evaluation_jobs()
    assert result["completed"] == 1, "strategy evaluation stage did not complete"
    return StrategyRun.objects.get(strategy_instance=domain.strategy)


def _complete_coordination(domain):
    run = _complete_strategy(domain)
    results = process_target_coordination()
    assert len(results) == 1, "target coordination stage did not claim one portfolio"
    return run, RebalanceRun.objects.get(portfolio=domain.portfolio, automatic=True)


def _complete_intent(domain, monkeypatch):
    _, rebalance = _complete_coordination(domain)
    monkeypatch.setattr(GatewayClient, "health", lambda self: dict(READY_GATEWAY))
    result = process_order_intents()
    assert result == {
        "claimed": 1,
        "commands_created": 1,
    }, "intent execution stage did not create one durable command"
    return rebalance, OrderIntent.objects.get(rebalance=rebalance)


def _broker_row(domain, order, **values):
    return {
        "account": domain.account.account_id,
        "conid": domain.instrument.broker_contract.conid,
        "symbol": domain.instrument.symbol,
        "local_symbol": domain.instrument.symbol,
        "asset_class": "STK",
        "exchange": "SMART",
        "primary_exchange": "NASDAQ",
        "currency": "USD",
        "internal_id": order.internal_id,
        "broker_order_id": "77001",
        "permanent_id": "99001",
        **values,
    }


@override_settings(PORTFOLIO_TARGET_COORDINATION_DEBOUNCE_SECONDS=0)
def test_synthetic_live_market_sequence_creates_one_idempotent_paper_order(
    settings, monkeypatch
):
    domain = _domain(settings, "MAIN")
    rebalance, intent = _complete_intent(domain, monkeypatch)

    command = BrokerCommand.objects.get(order__intent=intent)
    assert claim_next_broker_command() == command.pk
    gateway = PaperGateway()
    assert dispatch_broker_command(command.pk, gateway) == "ACKNOWLEDGED"

    order = Order.objects.get(intent=intent)
    process_snapshot(
        {
            "event_type": "broker.order",
            "payload": _broker_row(
                domain,
                order,
                source_event_id="paper-ack-1",
                broker_status="Submitted",
                status="Submitted",
                quantity=str(order.quantity),
                filled_quantity="0",
                occurred_at=timezone.now().isoformat(),
            ),
        },
        domain.session,
    )
    execution = _broker_row(
        domain,
        order,
        execution_id="paper-fill-1",
        side="BOT",
        quantity=str(order.quantity),
        price="110",
        commission="1.25",
        executed_at=timezone.now().isoformat(),
    )
    callback = {
        "event_type": "snapshot.executions",
        "payload": {"value": [execution]},
    }
    process_snapshot(callback, domain.session)
    process_snapshot(callback, domain.session)

    order.refresh_from_db()
    command.refresh_from_db()
    rebalance.refresh_from_db()
    position = PortfolioPosition.objects.get(
        portfolio=domain.portfolio, instrument=domain.instrument
    )
    fill = Fill.objects.get()
    cash_total = sum(
        CashLedgerEntry.objects.filter(portfolio=domain.portfolio).values_list(
            "amount", flat=True
        ),
        Decimal(0),
    )

    assert MarketBar.objects.count() == 4
    assert IndicatorValue.objects.count() == 9
    assert StrategyEvaluationJob.objects.count() == 1
    assert StrategyRun.objects.count() == StrategyTarget.objects.count() == 1
    assert PortfolioTargetSnapshot.objects.filter(status="READY").count() == 1
    assert RebalanceRun.objects.filter(automatic=True).count() == 1
    assert OrderIntent.objects.count() == RiskCheckResult.objects.count() == 1
    assert Order.objects.count() == BrokerCommand.objects.count() == 1
    assert gateway.place_calls == 1
    assert command.broker_order_id == order.broker_order_id == "77001"
    assert Fill.objects.count() == PositionLedgerEntry.objects.count() == 1
    assert position.quantity == order.quantity == fill.quantity
    assert cash_total == -(fill.quantity * fill.price) - fill.commission
    assert order.status == "FILLED"
    assert rebalance.status == "COMPLETED"

    from apps.strategies.workflow import execution_workflow
    workflow = execution_workflow(domain.strategy)
    by_stage = {row["stage"]: row for row in workflow["stages"]}
    assert workflow["status"] == "COMPLETED"
    assert workflow["terminal"] is True
    assert by_stage["TARGET_GENERATED"]["entity_id"] == str(
        StrategyTarget.objects.get().pk
    )
    assert by_stage["REBALANCE_CREATED"]["entity_id"] == str(rebalance.pk)
    assert by_stage["ORDER_INTENT_CREATED"]["entity_id"] == str(intent.pk)
    assert by_stage["RISK_DECISION"]["status"] == "COMPLETED"
    assert by_stage["BROKER_COMMAND_SENT"]["entity_id"] == str(command.pk)
    assert by_stage["ORDER_ACKNOWLEDGED"]["status"] == "COMPLETED"
    assert by_stage["FILL_PROGRESS"]["status"] == "COMPLETED"
    assert by_stage["FILL_PROGRESS"]["entity_id"] == str(fill.pk)
    assert {row["trace_id"] for row in workflow["stages"]} == {
        str(domain.strategy.workflow_trace_id)
    }

    broker = ReconciledBroker(
        position=_broker_row(
            domain,
            order,
            quantity=str(position.quantity),
            average_cost=str(position.average_cost),
            market_price="110",
        ),
        execution=execution,
    )
    reconciliation = reconcile(
        "automatic-e2e",
        broker,
        broker_account=domain.account,
        gateway_session=domain.session,
    )
    assert reconciliation.status == "COMPLETED"
    assert not reconciliation.breaks.filter(material=True).exists()


def test_raw_duplicate_and_checkpoint_restore_reuse_flink_identities(settings):
    domain = _domain(settings, "FLINK")
    first = _derive_market(domain, 0, "100", "WARMUP")
    duplicate = _derive_market(domain, 0, "100", "WARMUP")
    assert first["raw"]["event_id"] == duplicate["raw"]["event_id"]
    assert first["canonical"]["event_id"] == duplicate["canonical"]["event_id"]
    assert first["bar"]["event_id"] == duplicate["bar"]["event_id"]
    assert [item["event_id"] for item in first["indicators"]] == [
        item["event_id"] for item in duplicate["indicators"]
    ]

    state, version = advance_bar_version({}, first["bar_payload"])
    restored = json.loads(json.dumps(state))
    replayed_state, replayed_version = advance_bar_version(
        restored, duplicate["bar_payload"]
    )
    assert version == 1
    assert replayed_state == state
    assert replayed_version is None


@override_settings(PORTFOLIO_TARGET_COORDINATION_DEBOUNCE_SECONDS=0)
def test_delayed_indicator_and_consumer_replay_create_one_ready_job(
    settings,
):
    domain = _domain(settings, "DELAY")
    for index, close in enumerate(("100", "100", "100")):
        _persist_derived(
            domain, _derive_market(domain, index, close, "WARMUP")
        )
    live = _derive_market(domain, 3, "110", "LIVE")
    first = consume_market_event("automatic-e2e-market", live["bar"])
    assert consume_market_event("automatic-e2e-market", live["bar"]) == {
        "duplicate": True
    }
    job = StrategyEvaluationJob.objects.get()
    assert first["bar_id"] == job.bar_id
    assert job.status == "WAITING_FOR_INPUT"

    first_indicator = live["indicators"][0]
    consume_market_event("automatic-e2e-market", first_indicator)
    job.refresh_from_db()
    assert job.status == "WAITING_FOR_INPUT"
    assert consume_market_event(
        "automatic-e2e-market", first_indicator
    ) == {"duplicate": True}
    for indicator in live["indicators"][1:]:
        consume_market_event("automatic-e2e-market", indicator)
    job.refresh_from_db()
    assert job.status == "PENDING"
    assert StrategyEvaluationJob.objects.count() == 1


@override_settings(
    STRATEGY_EVALUATION_CLAIM_TIMEOUT_SECONDS=1,
    STRATEGY_EVALUATION_RETRY_BASE_SECONDS=1,
)
def test_strategy_worker_crash_recovers_exactly_one_run(settings, monkeypatch):
    domain = _domain(settings, "STRATCRASH")
    _schedule_live_decision(domain)
    real_complete = evaluation_jobs._complete_job
    monkeypatch.setattr(
        evaluation_jobs,
        "_complete_job",
        lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit("crash")),
    )
    with pytest.raises(SystemExit, match="crash"):
        process_strategy_evaluation_jobs()
    job = StrategyEvaluationJob.objects.get()
    StrategyEvaluationJob.objects.filter(pk=job.pk).update(
        claimed_at=timezone.now() - timedelta(seconds=2)
    )
    assert recover_stuck_strategy_evaluation_jobs() == 1
    StrategyEvaluationJob.objects.filter(pk=job.pk).update(
        next_attempt_at=timezone.now()
    )
    monkeypatch.setattr(evaluation_jobs, "_complete_job", real_complete)
    assert process_strategy_evaluation_jobs()["completed"] == 1
    assert StrategyRun.objects.filter(strategy_instance=domain.strategy).count() == 1


@override_settings(PORTFOLIO_TARGET_COORDINATION_DEBOUNCE_SECONDS=0)
def test_target_coordinator_crash_reclaims_durable_request(
    settings, monkeypatch
):
    domain = _domain(settings, "TARGETCRASH")
    _complete_strategy(domain)
    real_coordinate = target_coordinator.coordinate_portfolio
    monkeypatch.setattr(
        target_coordinator,
        "coordinate_portfolio",
        lambda portfolio_id: (_ for _ in ()).throw(SystemExit("crash")),
    )
    with pytest.raises(SystemExit, match="crash"):
        process_target_coordination()
    coordination = PortfolioTargetCoordination.objects.get(
        portfolio=domain.portfolio
    )
    assert coordination.status == "CLAIMED"
    assert coordination.needs_coordination is True
    monkeypatch.setattr(
        target_coordinator, "coordinate_portfolio", real_coordinate
    )
    assert len(process_target_coordination()) == 1
    assert RebalanceRun.objects.filter(
        portfolio=domain.portfolio, automatic=True
    ).count() == 1


@override_settings(
    PORTFOLIO_TARGET_COORDINATION_DEBOUNCE_SECONDS=0,
    ORDER_INTENT_CLAIM_TIMEOUT_SECONDS=1,
)
def test_intent_worker_crash_is_recovered_before_order_creation(
    settings, monkeypatch
):
    domain = _domain(settings, "INTENTCRASH")
    _, rebalance = _complete_coordination(domain)
    intent = OrderIntent.objects.get(rebalance=rebalance)
    assert claim_next_order_intent() == intent.pk
    OperationAttempt.objects.filter(
        operation_type="ORDER_INTENT", operation_id=str(intent.pk)
    ).update(started_at=timezone.now() - timedelta(seconds=2))
    if hasattr(OrderIntent, "claimed_at"):
        OrderIntent.objects.filter(pk=intent.pk).update(
            claimed_at=timezone.now() - timedelta(seconds=2)
        )
    recover = getattr(
        execution_dispatch, "recover_stuck_order_intents", None
    )
    assert callable(recover), "intent execution has no crash-recovery entry point"
    recover()
    intent.refresh_from_db()
    assert intent.operation_status == "PENDING"
    monkeypatch.setattr(GatewayClient, "health", lambda self: dict(READY_GATEWAY))
    assert process_order_intents()["commands_created"] == 1
    assert Order.objects.filter(intent=intent).count() == 1
    assert BrokerCommand.objects.filter(order__intent=intent).count() == 1


@override_settings(
    PORTFOLIO_TARGET_COORDINATION_DEBOUNCE_SECONDS=0,
    BROKER_COMMAND_CLAIM_TIMEOUT_SECONDS=1,
)
def test_broker_dispatcher_crash_before_send_recovers_one_submission(
    settings, monkeypatch
):
    domain = _domain(settings, "BROKERCRASH")
    _, intent = _complete_intent(domain, monkeypatch)
    command = BrokerCommand.objects.get(order__intent=intent)
    assert claim_next_broker_command() == command.pk
    BrokerCommand.objects.filter(pk=command.pk).update(
        claimed_at=timezone.now() - timedelta(seconds=2)
    )
    assert recover_stuck_broker_commands()["claimed"] == 1
    assert claim_next_broker_command() == command.pk
    gateway = PaperGateway()
    assert dispatch_broker_command(command.pk, gateway) == "ACKNOWLEDGED"
    assert gateway.place_calls == 1


@override_settings(PORTFOLIO_TARGET_COORDINATION_DEBOUNCE_SECONDS=0)
def test_lost_gateway_response_reconciles_before_retry(
    settings, monkeypatch
):
    domain = _domain(settings, "LOST")
    _, intent = _complete_intent(domain, monkeypatch)
    command = BrokerCommand.objects.get(order__intent=intent)
    gateway = PaperGateway(place_error=GatewayTransportError("lost response"))
    assert claim_next_broker_command() == command.pk
    assert dispatch_broker_command(command.pk, gateway) == "UNCERTAIN"
    order = Order.objects.get(intent=intent)
    gateway.state = {
        "commands": [],
        "reference": {
            "internal_id": order.internal_id,
            "broker_order_id": "88001",
            "permanent_id": "99002",
            "last_status": "Submitted",
        },
        "broker_order": {},
        "non_submission_established": False,
    }
    BrokerCommand.objects.filter(pk=command.pk).update(
        next_attempt_at=timezone.now()
    )
    assert claim_next_broker_command() == command.pk
    assert dispatch_broker_command(command.pk, gateway) == "ACKNOWLEDGED"
    command.refresh_from_db()
    assert gateway.place_calls == 1
    assert command.broker_order_id == "88001"


def test_duplicate_execution_and_late_commission_are_accounted_once(
    settings,
):
    domain = _domain(settings, "COMMISSION")
    intent = OrderIntent.objects.create(
        portfolio=domain.portfolio,
        instrument=domain.instrument,
        side="BUY",
        quantity=2,
        reference_price=100,
        mode="PAPER",
        operation_status="QUEUED",
        idempotency_key="late-commission-intent",
    )
    order = Order.objects.create(
        intent=intent,
        internal_id="late-commission-order",
        broker_order_id="commission-broker-order",
        broker_permanent_id="commission-permanent-order",
        status="ACKNOWLEDGED",
        quantity=2,
    )
    base = _broker_row(
        domain,
        order,
        broker_order_id=order.broker_order_id,
        permanent_id=order.broker_permanent_id,
        execution_id="late-commission-fill",
        side="BOT",
        quantity="2",
        price="100",
        commission="0",
        executed_at=timezone.now().isoformat(),
    )
    sync_executions([base], domain.session)
    with_commission = {**base, "commission": "1.25"}
    sync_executions([with_commission], domain.session)
    sync_executions([with_commission], domain.session)

    fill = Fill.objects.get()
    cash_total = sum(
        CashLedgerEntry.objects.filter(portfolio=domain.portfolio).values_list(
            "amount", flat=True
        ),
        Decimal(0),
    )
    assert Fill.objects.count() == PositionLedgerEntry.objects.count() == 1
    assert PortfolioPosition.objects.get(
        portfolio=domain.portfolio, instrument=domain.instrument
    ).quantity == 2
    assert fill.commission == Decimal("1.25")
    assert cash_total == Decimal("-201.25")
