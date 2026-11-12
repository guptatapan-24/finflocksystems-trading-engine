from datetime import date, datetime, timedelta, timezone as datetime_timezone
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.accounts.models import BrokerAccount
from apps.allocation.models import OrderIntentAttribution
from apps.instruments.models import BrokerContract, Instrument, OptionContract
from apps.market_streams.models import InstrumentMarketState, MarketBar
from apps.oms.models import Order, OrderIntent
from apps.oms.services import apply_execution
from apps.portfolios.models import TradingPortfolio
from apps.strategies.framework import create_instance, evaluate_instance
from apps.strategies.models import OptionSelectionSnapshot, StrategyAttributedPosition
from tests.managed_gateway import bind_gateway_mode

pytestmark = pytest.mark.django_db
D = Decimal
UTC = datetime_timezone.utc
EXPIRY = date(2026, 9, 24)


def _portfolio():
    account = BrokerAccount.objects.create(
        account_id="DU-REPORTING", net_liquidation=100000,
        available_cash=100000, buying_power=200000, base_currency="INR",
    )
    portfolio = TradingPortfolio.objects.create(name="Reporting", account=account)
    bind_gateway_mode(portfolio)
    return portfolio


def _underlying():
    item = Instrument.objects.create(
        symbol="NIFTY", asset_class="IND", exchange="NSE", currency="INR", trading_calendar="XNSE",
    )
    BrokerContract.objects.create(instrument=item, conid=810000, primary_exchange="NSE", local_symbol="NIFTY")
    return item


def _option(underlying, strike, right, index):
    item = Instrument.objects.create(
        symbol=f"NIFTY-{strike}-{right}", asset_class="OPT", exchange="NFO", currency="INR",
        multiplier=50, lot_size=1, fractional_support=False, trading_calendar="XNSE",
    )
    BrokerContract.objects.create(instrument=item, conid=820000 + index, primary_exchange="NFO", local_symbol=item.symbol)
    OptionContract.objects.create(
        instrument=item, underlying=underlying, underlying_conid=810000, expiration=EXPIRY,
        strike=strike, right=right, trading_class="NIFTY", multiplier=50,
    )
    return item


def _auto_selector(portfolio, underlying, name="NIFTY ledger"):
    instance, _ = create_instance(
        name=name, definition_key="RPS_OPTIONS_AUTO_SELECTOR", portfolio=portfolio,
        instrument_id=underlying.pk, timeframe="1d",
        parameters={"lots": 1, "rps_threshold": 60, "rps_iv_score_threshold": 60, "rps_tqs_threshold": 65},
        target_configuration={"target_weight": 0}, execution_mode="PAPER", qualify=False,
    )
    instance.enabled = True
    instance.state = "FLAT"
    instance.save(update_fields=["enabled", "state", "updated_at"])
    return instance


def _fill(portfolio, instrument, side, quantity, price, key, *, attributions, when):
    intent = OrderIntent.objects.create(
        portfolio=portfolio, instrument=instrument, side=side, quantity=quantity,
        reference_price=price, eligible=True, idempotency_key=key,
    )
    for instance, allocated in attributions:
        OrderIntentAttribution.objects.create(
            order_intent=intent, strategy_instance=instance, target_delta=allocated, allocated_quantity=allocated,
        )
    order = Order.objects.create(intent=intent, internal_id=key, status="ACKNOWLEDGED", quantity=quantity)
    apply_execution(order, {
        "execution_id": f"{key}-fill", "quantity": str(quantity), "price": str(price),
        "commission": "2", "currency": "INR", "executed_at": when,
    })
    return order


def _mark(instrument, price):
    InstrumentMarketState.objects.update_or_create(instrument=instrument, defaults={
        "status": "FRESH", "reference_price": price, "latest_event_at": timezone.now(),
        "reference_price_source": "ibkr_live", "market_data_type": "LIVE",
    })


def test_ledger_explains_every_refusal_with_rps_iv_and_tqs(client):
    portfolio, underlying = _portfolio(), _underlying()
    instance = _auto_selector(portfolio, underlying)
    evaluate_instance(
        instance,
        bar={"bar_id": "window", "window_end": "2026-09-01T03:40:00+00:00", "close": "22000", "is_final": True,
             "option_context": {"status": "ENTRY_WINDOW_CLOSED", "reason": "RPS entry window has not opened"}},
        indicators={}, event_id="window",
    )
    evaluate_instance(
        instance,
        bar={"bar_id": "rps", "window_end": "2026-09-02T10:00:00+00:00", "close": "22010", "is_final": True,
             "option_context": {
                 "status": "ENTRY_GATE_CLOSED", "reason": "RPS 52.10 below 60",
                 "entry_score": "52.1", "entry_threshold": "60",
                 "rps_observation": {"spot": 22010, "atr_support": 21700, "atr_resistance": 22300},
             }},
        indicators={}, event_id="rps",
    )
    evidence = {
        "reason": "No structure passed the approved range/IV/breach entry gate or the causal bounded-loss, POP and EV checks",
        "rankings": [
            {"strategy_key": "IRON_CONDOR", "eligible": False, "tqs": 58.4,
             "rejection_reason": "Approved range/IV/breach entry blocked: IV score 48.00 below 60.00",
             "entry_signal": {"iv_score": 48.0, "iv_rank": 31.0, "iv_percentile": 40.0}},
            {"strategy_key": "PUT_CREDIT_SPREAD", "eligible": False,
             "rejection_reason": "minimum POP not met", "entry_signal": {"iv_score": 48.0}},
        ],
    }
    evaluate_instance(
        instance,
        bar={"bar_id": "iv", "window_end": "2026-09-03T10:00:00+00:00", "close": "22050", "is_final": True,
             "option_context": {
                 "status": "NO_ELIGIBLE_STRUCTURE", "reason": evidence["reason"],
                 "entry_score": "71.5", "entry_threshold": "60",
                 "rps_observation": {"spot": 22050, "atr_support": 21750, "atr_resistance": 22350},
                 "selection_evidence": evidence,
             }},
        indicators={}, event_id="iv",
    )
    body = client.get(f"/api/v1/strategy-instances/{instance.pk}/ledger/").json()
    assert body["ok"]
    iv_row, rps_row, window_row = body["data"]
    assert [row["decision"] for row in body["data"]] == ["NO_TRADE"] * 3

    assert window_row["status"] == "ENTRY_WINDOW_CLOSED" and window_row["gate"] == "Entry window"
    assert window_row["rps"] is None and window_row["atr_support"] is None

    assert rps_row["gate"] == "RPS floor" and rps_row["reason"] == "RPS 52.10 below 60"
    assert (rps_row["rps"], rps_row["rps_threshold"], rps_row["rps_passed"]) == ("52.1", "60", False)
    assert (rps_row["atr_support"], rps_row["atr_resistance"]) == (21700, 22300)

    assert iv_row["rps_passed"] is True
    assert (iv_row["iv_score"], iv_row["iv_score_threshold"], iv_row["iv_passed"]) == (48.0, "60", False)
    assert (iv_row["tqs"], iv_row["tqs_threshold"], iv_row["tqs_passed"]) == ("58.4", "65", False)
    assert iv_row["iv_rank"] == 31.0
    assert iv_row["reason"] == evidence["reason"]
    assert "IRON CONDOR: Approved range/IV/breach entry blocked: IV score 48.00 below 60.00" in iv_row["reason_details"]
    assert "PUT CREDIT SPREAD: minimum POP not met" in iv_row["reason_details"]
    assert [item["strategy_key"] for item in iv_row["candidates"]] == ["IRON_CONDOR", "PUT_CREDIT_SPREAD"]


def _enter_condor(portfolio, underlying, instance, legs_instruments, *, when):
    rankings = [{
        "strategy_key": "IRON_CONDOR", "eligible": True, "tqs": 72.5, "pop": 0.71, "expected_pnl": 1450.0,
        "entry_signal": {"iv_score": 68.0, "entry_allowed": True},
    }]
    legs = [
        {"instrument_id": item.pk, "conid": item.broker_contract.conid, "role": role, "side": side, "ratio": 1,
         "right": item.option_contract.right, "strike": str(item.option_contract.strike),
         "expiration": EXPIRY.isoformat(), "multiplier": "50"}
        for item, role, side in zip(
            legs_instruments, ("long_put", "short_put", "short_call", "long_call"), ("BUY", "SELL", "SELL", "BUY"),
        )
    ]
    snapshot = OptionSelectionSnapshot.objects.create(
        strategy_instance=instance, strategy_version=instance.versions.order_by("-version").first(),
        market_bar_id="entry", market_bar_version=1, observed_at=when, provider="IBKR",
        atr_reference=22000, atr_value=400, atr_support=21700, atr_resistance=22300,
        spot=22000, entry_score=D("74.2"), selected_strategy="IRON_CONDOR", expiration=EXPIRY,
        candidate_rankings=rankings, selected_legs=legs,
        source_snapshot={"rps_observation": {"spot": 22000, "atr_support": 21700, "atr_resistance": 22300},
                         "selection_evidence": {"rankings": rankings}, "entry_threshold": "60"},
    )
    return evaluate_instance(
        instance,
        bar={"bar_id": "entry", "window_end": when.isoformat(), "close": "22000", "is_final": True,
             "option_context": {"status": "READY", "selected_strategy": "IRON_CONDOR", "entry_score": "74.2",
                                "reason": "RPS 74.2 selected IRON_CONDOR", "selection_snapshot_id": snapshot.pk,
                                "legs": legs}},
        indicators={}, event_id="entry", event_time=when,
    )


def test_entry_row_says_why_the_trade_was_placed_and_the_pnl_is_strategy_scoped(client):
    portfolio, underlying = _portfolio(), _underlying()
    instance = _auto_selector(portfolio, underlying)
    other = _auto_selector(portfolio, underlying, name="Other strategy")
    long_put, short_put = _option(underlying, 21600, "P", 1), _option(underlying, 21700, "P", 2)
    short_call, long_call = _option(underlying, 22300, "C", 3), _option(underlying, 22400, "C", 4)
    entered_at = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
    entry = _enter_condor(portfolio, underlying, instance, [long_put, short_put, short_call, long_call], when=entered_at)
    assert entry.signal.signal_type == "ENTER_OPTIONS"

    fill_time = entered_at + timedelta(minutes=2)
    _fill(portfolio, long_put, "BUY", 1, 20, "lp", attributions=[(instance, D(1))], when=fill_time)
    # The short put order is shared: this strategy owns one of the two contracts.
    _fill(portfolio, short_put, "SELL", 2, 60, "sp", attributions=[(instance, D(-1)), (other, D(-1))], when=fill_time)
    _fill(portfolio, short_call, "SELL", 1, 55, "sc", attributions=[(instance, D(-1))], when=fill_time)
    _fill(portfolio, long_call, "BUY", 1, 18, "lc", attributions=[(instance, D(1))], when=fill_time)
    # A manual trade on the same contract must never leak into this strategy.
    _fill(portfolio, short_call, "BUY", 1, 10, "manual", attributions=[], when=fill_time)
    assert StrategyAttributedPosition.objects.get(strategy_instance=instance, instrument=short_put).quantity == -1

    for item, price in ((long_put, 12), (short_put, 40), (short_call, 50), (long_call, 15)):
        _mark(item, price)

    ledger = client.get(f"/api/v1/strategy-instances/{instance.pk}/ledger/").json()["data"]
    row = ledger[0]
    assert row["decision"] == "ENTERED" and row["selected_strategy"] == "IRON_CONDOR"
    assert (row["rps"], row["iv_score"], row["tqs"]) == ("74.2", 68.0, 72.5)
    assert (row["rps_passed"], row["iv_passed"], row["tqs_passed"]) == (True, True, True)
    assert row["reason"] == "Entered IRON CONDOR; RPS 74.20 ≥ 60.00; IV score 68.00 ≥ 60.00; TQS 72.50 ≥ 65.00"
    assert (row["atr_support"], row["atr_resistance"]) == (21700, 22300)
    assert {order["symbol"] for order in row["orders"]} == {"NIFTY-21600-P", "NIFTY-21700-P", "NIFTY-22300-C", "NIFTY-22400-C"}

    pnl = client.get(f"/api/v1/strategy-instances/{instance.pk}/pnl/").json()["data"]
    # Unrealized per 50-multiplier contract, this strategy's quantity only:
    # long put (12-20)*1, short put (40-60)*-1, short call (50-55)*-1, long call (15-18)*1.
    assert D(pnl["unrealized_pnl"]) == (D(-8) + D(20) + D(5) + D(-3)) * 50
    # Commission: 2 per fill, one half of the shared short-put fill.
    assert D(pnl["commissions"]) == D(7)
    assert D(pnl["running_pnl"]) == D(700) - D(7)
    assert pnl["open_position"] is True and pnl["marks_complete"] is True and pnl["fill_count"] == 4
    assert pnl["series"][-1]["mark_basis"] == "CURRENT_MARK"

    instance.refresh_from_db()
    evaluate_instance(
        instance,
        bar={"bar_id": "hold", "window_end": (entered_at + timedelta(hours=6)).isoformat(), "close": "22100", "is_final": True,
             "option_context": {"status": "HOLDING", "reason": "No soft-exit trigger fired"}},
        indicators={}, event_id="hold",
    )
    hold = client.get(f"/api/v1/strategy-instances/{instance.pk}/ledger/").json()["data"][0]
    assert hold["decision"] == "HOLDING" and hold["reason"] == "Holding IRON CONDOR: No soft-exit trigger fired"
    # A hold reports its own bar's close, and is explicit that the scores are the locked entry's.
    assert hold["spot"] == "22100" and hold["rps"] == "74.2000"
    assert "entry values this structure is locked to" in hold["reason_details"][0]

    exit_time = entered_at + timedelta(days=1)
    _fill(portfolio, short_call, "BUY", 1, 30, "sc-exit", attributions=[(instance, D(1))], when=exit_time)
    pnl = client.get(f"/api/v1/strategy-instances/{instance.pk}/pnl/").json()["data"]
    assert D(pnl["gross_realized_pnl"]) == (D(55) - D(30)) * 50
    short_call_leg = next(leg for leg in pnl["legs"] if leg["symbol"] == "NIFTY-22300-C")
    assert short_call_leg["open"] is False


def test_chart_shows_the_atr_boundary_and_only_entry_and_exit_points(client):
    portfolio, underlying = _portfolio(), _underlying()
    instance = _auto_selector(portfolio, underlying)
    legs = [_option(underlying, 21600, "P", 1), _option(underlying, 21700, "P", 2),
            _option(underlying, 22300, "C", 3), _option(underlying, 22400, "C", 4)]
    entered_at = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
    for index, when in enumerate((entered_at, entered_at + timedelta(days=1))):
        MarketBar.objects.create(
            instrument=underlying, bar_id=f"chart-{index}", interval="1d", window_start=when - timedelta(days=1),
            window_end=when, open=22000, high=22100, low=21900, close=22000, volume=1000,
            version=1, is_final=True, source_event_count=1, produced_at=when,
        )
    _enter_condor(portfolio, underlying, instance, legs, when=entered_at)

    body = client.get(f"/api/v1/strategy-instances/{instance.pk}/chart/").json()["data"]
    assert body["atr_bands"] == [{"time": "2026-09-04T10:00:00Z", "support": "21700", "resistance": "22300"}]
    assert [(marker["type"], marker["status"]) for marker in body["trade_markers"]] == [("ENTRY", "PENDING")]

    fill_time = entered_at + timedelta(minutes=5)
    for item, side, key in zip(legs, ("BUY", "SELL", "SELL", "BUY"), ("a", "b", "c", "d")):
        _fill(portfolio, item, side, 1, 20, key, attributions=[(instance, D(1 if side == "BUY" else -1))], when=fill_time)
    body = client.get(f"/api/v1/strategy-instances/{instance.pk}/chart/").json()["data"]
    assert [(marker["type"], marker["status"], marker["label"]) for marker in body["trade_markers"]] == [
        ("ENTRY", "FILLED", "Entry · 4 fills"),
    ]

    exit_time = entered_at + timedelta(days=1)
    for item, side, key in zip(legs, ("SELL", "BUY", "BUY", "SELL"), ("e", "f", "g", "h")):
        _fill(portfolio, item, side, 1, 10, key, attributions=[(instance, D(1 if side == "BUY" else -1))], when=exit_time)
    body = client.get(f"/api/v1/strategy-instances/{instance.pk}/chart/").json()["data"]
    assert [marker["type"] for marker in body["trade_markers"]] == ["ENTRY", "EXIT"]
    pnl = client.get(f"/api/v1/strategy-instances/{instance.pk}/pnl/").json()["data"]
    assert pnl["open_position"] is False and D(pnl["unrealized_pnl"]) == 0
    # Longs lost 10 each, shorts gained 10 each: flat gross, eight fills of fees.
    assert D(pnl["gross_realized_pnl"]) == 0 and D(pnl["running_pnl"]) == D(-16)
