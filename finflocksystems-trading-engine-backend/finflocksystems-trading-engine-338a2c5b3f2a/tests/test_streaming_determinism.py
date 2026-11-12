from datetime import datetime, timedelta

import pytest

from apps.accounts.models import BrokerAccount
from apps.instruments.models import BrokerContract, Instrument
from apps.market_streams.models import IndicatorValue, StrategyEvaluationJob
from apps.market_streams.services import persist_bar, persist_indicator
from apps.portfolios.models import TradingPortfolio
from apps.strategies.evaluation_jobs import process_strategy_evaluation_jobs
from apps.strategies.framework import create_instance
from apps.strategies.input_identity import requirement_identity_hash
from tests.managed_gateway import bind_gateway_mode
from tests.strategy_activation import activate_strategy


pytestmark = pytest.mark.django_db


@pytest.fixture
def portfolio():
    account = BrokerAccount.objects.create(
        account_id="DU-STREAM-DETERMINISM",
        net_liquidation=100000,
        available_cash=100000,
        buying_power=200000,
    )
    portfolio = TradingPortfolio.objects.create(
        name="Streaming determinism",
        account=account,
        minimum_notional=1,
    )
    bind_gateway_mode(portfolio)
    return portfolio


@pytest.fixture
def instrument():
    item = Instrument.objects.create(
        symbol="ORDERED",
        exchange="SMART",
        currency="USD",
    )
    BrokerContract.objects.create(
        instrument=item,
        conid=770001,
        primary_exchange="NASDAQ",
        local_symbol="ORDERED",
    )
    return item


def strategy(portfolio, instrument, name="ORDERED_FIXED"):
    instance, _ = create_instance(
        name=name,
        definition_key="FIXED_WEIGHT_REBALANCE",
        portfolio=portfolio,
        instrument_id=instrument.pk,
        timeframe="5m",
        parameters={"direction": "LONG"},
        target_configuration={"target_weight": "0.05"},
        execution_mode="PAPER",
        qualify=False,
    )
    activate_strategy(instance)
    return instance


def bar_event(instrument, *, bar_id, window_end, mode="LIVE", version=1):
    end = datetime.fromisoformat(window_end.replace("Z", "+00:00"))
    start = end - timedelta(minutes=5)
    return {
        "event_id": f"{bar_id}:{version}:{mode}",
        "produced_at": end.isoformat(),
        "payload": {
            "bar_id": bar_id,
            "instrument_id": instrument.pk,
            "interval": "5m",
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "open": "100",
            "high": "102",
            "low": "99",
            "close": "101",
            "volume": "100",
            "source_event_count": 2,
            "version": version,
            "is_final": True,
            "processing_mode": mode,
        },
    }


def persist_required_indicators(instance, instrument, *, bar_id, event_time, version=1):
    """Supply the full persisted input identity required by a PAPER strategy."""
    requirements = (
        instance.input_bindings.filter(
            active=True,
            strategy_version__version=instance.version,
            requirement__input_type="INDICATOR",
        )
        .select_related("requirement")
        .order_by("requirement__identity_hash")
    )
    for binding in requirements:
        requirement = binding.requirement
        persist_indicator({
            "payload": {
                "source_key": (
                    f"{bar_id}:{version}:{requirement.identity_hash}:"
                    f"{requirement.implementation_version}"
                ),
                "instrument_id": instrument.pk,
                "indicator": requirement.name,
                "indicator_name": requirement.name,
                "indicator_role": requirement.role,
                "implementation_version": requirement.implementation_version,
                "requirement_identity_hash": requirement.identity_hash,
                "value": "100",
                "previous_value": "100",
                "parameters": requirement.parameters,
                "timeframe": instance.timeframe,
                "source_bar_id": bar_id,
                "source_bar_version": version,
                "event_time": event_time,
                "is_final": True,
                "processing_mode": "LIVE",
            },
        })


def test_delayed_older_live_bar_is_quarantined(portfolio, instrument):
    instance = strategy(portfolio, instrument)
    persist_bar(bar_event(
        instrument,
        bar_id="newer-live-bar",
        window_end="2026-07-23T00:10:00Z",
    ))
    persist_required_indicators(
        instance,
        instrument,
        bar_id="newer-live-bar",
        event_time="2026-07-23T00:10:00Z",
    )
    persist_bar(bar_event(
        instrument,
        bar_id="older-live-bar",
        window_end="2026-07-23T00:05:00Z",
    ))

    jobs = {job.market_bar_id: job for job in StrategyEvaluationJob.objects.all()}
    assert jobs["newer-live-bar"].status == "PENDING"
    assert jobs["older-live-bar"].status == "FAILED"
    assert jobs["older-live-bar"].error_code == "STALE_INPUT"
    instance.refresh_from_db()
    assert instance.last_market_bar_id == "newer-live-bar"


def test_historical_warmup_never_creates_an_executable_target(portfolio, instrument):
    instance = strategy(portfolio, instrument, "WARMUP_ONLY")
    persist_bar(bar_event(
        instrument,
        bar_id="historical-warmup",
        window_end="2025-01-01T00:05:00Z",
        mode="WARMUP",
    ))

    instance.refresh_from_db()
    assert instance.warmup_progress == 1
    assert not StrategyEvaluationJob.objects.exists()
    assert not instance.runs.exists()
    assert not instance.targets.exists()
    assert instance.last_market_event_at is None


def test_replay_does_not_modify_live_strategy_state(portfolio, instrument):
    instance = strategy(portfolio, instrument, "REPLAY_ISOLATED")
    persist_bar(bar_event(
        instrument,
        bar_id="accepted-live",
        window_end="2026-07-23T00:10:00Z",
    ))
    persist_required_indicators(
        instance,
        instrument,
        bar_id="accepted-live",
        event_time="2026-07-23T00:10:00Z",
    )
    assert process_strategy_evaluation_jobs()["completed"] == 1
    instance.refresh_from_db()
    live_state = instance.state
    live_cursor = (
        instance.last_market_event_at,
        instance.last_market_bar_id,
        instance.last_market_bar_version,
    )
    live_runs = instance.runs.count()
    live_targets = instance.targets.count()

    persist_bar(bar_event(
        instrument,
        bar_id="historical-replay",
        window_end="2025-01-01T00:05:00Z",
        mode="REPLAY",
    ))
    instance.refresh_from_db()
    assert instance.state == live_state
    assert (
        instance.last_market_event_at,
        instance.last_market_bar_id,
        instance.last_market_bar_version,
    ) == live_cursor
    assert instance.runs.count() == live_runs
    assert instance.targets.count() == live_targets


def test_later_live_delivery_promotes_matching_replay_fact(portfolio, instrument):
    instance = strategy(portfolio, instrument, "REPLAY_THEN_LIVE")
    replay = bar_event(
        instrument,
        bar_id="replayed-before-live",
        window_end="2026-07-23T00:15:00Z",
        mode="REPLAY",
    )
    persist_bar(replay)
    assert not StrategyEvaluationJob.objects.exists()

    live = {
        **replay,
        "event_id": "replayed-before-live:1:LIVE",
        "payload": {**replay["payload"], "processing_mode": "LIVE"},
    }
    persist_bar(live)
    persist_required_indicators(
        instance,
        instrument,
        bar_id="replayed-before-live",
        event_time="2026-07-23T00:15:00Z",
    )

    instance.refresh_from_db()
    assert instance.last_market_bar_id == "replayed-before-live"
    assert StrategyEvaluationJob.objects.filter(
        market_bar_id="replayed-before-live",
        status="PENDING",
    ).count() == 1


def test_identical_parameters_for_different_indicators_do_not_collide(instrument):
    parameters = {"window": 20}
    identities = {
        name: requirement_identity_hash(
            input_type="INDICATOR",
            name=name,
            role="",
            parameters=parameters,
            instrument_id=instrument.pk,
            timeframe="5m",
            implementation_version=1,
        )
        for name in ("sma", "momentum")
    }
    for name, identity in identities.items():
        persist_indicator({
            "payload": {
                "source_key": f"shared-bar:1:{identity}",
                "instrument_id": instrument.pk,
                "indicator": name,
                "indicator_name": name,
                "indicator_role": "",
                "implementation_version": 1,
                "requirement_identity_hash": identity,
                "value": "10",
                "previous_value": "9",
                "parameters": parameters,
                "timeframe": "5m",
                "source_bar_id": "shared-bar",
                "source_bar_version": 1,
                "event_time": "2026-07-23T00:05:00Z",
                "is_final": True,
                "processing_mode": "LIVE",
            },
        })

    assert identities["sma"] != identities["momentum"]
    assert IndicatorValue.objects.count() == 2
    assert set(IndicatorValue.objects.values_list(
        "requirement_identity_hash",
        flat=True,
    )) == set(identities.values())
