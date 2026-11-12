"""The remote per-session feature source and what it changes in the decision."""

from datetime import date

import pytest
from django.test import override_settings

from apps.market_data.remote_features import (
    RemoteFeatureProvider, RemoteFeatureUnavailable, remote_features_enabled,
    remote_symbol, decision_day,
)
from apps.strategies.option_analytics.runtime import selection_market


class _Instrument:
    def __init__(self, symbol):
        self.symbol = symbol


@override_settings(REMOTE_FEATURE_SYMBOLS={"NIFTY50": "NIFTY"})
def test_remote_symbol_maps_the_ibkr_name_to_the_feed_name():
    assert remote_symbol(_Instrument("NIFTY50")) == "NIFTY"
    # An unmapped symbol passes through rather than silently becoming another
    # instrument's series.
    assert remote_symbol(_Instrument("BANKNIFTY")) == "BANKNIFTY"


@override_settings(REMOTE_FEATURE_DB_URL="")
def test_remote_features_are_optional():
    assert remote_features_enabled() is False
    with pytest.raises(RemoteFeatureUnavailable):
        RemoteFeatureProvider()


@override_settings(REMOTE_FEATURE_DB_URL="postgresql://u:p@127.0.0.1:1/none",
                   REMOTE_FEATURE_CONNECT_TIMEOUT=1)
def test_an_unreachable_remote_source_raises_instead_of_scoring_neutral():
    """No silent fallback: entering on the neutral 50.0 when the real figure was
    meant to be available is the divergence this source exists to remove."""
    with pytest.raises(RemoteFeatureUnavailable):
        RemoteFeatureProvider().chain_features("NIFTY", before=date(2026, 9, 21))


def test_the_decision_day_is_the_exclusive_causal_bound():
    """The query asks for the latest session strictly before this day, so a
    Monday decision reads Friday rather than an empty Sunday."""
    assert decision_day(date(2026, 9, 21)) == date(2026, 9, 21)


def _observation(**overrides):
    base = {
        "rps_base": 66.0, "expected_move": 120.0, "event_risk": 0.0, "pir": 1.25,
        "event_expiry_gap_sessions": 1.0, "realized_volatility_20": 0.0768,
        "data_quality_ok": True, "event_risk_data_available": False,
    }
    return {**base, **overrides}


def _signal(**overrides):
    # `current_iv` is read off the live chain's ATM surface on every real
    # evaluation, with or without a remote feature source.
    base = {"enabled": True, "available": True, "current_iv": 0.1004,
            "upper_breach_probability": 0.2, "lower_breach_probability": 0.1}
    return {**base, **overrides}


def test_measured_iv_rank_is_used_and_keeps_the_candidate_measurable():
    market = selection_market(
        _observation(iv_rank_252=12.93, atm_iv=0.1004), _signal(), capital=100000,
    )
    assert market.iv_rank == pytest.approx(12.93)
    # IV rank is a required field in the source's quality test; once it is
    # measured it must count towards that test rather than be excused from it.
    assert market.data_quality_ok is True


def test_a_signal_reading_still_wins_over_the_session_figure():
    market = selection_market(
        _observation(iv_rank_252=12.93), _signal(iv_rank=40.0), capital=100000,
    )
    assert market.iv_rank == pytest.approx(40.0)


def test_without_a_remote_source_iv_rank_is_excused_not_failed():
    """The runtime keeps no IV history of its own, so an absent iv_rank must not
    fail GATE_DATA for every candidate; C1_IVRANK drops out of TQS instead."""
    market = selection_market(_observation(), _signal(), capital=100000)
    assert market.iv_rank == 0
    assert market.data_quality_ok is True


def test_a_genuinely_incomplete_market_still_fails_the_quality_test():
    market = selection_market(
        _observation(iv_rank_252=12.93, expected_move=None), _signal(), capital=100000,
    )
    assert market.data_quality_ok is False
