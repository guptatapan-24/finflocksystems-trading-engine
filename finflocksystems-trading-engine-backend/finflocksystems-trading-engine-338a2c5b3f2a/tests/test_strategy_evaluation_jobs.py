import uuid
from datetime import timedelta

import pytest
from django.db import IntegrityError, OperationalError
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import BrokerAccount
from apps.event_bus.models import ConsumedEvent
from apps.instruments.models import BrokerContract, Instrument
from apps.market_streams.models import (
    IndicatorValue,
    InstrumentMarketState,
    StrategyEvaluationJob,
)
from apps.market_streams.services import consume_market_event
from apps.portfolios.models import TradingPortfolio
from apps.strategies import evaluation_jobs
from apps.strategies.evaluation_jobs import (
    InvalidConfigurationError,
    MissingInputError,
    StaleInputError,
    claim_next_strategy_evaluation_job,
    classify_evaluation_failure,
    process_strategy_evaluation_jobs,
    recover_stuck_strategy_evaluation_jobs,
    resume_option_quote_evaluation_jobs,
)
from apps.strategies.framework import create_instance
from apps.strategies.models import StrategyRun
from tests.managed_gateway import bind_gateway_mode
from tests.strategy_activation import activate_strategy


pytestmark = pytest.mark.django_db


@pytest.fixture
def portfolio():
    account = BrokerAccount.objects.create(
        account_id="DU-EVALUATION-JOBS",
        net_liquidation=100000,
        available_cash=100000,
        buying_power=200000,
    )
    portfolio = TradingPortfolio.objects.create(
        name="Evaluation jobs",
        account=account,
        minimum_notional=1,
    )
    bind_gateway_mode(portfolio)
    return portfolio


@pytest.fixture
def instrument():
    item = Instrument.objects.create(
        symbol="JOBS",
        exchange="SMART",
        currency="USD",
    )
    BrokerContract.objects.create(
        instrument=item,
        conid=900001,
        primary_exchange="NASDAQ",
        local_symbol="JOBS",
    )
    return item


def make_instance(portfolio, instrument, *, definition="FIXED_WEIGHT_REBALANCE", name="DURABLE_JOB"):
    parameters = {"direction": "LONG"}
    if definition == "SMA_CROSSOVER":
        parameters = {"fast_window": 2, "slow_window": 3, "direction": "LONG"}
    instance, _ = create_instance(
        name=name,
        definition_key=definition,
        portfolio=portfolio,
        instrument_id=instrument.pk,
        timeframe="5m",
        parameters=parameters,
        target_configuration={"target_weight": "0.05"},
        execution_mode="PAPER",
        qualify=False,
    )
    activate_strategy(instance)
    return instance


def envelope(event_type, payload, *, event_id=None):
    event_id = str(event_id or uuid.uuid4())
    timestamp = "2026-07-23T00:05:00+00:00"
    return {
        "event_id": event_id,
        "event_type": event_type,
        "schema_version": 1,
        "occurred_at": timestamp,
        "produced_at": timestamp,
        "producer": "streaming-tests",
        "aggregate_type": "market",
        "aggregate_id": str(payload["instrument_id"]),
        "correlation_id": str(uuid.uuid4()),
        "causation_id": None,
        "idempotency_key": f"{event_type}:{event_id}",
        "payload": payload,
    }


def bar_envelope(instrument, *, bar_id="bar-1", version=1, event_id=None):
    return envelope(
        "market.bar",
        {
            "bar_id": bar_id,
            "instrument_id": instrument.pk,
            "interval": "5m",
            "window_start": "2026-07-23T00:00:00+00:00",
            "window_end": "2026-07-23T00:05:00+00:00",
            "open": "100",
            "high": "102",
            "low": "99",
            "close": "101",
            "volume": "1000",
            "source_event_count": 10,
            "is_final": True,
            "version": version,
        },
        event_id=event_id,
    )


def indicator_envelope(instrument, requirement, *, bar_id="bar-1", version=1):
    role = requirement.parameters["role"]
    value = "11" if role == "fast" else "10"
    previous = "9" if role == "fast" else "10"
    return envelope(
        "market.indicator",
        {
            "source_key": f"{bar_id}:{version}:{role}",
            "instrument_id": instrument.pk,
            "indicator": f"sma_{role}",
            "indicator_name": "sma",
            "indicator_role": role,
            "implementation_version": requirement.implementation_version,
            "requirement_identity_hash": requirement.identity_hash,
            "value": value,
            "previous_value": previous,
            "parameters": requirement.parameters,
            "timeframe": "5m",
            "source_bar_id": bar_id,
            "source_bar_version": version,
            "event_time": "2026-07-23T00:05:00+00:00",
            "is_final": True,
        },
    )


def consume(envelope_value):
    return consume_market_event("strategy-job-tests", envelope_value)


def schedule_fixed_job(portfolio, instrument, *, event_id=None):
    instance = make_instance(portfolio, instrument)
    event = bar_envelope(instrument, event_id=event_id)
    consume(event)
    return instance, event, StrategyEvaluationJob.objects.get()


def test_duplicate_market_event_creates_one_job_and_one_run(portfolio, instrument):
    instance = make_instance(portfolio, instrument)
    event = bar_envelope(instrument)

    consume(event)
    assert consume(event) == {"duplicate": True}
    assert StrategyEvaluationJob.objects.count() == 1
    assert ConsumedEvent.objects.get(event_id=event["event_id"]).result["bar_id"]

    assert process_strategy_evaluation_jobs()["completed"] == 1
    assert process_strategy_evaluation_jobs()["claimed"] == 0
    assert instance.runs.count() == 1


def test_bar_before_indicators_promotes_the_same_waiting_job(portfolio, instrument):
    instance = make_instance(
        portfolio,
        instrument,
        definition="SMA_CROSSOVER",
        name="BAR_FIRST",
    )
    requirements = {
        binding.requirement.parameters["role"]: binding.requirement
        for binding in instance.input_bindings.filter(requirement__input_type="INDICATOR")
    }

    consume(bar_envelope(instrument))
    job = StrategyEvaluationJob.objects.get()
    assert job.status == "WAITING_FOR_INPUT"
    assert job.error_code == "MISSING_INPUT"

    consume(indicator_envelope(instrument, requirements["fast"]))
    job.refresh_from_db()
    assert job.status == "WAITING_FOR_INPUT"

    consume(indicator_envelope(instrument, requirements["slow"]))
    job.refresh_from_db()
    assert job.status == "PENDING"
    assert StrategyEvaluationJob.objects.count() == 1
    assert process_strategy_evaluation_jobs()["completed"] == 1


def test_option_quote_quality_resumes_waiting_evaluation_only_after_all_legs_are_fresh(
    portfolio, instrument,
):
    _, _, job = schedule_fixed_job(portfolio, instrument)
    second = Instrument.objects.create(symbol="JOBS-OPTION-2", asset_class="OPT", exchange="SMART")
    StrategyEvaluationJob.objects.filter(pk=job.pk).update(
        status="WAITING_FOR_INPUT",
        error_code="MISSING_INPUT",
        error_details={
            "input_kind": "OPTION_QUOTES",
            "selection_snapshot_id": 88,
            "instrument_ids": [instrument.pk, second.pk],
        },
    )
    InstrumentMarketState.objects.create(
        instrument=instrument, status="FRESH", reference_price=100,
        latest_event_at=timezone.now(), stale_after_seconds=300,
        reference_price_provider="IBKR", reference_price_source="ibkr_realtime",
        market_data_type="LIVE",
    )
    assert resume_option_quote_evaluation_jobs(instrument.pk) == 0

    result = consume(envelope("market.quality", {
        "instrument_id": second.pk,
        "status": "FRESH",
        "reference_price": "10",
        "latest_event_at": timezone.now().isoformat(),
        "market_event_at": timezone.now().isoformat(),
        "watermark_at": timezone.now().isoformat(),
        "stale_after_seconds": 300,
        "provider": "IBKR",
        "source": "ibkr_realtime",
        "market_data_type": "LIVE",
        "processing_mode": "LIVE",
    }))
    job.refresh_from_db()
    assert result["option_evaluation_jobs_resumed"] == 1
    assert job.status == "PENDING" and job.error_code == ""


def test_option_evaluation_waits_without_emitting_a_run_then_resumes(
    portfolio, instrument, monkeypatch,
):
    instance, _, job = schedule_fixed_job(portfolio, instrument)

    real_plugin = evaluation_jobs.get_plugin(instance.definition)

    class OptionContextPlugin:
        requires_option_context = True

        def __getattr__(self, name):
            return getattr(real_plugin, name)

    monkeypatch.setattr(evaluation_jobs, "get_plugin", lambda definition: OptionContextPlugin())
    monkeypatch.setattr(
        "apps.strategies.option_execution.prepare_option_context",
        lambda *args, **kwargs: {
            "status": "WAITING_FOR_OPTION_QUOTES",
            "reason": "Waiting for the exact IBKR option quote",
            "selection_snapshot_id": 99,
            "legs": [{"instrument_id": instrument.pk}],
        },
    )
    result = process_strategy_evaluation_jobs()
    job.refresh_from_db()
    assert result["waiting_for_input"] == 1
    assert job.status == "WAITING_FOR_INPUT"
    assert job.error_details["input_kind"] == "OPTION_QUOTES"
    assert not instance.runs.exists()

    InstrumentMarketState.objects.create(
        instrument=instrument, status="FRESH", reference_price=100,
        latest_event_at=timezone.now(), stale_after_seconds=300,
        reference_price_provider="IBKR", reference_price_source="ibkr_realtime",
        market_data_type="LIVE",
    )
    assert resume_option_quote_evaluation_jobs(instrument.pk) == 1
    monkeypatch.setattr(
        "apps.strategies.option_execution.prepare_option_context",
        lambda *args, **kwargs: {"status": "READY"},
    )
    assert process_strategy_evaluation_jobs()["completed"] == 1
    assert instance.runs.count() == 1


def test_indicators_before_bar_create_one_ready_job_when_bar_arrives(portfolio, instrument):
    instance = make_instance(
        portfolio,
        instrument,
        definition="SMA_CROSSOVER",
        name="INDICATORS_FIRST",
    )
    requirements = [
        binding.requirement
        for binding in instance.input_bindings.filter(requirement__input_type="INDICATOR")
    ]

    for requirement in requirements:
        consume(indicator_envelope(instrument, requirement))
    assert IndicatorValue.objects.count() == 2
    assert not StrategyEvaluationJob.objects.exists()

    consume(bar_envelope(instrument))
    job = StrategyEvaluationJob.objects.get()
    assert job.status == "PENDING"
    assert len(job.expected_input_identity_hashes) == 3
    assert process_strategy_evaluation_jobs()["completed"] == 1


@pytest.mark.parametrize(
    ("error", "code", "retryable"),
    [
        (OperationalError("database unavailable"), "INFRASTRUCTURE_RETRYABLE", True),
        (MissingInputError("missing"), "MISSING_INPUT", False),
        (StaleInputError("stale"), "STALE_INPUT", False),
        (InvalidConfigurationError("invalid"), "INVALID_CONFIGURATION", False),
        (RuntimeError("plugin"), "PLUGIN_LOGIC_ERROR", False),
        (IntegrityError("integrity"), "DATA_INTEGRITY_ERROR", False),
    ],
)
def test_evaluation_failure_classification(error, code, retryable):
    classified = classify_evaluation_failure(error)
    assert classified.error_code == code
    assert classified.retryable is retryable


@override_settings(
    STRATEGY_EVALUATION_CLAIM_TIMEOUT_SECONDS=10,
    STRATEGY_EVALUATION_RETRY_BASE_SECONDS=1,
)
def test_worker_crash_after_claim_is_recovered(portfolio, instrument):
    _, _, job = schedule_fixed_job(portfolio, instrument)
    assert claim_next_strategy_evaluation_job() == job.pk
    job.refresh_from_db()
    assert job.status == "CLAIMED" and job.attempt_count == 1

    StrategyEvaluationJob.objects.filter(pk=job.pk).update(
        claimed_at=timezone.now() - timedelta(seconds=11),
    )
    assert recover_stuck_strategy_evaluation_jobs() == 1
    job.refresh_from_db()
    assert job.status == "RETRY"
    assert job.error_code == "INFRASTRUCTURE_RETRYABLE"


def test_temporary_database_error_retries_without_poisoning_strategy(
    portfolio,
    instrument,
    monkeypatch,
):
    instance, _, job = schedule_fixed_job(portfolio, instrument)

    class TemporarilyUnavailablePlugin:
        def evaluate(self, context):
            raise OperationalError("database unavailable")

    monkeypatch.setattr(
        "apps.strategies.framework.get_plugin",
        lambda definition: TemporarilyUnavailablePlugin(),
    )

    assert process_strategy_evaluation_jobs()["retry"] == 1
    job.refresh_from_db()
    instance.refresh_from_db()
    assert job.status == "RETRY"
    assert job.error_code == "INFRASTRUCTURE_RETRYABLE"
    assert instance.state != "ERROR"
    assert not instance.runs.exists()


def test_plugin_logic_error_is_terminal_but_market_event_remains_consumed(
    portfolio,
    instrument,
    monkeypatch,
):
    instance, event, job = schedule_fixed_job(portfolio, instrument)

    class BrokenPlugin:
        def evaluate(self, context):
            raise RuntimeError("plugin exploded")

    monkeypatch.setattr(
        "apps.strategies.framework.get_plugin",
        lambda definition: BrokenPlugin(),
    )
    assert process_strategy_evaluation_jobs()["failed"] == 1

    job.refresh_from_db()
    instance.refresh_from_db()
    consumed = ConsumedEvent.objects.get(event_id=event["event_id"])
    assert job.status == "FAILED"
    assert job.error_code == "PLUGIN_LOGIC_ERROR"
    assert instance.state == "ERROR"
    assert consumed.result.get("status") != "FAILED"


@override_settings(STRATEGY_EVALUATION_RETRY_BASE_SECONDS=1)
def test_job_retry_recovers_and_completes(portfolio, instrument, monkeypatch):
    instance, _, job = schedule_fixed_job(portfolio, instrument)
    real_evaluate = evaluation_jobs.evaluate_instance
    calls = 0

    def flaky(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OperationalError("temporary database outage")
        return real_evaluate(*args, **kwargs)

    monkeypatch.setattr(evaluation_jobs, "evaluate_instance", flaky)
    assert process_strategy_evaluation_jobs()["retry"] == 1
    StrategyEvaluationJob.objects.filter(pk=job.pk).update(next_attempt_at=timezone.now())

    assert process_strategy_evaluation_jobs()["completed"] == 1
    job.refresh_from_db()
    assert job.status == "COMPLETED"
    assert job.attempt_count == 2
    assert instance.runs.count() == 1


@override_settings(
    STRATEGY_EVALUATION_CLAIM_TIMEOUT_SECONDS=10,
    STRATEGY_EVALUATION_RETRY_BASE_SECONDS=1,
)
def test_recovery_reuses_strategy_run_after_crash_before_job_completion(
    portfolio,
    instrument,
    monkeypatch,
):
    instance, _, job = schedule_fixed_job(portfolio, instrument)
    real_complete = evaluation_jobs._complete_job

    def crash(*args, **kwargs):
        raise SystemExit("worker terminated")

    monkeypatch.setattr(evaluation_jobs, "_complete_job", crash)
    with pytest.raises(SystemExit, match="worker terminated"):
        process_strategy_evaluation_jobs()

    job.refresh_from_db()
    assert job.status == "RUNNING"
    assert instance.runs.count() == 1
    first_run_id = instance.runs.get().pk

    StrategyEvaluationJob.objects.filter(pk=job.pk).update(
        claimed_at=timezone.now() - timedelta(seconds=11),
    )
    assert recover_stuck_strategy_evaluation_jobs() == 1
    StrategyEvaluationJob.objects.filter(pk=job.pk).update(next_attempt_at=timezone.now())
    monkeypatch.setattr(evaluation_jobs, "_complete_job", real_complete)

    assert process_strategy_evaluation_jobs()["completed"] == 1
    job.refresh_from_db()
    assert job.strategy_run_id == first_run_id
    assert job.status == "COMPLETED"
    assert StrategyRun.objects.filter(strategy_instance=instance).count() == 1
