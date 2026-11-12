import json
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.accounts.models import BrokerAccount
from apps.allocation.models import (
    AllocationDecision,
    AllocationRun,
    OrderIntentAttribution,
    PortfolioFlow,
    RebalanceRun,
    StrategyCapitalSnapshot,
)
from apps.audit.models import AuditEvent, OutboxEvent
from apps.execution.models import Fill
from apps.instruments.models import BrokerContract, Instrument
from apps.oms.models import Order, OrderIntent
from apps.portfolios.models import (
    CashLedgerEntry,
    PositionLedgerEntry,
    TradingPortfolio,
)
from apps.portfolio_construction.models import (
    GoalInstrumentSelection,
    GoalStrategyAssignment,
    PortfolioConstructionPlan,
    PortfolioGoalAllocation,
)
from apps.reconciliation.models import ReconciliationBreak, ReconciliationRun
from apps.strategies.deletion import delete_strategy_instance
from apps.strategies.framework import create_instance, evaluate_instance
from apps.strategies.models import (
    StrategyAllocation,
    StrategyAttributedPosition,
    StrategyInputRequirement,
    StrategyInstance,
    StrategyRun,
    StrategyTarget,
    StrategyVersion,
)
from tests.managed_gateway import bind_gateway_mode
from tests.strategy_activation import activate_strategy


pytestmark = pytest.mark.django_db


@pytest.fixture
def portfolio():
    account = BrokerAccount.objects.create(
        account_id="DU-DELETE",
        net_liquidation=100000,
        available_cash=50000,
        buying_power=200000,
    )
    portfolio = TradingPortfolio.objects.create(
        name="Deletion portfolio",
        account=account,
        minimum_notional=1,
    )
    bind_gateway_mode(portfolio)
    return portfolio


@pytest.fixture
def instrument():
    item = Instrument.objects.create(
        symbol="DELETE",
        exchange="SMART",
        currency="USD",
    )
    BrokerContract.objects.create(
        instrument=item,
        conid=987654,
        primary_exchange="NASDAQ",
        local_symbol=item.symbol,
    )
    return item


def make_instance(portfolio, instrument, name="Delete me"):
    instance, _ = create_instance(
        name=name,
        definition_key="FIXED_WEIGHT_REBALANCE",
        portfolio=portfolio,
        instrument_id=instrument.pk,
        timeframe="5m",
        parameters={"direction": "LONG"},
        target_configuration={"target_weight": "0.10"},
        execution_mode="PAPER",
        qualify=False,
    )
    return instance


def delete_request(client, instance, name=None, key="delete-attempt-1"):
    return client.delete(
        f"/api/v1/strategy-instances/{instance.pk}/",
        data=json.dumps({"strategy_name": instance.name if name is None else name}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=key,
    )


def test_deletion_removes_configuration_runtime_and_allocations_but_preserves_financial_history(
    client, portfolio, instrument
):
    instance = make_instance(portfolio, instrument)
    instance_id = instance.pk
    version = instance.versions.get()
    requirement_ids = list(instance.input_bindings.values_list("requirement_id", flat=True))

    activate_strategy(instance)
    completed_run = evaluate_instance(
        instance,
        bar={"bar_id": "delete-complete", "close": "100", "is_final": True},
        indicators={},
        event_id="delete-complete",
    )
    StrategyAttributedPosition.objects.create(
        strategy_instance=instance,
        portfolio=portfolio,
        instrument=instrument,
        quantity=0,
    )

    intent = OrderIntent.objects.create(
        portfolio=portfolio,
        strategy_instance=instance,
        strategy_version=version,
        instrument=instrument,
        side="BUY",
        quantity=1,
        idempotency_key="historical-delete-intent",
    )
    attribution = OrderIntentAttribution.objects.create(
        order_intent=intent,
        strategy_instance=instance,
        strategy_version=version,
        target_delta=1,
        allocated_quantity=1,
        allocated_value=100,
        allocated_cost=1,
    )
    order = Order.objects.create(
        intent=intent,
        internal_id="historical-delete-order",
        status="FILLED",
        quantity=1,
        filled_quantity=1,
        average_fill_price=100,
    )
    fill = Fill.objects.create(
        order=order,
        execution_id="historical-delete-fill",
        quantity=1,
        price=100,
        commission=1,
        executed_at=timezone.now(),
    )
    cash = CashLedgerEntry.objects.create(
        portfolio=portfolio,
        amount=-101,
        currency="USD",
        kind="FILL",
        reference=fill.execution_id,
        idempotency_key="cash:historical-delete-fill",
    )
    position_ledger = PositionLedgerEntry.objects.create(
        portfolio=portfolio,
        instrument=instrument,
        quantity_delta=1,
        price=100,
        kind="FILL",
        reference=fill.execution_id,
        idempotency_key="position:historical-delete-fill",
    )
    reconciliation = ReconciliationRun.objects.create(
        trigger="STRATEGY_DELETE_TEST", status="COMPLETED", completed_at=timezone.now()
    )
    reconciliation_break = ReconciliationBreak.objects.create(
        run=reconciliation,
        category="POSITION",
        severity="INFO",
        internal_value={"strategy_instance_id": instance_id},
        broker_value={},
        resolved=True,
    )
    prior_audit = AuditEvent.objects.create(
        event_type="strategy.preexisting",
        actor="test",
        aggregate_type="strategy_instance",
        aggregate_id=str(instance_id),
        data={"kept": True},
        idempotency_key="audit:strategy-preexisting-delete",
    )
    flow = PortfolioFlow.objects.create(
        portfolio=portfolio,
        flow_type="DEPOSIT",
        amount=1000,
        effective_at=timezone.now(),
        idempotency_key="historical-delete-flow",
        status="ALLOCATED",
    )
    allocation_run = AllocationRun.objects.create(
        flow=flow,
        portfolio_nav_before=100000,
        status="COMPLETED",
        completed_at=timezone.now(),
    )
    capital_snapshot = StrategyCapitalSnapshot.objects.create(
        allocation_run=allocation_run,
        strategy_instance=instance,
        capital_before=1000,
        target_capital=1100,
    )
    allocation_decision = AllocationDecision.objects.create(
        run=allocation_run,
        strategy_instance=instance,
        source="CAPITAL_DEFICIT",
        requested_amount=100,
        approved_amount=100,
    )

    response = delete_request(client, instance)

    assert response.status_code == 200
    assert response.json()["data"]["id"] == instance_id
    assert not StrategyInstance.objects.filter(pk=instance_id).exists()
    assert not StrategyVersion.objects.filter(pk=version.pk).exists()
    assert not StrategyRun.objects.filter(pk=completed_run.pk).exists()
    assert not StrategyTarget.objects.filter(run_id=completed_run.pk).exists()
    assert not StrategyAllocation.objects.filter(strategy_instance_id=instance_id).exists()
    assert not StrategyAttributedPosition.objects.filter(strategy_instance_id=instance_id).exists()
    assert not StrategyInputRequirement.objects.filter(pk__in=requirement_ids).exists()

    intent.refresh_from_db()
    attribution.refresh_from_db()
    capital_snapshot.refresh_from_db()
    allocation_decision.refresh_from_db()
    assert intent.strategy_instance_id is None and intent.strategy_version_id is None
    assert intent.strategy_snapshot["strategy_instance_name"] == "Delete me"
    assert attribution.strategy_instance_id is None and attribution.strategy_version_id is None
    assert attribution.strategy_snapshot["strategy_name"] == "Delete me"
    assert capital_snapshot.strategy_instance_id is None and capital_snapshot.strategy_snapshot["strategy_id"] == instance_id
    assert allocation_decision.strategy_instance_id is None and allocation_decision.strategy_snapshot["strategy_instance_id"] == instance_id
    assert Fill.objects.filter(pk=fill.pk).exists()
    assert CashLedgerEntry.objects.filter(pk=cash.pk).exists()
    assert PositionLedgerEntry.objects.filter(pk=position_ledger.pk).exists()
    assert ReconciliationRun.objects.filter(pk=reconciliation.pk).exists()
    assert ReconciliationBreak.objects.filter(pk=reconciliation_break.pk).exists()
    assert AuditEvent.objects.filter(pk=prior_audit.pk).exists()
    assert AuditEvent.objects.filter(
        event_type="strategy.deletion.succeeded", aggregate_id=str(instance_id)
    ).exists()

    detail = client.get(f"/api/v1/orders/{order.internal_id}/detail/").json()["data"]
    assert detail["strategy_attribution"][0]["strategy"] == "Delete me"
    assert detail["strategy_attribution"][0]["strategy_instance_id"] == instance_id
    allocation_detail = client.get(
        f"/api/v1/allocations/runs/{allocation_run.pk}/"
    ).json()["data"]
    assert allocation_detail["snapshots"][0]["strategy"] == "Delete me"
    assert allocation_detail["decisions"][0]["strategy_id"] == instance_id


@pytest.mark.parametrize(
    ("kind", "expected_code"),
    [
        ("open_order", "OPEN_ORDERS"),
        ("pending_intent", "ACTIVE_EXECUTIONS"),
        ("running_strategy", "ACTIVE_EXECUTIONS"),
        ("pending_rebalance", "PENDING_REBALANCES"),
        ("position", "NON_ZERO_POSITIONS"),
    ],
)
def test_deletion_reports_every_blocker_and_audits_rejection(
    client, portfolio, instrument, kind, expected_code
):
    instance = make_instance(portfolio, instrument, name=f"Blocked {kind}")
    version = instance.versions.get()
    if kind in {"open_order", "pending_intent"}:
        intent = OrderIntent.objects.create(
            portfolio=portfolio,
            strategy_instance=instance,
            strategy_version=version,
            instrument=instrument,
            side="BUY",
            quantity=1,
            idempotency_key=f"blocked-{kind}",
        )
        if kind == "open_order":
            Order.objects.create(
                intent=intent,
                internal_id="blocked-open-order",
                status="ACKNOWLEDGED",
                quantity=1,
            )
    elif kind == "running_strategy":
        StrategyRun.objects.create(
            strategy_instance=instance,
            strategy_version=version,
            input_hash="running-delete",
            status="RUNNING",
        )
    elif kind == "pending_rebalance":
        RebalanceRun.objects.create(
            portfolio=portfolio,
            trigger="DELETE_TEST",
            idempotency_key="pending-delete-rebalance",
            status="CALCULATING",
        )
    else:
        StrategyAttributedPosition.objects.create(
            strategy_instance=instance,
            portfolio=portfolio,
            instrument=instrument,
            quantity=Decimal("0.00000001"),
        )

    response = delete_request(client, instance, key=f"delete-blocked-{kind}")

    assert response.status_code == 409
    body = response.json()["error"]
    assert body["code"] == "STRATEGY_DELETION_BLOCKED"
    assert body["message"] == body["details"]["blockers"][0]["message"]
    blocker_codes = {item["code"] for item in body["details"]["blockers"]}
    assert expected_code in blocker_codes
    assert StrategyInstance.objects.filter(pk=instance.pk).exists()
    audit = AuditEvent.objects.get(
        event_type="strategy.deletion.rejected", aggregate_id=str(instance.pk)
    )
    assert audit.data["error"]["code"] == "STRATEGY_DELETION_BLOCKED"
    assert expected_code in {
        item["code"] for item in audit.data["error"]["details"]["blockers"]
    }


def test_deletion_requires_exact_name_and_audits_not_found(client, portfolio, instrument):
    instance = make_instance(portfolio, instrument, name="Exact strategy name")
    mismatch = delete_request(client, instance, name="exact strategy name", key="name-mismatch")
    assert mismatch.status_code == 400
    assert mismatch.json()["error"]["code"] == "STRATEGY_NAME_CONFIRMATION_MISMATCH"
    assert StrategyInstance.objects.filter(pk=instance.pk).exists()
    assert AuditEvent.objects.filter(
        event_type="strategy.deletion.rejected",
        aggregate_id=str(instance.pk),
        data__error__code="STRATEGY_NAME_CONFIRMATION_MISMATCH",
    ).exists()

    missing = client.delete(
        "/api/v1/strategy-instances/999999/",
        data=json.dumps({"strategy_name": "Missing"}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="missing-strategy-delete",
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "STRATEGY_NOT_FOUND"
    assert AuditEvent.objects.filter(
        event_type="strategy.deletion.rejected", aggregate_id="999999"
    ).exists()


def test_deletion_detaches_portfolio_builder_assignment(client, portfolio, instrument):
    instance = make_instance(portfolio, instrument, name="Builder-created strategy")
    plan = PortfolioConstructionPlan.objects.create(portfolio=portfolio, name="Deletion plan")
    goal = PortfolioGoalAllocation.objects.create(
        plan=plan,
        name="Deletion goal",
        allocation_weight=1,
        timeframe_bucket="BUILD",
        risk_level=3,
    )
    selection = GoalInstrumentSelection.objects.create(
        goal_allocation=goal,
        instrument=instrument,
    )
    assignment = GoalStrategyAssignment.objects.create(
        goal_instrument_selection=selection,
        strategy_definition=instance.definition,
        execution_timeframe=instance.timeframe,
        parameter_overrides=instance.parameters,
        parameter_hash="builder-deletion-parameter-hash",
        created_strategy_instance=instance,
    )

    response = delete_request(client, instance, key="delete-builder-created-strategy")

    assert response.status_code == 200
    assert response.json()["data"]["deleted"]["construction_assignments_detached"] == 1
    assert not StrategyInstance.objects.filter(pk=instance.pk).exists()
    assignment.refresh_from_db()
    assert assignment.created_strategy_instance_id is None
    assert assignment.strategy_definition_id == instance.definition_id
    assert assignment.enabled is True and assignment.create_instance is True


def test_deletion_updates_shared_input_reference_counts(portfolio, instrument):
    first = make_instance(portfolio, instrument, "Shared delete first")
    second = make_instance(portfolio, instrument, "Shared delete second")
    activate_strategy(first)
    activate_strategy(second)
    requirement = first.input_bindings.get().requirement
    requirement.refresh_from_db()
    assert requirement.active_ref_count == 2

    delete_strategy_instance(
        first.pk,
        first.name,
        attempt_key="shared-input-delete",
        actor="test",
    )

    requirement.refresh_from_db()
    assert requirement.active_ref_count == 1
    assert requirement.bindings.filter(strategy_instance=second, active=True).exists()
    assert not OutboxEvent.objects.filter(
        event_type="strategy.inputs.changed",
        payload__deletion=True,
    ).exists()


def test_deletion_tombstones_shared_input_when_only_inactive_binding_remains(
    portfolio, instrument
):
    first = make_instance(portfolio, instrument, "Active shared delete")
    second = make_instance(portfolio, instrument, "Inactive shared survivor")
    activate_strategy(first)
    requirement = first.input_bindings.get().requirement

    delete_strategy_instance(
        first.pk,
        first.name,
        attempt_key="inactive-shared-input-delete",
        actor="test",
    )

    requirement.refresh_from_db()
    assert requirement.active_ref_count == 0
    assert requirement.bindings.filter(
        strategy_instance=second,
        active=False,
    ).exists()
    event = OutboxEvent.objects.get(
        event_type="strategy.inputs.changed",
        payload__deletion=True,
    )
    assert event.payload["removed_requirement_hashes"] == [
        requirement.identity_hash
    ]


def test_deletion_publishes_durable_tombstone_for_unused_stream_inputs(
    portfolio, instrument
):
    instance = make_instance(portfolio, instrument, "Delete stream inputs")
    activate_strategy(instance)
    instance_id = instance.pk
    requirement_hashes = sorted(
        instance.input_bindings.values_list("requirement__identity_hash", flat=True)
    )

    result = delete_strategy_instance(
        instance_id,
        instance.name,
        attempt_key="stream-input-delete",
        actor="test",
    )

    assert result["deleted"]["input_requirements_removed"] == len(requirement_hashes)
    assert not StrategyInstance.objects.filter(pk=instance_id).exists()
    event = OutboxEvent.objects.get(
        event_type="strategy.inputs.changed",
        payload__deletion=True,
    )
    assert event.aggregate_type == "instrument"
    assert event.aggregate_id == str(instrument.pk)
    assert event.partition_key == str(instrument.pk)
    assert event.payload["strategy_instance_id"] == instance_id
    assert event.payload["requirements"] == []
    assert event.payload["removed_requirement_hashes"] == requirement_hashes


def test_strategy_deletion_is_atomic_on_unexpected_failure(
    portfolio, instrument, monkeypatch
):
    instance = make_instance(portfolio, instrument, "Atomic deletion")
    instance_id = instance.pk
    original_delete = StrategyInstance.delete

    def fail_after_instance_delete(self, *args, **kwargs):
        if self.pk == instance_id:
            raise RuntimeError("simulated deletion failure")
        return original_delete(self, *args, **kwargs)

    monkeypatch.setattr(StrategyInstance, "delete", fail_after_instance_delete)
    with pytest.raises(RuntimeError, match="simulated deletion failure"):
        delete_strategy_instance(
            instance_id,
            instance.name,
            attempt_key="atomic-delete",
            actor="test",
        )

    assert StrategyInstance.objects.filter(pk=instance_id).exists()
    assert StrategyVersion.objects.filter(strategy_instance_id=instance_id).exists()
    assert StrategyAllocation.objects.filter(strategy_instance_id=instance_id).exists()
    assert not AuditEvent.objects.filter(
        event_type="strategy.deletion.succeeded", aggregate_id=str(instance_id)
    ).exists()
