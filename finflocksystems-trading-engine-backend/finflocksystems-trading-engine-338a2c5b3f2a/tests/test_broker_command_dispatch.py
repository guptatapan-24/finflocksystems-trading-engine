from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.accounts.models import BrokerAccount
from apps.broker_gateway.client import (
    GatewayCommandRejected,
    GatewayError,
    GatewayTransportError,
)
from apps.broker_gateway.crypto import BrokerCredentialError
from apps.execution.dispatch import (
    claim_next_broker_command,
    dispatch_broker_command,
    enqueue_place_command,
    execute_order_intent,
    record_broker_order_observation,
    recover_stuck_broker_commands,
    request_order_cancellation,
    request_order_modification,
)
from apps.execution.models import BrokerCommand
from apps.instruments.models import BrokerContract, Instrument, OptionContract
from apps.oms.models import Order, OrderIntent
from apps.oms.services import apply_execution
from apps.portfolios.models import TradingPortfolio
from tests.managed_gateway import bind_managed_gateway


pytestmark = pytest.mark.django_db


class FakeGateway:
    def __init__(
        self,
        *,
        place_result=None,
        place_error=None,
        cancel_error=None,
        state=None,
        health=None,
    ):
        self.place_result = place_result or {"command_id": 41, "status": "PENDING"}
        self.place_error = place_error
        self.cancel_error = cancel_error
        self.state = state or {
            "commands": [],
            "reference": {},
            "broker_order": {},
            "non_submission_established": False,
        }
        self.health_result = health or {
            "connected": True,
            "reconciled": True,
            "mode": "paper",
        }
        self.place_calls = 0
        self.modify_calls = 0
        self.cancel_calls = 0

    def health(self):
        if isinstance(self.health_result, Exception):
            raise self.health_result
        return self.health_result

    def place_order(self, payload, key):
        self.place_calls += 1
        assert payload["internal_id"]
        if self.place_error:
            raise self.place_error
        return self.place_result

    def modify_order(self, internal_id, payload, key):
        self.modify_calls += 1
        return {"command_id": 42, "status": "PENDING"}

    def cancel_order(self, internal_id, key):
        self.cancel_calls += 1
        if self.cancel_error:
            raise self.cancel_error
        return {"command_id": 43, "status": "PENDING"}

    def order_state(self, internal_id):
        return self.state


def _order(settings, *, status="QUEUED", filled="0", operation_status="QUEUED"):
    account = BrokerAccount.objects.create(
        account_id=f"DU-{Order.objects.count()+1}",
        available_cash=100000,
        net_liquidation=100000,
        is_reconciled=True,
    )
    portfolio = TradingPortfolio.objects.create(
        name=f"Paper {account.account_id}", account=account
    )
    session = bind_managed_gateway(portfolio, settings)
    instrument = Instrument.objects.create(symbol=f"T{Instrument.objects.count()+1}")
    intent = OrderIntent.objects.create(
        portfolio=portfolio,
        instrument=instrument,
        side="BUY",
        quantity=5,
        reference_price=100,
        mode="PAPER",
        operation_status=operation_status,
        idempotency_key=f"intent-{account.account_id}",
    )
    order = Order.objects.create(
        intent=intent,
        internal_id=f"internal-{account.account_id}",
        status=status,
        quantity=5,
        filled_quantity=Decimal(filled),
    )
    return order, session


def _claimed_place(settings):
    order, _ = _order(settings)
    command = enqueue_place_command(order)
    assert claim_next_broker_command() == command.pk
    command.refresh_from_db()
    return order, command


def test_option_place_command_carries_exact_qualified_contract_identity(settings):
    order, _ = _order(settings)
    instrument = order.intent.instrument
    instrument.asset_class = "OPT"
    instrument.symbol = "NIFTY26AUG25000CE"
    instrument.exchange = "NFO"
    instrument.currency = "INR"
    instrument.multiplier = 75
    instrument.save()
    BrokerContract.objects.create(
        instrument=instrument, conid=7654321, local_symbol=instrument.symbol,
        primary_exchange="NSE",
    )
    OptionContract.objects.create(
        instrument=instrument, underlying_conid=1234, expiration=date(2026,8,26),
        strike=25000, right="C", trading_class="NIFTY", multiplier=75,
    )

    payload = enqueue_place_command(order).request_payload

    assert payload == {
        **payload,
        "asset_class": "OPT", "conid": 7654321,
        "expiration": "2026-08-26", "strike": "25000", "right": "C",
        "multiplier": "75", "trading_class": "NIFTY",
        "underlying_conid": 1234, "local_symbol": instrument.symbol,
        "primary_exchange": "NSE",
    }


def _make_uncertain(settings, state):
    order, command = _claimed_place(settings)
    gateway = FakeGateway(
        place_error=GatewayTransportError("response lost"),
        state=state,
    )
    assert dispatch_broker_command(command.pk, gateway) == "UNCERTAIN"
    command.refresh_from_db()
    assert command.status == BrokerCommand.Status.UNCERTAIN
    BrokerCommand.objects.filter(pk=command.pk).update(next_attempt_at=timezone.now())
    assert claim_next_broker_command() == command.pk
    return order, command, gateway


def test_process_crash_before_gateway_call_is_recovered(settings):
    order, command = _claimed_place(settings)
    BrokerCommand.objects.filter(pk=command.pk).update(
        claimed_at=timezone.now() - timedelta(minutes=10)
    )
    assert recover_stuck_broker_commands()["claimed"] == 1
    command.refresh_from_db()
    assert command.status == BrokerCommand.Status.PENDING
    assert claim_next_broker_command() == command.pk
    gateway = FakeGateway()
    assert dispatch_broker_command(command.pk, gateway) == "ACKNOWLEDGED"
    assert gateway.place_calls == 1


def test_process_crash_after_sending_marker_reconciles_before_retry(settings):
    order, command = _claimed_place(settings)
    BrokerCommand.objects.filter(pk=command.pk).update(
        status=BrokerCommand.Status.SENDING,
        sent_at=timezone.now() - timedelta(minutes=10),
    )
    assert recover_stuck_broker_commands()["sending"] == 1
    command.refresh_from_db()
    order.refresh_from_db()
    assert command.status == BrokerCommand.Status.UNCERTAIN
    assert order.status == "UNKNOWN"
    assert claim_next_broker_command() == command.pk
    gateway = FakeGateway(
        state={
            "commands": [],
            "reference": {},
            "broker_order": {},
            "non_submission_established": True,
        }
    )
    assert dispatch_broker_command(command.pk, gateway) == "RETRY"
    assert gateway.place_calls == 0


def test_gateway_accepts_order_but_response_is_lost(settings):
    order, command, gateway = _make_uncertain(
        settings,
        {
            "commands": [
                {
                    "command_id": 77,
                    "command_type": "PLACE_ORDER",
                    "status": "PENDING",
                    "result": {},
                }
            ],
            "reference": {},
            "broker_order": {},
            "non_submission_established": False,
        },
    )
    assert dispatch_broker_command(command.pk, gateway) == "ACKNOWLEDGED"
    command.refresh_from_db()
    assert command.gateway_command_id == 77
    assert gateway.place_calls == 1


def test_http_timeout_with_broker_order_present_attaches_order(settings):
    order, command, gateway = _make_uncertain(
        settings,
        {
            "commands": [],
            "reference": {},
            "broker_order": {
                "internal_id": "ignored-by-test-helper",
                "broker_order_id": "9001",
                "permanent_id": "8001",
                "status": "Submitted",
            },
            "non_submission_established": False,
        },
    )
    assert dispatch_broker_command(command.pk, gateway) == "ACKNOWLEDGED"
    command.refresh_from_db()
    order.refresh_from_db()
    assert command.broker_order_id == order.broker_order_id == "9001"
    assert command.broker_permanent_id == order.broker_permanent_id == "8001"
    assert gateway.place_calls == 1


def test_http_timeout_with_no_broker_order_retries_only_after_negative_proof(settings):
    order, command, gateway = _make_uncertain(
        settings,
        {
            "commands": [],
            "reference": {},
            "broker_order": {},
            "non_submission_established": True,
        },
    )
    assert dispatch_broker_command(command.pk, gateway) == "RETRY"
    command.refresh_from_db()
    assert command.status == BrokerCommand.Status.RETRY
    assert gateway.place_calls == 1
    BrokerCommand.objects.filter(pk=command.pk).update(next_attempt_at=timezone.now())
    assert claim_next_broker_command() == command.pk
    gateway.place_error = None
    assert dispatch_broker_command(command.pk, gateway) == "ACKNOWLEDGED"
    assert gateway.place_calls == 2


def test_duplicate_dispatch_does_not_submit_twice(settings):
    order, command = _claimed_place(settings)
    duplicate = enqueue_place_command(order)
    assert duplicate.pk == command.pk
    gateway = FakeGateway()
    assert dispatch_broker_command(command.pk, gateway) == "ACKNOWLEDGED"
    assert dispatch_broker_command(command.pk, gateway) == "ACKNOWLEDGED"
    assert gateway.place_calls == 1
    assert BrokerCommand.objects.filter(
        order=order, command_type=BrokerCommand.CommandType.PLACE
    ).count() == 1


def test_gateway_restart_holds_then_recovers_without_losing_command(settings):
    order, command = _claimed_place(settings)
    unavailable = FakeGateway(health=GatewayError("gateway restarting"))
    assert dispatch_broker_command(command.pk, unavailable) == "RETRY"
    command.refresh_from_db()
    order.refresh_from_db()
    order.intent.refresh_from_db()
    assert command.status == BrokerCommand.Status.RETRY
    assert order.status == "BROKER_BLOCKED"
    assert order.intent.operation_status == "BROKER_BLOCKED"
    assert unavailable.place_calls == 0
    BrokerCommand.objects.filter(pk=command.pk).update(next_attempt_at=timezone.now())
    assert claim_next_broker_command() == command.pk
    available = FakeGateway()
    assert dispatch_broker_command(command.pk, available) == "ACKNOWLEDGED"
    assert available.place_calls == 1


def test_gateway_credential_failure_holds_broker_command_instead_of_leaving_it_claimed(
    settings, monkeypatch
):
    order, command = _claimed_place(settings)
    monkeypatch.setattr(
        "apps.execution.dispatch.GatewayClient",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            BrokerCredentialError("Stored broker credential cannot be decrypted")
        ),
    )

    assert dispatch_broker_command(command.pk) == "RETRY"

    command.refresh_from_db()
    order.refresh_from_db()
    order.intent.refresh_from_db()
    assert command.status == BrokerCommand.Status.RETRY
    assert command.last_error == "Stored broker credential cannot be decrypted"
    assert order.status == "BROKER_BLOCKED"
    assert order.intent.operation_status == "BROKER_BLOCKED"
    assert order.intent.operation_error == command.last_error


def test_definitive_gateway_rejection_records_failure_consistently(settings):
    order, command = _claimed_place(settings)
    gateway = FakeGateway(
        place_error=GatewayCommandRejected("invalid broker order")
    )
    assert dispatch_broker_command(command.pk, gateway) == "FAILED"
    command.refresh_from_db()
    order.refresh_from_db()
    order.intent.refresh_from_db()
    assert command.status == BrokerCommand.Status.FAILED
    assert order.status == "REJECTED"
    assert order.intent.operation_status == "BROKER_REJECTED"
    assert order.intent.operation_status != "FAILED"


def test_broker_acknowledgement_before_local_response_updates_command(settings):
    order, command = _claimed_place(settings)
    BrokerCommand.objects.filter(pk=command.pk).update(
        status=BrokerCommand.Status.SENDING, sent_at=timezone.now()
    )
    record_broker_order_observation(
        order,
        {
            "internal_id": order.internal_id,
            "broker_order_id": "501",
            "permanent_id": "601",
            "broker_status": "Submitted",
        },
    )
    command.refresh_from_db()
    order.refresh_from_db()
    assert command.status == BrokerCommand.Status.ACKNOWLEDGED
    assert order.broker_order_id == "501"
    assert order.broker_permanent_id == "601"


def test_place_modify_and_cancel_use_same_durable_framework(settings):
    order, _ = _order(settings, status="PARTIALLY_FILLED", filled="2")
    place = enqueue_place_command(order)
    BrokerCommand.objects.filter(pk=place.pk).update(
        status=BrokerCommand.Status.ACKNOWLEDGED,
        acknowledged_at=timezone.now(),
    )
    modify = request_order_modification(order, {"quantity": "4"}, "modify-1")
    assert claim_next_broker_command() == modify.pk
    gateway = FakeGateway()
    assert dispatch_broker_command(modify.pk, gateway) == "ACKNOWLEDGED"
    cancel = request_order_cancellation(order, "cancel-1", "operator request")
    assert claim_next_broker_command() == cancel.pk
    assert dispatch_broker_command(cancel.pk, gateway) == "ACKNOWLEDGED"
    order.refresh_from_db()
    assert {
        place.command_type,
        modify.command_type,
        cancel.command_type,
    } == {"PLACE", "MODIFY", "CANCEL"}
    assert order.status == "CANCEL_PENDING"
    assert cancel.request_payload["internal_id"] == order.internal_id
    assert gateway.modify_calls == gateway.cancel_calls == 1


def test_fill_while_cancellation_pending_keeps_cancel_state(settings):
    order, _ = _order(settings, status="PARTIALLY_FILLED", filled="2")
    request_order_cancellation(order, "cancel-fill", "partial fill")
    order.refresh_from_db()
    apply_execution(
        order,
        {
            "execution_id": "fill-during-cancel",
            "quantity": "1",
            "price": "100",
            "commission": "1",
            "currency": "USD",
            "executed_at": timezone.now(),
        },
    )
    order.refresh_from_db()
    assert order.filled_quantity == Decimal("3")
    assert order.status == "CANCEL_PENDING"


def test_definitive_cancel_rejection_restores_partially_filled_state(settings):
    order, _ = _order(settings, status="PARTIALLY_FILLED", filled="2")
    command = request_order_cancellation(order, "cancel-rejected", "test")
    assert claim_next_broker_command() == command.pk
    gateway = FakeGateway(
        cancel_error=GatewayCommandRejected("cancel was rejected")
    )
    assert dispatch_broker_command(command.pk, gateway) == "FAILED"
    order.refresh_from_db()
    assert order.status == "PARTIALLY_FILLED"


def test_automatic_paper_intent_creates_durable_place_command(
    settings, monkeypatch
):
    account = BrokerAccount.objects.create(
        account_id="DU-AUTO",
        available_cash=100000,
        net_liquidation=100000,
        is_reconciled=True,
    )
    portfolio = TradingPortfolio.objects.create(name="Automatic", account=account)
    bind_managed_gateway(portfolio, settings)
    instrument = Instrument.objects.create(symbol="AUTO")
    intent = OrderIntent.objects.create(
        portfolio=portfolio,
        instrument=instrument,
        side="BUY",
        quantity=2,
        reference_price=100,
        source="REBALANCE",
        mode="PAPER",
        idempotency_key="automatic-intent",
    )
    from apps.broker_gateway.client import GatewayClient

    monkeypatch.setattr(
        GatewayClient,
        "health",
        lambda self: {"connected": True, "reconciled": True, "mode": "paper"},
    )
    command = execute_order_intent(intent.pk)
    assert command.command_type == BrokerCommand.CommandType.PLACE
    assert command.request_payload["internal_id"] == command.order.internal_id
    assert "local_symbol" not in command.request_payload
    assert "primary_exchange" not in command.request_payload
    assert command.status == BrokerCommand.Status.PENDING
