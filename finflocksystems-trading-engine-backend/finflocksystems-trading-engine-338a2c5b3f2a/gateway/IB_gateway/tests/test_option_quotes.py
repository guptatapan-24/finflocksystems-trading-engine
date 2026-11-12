from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from django.test import override_settings

from broker.option_quotes import (
    DELAYED_TICK_TYPE, LIVE_TICK_TYPE, OptionQuotesMixin, number, option_tick_type,
)


def test_quote_numbers_reject_ibkr_missing_values():
    assert number(float("nan")) is None
    assert number(float("inf")) is None
    assert number(-1) is None
    assert number(0) == 0


def test_quote_readiness_requires_new_realtime_oi_and_greeks():
    start = datetime.now(timezone.utc)
    ticker = SimpleNamespace(
        time=start + timedelta(milliseconds=1), marketDataType=1,
        contract=SimpleNamespace(secType="OPT", right="P"),
        bid=10, ask=11, volume=12, putOpenInterest=100, modelGreeks=object(),
    )
    assert OptionQuotesMixin._option_quote_ready(ticker, start)
    ticker.putOpenInterest = float("nan")
    assert not OptionQuotesMixin._option_quote_ready(ticker, start)
    ticker.putOpenInterest = 100
    ticker.marketDataType = 3
    assert not OptionQuotesMixin._option_quote_ready(ticker, start)
    ticker.marketDataType = 1
    ticker.time = start - timedelta(seconds=1)
    assert not OptionQuotesMixin._option_quote_ready(ticker, start)


def test_quote_readiness_accepts_delayed_only_when_delayed_was_requested():
    start = datetime.now(timezone.utc)
    ticker = SimpleNamespace(
        time=start + timedelta(milliseconds=1), marketDataType=DELAYED_TICK_TYPE,
        contract=SimpleNamespace(secType="OPT", right="C"),
        bid=10, ask=11, volume=12, callOpenInterest=100, modelGreeks=object(),
    )
    assert OptionQuotesMixin._option_quote_ready(ticker, start, DELAYED_TICK_TYPE)
    # Real time is not a substitute for what a paper session asked for.
    assert not OptionQuotesMixin._option_quote_ready(ticker, start, LIVE_TICK_TYPE)
    # Delayed-frozen is a closed book and is refused in either mode.
    ticker.marketDataType = 4
    assert not OptionQuotesMixin._option_quote_ready(ticker, start, DELAYED_TICK_TYPE)
    ticker.marketDataType = 2
    assert not OptionQuotesMixin._option_quote_ready(ticker, start, LIVE_TICK_TYPE)


@override_settings(IBC_TRADING_MODE="paper", IBKR_MARKET_DATA_TYPE="DELAYED")
def test_paper_mode_resolves_delayed_tick_type():
    assert option_tick_type() == DELAYED_TICK_TYPE


@override_settings(IBC_TRADING_MODE="live", IBKR_MARKET_DATA_TYPE="LIVE")
def test_live_mode_resolves_realtime_tick_type():
    assert option_tick_type() == LIVE_TICK_TYPE


@override_settings(IBC_TRADING_MODE="paper", IBKR_MARKET_DATA_TYPE="LIVE")
def test_paper_mode_may_narrow_to_realtime_when_entitled():
    assert option_tick_type() == LIVE_TICK_TYPE


@override_settings(IBC_TRADING_MODE="live", IBKR_MARKET_DATA_TYPE="DELAYED")
def test_live_mode_refuses_delayed_option_data():
    with pytest.raises(RuntimeError, match="paper mode"):
        option_tick_type()


@pytest.mark.django_db
def test_quote_api_rejects_cross_mode_before_queueing(client):
    from gateway_service.models import GatewayCommand
    result = client.post("/api/v1/options/quotes/", {
        "mode": "live", "contracts": [{"conid": 1, "asset_class": "OPT"}],
    }, content_type="application/json", HTTP_AUTHORIZATION="Bearer test-token", HTTP_IDEMPOTENCY_KEY="wrong-mode")
    assert result.status_code == 400
    assert not GatewayCommand.objects.exists()


@pytest.mark.django_db
def test_quote_api_queues_bounded_exact_paper_contracts(client):
    from gateway_service.models import GatewayCommand
    payload = {"mode": "paper", "contracts": [{"conid": 1, "asset_class": "OPT"}]}
    result = client.post("/api/v1/options/quotes/", payload, content_type="application/json",
        HTTP_AUTHORIZATION="Bearer test-token", HTTP_IDEMPOTENCY_KEY="paper-quote")
    assert result.status_code == 202
    assert GatewayCommand.objects.get().command_type == "OPTION_QUOTES"
    payload["contracts"] *= 43
    result = client.post("/api/v1/options/quotes/", payload, content_type="application/json",
        HTTP_AUTHORIZATION="Bearer test-token", HTTP_IDEMPOTENCY_KEY="too-many")
    assert result.status_code == 400
