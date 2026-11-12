import json

import pytest

from apps.accounts.models import BrokerAccount
from apps.broker_gateway.models import BrokerGatewaySession
from apps.instruments.models import BrokerContract, Instrument
from apps.market_streams.models import InstrumentMarketState, MarketDataSubscription
from apps.market_streams.services import persist_bar
from apps.market_streams.tasks import check_warmup_timeouts
from apps.portfolios.models import TradingPortfolio
from apps.strategies.evaluation_jobs import process_strategy_evaluation_jobs
from apps.strategies.framework import create_instance, enable_instance
from apps.strategies.models import StrategyAction, StrategyInstance
from apps.strategies.workflow import STAGES, execution_workflow
from tests.managed_gateway import bind_gateway_mode
from tests.strategy_activation import FakeActivationGateway
from tests.test_strategy_activation_lifecycle import confirm_subscription


pytestmark = pytest.mark.django_db


@pytest.fixture
def phase2_domain(settings):
    settings.KAFKA_ENABLED = True
    account = BrokerAccount.objects.create(
        account_id="DU-PHASE2",
        net_liquidation=100000,
        available_cash=100000,
        buying_power=200000,
    )
    portfolio = TradingPortfolio.objects.create(
        name="Phase 2 paper",
        account=account,
        minimum_notional=1,
    )
    session = bind_gateway_mode(portfolio)
    instrument = Instrument.objects.create(
        symbol="PHASE2",
        exchange="SMART",
        currency="USD",
    )
    BrokerContract.objects.create(
        instrument=instrument,
        conid=920002,
        primary_exchange="NASDAQ",
        local_symbol="PHASE2",
    )
    return portfolio, session, instrument


def create_payload(portfolio, instrument, name, *, activate):
    return {
        "name": name,
        "definition_key": "FIXED_WEIGHT_REBALANCE",
        "portfolio_id": portfolio.pk,
        "instrument_id": instrument.pk,
        "timeframe": "1m",
        "parameters": {"direction": "LONG"},
        "target_configuration": {"target_weight": "0.05"},
        "execution_mode": "PAPER",
        "qualify": False,
        "activate": activate,
    }


def bar_envelope(instrument, bar_id, minute, mode):
    return {
        "event_id": f"{bar_id}:1",
        "produced_at": f"2026-07-30T00:{minute + 1:02d}:00+00:00",
        "payload": {
            "bar_id": bar_id,
            "instrument_id": instrument.pk,
            "interval": "1m",
            "window_start": f"2026-07-30T00:{minute:02d}:00+00:00",
            "window_end": f"2026-07-30T00:{minute + 1:02d}:00+00:00",
            "open": "100",
            "high": "102",
            "low": "99",
            "close": "101",
            "volume": "1000",
            "source_event_count": 2,
            "version": 1,
            "is_final": True,
            "provider": "IBKR",
            "source": "ibkr_live" if mode == "LIVE" else "ibkr_historical",
            "processing_mode": mode,
        },
    }


def test_create_as_disabled_has_no_activation_side_effects(
    phase2_domain,
    client,
):
    portfolio, _, instrument = phase2_domain
    response = client.post(
        "/api/v1/strategy-instances/",
        data=json.dumps(create_payload(portfolio, instrument, "Phase2 disabled", activate=False)),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="phase2-create-disabled",
    )
    assert response.status_code == 201
    body = response.json()["data"]
    assert body["state"] == "DISABLED"
    assert body["enabled"] is False
    assert body["activation_operation"] is None
    assert body["execution_workflow"]["terminal"] is True
    assert not StrategyAction.objects.exists()


def test_create_and_activate_returns_processing_until_first_live_evaluation(
    phase2_domain,
    client,
    django_capture_on_commit_callbacks,
):
    portfolio, _, instrument = phase2_domain
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        response = client.post(
            "/api/v1/strategy-instances/",
            data=json.dumps(create_payload(portfolio, instrument, "Phase2 active", activate=True)),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="phase2-create-active",
        )
    assert response.status_code == 202
    body = response.json()["data"]
    assert body["state"] == "ACTIVATING"
    assert body["activation_operation"]["status"] == "PROCESSING"
    assert body["execution_workflow"]["active"] is True
    assert body["execution_workflow"]["terminal"] is False
    assert len(callbacks) == 1


def test_activation_warmup_and_first_evaluation_progress_exact_stages(
    phase2_domain,
):
    portfolio, _, instrument = phase2_domain
    instance, _ = create_instance(
        name="Phase2 progression",
        definition_key="FIXED_WEIGHT_REBALANCE",
        portfolio=portfolio,
        instrument_id=instrument.pk,
        timeframe="1m",
        parameters={"direction": "LONG"},
        target_configuration={"target_weight": "0.05"},
        execution_mode="PAPER",
        qualify=False,
    )
    action = StrategyAction.objects.create(
        strategy_instance=instance,
        action="enable",
        idempotency_key="phase2-progression-enable",
        request_hash="phase2-progression-hash",
    )
    enable_instance(instance, FakeActivationGateway(), action=action)
    action.refresh_from_db()
    assert action.status == "PROCESSING"
    subscription = MarketDataSubscription.objects.get()
    pending = execution_workflow(StrategyInstance.objects.get(pk=instance.pk))
    assert [row["label"] for row in pending["stages"]] == [label for _, label in STAGES]
    assert pending["stages"][3]["status"] == "COMPLETED"
    assert pending["stages"][4]["status"] == "PENDING"

    confirm_subscription(portfolio, subscription)
    persist_bar(bar_envelope(instrument, "phase2-warmup", 0, "WARMUP"))
    instance.refresh_from_db()
    waiting = execution_workflow(instance)
    assert waiting["stages"][4]["status"] == "COMPLETED"
    assert waiting["stages"][6]["status"] == "COMPLETED"
    assert waiting["stages"][7]["status"] == "ACTIVE"

    # Waiting through repeated watchdog runs remains normal and non-terminal.
    assert check_warmup_timeouts() == 0
    instance.refresh_from_db()
    assert instance.state == "READY_WAITING_FOR_LIVE_BAR"
    assert not instance.block_reason

    persist_bar(bar_envelope(instrument, "phase2-live", 1, "LIVE"))
    assert process_strategy_evaluation_jobs()["completed"] == 1
    instance.refresh_from_db()
    action.refresh_from_db()
    completed = execution_workflow(instance)
    assert action.status == "COMPLETED"
    assert completed["stages"][8]["status"] == "COMPLETED"
    assert completed["stages"][9]["status"] == "COMPLETED"
    assert {row["trace_id"] for row in completed["stages"]} == {
        str(instance.workflow_trace_id)
    }


def test_activation_failure_exposes_blocker_and_retries_only_when_marked_retryable(
    phase2_domain,
    client,
    django_capture_on_commit_callbacks,
):
    portfolio, session, instrument = phase2_domain
    instance, _ = create_instance(
        name="Phase2 retry",
        definition_key="FIXED_WEIGHT_REBALANCE",
        portfolio=portfolio,
        instrument_id=instrument.pk,
        timeframe="1m",
        parameters={"direction": "LONG"},
        target_configuration={"target_weight": "0.05"},
        execution_mode="PAPER",
        qualify=False,
    )
    session.status = BrokerGatewaySession.Status.DISCONNECTED
    session.save(update_fields=["status", "updated_at"])
    path = f"/api/v1/strategy-instances/{instance.pk}/enable/"
    failed = client.post(
        path,
        data=b"{}",
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="phase2-retry-enable",
    )
    assert failed.status_code == 503
    action = StrategyAction.objects.get()
    instance.refresh_from_db()
    assert action.status == "FAILED"
    assert action.retryable is True
    assert "not connected and command-ready" in action.last_error
    failed_stage = next(
        row for row in execution_workflow(instance)["stages"]
        if row["status"] == "FAILED"
    )
    assert failed_stage["blocker"] == action.last_error
    assert failed_stage["retryable"] is True

    session.status = BrokerGatewaySession.Status.CONNECTED
    session.save(update_fields=["status", "updated_at"])
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        retried = client.post(
            path,
            data=b"{}",
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="phase2-retry-enable",
            HTTP_IDEMPOTENCY_RETRY="true",
        )
    action.refresh_from_db()
    assert retried.status_code == 202
    assert action.status == "PROCESSING"
    assert action.attempt_count == 2
    assert len(callbacks) == 1


def test_price_provenance_and_portfolio_gateway_resolution_are_authoritative(
    phase2_domain,
    client,
    monkeypatch,
):
    portfolio, session, instrument = phase2_domain
    other_account = BrokerAccount.objects.create(account_id="DU-OTHER")
    other_portfolio = TradingPortfolio.objects.create(
        name="Other route",
        account=other_account,
    )
    other_session = bind_gateway_mode(other_portfolio)
    captured = {}

    # Mirrors apps.instruments.services.search_broker_instruments. The view
    # passes asset_classes/country/currency, which this stub used to reject;
    # the resulting TypeError was swallowed by the view's broad `except
    # Exception` and surfaced as an opaque 503 rather than a signature error.
    def fake_search(query, *, gateway_session, asset_classes=None, country=None, currency=None):
        captured["query"] = query
        captured["session"] = gateway_session
        captured["asset_classes"] = asset_classes
        return []

    monkeypatch.setattr(
        "apps.strategies.views.search_broker_instruments",
        fake_search,
    )
    response = client.get(
        "/api/v1/instruments/search/",
        {
            "query": "PHASE2",
            "portfolio_id": portfolio.pk,
            "session_id": other_session.pk,
        },
    )
    assert response.status_code == 200
    assert captured["session"] == session

    instance, _ = create_instance(
        name="Phase2 price",
        definition_key="FIXED_WEIGHT_REBALANCE",
        portfolio=portfolio,
        instrument_id=instrument.pk,
        timeframe="1m",
        parameters={"direction": "LONG"},
        target_configuration={"target_weight": "0.05"},
        execution_mode="PAPER",
        qualify=False,
    )
    InstrumentMarketState.objects.create(
        instrument=instrument,
        status="FRESH",
        reference_price="101.25",
        latest_event_at=instance.created_at,
        reference_price_provider="IBKR",
        reference_price_source="ibkr_live",
        stale_after_seconds=300,
    )
    detail = client.get(f"/api/v1/strategy-instances/{instance.pk}/").json()["data"]
    assert detail["gateway_session_id"] == str(session.pk)
    assert detail["current_price"]["provider"] == "IBKR"
    assert detail["current_price"]["source"] == "ibkr_live"
    assert detail["current_price"]["data_kind"] == "LIVE"
    assert detail["current_price"]["fresh_for_execution"] is True
