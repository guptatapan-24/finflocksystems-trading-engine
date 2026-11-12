#!/usr/bin/env python3
"""Verify the Flink state fix locally: no Docker, no Kafka, no Flink, no database.

Runs the *real* operator code of the bar-aggregation and indicator stages on a
synthetic 5-second tick stream (1m bars, one SMA(20) indicator), using the
in-memory runtime in ``scripts/flink_local_harness.py``. The pre-fix code is
loaded from git (``--baseline-ref``, default the first commit) and run on the
same feed, so the two can be compared directly.

Checks:
  1. MEMORY      old bar state grows with every tick; new bar state stays flat
  2. LONG RUN    over several simulated days the new state plateaus (TTL + caps)
  3. SAME OUTPUT old and new emit identical bars and indicators
  4. RESTART     after state loss, the outbox seed restores indicator values
  5. REGISTRY    a DB snapshot alone rebuilds the registry; stale snapshots are ignored
  6. LATE TICKS  late ticks still re-version open bars; after eviction they are dead-lettered

Usage (from the backend folder):
    python scripts/verify_flink_state_fix.py            # about 1-2 minutes
    python scripts/verify_flink_state_fix.py --quick    # about 20 seconds
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import flink_local_harness as fh  # noqa: E402

RESULTS = []


def check(name, passed, detail):
    RESULTS.append((name, bool(passed), detail))
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}: {detail}")


def kb(value):
    return f"{value / 1024:,.1f} KB"


def bucket_bytes(pipeline):
    report = pipeline.bars.state_report()
    return sum(item["bytes"] for name, item in report.items() if "bucket" in name)


def memory_and_equivalence(old, new, hours):
    print(f"\n== 1. MEMORY + 3. SAME OUTPUT: {hours:g} h of 5-second ticks, old vs new ==")
    print(f"{'sim time':>9} | {'old bar state':>14} | {'new bar state':>14} | "
          f"{'old ind state':>14} | {'new ind state':>14} | {'old ms/tick':>11} | {'new ms/tick':>11}")
    pipelines = {}
    samples = {"old": {}, "new": {}}
    timings = {"old": {}, "new": {}}
    seconds = int(hours * 3600)
    for label, (bar_module, indicator_module) in (("old", old), ("new", new)):
        if bar_module is None:
            continue
        pipeline = fh.Pipeline(bar_module, indicator_module)
        clock = {"t": time.perf_counter(), "ticks": 0}

        def sample(offset, item, label=label, clock=clock):
            now = time.perf_counter()
            ticks = item.sequence - clock["ticks"]
            timings[label][offset] = (now - clock["t"]) * 1000 / max(ticks, 1)
            clock.update(t=now, ticks=item.sequence)
            samples[label][offset] = (item.bars.state_bytes(), item.indicators.state_bytes(), bucket_bytes(item))
        pipeline.run(fh.EPOCH_MS, seconds, on_sample=sample, sample_every_s=1800)
        sample(seconds, pipeline)
        pipelines[label] = pipeline
    for offset in sorted(samples["new"]):
        old_row = samples["old"].get(offset)
        cells = [
            kb(old_row[0]) if old_row else "-", kb(samples["new"][offset][0]),
            kb(old_row[1]) if old_row else "-", kb(samples["new"][offset][1]),
            f"{timings['old'].get(offset, 0):.2f}" if old_row else "-", f"{timings['new'][offset]:.2f}",
        ]
        print(f"{offset / 3600:>8.1f}h | " + " | ".join(f"{cell:>14}" if i < 4 else f"{cell:>11}"
                                                       for i, cell in enumerate(cells)))
    first = min(offset for offset in samples["new"] if offset >= 3600)
    last = max(samples["new"])
    new_first, new_last = samples["new"][first][2], samples["new"][last][2]
    check("new open-bucket state is flat", new_last <= new_first * 1.25 + 4096,
          f"{kb(new_first)} at {first / 3600:g}h -> {kb(new_last)} at {last / 3600:g}h")
    if "old" in pipelines:
        old_first, old_last = samples["old"][first][0], samples["old"][last][0]
        check("old bar state grows without bound (the bug)", old_last >= old_first * (last / first) * 0.8,
              f"{kb(old_first)} at {first / 3600:g}h -> {kb(old_last)} at {last / 3600:g}h "
              f"(x{old_last / max(old_first, 1):.1f}; new is {samples['new'][last][0] / max(old_last, 1):.0%} of old)")
        same_bars = fh.comparable(pipelines["old"].bar_events) == fh.comparable(pipelines["new"].bar_events)
        same_indicators = (fh.comparable(pipelines["old"].indicator_events)
                           == fh.comparable(pipelines["new"].indicator_events))
        check("old and new emit identical bars", same_bars and pipelines["new"].bar_events,
              f"{len(pipelines['new'].bar_events)} bars")
        check("old and new emit identical indicators", same_indicators and pipelines["new"].indicator_events,
              f"{len(pipelines['new'].indicator_events)} indicator values")
    return pipelines["new"]


def long_run(new, days):
    print(f"\n== 2. LONG RUN: {days:g} simulated days, new code only "
          f"(version-ledger TTL shortened to 1 day so the plateau is visible) ==")
    pipeline = fh.Pipeline(*new, bar_kwargs={"ledger_ttl_seconds": 86400})
    samples = {}

    def sample(offset, item):
        samples[offset] = (item.bars.state_report(), item.indicators.state_report())
    seconds = int(days * 86400)
    pipeline.run(fh.EPOCH_MS, seconds, on_sample=sample, sample_every_s=43200)
    sample(seconds, pipeline)
    for offset in sorted(samples):
        bars, indicators = samples[offset]
        parts = ", ".join(f"{name}={item['entries']} entries/{kb(item['bytes'])}"
                          for name, item in sorted({**bars, **indicators}.items()) if "timer" not in name)
        print(f"  day {offset / 86400:>4.1f}: {parts}")
    last = max(samples)
    previous = last - 43200
    total = lambda offset: sum(item["bytes"] for part in samples[offset] for item in part.values())
    check("new state plateaus over days", total(last) <= total(previous) * 1.1 + 4096,
          f"{kb(total(previous))} at day {previous / 86400:g} -> {kb(total(last))} at day {last / 86400:g}")
    history = json.loads(next(iter(pipeline.indicators.keyed["rolling-final-bars-v3"].values()))[0])
    check("indicator history is capped", len(history.get("LIVE", [])) <= 1000,
          f"{len(history.get('LIVE', []))} bars kept (cap 1000; old cap 10,000 full payloads per scope)")


def restart_with_seed(new, reference):
    print("\n== 4. RESTART: Flink loses its state; the backend re-feeds it through the outbox ==")
    bars = reference.bar_payloads()
    last_event = reference.bar_events[-1]
    last_bar = bars[-1]
    expected = next(item["value"] for item in reference.indicator_payloads()
                    if item["source_bar_id"] == last_bar["bar_id"]
                    and int(item["source_bar_version"]) == int(last_bar["version"]))
    identity = new[0]._harness_identity

    def fresh():
        harness = fh.OperatorHarness(new[1].RegistryIndicators(), fh.indicator_key)
        harness.process_broadcast(fh.strategy_inputs_event(identity, fh.EPOCH_MS))
        return harness

    without_seed = fresh()
    without_seed.process_element(last_event)
    value_without = json.loads(without_seed.drain()[0])["payload"]["value"]
    with_seed = fresh()
    with_seed.process_element(fh.seed_event(bars[:-1][-500:]))
    seeded_output = with_seed.drain()
    with_seed.process_element(last_event)
    value_with = json.loads(with_seed.drain()[0])["payload"]["value"]
    check("without the seed the next indicator is null (the bug after a restart)", value_without is None,
          f"value={value_without}")
    check("seed events emit nothing downstream", seeded_output == [], f"{len(seeded_output)} events")
    check("with the outbox seed the next indicator matches the uninterrupted run",
          value_with is not None and Decimal(value_with) == Decimal(expected),
          f"seeded={value_with} uninterrupted={expected}")


def registry_from_snapshot(new):
    print("\n== 5. REGISTRY: rebuilt from a PostgreSQL snapshot, independent of Kafka retention ==")
    identity = new[0]._harness_identity
    pipeline = fh.Pipeline(*new, register=False)
    pipeline.register(fh.strategy_inputs_event(identity, fh.EPOCH_MS, event_type="strategy.inputs.snapshot"))
    pipeline.run(fh.EPOCH_MS, 1800)
    check("snapshot alone (no activation event) produces bars and indicators",
          pipeline.bar_events and pipeline.indicator_events,
          f"{len(pipeline.bar_events)} bars, {len(pipeline.indicator_events)} indicators")

    stale = fh.Pipeline(*new, register=False)
    stale.register(fh.strategy_inputs_event(identity, fh.EPOCH_MS))  # activation at T
    stale.register(fh.strategy_inputs_event(identity, fh.EPOCH_MS - 3_600_000,  # snapshot read at T-1h
                                            event_type="strategy.inputs.snapshot", requirements=[]))
    stale.run(fh.EPOCH_MS, 600)
    check("a late, older snapshot does not undo a newer activation", stale.bar_events,
          f"{len(stale.bar_events)} bars still produced")

    cleared = fh.Pipeline(*new, register=False)
    cleared.register(fh.strategy_inputs_event(identity, fh.EPOCH_MS))
    cleared.register(fh.strategy_inputs_event(identity, fh.EPOCH_MS + 1000,
                                              event_type="strategy.inputs.snapshot", requirements=[]))
    cleared.run(fh.EPOCH_MS, 600)
    check("a newer empty snapshot removes the pair's requirements", not cleared.bar_events,
          f"{len(cleared.bar_events)} bars produced")


def late_ticks(pipeline):
    print("\n== 6. LATE TICKS ==")
    last_ms = pipeline.bars.max_event_ms
    recent_ms = last_ms - 90_000  # window that fired ~1 minute ago, still retained
    pipeline.sequence += 1
    pipeline.bars.process_element(fh.tick(pipeline.sequence, recent_ms), recent_ms)
    recent = [json.loads(value)["payload"] for value in pipeline.bars.drain()]
    check("late tick inside retention still produces the next bar version",
          any(int(item["version"]) >= 2 for item in recent),
          f"versions emitted: {[item['version'] for item in recent]}")
    old_ms = fh.EPOCH_MS + 2000  # first window of the run, evicted long ago
    pipeline.sequence += 1
    pipeline.bars.process_element(fh.tick(pipeline.sequence, old_ms), old_ms)
    emitted = pipeline.bars.drain()
    dead = pipeline.bars.side.get("bar-aggregation-late-tick-v1", [])
    reason = json.loads(dead[-1])["payload"]["reason_code"] if dead else None
    check("tick after eviction is dead-lettered, not turned into a partial bar",
          not emitted and reason == "LATE_TICK_AFTER_BAR_EVICTION",
          f"bars emitted={len(emitted)}, dead-letter reason={reason}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--baseline-ref", default=fh.BASELINE_REF,
                        help="git revision with the pre-fix operators (default: %(default)s)")
    parser.add_argument("--skip-baseline", action="store_true", help="do not load or run the old code")
    parser.add_argument("--hours", type=float, default=3.0, help="old-vs-new comparison length")
    parser.add_argument("--days", type=float, default=3.0, help="new-code long run length")
    parser.add_argument("--quick", action="store_true", help="--hours 1.5 --days 1.5")
    args = parser.parse_args()
    if args.quick:
        args.hours, args.days = 1.5, 1.5
    args.hours = max(args.hours, 1.5)
    args.days = max(args.days, 1.5)

    new = (fh.load_stage("bar_aggregation"), fh.load_stage("indicator_computation"))
    old = (None, None)
    if not args.skip_baseline:
        try:
            old = (fh.load_stage("bar_aggregation", args.baseline_ref),
                   fh.load_stage("indicator_computation", args.baseline_ref))
        except Exception as exc:  # git missing, shallow clone, ...
            print(f"WARNING: could not load baseline {args.baseline_ref!r} ({exc}); comparing new code only")

    reference = memory_and_equivalence(old, new, args.hours)
    restart_with_seed(new, reference)
    registry_from_snapshot(new)
    late_ticks(reference)
    long_run(new, args.days)

    failed = [name for name, passed, _ in RESULTS if not passed]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    for name in failed:
        print(f"  FAILED: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
