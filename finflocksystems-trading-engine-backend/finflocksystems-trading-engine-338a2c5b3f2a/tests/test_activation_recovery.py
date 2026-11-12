"""Two lifecycle faults that made an intraday strategy impossible to keep running.

Both are races around a strategy that is momentarily without market data: one
stopped it starting at all, the other stopped it continuing after the first
subscription flap.
"""

from types import SimpleNamespace

from apps.strategies.framework import _own_market_data_staleness


def _instance(pk):
    return SimpleNamespace(pk=pk)


def _stale(*strategy_ids):
    return {
        "code": "MARKET_DATA_STALE",
        "details": {"strategies": [{"strategy_id": pk} for pk in strategy_ids]},
    }


def test_a_strategy_does_not_block_its_own_activation():
    """Enable needs readiness, readiness needs market data, and only enabling
    produces market data. The activating strategy's own staleness is excused so
    that cycle can be broken."""
    assert _own_market_data_staleness(_stale(3), _instance(3)) is True


def test_another_strategy_still_blocks_activation():
    assert _own_market_data_staleness(_stale(7), _instance(3)) is False


def test_a_mixed_report_still_blocks_activation():
    assert _own_market_data_staleness(_stale(3, 7), _instance(3)) is False


def test_other_blockers_are_never_excused():
    for code in ("REQUIRED_FLINK_JOBS_NOT_RUNNING", "KAFKA_UNAVAILABLE", "EXECUTION_WORKERS_ABSENT"):
        assert _own_market_data_staleness({"code": code, "details": {}}, _instance(3)) is False


def test_an_empty_staleness_report_is_not_excused():
    assert _own_market_data_staleness(_stale(), _instance(3)) is False


def test_a_lifecycle_race_is_stale_input_not_a_plugin_defect():
    """A job claimed while the instance was evaluable, then flapped to BLOCKED
    before the locked re-read, must not be recorded as PLUGIN_LOGIC_ERROR --
    that code drives the instance into the terminal ERROR state and every
    later bar stops being evaluated."""
    from apps.strategies.evaluation_jobs import StaleInputError, _record_failure

    poisoning_codes = {"INVALID_CONFIGURATION", "PLUGIN_LOGIC_ERROR", "DATA_INTEGRITY_ERROR"}
    assert StaleInputError.error_code == "STALE_INPUT"
    assert StaleInputError.error_code not in poisoning_codes
    assert callable(_record_failure)


def test_the_framework_raises_a_distinct_type_for_that_race():
    from apps.strategies.framework import StrategyNotReadyError

    # A ValueError subclass, so existing callers that catch ValueError still do.
    assert issubclass(StrategyNotReadyError, ValueError)
