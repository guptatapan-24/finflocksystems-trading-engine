"""Pure helpers that keep Flink state bounded (no PyFlink needed)."""

import json

from streaming.flink.jobs.processing import (
    STRATEGY_INPUTS_SNAPSHOT_EVENT_TYPE,
    apply_registry_event,
    compact_history_bar,
    history_limit,
    merge_bar_history,
    plan_registry_update,
    registry_meta_key,
)


def requirement(identity_hash, input_type="INDICATOR", warmup_bars=20):
    return {"identity_hash": identity_hash, "input_type": input_type, "name": "sma",
            "role": "", "parameters": {"window": 20}, "implementation_version": 1,
            "warmup_bars": warmup_bars}


def changed(occurred_at, requirements=(), removed=(), instrument_id=7, timeframe="1m"):
    return {"event_type": "strategy.inputs.changed", "occurred_at": occurred_at,
            "payload": {"instrument_id": instrument_id, "timeframe": timeframe,
                        "requirements": list(requirements),
                        "removed_requirement_hashes": list(removed)}}


def snapshot(occurred_at, requirements=(), instrument_id=7, timeframe="1m"):
    return {"event_type": STRATEGY_INPUTS_SNAPSHOT_EVENT_TYPE, "occurred_at": occurred_at,
            "payload": {"snapshot": True, "instrument_id": instrument_id, "timeframe": timeframe,
                        "requirements": list(requirements), "removed_requirement_hashes": []}}


class FakeMapState(dict):
    def put(self, key, value):
        self[key] = value

    def remove(self, key):
        self.pop(key, None)


def test_incremental_event_keeps_upsert_and_remove_behaviour():
    state = FakeMapState()
    apply_registry_event(state, changed("2026-11-12T10:00:00+00:00", [requirement("a"), requirement("b")]), "INDICATOR")
    apply_registry_event(state, changed("2026-11-12T10:01:00+00:00", removed=["a"]), "INDICATOR")
    assert "a" not in state and "b" in state
    assert json.loads(state[registry_meta_key("7", "1m")])["occurred_at"] == "2026-11-12T10:01:00+00:00"


def test_stage_only_keeps_its_own_input_type():
    state = FakeMapState()
    apply_registry_event(state, changed("2026-11-12T10:00:00+00:00",
        [requirement("bar", input_type="BAR"), requirement("ind")]), "BAR")
    assert "bar" in state and "ind" not in state


def test_snapshot_replaces_requirements_of_its_pair_only():
    state = FakeMapState()
    apply_registry_event(state, changed("2026-11-12T10:00:00+00:00", [requirement("stale")]), "INDICATOR")
    apply_registry_event(state, changed("2026-11-12T10:00:00+00:00", [requirement("other")], timeframe="5m"), "INDICATOR")
    apply_registry_event(state, snapshot("2026-11-12T11:00:00+00:00", [requirement("fresh")]), "INDICATOR")
    assert "stale" not in state
    assert "fresh" in state
    assert "other" in state  # different (instrument, timeframe)


def test_snapshot_older_than_an_applied_incremental_event_is_ignored():
    current = {registry_meta_key("7", "1m"): {"occurred_at": "2026-11-12T12:00:00+00:00"},
               "new": {**requirement("new"), "instrument_id": "7", "timeframe": "1m"}}
    puts, removes = plan_registry_update(snapshot("2026-11-12T11:00:00+00:00", []), current, "INDICATOR")
    assert (puts, removes) == ({}, [])


def test_empty_snapshot_clears_the_pair():
    state = FakeMapState()
    apply_registry_event(state, changed("2026-11-12T10:00:00+00:00", [requirement("a")]), "INDICATOR")
    apply_registry_event(state, snapshot("2026-11-12T11:00:00+00:00", []), "INDICATOR")
    assert [key for key in state if not key.startswith("meta:")] == []


def bar(bar_id, minute, version=1, close="100"):
    return {"bar_id": bar_id, "version": version, "window_end": f"2026-11-12T10:{minute:02d}:00+00:00",
            "open": "100", "high": "101", "low": "99", "close": close, "volume": "10",
            "is_final": True, "provider": "IBKR", "received_at": "x", "source_event_count": 3}


def test_compact_history_bar_drops_unused_fields():
    compact = compact_history_bar(bar("a", 1))
    assert "provider" not in compact and "received_at" not in compact
    assert compact["close"] == "100"


def test_merge_is_order_independent_and_keeps_highest_version():
    live = [bar("a", 1), bar("b", 2, version=2, close="105")]
    seed = [bar("b", 2, version=1, close="104"), bar("c", 3), bar("z", 0)]
    first = merge_bar_history(live, seed, 10)
    second = merge_bar_history(seed, live, 10)
    assert first == second
    assert [item["bar_id"] for item in first] == ["z", "a", "b", "c"]
    assert next(item for item in first if item["bar_id"] == "b")["close"] == "105"


def test_merge_caps_to_newest_bars():
    bars = [bar(str(index), index) for index in range(30)]
    assert [item["bar_id"] for item in merge_bar_history([], bars, 5)] == ["25", "26", "27", "28", "29"]


def test_history_limit_never_below_longest_warmup():
    assert history_limit([{"warmup_bars": 20}], 1000) == 1000
    assert history_limit([{"warmup_bars": 1500}], 1000) == 1501
    assert history_limit([], 50) == 50
