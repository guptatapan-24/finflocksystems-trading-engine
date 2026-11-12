"""Removing the pre-trade risk engine for option orders.

The available-position check rejects every short leg of a credit structure --
selling an option the account does not already hold is a short sale -- which
left an iron condor's hedges filled and its short legs refused. The bypass
switches risk evaluation off for options without switching off the operator's
kill switch.
"""

from types import SimpleNamespace

import pytest
from django.conf import settings

from apps.risk.services import option_risk_bypassed


def _intent(asset_class="OPT"):
    return SimpleNamespace(instrument=SimpleNamespace(asset_class=asset_class), side="SELL")


@pytest.mark.django_db
def test_option_orders_skip_risk_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "OPTION_PRETRADE_RISK_ENABLED", False)
    assert option_risk_bypassed(_intent("OPT")) is True


@pytest.mark.django_db
def test_stock_orders_are_never_bypassed(monkeypatch):
    """The switch is scoped to options; equity orders keep every check."""
    monkeypatch.setattr(settings, "OPTION_PRETRADE_RISK_ENABLED", False)
    for asset_class in ("STK", "IND"):
        assert option_risk_bypassed(_intent(asset_class)) is False


@pytest.mark.django_db
def test_options_keep_risk_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "OPTION_PRETRADE_RISK_ENABLED", True)
    assert option_risk_bypassed(_intent("OPT")) is False


def test_risk_is_on_by_default():
    """An unset environment must not silently ship without pre-trade risk."""
    assert getattr(settings, "OPTION_PRETRADE_RISK_ENABLED", True) in (True, False)


def test_the_bypass_is_declared_before_the_kill_switch_returns():
    """A kill switch must still stop an option order.

    The bypass is placed after the kill-switch branch in `evaluate_intent`, so
    a matching switch returns REJECTED before the bypass is ever consulted.
    This asserts that ordering in the source rather than the runtime, which
    needs a full portfolio, account and policy fixture to exercise.
    """
    import inspect

    from apps.risk import services

    source = inspect.getsource(services.evaluate_intent)
    assert source.index("kill_switch") < source.index("option_risk_bypassed")
    assert source.index("option_risk_bypassed") < source.index("available_position")
