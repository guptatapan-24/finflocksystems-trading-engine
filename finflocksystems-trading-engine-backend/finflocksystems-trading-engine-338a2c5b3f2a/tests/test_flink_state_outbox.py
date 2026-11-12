"""Backend side of the Flink state rehydration through the transactional outbox."""

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import BrokerAccount
from apps.audit.models import OutboxEvent
from apps.event_bus.services import envelope_for
from apps.event_bus.schemas import validate_envelope
from apps.instruments.models import BrokerContract, Instrument
from apps.market_streams.models import MarketBar
from apps.market_streams.rehydration import (
    BAR_HISTORY_SEED_TOPIC,
    SNAPSHOT_KEY_PREFIX,
    STRATEGY_INPUTS_SNAPSHOT_EVENT_TYPE,
    publish_bar_history_seed,
    publish_strategy_inputs_snapshot,
    rehydrate_flink_state_if_needed,
)
from apps.portfolios.models import TradingPortfolio
from apps.strategies.framework import create_instance, deactivate_inputs
from tests.managed_gateway import bind_gateway_mode
from tests.strategy_activation import activate_strategy

pytestmark = pytest.mark.django_db


@pytest.fixture
def instance():
    account = BrokerAccount.objects.create(account_id="DU-REHYDRATE", net_liquidation=100000,
        available_cash=100000, buying_power=200000)
    portfolio = TradingPortfolio.objects.create(name="Rehydration", account=account, minimum_notional=1)
    bind_gateway_mode(portfolio)
    instrument = Instrument.objects.create(symbol="REHY", exchange="SMART", currency="USD")
    BrokerContract.objects.create(instrument=instrument, conid=880001, primary_exchange="NASDAQ", local_symbol="REHY")
    item, _ = create_instance(name="REHY_FIXED", definition_key="FIXED_WEIGHT_REBALANCE", portfolio=portfolio,
        instrument_id=instrument.pk, timeframe="5m", parameters={"direction": "LONG"},
        target_configuration={"target_weight": "0.05"}, execution_mode="PAPER", qualify=False)
    activate_strategy(item)
    item.refresh_from_db()
    return item


def snapshot_rows():
    return OutboxEvent.objects.filter(idempotency_key__startswith=SNAPSHOT_KEY_PREFIX)


def make_bars(instrument, count, interval="5m"):
    start = datetime(2026, 11, 2, 14, 30, tzinfo=dt_timezone.utc)
    for index in range(count):
        window_start = start + timedelta(minutes=5 * index)
        MarketBar.objects.create(instrument=instrument, bar_id=f"bar-{index}", interval=interval,
            window_start=window_start, window_end=window_start + timedelta(minutes=5),
            open=Decimal("100"), high=Decimal("101"), low=Decimal("99"), close=Decimal(100 + index),
            volume=Decimal("10"), version=1, is_final=True, processing_mode="WARMUP",
            produced_at=window_start)
    # A superseding version of the newest bar: the seed must carry version 2.
    newest = MarketBar.objects.get(bar_id=f"bar-{count - 1}")
    MarketBar.objects.create(instrument=instrument, bar_id=newest.bar_id, interval=interval,
        window_start=newest.window_start, window_end=newest.window_end, open=Decimal("100"),
        high=Decimal("101"), low=Decimal("99"), close=Decimal("999"), volume=Decimal("10"),
        version=2, is_final=True, processing_mode="LIVE", produced_at=newest.window_end)


def test_disabled_kafka_publishes_nothing(instance):
    assert publish_strategy_inputs_snapshot() == {"skipped": "KAFKA_DISABLED"}
    assert not snapshot_rows().exists()


@override_settings(KAFKA_ENABLED=True)
def test_snapshot_lists_every_active_requirement_and_is_a_valid_envelope(instance):
    result = publish_strategy_inputs_snapshot()
    assert result["pairs"] == 1 and result["queued"] == 1
    row = snapshot_rows().get()
    assert row.topic == "strategy.inputs.v1"
    assert row.event_type == STRATEGY_INPUTS_SNAPSHOT_EVENT_TYPE
    assert row.partition_key == str(instance.instrument_id)
    expected = set(instance.input_bindings.filter(active=True).values_list("requirement__identity_hash", flat=True))
    assert {item["identity_hash"] for item in row.payload["requirements"]} == expected
    validate_envelope(envelope_for(row))


@override_settings(KAFKA_ENABLED=True)
def test_snapshot_is_requeued_not_duplicated(instance):
    publish_strategy_inputs_snapshot()
    snapshot_rows().update(status="PUBLISHED", published_at=timezone.now())
    publish_strategy_inputs_snapshot()
    row = snapshot_rows().get()
    assert row.status == "PENDING" and row.published_at is None


@override_settings(KAFKA_ENABLED=True)
def test_snapshot_never_rewrites_a_row_being_published(instance):
    publish_strategy_inputs_snapshot()
    lease = timezone.now() + timedelta(seconds=60)
    snapshot_rows().update(status="PUBLISHING", available_at=lease)
    publish_strategy_inputs_snapshot()
    assert snapshot_rows().get().status == "PUBLISHING"


@override_settings(KAFKA_ENABLED=True)
def test_deactivated_pair_gets_one_empty_snapshot(instance):
    publish_strategy_inputs_snapshot()
    snapshot_rows().update(status="PUBLISHED", published_at=timezone.now())
    deactivate_inputs(instance)
    result = publish_strategy_inputs_snapshot()
    assert result["cleared"] == 1
    assert snapshot_rows().get().payload["requirements"] == []
    snapshot_rows().update(status="PUBLISHED", published_at=timezone.now())
    assert publish_strategy_inputs_snapshot()["cleared"] == 0


@override_settings(KAFKA_ENABLED=True, FLINK_REHYDRATION_SEED_BARS=12, FLINK_REHYDRATION_SEED_CHUNK_BARS=5,
                   WARMUP_SAFETY_BARS=0)
def test_seed_is_chunked_latest_version_and_idempotent(instance):
    from apps.strategies.models import StrategyInputRequirement
    StrategyInputRequirement.objects.filter(bindings__strategy_instance=instance).update(warmup_bars=1)
    make_bars(instance.instrument, 30)
    first = publish_bar_history_seed("job-1")
    rows = list(OutboxEvent.objects.filter(topic=BAR_HISTORY_SEED_TOPIC).order_by("payload__chunk"))
    assert first["rows"] == 3 and len(rows) == 3  # 12 bars in chunks of 5
    bars = [item for row in rows for item in row.payload["bars"]]
    assert [item["bar_id"] for item in bars] == [f"bar-{index}" for index in range(18, 30)]
    assert bars[-1]["version"] == 2 and bars[-1]["close"].startswith("999")
    for row in rows:
        validate_envelope(envelope_for(row))
    assert publish_bar_history_seed("job-1")["rows"] == 0


@override_settings(KAFKA_ENABLED=True)
def test_rehydration_runs_once_per_fresh_job_and_never_for_restored_state(instance):
    make_bars(instance.instrument, 3)
    fresh = [{"job_id": "a" * 32, "name": "market-pipeline-v1", "restored_checkpoint_id": None}]
    first = rehydrate_flink_state_if_needed(lambda: fresh)
    assert first["rehydrated"] is True
    assert OutboxEvent.objects.filter(topic=BAR_HISTORY_SEED_TOPIC).exists()
    assert rehydrate_flink_state_if_needed(lambda: fresh)["rehydrated"] is False
    restored = [{"job_id": "b" * 32, "name": "market-pipeline-v1", "restored_checkpoint_id": 42}]
    assert rehydrate_flink_state_if_needed(lambda: restored)["rehydrated"] is False


@override_settings(KAFKA_ENABLED=True)
def test_rehydration_fails_open_when_flink_is_unreachable(instance):
    def boom():
        raise ConnectionError("flink down")
    assert rehydrate_flink_state_if_needed(boom)["skipped"] == "FLINK_UNAVAILABLE"
    assert not OutboxEvent.objects.filter(topic=BAR_HISTORY_SEED_TOPIC).exists()
