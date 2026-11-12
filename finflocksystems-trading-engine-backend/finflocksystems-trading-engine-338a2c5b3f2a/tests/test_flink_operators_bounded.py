"""Drive the real Flink operator code in plain Python (scripts/flink_local_harness.py).

Fast regression versions of scripts/verify_flink_state_fix.py.
"""

import json
from decimal import Decimal

import pytest

from scripts import flink_local_harness as fh


@pytest.fixture(scope="module")
def new():
    return fh.load_stage("bar_aggregation"), fh.load_stage("indicator_computation")


@pytest.fixture(scope="module")
def two_hours(new):
    pipeline = fh.Pipeline(*new)
    samples = {}
    pipeline.run(fh.EPOCH_MS, 7200, on_sample=lambda offset, item: samples.__setitem__(
        offset, item.bars.state_report()["open-bar-buckets-v1"]["bytes"]), sample_every_s=1800)
    return pipeline, samples


def test_open_bucket_state_stays_flat(two_hours):
    _, samples = two_hours
    assert samples[5400] <= samples[1800] * 1.25 + 4096


def test_bars_and_indicators_are_produced(two_hours):
    pipeline, _ = two_hours
    assert len(pipeline.bar_events) >= 115
    assert [item["value"] for item in pipeline.indicator_payloads()][-1] is not None


def test_matches_pre_fix_output(new):
    try:
        old = (fh.load_stage("bar_aggregation", fh.BASELINE_REF),
               fh.load_stage("indicator_computation", fh.BASELINE_REF))
    except Exception as exc:  # no git / shallow clone
        pytest.skip(f"baseline {fh.BASELINE_REF} not available: {exc}")
    before, after = fh.Pipeline(*old), fh.Pipeline(*new)
    before.run(fh.EPOCH_MS, 3600)
    after.run(fh.EPOCH_MS, 3600)
    assert fh.comparable(after.bar_events) == fh.comparable(before.bar_events)
    assert fh.comparable(after.indicator_events) == fh.comparable(before.indicator_events)


def test_outbox_seed_restores_indicator_after_restart(new, two_hours):
    pipeline, _ = two_hours
    bars = pipeline.bar_payloads()
    expected = next(item["value"] for item in pipeline.indicator_payloads()
                    if item["source_bar_id"] == bars[-1]["bar_id"])
    restarted = fh.OperatorHarness(new[1].RegistryIndicators(), fh.indicator_key)
    restarted.process_broadcast(fh.strategy_inputs_event(new[0]._harness_identity, fh.EPOCH_MS))
    restarted.process_element(fh.seed_event(bars[:-1]))
    assert restarted.drain() == []
    restarted.process_element(pipeline.bar_events[-1])
    assert Decimal(json.loads(restarted.drain()[0])["payload"]["value"]) == Decimal(expected)


def test_snapshot_alone_rebuilds_registry(new):
    pipeline = fh.Pipeline(*new, register=False)
    pipeline.register(fh.strategy_inputs_event(
        new[0]._harness_identity, fh.EPOCH_MS, event_type="strategy.inputs.snapshot"))
    pipeline.run(fh.EPOCH_MS, 600)
    assert pipeline.bar_events and pipeline.indicator_events


def test_tick_after_eviction_is_dead_lettered(new):
    pipeline = fh.Pipeline(*new)
    pipeline.run(fh.EPOCH_MS, 3600)
    pipeline.sequence += 1
    pipeline.bars.process_element(fh.tick(pipeline.sequence, fh.EPOCH_MS + 2000), fh.EPOCH_MS + 2000)
    assert pipeline.bars.drain() == []
    dead = pipeline.bars.side["bar-aggregation-late-tick-v1"]
    assert json.loads(dead[-1])["payload"]["reason_code"] == "LATE_TICK_AFTER_BAR_EVICTION"
