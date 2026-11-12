import json

import pytest
import responses

from apps.accounts.models import BrokerAccount
from apps.broker_gateway.sync import process_snapshot
from apps.instruments.models import Instrument
from apps.oms.models import OrderIntent
from apps.oms.services import create_order, transition
from apps.portfolios.models import TradingPortfolio
from tests.managed_gateway import bind_managed_gateway


pytestmark = pytest.mark.django_db


@pytest.fixture
def submitted_order(settings):
    account = BrokerAccount.objects.create(account_id="DU-DIAGNOSTICS", is_reconciled=True)
    portfolio = TradingPortfolio.objects.create(name="Diagnostics paper", account=account)
    bind_managed_gateway(portfolio, settings)
    instrument = Instrument.objects.create(symbol="AAPL", exchange="SMART", primary_exchange="NASDAQ")
    intent = OrderIntent.objects.create(portfolio=portfolio, instrument=instrument, side="BUY", quantity=1,
        idempotency_key="diagnostic-intent", origin=OrderIntent.Origin.MANUAL)
    order = create_order(intent)
    order = transition(order, "QUEUED", "oms", "diagnostic:queued")
    return transition(order, "SUBMITTED", "gateway", "diagnostic:submitted")


def test_exact_ibkr_rejection_is_append_only_and_changes_status(submitted_order):
    payload = {"source_event_id":"reject-201","internal_id":submitted_order.internal_id,"account":"DU-DIAGNOSTICS",
        "broker_order_id":"881","permanent_id":"9901","broker_status":"Inactive","error_code":"201",
        "error_message":"Order rejected - insufficient available equity","why_held":"locate pending",
        "warning_text":"Margin check failed","advanced_reject":{"errorCode":201,"errorMsg":"insufficient equity"},
        "trade_log":[{"time":"2026-07-13T01:00:00+00:00","status":"Inactive","message":"Margin check failed","error_code":"201"}],
        "occurred_at":"2026-07-13T01:00:00+00:00","operator_requested":False}
    process_snapshot({"event_type":"broker.order","payload":payload})
    process_snapshot({"event_type":"broker.order","payload":payload})
    submitted_order.refresh_from_db()
    history=submitted_order.status_history.get(event_key="broker-order:reject-201")
    assert submitted_order.status=="REJECTED" and history.reason_code=="201"
    assert history.reason=="Order rejected - insufficient available equity"
    assert history.broker_status=="Inactive" and history.details["why_held"]=="locate pending"
    assert submitted_order.status_history.filter(event_key="broker-order:reject-201").count()==1


def test_surveillance_warning_requires_confirmation_and_preserves_advanced_reject(client, submitted_order):
    message="Order rejected - reason: Security is under Surveillance Measure - High low variation. Would you like to continue?"
    process_snapshot({"event_type":"broker.order","payload":{"source_event_id":"warning-201",
        "internal_id":submitted_order.internal_id,"broker_order_id":"881","broker_status":"Inactive",
        "error_code":"201","error_message":message,
        "advanced_reject":{"errorCode":201,"errorData":{"rejectEventCode":"IBKR-PROVIDED"}},
        "occurred_at":"2026-07-13T01:30:00+00:00"}})
    submitted_order.refresh_from_db();submitted_order.intent.refresh_from_db()
    assert submitted_order.status=="BROKER_BLOCKED"
    assert submitted_order.intent.operation_status=="CONFIRMATION_REQUIRED"
    status=client.get(f"/api/v1/orders/intents/{submitted_order.intent_id}/status/").json()["data"]
    assert status["confirmation"]=={"required":True,"warning_code":"201",
        "warning_message":message,"broker_order_id":"881","can_confirm":True,
        "override_options":[{"code":"IBKR-PROVIDED","text":""}]}

def test_surveillance_fixstr_override_is_extracted_and_can_be_confirmed(client, submitted_order):
    message="Security is under Surveillance Measure. Would you like to continue?"
    advanced={"rejects":[{"buttons":[{"options":[
        {"fixstr":"8229=SURVEILLANCE","text":"Yes, transmit the order."},
        {"fixstr":"8229=SEBI-GSM","text":"Accept additional surveillance warning"},
    ]}]}]}
    process_snapshot({"event_type":"broker.order","payload":{"source_event_id":"warning-fixstr",
        "internal_id":submitted_order.internal_id,"broker_order_id":"886","broker_status":"Inactive",
        "error_code":"201","error_message":message,"advanced_reject":advanced}})
    status=client.get(f"/api/v1/orders/intents/{submitted_order.intent_id}/status/").json()["data"]
    assert status["confirmation"]["can_confirm"] is True
    assert status["confirmation"]["override_options"]==[
        {"code":"SURVEILLANCE","text":"Yes, transmit the order."},
        {"code":"SEBI-GSM","text":"Accept additional surveillance warning"},
    ]
    result=client.post(f"/api/v1/orders/intents/{submitted_order.intent_id}/confirmation/",
        json.dumps({"confirmed":True}),content_type="application/json",HTTP_IDEMPOTENCY_KEY="fixstr-confirm")
    assert result.status_code==202
    command=submitted_order.broker_commands.get(command_type="PLACE")
    assert command.request_payload["advanced_error_override"]=="SURVEILLANCE,SEBI-GSM"


def test_advanced_confirmation_is_idempotent_and_decline_does_not_resubmit(client, submitted_order):
    message="Security is under Surveillance Measure. Would you like to continue?"
    process_snapshot({"event_type":"broker.order","payload":{"source_event_id":"warning-201",
        "internal_id":submitted_order.internal_id,"broker_order_id":"882","broker_status":"Inactive",
        "error_code":"201","error_message":message,
        "advanced_reject":{"errorData":{"rejectEventCode":"IBKR-PROVIDED"}}}})
    url=f"/api/v1/orders/intents/{submitted_order.intent_id}/confirmation/"
    first=client.post(url,json.dumps({"confirmed":True}),content_type="application/json",HTTP_IDEMPOTENCY_KEY="confirm-a")
    second=client.post(url,json.dumps({"confirmed":True}),content_type="application/json",HTTP_IDEMPOTENCY_KEY="confirm-b")
    assert first.status_code==second.status_code==202
    commands=submitted_order.broker_commands.filter(command_type="PLACE")
    assert commands.count()==1
    assert commands.get().request_payload["advanced_error_override"]=="IBKR-PROVIDED"
    assert commands.get().request_payload["original_broker_order_id"]=="882"

    other_intent=OrderIntent.objects.create(portfolio=submitted_order.intent.portfolio,
        instrument=submitted_order.intent.instrument,side="BUY",quantity=1,
        idempotency_key="diagnostic-decline",origin=OrderIntent.Origin.MANUAL)
    other=create_order(other_intent);other=transition(other,"QUEUED","oms","decline:queued")
    other=transition(other,"SUBMITTED","gateway","decline:submitted")
    process_snapshot({"event_type":"broker.order","payload":{"source_event_id":"warning-decline",
        "internal_id":other.internal_id,"broker_order_id":"883","broker_status":"Inactive",
        "error_code":"201","error_message":message}})
    declined=client.post(f"/api/v1/orders/intents/{other_intent.pk}/confirmation/",
        json.dumps({"confirmed":False}),content_type="application/json",HTTP_IDEMPOTENCY_KEY="decline")
    other.refresh_from_db();other_intent.refresh_from_db()
    assert declined.status_code==200 and other.status=="REJECTED"
    assert other_intent.operation_status=="USER_CANCELLED"
    assert not other.broker_commands.exists()

def test_surveillance_confirmation_without_ibkr_override_cannot_resubmit(client, submitted_order):
    message="Security is under Surveillance Measure. Would you like to continue?"
    process_snapshot({"event_type":"broker.order","payload":{"source_event_id":"warning-no-override",
        "internal_id":submitted_order.internal_id,"broker_order_id":"884","broker_status":"Inactive",
        "error_code":"201","error_message":message,"advanced_reject":None}})
    status=client.get(f"/api/v1/orders/intents/{submitted_order.intent_id}/status/").json()["data"]
    assert status["confirmation"]["can_confirm"] is False
    result=client.post(f"/api/v1/orders/intents/{submitted_order.intent_id}/confirmation/",
        json.dumps({"confirmed":True}),content_type="application/json",HTTP_IDEMPOTENCY_KEY="unsafe")
    assert result.status_code==409
    assert not submitted_order.broker_commands.filter(command_type="PLACE").exists()

def test_same_surveillance_rejection_is_deduplicated_across_callback_and_snapshot(submitted_order):
    message="Security is under Surveillance Measure. Would you like to continue?"
    common={"internal_id":submitted_order.internal_id,"broker_order_id":"885",
        "broker_status":"Inactive","error_code":"201"}
    process_snapshot({"event_type":"broker.order","payload":{**common,"source_event_id":"callback-error",
        "error_message":f"Error 201, reqId 885: {message}"}})
    process_snapshot({"event_type":"broker.order","payload":{**common,"source_event_id":"callback-status",
        "error_message":message}})
    process_snapshot({"event_type":"snapshot.completed_orders","payload":{"value":[{**common,
        "error_message":f"Error 201, reqId 885: {message}","account":"DU-DIAGNOSTICS",
        "symbol":"AAPL","asset_class":"STK","exchange":"SMART","currency":"USD"}],
        "snapshot_key":"duplicate-warning"}})
    histories=submitted_order.status_history.filter(reason_code="201",
        details__original_broker_order_id="885")
    assert histories.count()==1


@responses.activate
def test_operator_request_and_ibkr_cancel_confirmation_remain_distinct(client, submitted_order):
    base_url = submitted_order.intent.portfolio.gateway_session.internal_base_url
    responses.post(f"{base_url}/orders/{submitted_order.internal_id}/cancel/",status=202,
        json={"ok":True,"data":{"command_id":7,"status":"PENDING"},"error":None,"meta":{}})
    response=client.post(f"/api/v1/orders/{submitted_order.internal_id}/cancel/",json.dumps({"reason":"Operator risk review"}),
        content_type="application/json",HTTP_IDEMPOTENCY_KEY="cancel-diagnostic")
    assert response.status_code==202
    process_snapshot({"event_type":"broker.order","payload":{"source_event_id":"cancel-881",
        "internal_id":submitted_order.internal_id,"broker_order_id":"881","broker_status":"Cancelled",
        "error_code":"202","error_message":"Order cancelled - reason: exchange closed","occurred_at":"2026-07-13T02:00:00+00:00",
        "operator_requested":True}})
    submitted_order.refresh_from_db()
    operator=submitted_order.status_history.get(reason_code="OPERATOR_CANCEL_REQUEST")
    broker=submitted_order.status_history.get(reason_code="202")
    assert submitted_order.status=="CANCELLED"
    assert operator.source=="operator" and operator.reason=="Operator risk review" and operator.operator_requested
    assert broker.source=="ibkr" and broker.reason=="Order cancelled - reason: exchange closed" and broker.operator_requested


def test_order_detail_returns_chronological_exact_diagnostics(client, submitted_order):
    process_snapshot({"event_type":"broker.order","payload":{"source_event_id":"detail-reject",
        "internal_id":submitted_order.internal_id,"broker_status":"Inactive","error_code":"321",
        "error_message":"Error validating request","occurred_at":"2026-07-13T03:00:00+00:00","operator_requested":False}})
    response=client.get(f"/api/v1/orders/{submitted_order.internal_id}/detail/")
    assert response.status_code==200
    data=response.json()["data"]
    assert data["order"]["internal_id"]==submitted_order.internal_id
    assert [row["occurred_at"] for row in data["status_history"]]==sorted(row["occurred_at"] for row in data["status_history"])
    diagnostic=next(row for row in data["broker_diagnostics"] if row["reason_code"]=="321")
    assert diagnostic["reason"]=="Error validating request"
    assert set(data)=={"order","status_history","broker_diagnostics","risk_decisions","fills","strategy_attribution"}


def test_new_session_matches_an_order_placed_through_a_previous_session(submitted_order, settings):
    # A replacement session on the same IBKR account still receives today's
    # orders, orderRef included. They are ours: importing them again as
    # external orders collided with oms_order_internal_id_key.
    from apps.broker_gateway.sync import sync_orders
    from apps.oms.models import Order

    replacement = TradingPortfolio.objects.create(name="Replacement session", account=submitted_order.intent.portfolio.account)
    new_session = bind_managed_gateway(replacement, settings)
    assert new_session != submitted_order.intent.portfolio.gateway_session

    sync_orders([{"internal_id": submitted_order.internal_id, "broker_order_id": "885", "permanent_id": "4242",
        "status": "Filled", "account": "DU-DIAGNOSTICS", "symbol": "AAPL", "asset_class": "STK",
        "exchange": "SMART", "currency": "USD", "side": "BUY", "quantity": "1"}], "completed", new_session)

    assert Order.objects.filter(internal_id=submitted_order.internal_id).count() == 1
    assert Order.objects.count() == 1
    submitted_order.refresh_from_db()
    assert submitted_order.broker_permanent_id == "4242"
