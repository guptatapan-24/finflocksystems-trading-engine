import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier

import pytest
from django.db import close_old_connections, connection
from django.test import Client
from django.utils import timezone

from apps.accounts.models import BrokerAccount
from apps.audit.models import AuditEvent, OperationAttempt
from apps.broker_gateway.client import GatewayClient, GatewayError
from apps.broker_gateway.crypto import BrokerCredentialError
from apps.broker_gateway.models import BrokerSessionAccount
from apps.execution.dispatch import (
    claim_next_broker_command,
    dispatch_broker_command,
    execute_order_intent,
    process_order_intents,
)
from apps.execution.models import BrokerCommand
from apps.execution.tasks import execute_order_intents
from apps.instruments.models import Instrument
from apps.market_streams.models import InstrumentMarketState
from apps.oms.models import Order, OrderIntent
from apps.portfolios.models import PortfolioPosition, TradingPortfolio
from apps.reconciliation.models import ReconciliationBreak, ReconciliationRun
from apps.risk.models import CapitalReservation
from tests.managed_gateway import bind_managed_gateway


pytestmark = pytest.mark.django_db(transaction=True)


def _manual_case(settings, *, mode="paper", market_price="101", fresh=True):
    account = BrokerAccount.objects.create(
        account_id=f"DU-{mode.upper()}",
        available_cash=100000,
        net_liquidation=100000,
        is_reconciled=True,
    )
    portfolio = TradingPortfolio.objects.create(name=f"{mode.title()} manual", account=account)
    session = bind_managed_gateway(portfolio, settings, mode=mode)
    instrument = Instrument.objects.create(symbol=f"MAN-{mode.upper()}")
    if market_price is not None:
        InstrumentMarketState.objects.create(
            instrument=instrument,
            status="FRESH" if fresh else "STALE",
            reference_price=market_price,
            latest_event_at=timezone.now() if fresh else timezone.now() - timedelta(hours=1),
            stale_after_seconds=300,
            reference_price_provider="IBKR",
            reference_price_source="persisted_test_tick",
        )
    return account, portfolio, session, instrument


def _payload(portfolio, instrument, **changes):
    payload = {
        "portfolio_id": portfolio.pk,
        "instrument_id": instrument.pk,
        "side": "BUY",
        "order_type": "MKT",
        "quantity": "5",
        "time_in_force": "DAY",
    }
    payload.update(changes)
    return payload


def _post(client, payload, key):
    return client.post(
        "/api/v1/orders/",
        json.dumps(payload),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=key,
    )


def _disable_task_enqueue(monkeypatch):
    calls = []
    monkeypatch.setattr(execute_order_intents, "delay", lambda *args, **kwargs: calls.append((args, kwargs)))
    return calls


def _healthy_gateway(monkeypatch, mode="paper"):
    monkeypatch.setattr(
        GatewayClient,
        "health",
        lambda self: {"connected": True, "reconciled": True, "mode": mode},
    )


def test_manual_market_order_creates_one_pending_origin_intent(client, settings, monkeypatch):
    _, portfolio, _, instrument = _manual_case(settings)
    task_calls = _disable_task_enqueue(monkeypatch)

    result = _post(client, _payload(portfolio, instrument), "manual-pending")

    assert result.status_code == 202
    assert result.json()["data"] == {
        "intent_id": OrderIntent.objects.get().pk,
        "origin": "MANUAL",
        "operation_status": "PENDING",
        "operation_error": "",
        "retryable": False,
        "attempt_count": 1,
        "message": "Manual order intent accepted for asynchronous execution",
    }
    intent = OrderIntent.objects.get()
    assert intent.source == "MANUAL"
    assert intent.origin == OrderIntent.Origin.MANUAL
    assert intent.strategy_instance_id is None
    assert intent.strategy_version_id is None
    assert intent.rebalance_id is None
    assert not Order.objects.exists()
    assert task_calls == [((1,), {})]
    assert AuditEvent.objects.get(aggregate_id=str(intent.pk)).data["origin"] == "MANUAL"


def test_manual_intent_status_reports_durable_pending_state(client, settings, monkeypatch):
    _, portfolio, _, instrument = _manual_case(settings)
    _disable_task_enqueue(monkeypatch)
    accepted = _post(client, _payload(portfolio, instrument), "manual-status-pending")
    intent_id = accepted.json()["data"]["intent_id"]

    status = client.get(f"/api/v1/orders/intents/{intent_id}/status/")

    assert status.status_code == 200
    assert status.json()["data"] == accepted.json()["data"]


def test_old_gateway_session_break_does_not_hold_current_session_intent(
    client, settings, monkeypatch
):
    account, portfolio, current_session, instrument = _manual_case(settings)
    old_portfolio = TradingPortfolio.objects.create(name="Retired live route", account=account)
    old_session = bind_managed_gateway(old_portfolio, settings)
    old_run = ReconciliationRun.objects.create(
        broker_account=account, gateway_session=old_session, status="BLOCKED"
    )
    ReconciliationBreak.objects.create(
        run=old_run, category="GATEWAY", severity="CRITICAL", material=True
    )
    _disable_task_enqueue(monkeypatch)
    _healthy_gateway(monkeypatch)
    accepted = _post(client, _payload(portfolio, instrument), "current-session-risk")

    command = execute_order_intent(accepted.json()["data"]["intent_id"])

    assert command is not None
    assert command.gateway_session_id == current_session.pk


def test_manual_intent_status_reports_real_oms_order_and_command(client, settings, monkeypatch):
    _, portfolio, _, instrument = _manual_case(settings)
    _disable_task_enqueue(monkeypatch)
    _healthy_gateway(monkeypatch)
    accepted = _post(client, _payload(portfolio, instrument), "manual-status-order")
    command = execute_order_intent(accepted.json()["data"]["intent_id"])

    status = client.get(f"/api/v1/orders/intents/{command.order.intent_id}/status/")

    assert status.status_code == 200
    assert status.json()["data"] == {
        "intent_id": command.order.intent_id,
        "origin": "MANUAL",
        "operation_status": "QUEUED",
        "operation_error": "",
        "retryable": False,
        "attempt_count": 1,
        "message": "Manual order intent accepted for asynchronous execution",
        "internal_id": command.order.internal_id,
        "status": "QUEUED",
        "approved_quantity": "5.00000000",
        "broker_order_id": "",
        "broker_permanent_id": "",
        "filled_quantity": "0E-8",
        "average_fill_price": "0E-8",
        "fill_count": 0,
        "broker_command": {
            "id": command.pk,
            "command_type": "PLACE",
            "status": "PENDING",
            "attempt_count": 0,
            "gateway_command_id": None,
            "broker_order_id": "",
            "broker_permanent_id": "",
            "last_error": "",
            "uncertainty_reason": "",
        },
    }


def test_manual_intent_status_does_not_expose_non_manual_or_missing_intents(client, settings):
    _, portfolio, _, instrument = _manual_case(settings)
    automatic = OrderIntent.objects.create(
        portfolio=portfolio,
        instrument=instrument,
        side="BUY",
        quantity=1,
        reference_price=101,
        idempotency_key="strategy-intent",
        origin=OrderIntent.Origin.STRATEGY,
    )

    automatic_result = client.get(f"/api/v1/orders/intents/{automatic.pk}/status/")
    missing_result = client.get("/api/v1/orders/intents/999999/status/")

    assert automatic_result.status_code == 404
    assert automatic_result.json()["error"]["code"] == "MANUAL_ORDER_INTENT_NOT_FOUND"
    assert missing_result.status_code == 404


def test_manual_http_request_never_calls_gateway(client, settings, monkeypatch):
    _, portfolio, _, instrument = _manual_case(settings)
    _disable_task_enqueue(monkeypatch)

    def unexpected_gateway_call(*args, **kwargs):
        raise AssertionError("Gateway must not be called by the manual-order HTTP request")

    monkeypatch.setattr(GatewayClient, "health", unexpected_gateway_call)
    monkeypatch.setattr(GatewayClient, "place_order", unexpected_gateway_call)

    result = _post(client, _payload(portfolio, instrument), "manual-no-gateway")

    assert result.status_code == 202
    assert OrderIntent.objects.count() == 1
    assert BrokerCommand.objects.count() == 0


def test_existing_intent_worker_creates_oms_order_and_broker_command(
    client, settings, monkeypatch
):
    _, portfolio, _, instrument = _manual_case(settings)
    _disable_task_enqueue(monkeypatch)
    _healthy_gateway(monkeypatch)
    accepted = _post(client, _payload(portfolio, instrument), "manual-worker")
    intent = OrderIntent.objects.get(pk=accepted.json()["data"]["intent_id"])

    result = process_order_intents(limit=1)

    assert result == {"claimed": 1, "commands_created": 1}
    order = Order.objects.get(intent=intent)
    command = BrokerCommand.objects.get(order=order)
    assert order.status == "QUEUED"
    assert command.command_type == BrokerCommand.CommandType.PLACE
    assert command.status == BrokerCommand.Status.PENDING
    attempt = OperationAttempt.objects.get(operation_type="ORDER_INTENT", operation_id=str(intent.pk))
    assert attempt.status == "COMPLETED"
    assert attempt.result == {"order_id": order.internal_id, "broker_command_id": command.pk}


def test_manual_intent_records_gateway_credential_failure_instead_of_staying_claimed(
    client, settings, monkeypatch
):
    _, portfolio, _, instrument = _manual_case(settings)
    _disable_task_enqueue(monkeypatch)
    accepted = _post(client, _payload(portfolio, instrument), "manual-bad-gateway-token")
    monkeypatch.setattr(
        GatewayClient,
        "for_portfolio",
        classmethod(lambda cls, portfolio: (_ for _ in ()).throw(
            BrokerCredentialError("Stored broker credential cannot be decrypted")
        )),
    )

    assert process_order_intents(limit=1) == {"claimed": 1, "commands_created": 0}

    intent = OrderIntent.objects.get(pk=accepted.json()["data"]["intent_id"])
    assert intent.operation_status == "PENDING"
    assert intent.retryable is True
    assert intent.operation_error == "Stored broker credential cannot be decrypted"
    assert not Order.objects.exists()
    assert not BrokerCommand.objects.exists()
    attempt = OperationAttempt.objects.get(
        operation_type="ORDER_INTENT", operation_id=str(intent.pk)
    )
    assert attempt.status == "FAILED"
    assert attempt.retryable is True
    assert attempt.error == intent.operation_error


def test_manual_intent_flows_through_broker_command_worker_to_gateway(
    client, settings, monkeypatch
):
    _, portfolio, _, instrument = _manual_case(settings)
    _disable_task_enqueue(monkeypatch)
    _healthy_gateway(monkeypatch)
    accepted = _post(client, _payload(portfolio, instrument), "manual-to-gateway")
    command = execute_order_intent(accepted.json()["data"]["intent_id"])

    class RecordingGateway:
        place_calls = []

        def health(self):
            return {"connected": True, "reconciled": True, "mode": "paper"}

        def place_order(self, payload, key):
            self.place_calls.append((payload, key))
            return {"command_id": 71, "status": "PENDING"}

    gateway = RecordingGateway()
    assert claim_next_broker_command() == command.pk
    assert dispatch_broker_command(command.pk, gateway) == "ACKNOWLEDGED"

    command.refresh_from_db()
    command.order.refresh_from_db()
    assert command.status == BrokerCommand.Status.ACKNOWLEDGED
    assert command.order.status == "SUBMITTED"
    assert len(gateway.place_calls) == 1
    assert gateway.place_calls[0][0]["internal_id"] == command.order.internal_id


def test_duplicate_identical_manual_request_returns_same_durable_intent(
    client, settings, monkeypatch
):
    _, portfolio, _, instrument = _manual_case(settings)
    _disable_task_enqueue(monkeypatch)
    payload = _payload(portfolio, instrument)

    first = _post(client, payload, "manual-duplicate")
    InstrumentMarketState.objects.filter(instrument=instrument).update(status="STALE")
    duplicate = _post(client, payload, "manual-duplicate")

    assert first.status_code == duplicate.status_code == 202
    assert first.json()["data"]["intent_id"] == duplicate.json()["data"]["intent_id"]
    assert OrderIntent.objects.count() == 1
    assert Order.objects.count() == BrokerCommand.objects.count() == 0


def test_duplicate_key_with_changed_payload_is_an_idempotency_conflict(
    client, settings, monkeypatch
):
    _, portfolio, _, instrument = _manual_case(settings)
    _disable_task_enqueue(monkeypatch)
    assert _post(client, _payload(portfolio, instrument), "manual-conflict").status_code == 202

    conflict = _post(
        client,
        _payload(portfolio, instrument, quantity="6"),
        "manual-conflict",
    )

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert OrderIntent.objects.count() == 1


def test_live_manual_request_is_rejected_without_creating_an_intent(
    client, settings, monkeypatch
):
    settings.ALLOW_LIVE_TRADING = False
    _, portfolio, _, instrument = _manual_case(settings, mode="live")
    task_calls = _disable_task_enqueue(monkeypatch)

    result = _post(client, _payload(portfolio, instrument), "manual-live")

    assert result.status_code == 403
    assert result.json()["error"]["code"] == "LIVE_MANUAL_TRADING_DISABLED"
    assert not OrderIntent.objects.exists()
    assert task_calls == []


def test_live_manual_request_uses_live_intent_command_and_gateway(
    client, settings, monkeypatch
):
    settings.ALLOW_LIVE_TRADING = True
    _, portfolio, _, instrument = _manual_case(settings, mode="live")
    _disable_task_enqueue(monkeypatch)
    _healthy_gateway(monkeypatch, mode="live")

    accepted = _post(
        client,
        _payload(portfolio, instrument),
        "manual-live-enabled",
    )

    assert accepted.status_code == 202
    intent = OrderIntent.objects.get(pk=accepted.json()["data"]["intent_id"])
    assert intent.mode == "LIVE"
    command = execute_order_intent(intent.pk)
    assert command.mode == "LIVE"
    assert command.gateway_session.mode == "live"

    class LiveGateway:
        place_calls = 0

        def health(self):
            return {"connected": True, "reconciled": True, "mode": "live"}

        def place_order(self, payload, key):
            self.place_calls += 1
            return {"command_id": 72, "status": "PENDING"}

    gateway = LiveGateway()
    assert claim_next_broker_command() == command.pk
    assert dispatch_broker_command(command.pk, gateway) == "ACKNOWLEDGED"
    assert gateway.place_calls == 1


def test_market_order_ignores_untrusted_client_reference_price(
    client, settings, monkeypatch
):
    _, portfolio, _, instrument = _manual_case(settings, market_price="123.45")
    _disable_task_enqueue(monkeypatch)

    result = _post(
        client,
        _payload(portfolio, instrument, reference_price="1.00"),
        "manual-trusted-price",
    )

    assert result.status_code == 202
    intent = OrderIntent.objects.get()
    assert intent.reference_price == Decimal("123.45")
    assert intent.requires_fresh_price is True


@pytest.mark.parametrize(
    ("market_price", "fresh"),
    [(None, False), ("100", False)],
)
def test_market_order_rejects_unavailable_or_stale_persisted_price(
    client, settings, monkeypatch, market_price, fresh
):
    _, portfolio, _, instrument = _manual_case(
        settings, market_price=market_price, fresh=fresh
    )
    _disable_task_enqueue(monkeypatch)

    result = _post(client, _payload(portfolio, instrument), "manual-stale-price")

    assert result.status_code == 422
    assert result.json()["error"]["code"] == "MARKET_PRICE_UNAVAILABLE"
    assert not OrderIntent.objects.exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"order_type": "LMT"},
        {"order_type": "LMT", "limit_price": "100", "stop_price": "99"},
        {"order_type": "STP"},
        {"order_type": "STP", "stop_price": "100", "limit_price": "99"},
        {"order_type": "STP_LMT", "limit_price": "100"},
        {"order_type": "STP_LMT", "stop_price": "100"},
    ],
)
def test_manual_order_type_price_requirements_are_enforced(
    client, settings, monkeypatch, changes
):
    _, portfolio, _, instrument = _manual_case(settings)
    _disable_task_enqueue(monkeypatch)

    result = _post(
        client,
        _payload(portfolio, instrument, **changes),
        f"invalid-{changes}",
    )

    assert result.status_code == 400
    assert result.json()["error"]["code"] == "INVALID_ORDER"
    assert not OrderIntent.objects.exists()


def test_limit_order_uses_limit_price_without_market_state(
    client, settings, monkeypatch
):
    _, portfolio, _, instrument = _manual_case(settings, market_price=None)
    _disable_task_enqueue(monkeypatch)

    result = _post(
        client,
        _payload(portfolio, instrument, order_type="LMT", limit_price="98.75"),
        "manual-limit-price",
    )

    assert result.status_code == 202
    intent = OrderIntent.objects.get()
    assert intent.reference_price == Decimal("98.75")
    assert intent.requires_fresh_price is False


@pytest.mark.parametrize(
    ("order_type", "prices", "expected"),
    [
        ("STP", {"stop_price": "90"}, Decimal("101")),
        ("STP", {"stop_price": "110"}, Decimal("110")),
        (
            "STP_LMT",
            {"stop_price": "99", "limit_price": "105"},
            Decimal("105"),
        ),
    ],
)
def test_stop_orders_use_conservative_risk_prices(
    client, settings, monkeypatch, order_type, prices, expected
):
    _, portfolio, _, instrument = _manual_case(settings)
    _disable_task_enqueue(monkeypatch)

    result = _post(
        client,
        _payload(portfolio, instrument, order_type=order_type, **prices),
        f"manual-{order_type}-{expected}",
    )

    assert result.status_code == 202
    assert OrderIntent.objects.get().reference_price == expected


def test_sell_exceeding_unreserved_long_position_is_rejected_by_common_risk(
    client, settings, monkeypatch
):
    _, portfolio, _, instrument = _manual_case(settings)
    PortfolioPosition.objects.create(
        portfolio=portfolio, instrument=instrument, quantity=2, average_cost=90
    )
    _disable_task_enqueue(monkeypatch)
    _healthy_gateway(monkeypatch)
    accepted = _post(
        client,
        _payload(portfolio, instrument, side="SELL", quantity="3"),
        "manual-oversell",
    )

    assert accepted.status_code == 202
    assert execute_order_intent(accepted.json()["data"]["intent_id"]) is None
    intent = OrderIntent.objects.get()
    intent.refresh_from_db()
    assert intent.operation_status == "RISK_REJECTED"
    assert "short selling is disabled" in intent.operation_error
    check = intent.risk_checks.get(check_name="available_position")
    assert check.details["available_position_quantity"] == "2.00000000"
    assert not Order.objects.exists()


def test_pending_and_open_sells_reduce_available_position(
    client, settings, monkeypatch
):
    _, portfolio, _, instrument = _manual_case(settings)
    PortfolioPosition.objects.create(
        portfolio=portfolio, instrument=instrument, quantity=10, average_cost=90
    )
    pending = OrderIntent.objects.create(
        portfolio=portfolio,
        instrument=instrument,
        side="SELL",
        quantity=2,
        reference_price=101,
        operation_status="PENDING",
        idempotency_key="pending-sell-reservation",
    )
    open_intent = OrderIntent.objects.create(
        portfolio=portfolio,
        instrument=instrument,
        side="SELL",
        quantity=3,
        reference_price=101,
        operation_status="QUEUED",
        idempotency_key="open-sell-reservation",
    )
    Order.objects.create(
        intent=open_intent,
        internal_id="open-sell-order",
        status="ACKNOWLEDGED",
        quantity=3,
        filled_quantity=1,
    )
    _disable_task_enqueue(monkeypatch)
    _healthy_gateway(monkeypatch)
    accepted = _post(
        client,
        _payload(portfolio, instrument, side="SELL", quantity="7"),
        "manual-reserved-oversell",
    )

    assert execute_order_intent(accepted.json()["data"]["intent_id"]) is None
    manual = OrderIntent.objects.get(origin=OrderIntent.Origin.MANUAL)
    check = manual.risk_checks.get(check_name="available_position")
    assert check.details["reserved_sell_quantity"] == "4.00000000"
    assert check.details["available_position_quantity"] == "6.00000000"
    assert pending.operation_status == "PENDING"


def test_disconnected_gateway_leaves_truthful_retryable_pending_intent(
    client, settings, monkeypatch
):
    _, portfolio, _, instrument = _manual_case(settings)
    _disable_task_enqueue(monkeypatch)
    monkeypatch.setattr(
        GatewayClient,
        "health",
        lambda self: {"connected": False, "reconciled": False, "mode": "paper"},
    )
    accepted = _post(client, _payload(portfolio, instrument), "manual-held")

    assert execute_order_intent(accepted.json()["data"]["intent_id"]) is None
    intent = OrderIntent.objects.get()
    intent.refresh_from_db()
    assert intent.operation_status == "PENDING"
    assert intent.retryable is True
    assert intent.operation_error == "Gateway is disconnected"
    assert not Order.objects.exists()
    attempt = OperationAttempt.objects.get(operation_id=str(intent.pk))
    assert attempt.status == "FAILED" and attempt.retryable is True


def test_gateway_transport_failure_leaves_truthful_retryable_pending_intent(
    client, settings, monkeypatch
):
    _, portfolio, _, instrument = _manual_case(settings)
    _disable_task_enqueue(monkeypatch)
    monkeypatch.setattr(
        GatewayClient, "health", lambda self: (_ for _ in ()).throw(GatewayError("gateway restarting"))
    )
    accepted = _post(client, _payload(portfolio, instrument), "manual-gateway-error")

    assert execute_order_intent(accepted.json()["data"]["intent_id"]) is None
    intent = OrderIntent.objects.get()
    intent.refresh_from_db()
    assert (intent.operation_status, intent.retryable, intent.operation_error) == (
        "PENDING",
        True,
        "gateway restarting",
    )


def test_unavailable_bound_account_terminally_fails_intent(
    client, settings, monkeypatch
):
    _, portfolio, _, instrument = _manual_case(settings)
    _disable_task_enqueue(monkeypatch)
    accepted = _post(client, _payload(portfolio, instrument), "manual-account-removed")
    BrokerSessionAccount.objects.filter(
        session=portfolio.gateway_session, broker_account=portfolio.account
    ).delete()

    assert execute_order_intent(accepted.json()["data"]["intent_id"]) is None

    intent = OrderIntent.objects.get(pk=accepted.json()["data"]["intent_id"])
    assert intent.operation_status == "FAILED"
    assert intent.retryable is False
    assert intent.eligible is False
    assert "not available" in intent.operation_error


def test_held_manual_intent_can_be_retried_by_existing_worker(
    client, settings, monkeypatch
):
    _, portfolio, _, instrument = _manual_case(settings)
    _disable_task_enqueue(monkeypatch)
    states = iter(
        [
            {"connected": False, "reconciled": False, "mode": "paper"},
            {"connected": True, "reconciled": True, "mode": "paper"},
        ]
    )
    monkeypatch.setattr(GatewayClient, "health", lambda self: next(states))
    accepted = _post(client, _payload(portfolio, instrument), "manual-retry")
    intent_id = accepted.json()["data"]["intent_id"]

    assert execute_order_intent(intent_id) is None
    command = execute_order_intent(intent_id)

    assert command is not None
    assert command.command_type == BrokerCommand.CommandType.PLACE
    intent = OrderIntent.objects.get(pk=intent_id)
    assert intent.operation_status == "QUEUED"
    assert intent.attempt_count == 2
    assert list(
        intent.risk_checks.values_list("decision", flat=True).order_by("pk")
    ) == ["HELD", "APPROVED"]


@pytest.mark.django_db(transaction=True)
def test_concurrent_identical_manual_requests_create_exactly_one_intent(
    settings, monkeypatch
):
    if connection.vendor != "postgresql":
        pytest.skip("Row-lock concurrency is verified against PostgreSQL")
    _, portfolio, _, instrument = _manual_case(settings)
    _disable_task_enqueue(monkeypatch)
    payload = _payload(portfolio, instrument)
    barrier = Barrier(2)

    def submit():
        close_old_connections()
        barrier.wait()
        try:
            return _post(Client(), payload, "manual-concurrent").status_code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: submit(), range(2)))

    assert statuses == [202, 202]
    assert OrderIntent.objects.count() == 1
    assert not Order.objects.exists()
    assert not BrokerCommand.objects.exists()


def test_manual_modification_and_cancellation_use_existing_broker_commands(
    client, settings, monkeypatch
):
    _, portfolio, _, instrument = _manual_case(settings)
    _disable_task_enqueue(monkeypatch)
    _healthy_gateway(monkeypatch)
    accepted = _post(client, _payload(portfolio, instrument), "manual-command-paths")
    command = execute_order_intent(accepted.json()["data"]["intent_id"])
    order = command.order

    modified = client.patch(
        f"/api/v1/orders/{order.internal_id}/",
        json.dumps({"quantity": "4"}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="manual-modify",
    )
    cancelled = client.post(
        f"/api/v1/orders/{order.internal_id}/cancel/",
        json.dumps({"reason": "operator request"}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="manual-cancel",
    )

    assert modified.status_code == cancelled.status_code == 202
    assert list(
        BrokerCommand.objects.filter(order=order)
        .order_by("pk")
        .values_list("command_type", flat=True)
    ) == ["PLACE", "MODIFY", "CANCEL"]
    assert Order.objects.get(pk=order.pk).status == "CANCEL_PENDING"


def test_duplicate_after_worker_execution_returns_existing_order_state(
    client, settings, monkeypatch
):
    _, portfolio, _, instrument = _manual_case(settings)
    _disable_task_enqueue(monkeypatch)
    _healthy_gateway(monkeypatch)
    payload = _payload(portfolio, instrument)
    accepted = _post(client, payload, "manual-existing-order")
    command = execute_order_intent(accepted.json()["data"]["intent_id"])

    duplicate = _post(client, payload, "manual-existing-order")

    assert duplicate.status_code == 200
    assert duplicate.json()["data"]["intent_id"] == command.order.intent_id
    assert duplicate.json()["data"]["internal_id"] == command.order.internal_id
    assert duplicate.json()["data"]["origin"] == "MANUAL"
    assert OrderIntent.objects.count() == Order.objects.count() == BrokerCommand.objects.count() == 1


def test_buy_risk_uses_trusted_price_for_cash_reservation(
    client, settings, monkeypatch
):
    account, portfolio, _, instrument = _manual_case(settings, market_price="100")
    account.available_cash = Decimal("500")
    account.save(update_fields=["available_cash"])
    _disable_task_enqueue(monkeypatch)
    _healthy_gateway(monkeypatch)
    accepted = _post(
        client,
        _payload(portfolio, instrument, quantity="4", reference_price="1"),
        "manual-cash-price",
    )

    command = execute_order_intent(accepted.json()["data"]["intent_id"])

    assert command is not None
    reservation = CapitalReservation.objects.get(order_intent_id=accepted.json()["data"]["intent_id"])
    assert reservation.amount > Decimal("400")


def test_manual_order_requires_idempotency_key(client):
    result = client.post("/api/v1/orders/", data="{}", content_type="application/json")
    assert result.status_code == 400
    assert result.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_manual_order_rejects_non_object_json_and_wrong_method(client):
    invalid = client.post(
        "/api/v1/orders/",
        "[]",
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="json-array",
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "INVALID_ORDER"
    assert client.put("/api/v1/orders/", data="{}", content_type="application/json").status_code == 405
