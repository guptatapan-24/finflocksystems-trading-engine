from datetime import date, datetime, timedelta, timezone as datetime_timezone
from decimal import Decimal

import pytest
import requests
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.utils import timezone

from apps.accounts.models import BrokerAccount
from apps.allocation.models import RebalancePolicy
from apps.instruments.models import BrokerContract, Instrument, OptionContract
from apps.market_streams.models import InstrumentMarketState
from apps.market_data.dhan_options import DhanOptionChainClient, DhanOptionsTemporaryError
from apps.oms.models import Order, OrderIntent
from apps.oms.services import apply_execution
from apps.portfolios.models import CashLedgerEntry, PortfolioPosition, PositionLedgerEntry, TradingPortfolio
from apps.rebalancing.coordinator import build_portfolio_target_snapshot
from apps.rebalancing.services import plan_rebalance
from apps.risk.models import CapitalReservation
from apps.risk.services import evaluate_intent
from apps.strategies.framework import create_instance, evaluate_instance
from apps.strategies.models import OptionSelectionSnapshot, StrategyAttributedPosition, StrategyDefinition
from apps.strategies.option_analytics import rps as rps_engine
from apps.strategies.option_execution import (
    prepare_option_context, select_template_legs, strike_ladder,
)
from apps.strategies.option_templates import OPTION_TEMPLATES
from apps.strategies.plugins import get_plugin
from streaming.flink.jobs.processing import compute_indicator
from tests.managed_gateway import bind_gateway_mode


pytestmark = pytest.mark.django_db
D = Decimal


def _portfolio():
    account = BrokerAccount.objects.create(
        account_id="DU-OPTIONS", net_liquidation=100000,
        available_cash=100000, buying_power=200000, base_currency="INR",
    )
    portfolio = TradingPortfolio.objects.create(name="Automatic options", account=account)
    bind_gateway_mode(portfolio)
    return portfolio


def _underlying():
    item = Instrument.objects.create(
        symbol="NIFTY", asset_class="IND", exchange="NSE", currency="INR",
        trading_calendar="XNSE",
    )
    BrokerContract.objects.create(
        instrument=item, conid=910000, primary_exchange="NSE", local_symbol="NIFTY",
    )
    return item


def _option(underlying, expiration, strike, right, index):
    item = Instrument.objects.create(
        symbol=f"NIFTY-{expiration:%Y%m%d}-{strike}-{right}", asset_class="OPT",
        exchange="NFO", currency="INR", multiplier=50, lot_size=1,
        fractional_support=False, trading_calendar="XNSE",
    )
    BrokerContract.objects.create(
        instrument=item, conid=920000 + index, primary_exchange="NFO",
        local_symbol=item.symbol,
    )
    OptionContract.objects.create(
        instrument=item, underlying=underlying, underlying_conid=910000,
        expiration=expiration, strike=strike, right=right,
        trading_class="NIFTY", multiplier=50,
    )
    return item


def test_all_source_option_templates_and_auto_selector_are_seeded():
    expected = {*OPTION_TEMPLATES, "RPS_OPTIONS_AUTO_SELECTOR"}
    definitions = StrategyDefinition.objects.filter(key__in=expected)
    assert definitions.count() == 9
    assert set(definitions.values_list("key", flat=True)) == expected
    assert all(get_plugin(item).execution_model == "MULTI_LEG_QUANTITY" for item in definitions)


def test_full_strategy_seed_command_is_idempotent():
    call_command("seed_strategy_definitions", verbosity=0)
    call_command("seed_strategy_definitions", verbosity=0)
    assert StrategyDefinition.objects.count() == 14
    assert StrategyDefinition.objects.filter(key__in={*OPTION_TEMPLATES, "RPS_OPTIONS_AUTO_SELECTOR"}).count() == 9


def test_completed_weekly_boundary_excludes_the_current_partial_week():
    start = datetime(2026, 1, 5, tzinfo=datetime_timezone.utc)
    bars = []
    for index in range(105):
        stamp = start + timedelta(days=index)
        if stamp.weekday() >= 5:
            continue
        close = D(100 + index)
        bars.append({
            "window_end": stamp.isoformat(), "open": str(close - 1),
            "high": str(close + 2), "low": str(close - 2), "close": str(close),
            "volume": "100", "is_final": True,
        })
    weekly_close = compute_indicator(bars, "weekly_close", {"window": 14})
    weekly_atr = compute_indicator(bars, "weekly_atr", {"window": 14})
    current_week = datetime.fromisoformat(bars[-1]["window_end"]).date().isocalendar()[:2]
    expected_close = D(next(
        row["close"] for row in reversed(bars)
        if datetime.fromisoformat(row["window_end"]).date().isocalendar()[:2] != current_week
    ))
    assert weekly_close == expected_close
    assert weekly_atr is not None and weekly_atr > 0


def test_rps_weights_match_the_source_and_missing_factors_renormalize():
    assert sum(rps_engine.WEIGHTS.values()) == pytest.approx(100.0)
    assert rps_engine.WEIGHT_UNITS == {
        "distance_sr": 18, "oi_structure": 17, "expected_move": 15, "volume_pressure": 10,
        "atr_realized_vol": 10, "trend_adx": 7, "bollinger_compression": 4, "mean_reversion": 4,
    }
    assert rps_engine.ATR_MULTIPLIER == 0.75 and rps_engine.OI_TOP_K == 5

    features = rps_engine.daily_features(_range_bound_daily_bars(120))
    session = features.index[-1].date() + timedelta(days=1)
    spot = float(features["close"].iloc[-1])
    rows = _flat_chain(spot, date(2026, 9, 24))
    observation = rps_engine.build_observation(
        features=features, session_date=session, spot=spot, chain_rows=rows, parameters={},
    )
    # The ATR envelope is the prior completed *daily* session's Wilder ATR14,
    # never a weekly bar, and the OI walls are an independent factor that must
    # not have moved it.
    reference = float(features["close"].iloc[-1])
    atr = float(features["atr14"].iloc[-1])
    assert observation["atr_support"] == pytest.approx(reference - 0.75 * atr, rel=1e-9)
    assert observation["atr_resistance"] == pytest.approx(reference + 0.75 * atr, rel=1e-9)
    # Every factor this fixture can measure is present, and the score is the
    # frozen weights renormalised over exactly those.
    available = {
        name: value for name, value in observation["component_scores"].items() if value is not None
    }
    expected_weight = sum(rps_engine.WEIGHTS[name] for name in available)
    assert observation["available_weight"] == pytest.approx(expected_weight)
    assert observation["rps_base"] == pytest.approx(
        sum(rps_engine.WEIGHTS[name] * value for name, value in available.items()) / expected_weight
    )
    # A chain with no open interest leaves `oi_structure` unmeasured; the score
    # renormalises over the rest instead of scoring a fabricated neutral.
    blank = rps_engine.build_observation(
        features=features, session_date=session, spot=spot,
        chain_rows=[{**row, "oi": 0} for row in rows], parameters={},
    )
    assert blank["oi_support"] is None and blank["component_scores"]["oi_structure"] is None
    assert "OI_FACTOR_UNAVAILABLE" in blank["flags"]
    assert blank["available_weight"] == pytest.approx(
        expected_weight - rps_engine.WEIGHTS["oi_structure"]
    )


def test_open_interest_never_moves_a_strike_and_offsets_match_the_source():
    """Strikes come from the ATM ladder, not from where the OI wall sits."""
    expiration = date(2026, 9, 24)
    spot = D(22000)
    heavy = _flat_chain(float(spot), expiration, oi_peak_strike=21500)
    light = _flat_chain(float(spot), expiration, oi_peak_strike=22500)
    parameters = {"minimum_open_interest": 100, "minimum_volume": 1, "target_delta": D("-0.20"), "strike_width": D(100)}

    def condor(rows):
        return select_template_legs(
            "IRON_CONDOR", {"main": {"rows": rows, "expiration": expiration}},
            parameters, spot=spot,
        )

    heavy_legs, light_legs = condor(heavy), condor(light)
    assert [leg["role"] for leg in heavy_legs] == ["long_put", "short_put", "short_call", "long_call"]
    # Moving the entire open-interest mass 1000 points must not move one strike.
    assert [leg["strike"] for leg in heavy_legs] == [leg["strike"] for leg in light_legs]
    ladder, atm = strike_ladder(heavy, "P", spot)
    assert ladder[atm]["strike"] == spot
    # The source's offsets: shorts +/-2 from ATM, wings one strike beyond.
    assert [leg["strike"] for leg in heavy_legs] == [
        spot - 300, spot - 200, spot + 200, spot + 300,
    ]
    butterfly = select_template_legs(
        "IRON_BUTTERFLY", {"main": {"rows": heavy, "expiration": expiration}},
        parameters, spot=spot,
    )
    # Both shorts on the ATM strike, wings at +/-2 -- what makes it a butterfly.
    assert [leg["strike"] for leg in butterfly] == [spot - 200, spot, spot, spot + 200]
    spread = select_template_legs(
        "PUT_CREDIT_SPREAD", {"main": {"rows": heavy, "expiration": expiration}},
        parameters, spot=spot,
    )
    assert [leg["strike"] for leg in spread] == [spot - 200, spot - 100]


def _range_bound_daily_bars(count, *, start=datetime(2026, 1, 5, tzinfo=datetime_timezone.utc)):
    bars, price = [], 22000.0
    for index in range(count):
        price += 40.0 if index % 2 else -35.0
        bars.append({
            "window_end": start + timedelta(days=index), "open": price - 10,
            "high": price + 30, "low": price - 30, "close": price, "volume": 1_000_000,
        })
    return bars


def _flat_chain(spot, expiration, *, oi_peak_strike=None, step=100, iv=0.18, dte=21):
    """A chain priced on the same forward the engines assume.

    Black prices off `F = S*exp((r-q)T)` with no discounting, because
    `calculate_pop` compares undiscounted settlement payoffs against that same
    forward. A chain priced on spot instead would hand every structure a
    drift-sized P&L that has nothing to do with the volatility it sells.
    """
    import math

    years = dte / 365.25
    forward = float(spot) * math.exp((0.068 - 0.012) * years)
    normal = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))

    def black(right, strike):
        d1 = (math.log(forward / strike) + 0.5 * iv * iv * years) / (iv * math.sqrt(years))
        d2 = d1 - iv * math.sqrt(years)
        value = (
            forward * normal(d1) - strike * normal(d2) if right == "C"
            else strike * normal(-d2) - forward * normal(-d1)
        )
        return max(value, 0.05), (normal(d1) if right == "C" else normal(d1) - 1)

    atm = round(float(spot) / step) * step
    peak = oi_peak_strike if oi_peak_strike is not None else atm
    rows = []
    for index in range(-8, 9):
        strike = D(str(atm + index * step))
        for right in ("C", "P"):
            price, delta = black(right, float(strike))
            price = D(str(round(price, 2)))
            rows.append({
                "strike": strike, "right": right, "price": price,
                "bid": price - D("0.50"), "ask": price + D("0.50"),
                "oi": 5000 - int(abs(D(str(peak)) - strike)), "volume": 500,
                "delta": D(str(round(delta, 4))),
                "iv": D(str(iv)), "expiration": expiration, "lot_size": "1",
                "chain_definition": {"multiplier": "50"},
            })
    return rows


def test_dhan_option_client_retries_rate_limit_payload_and_transport_errors(settings):
    settings.DHAN_OPTIONS_ENABLED = True
    settings.DHAN_CLIENT_ID = "client"
    settings.DHAN_ACCESS_TOKEN = "token"
    settings.DHAN_MAX_ATTEMPTS = 3
    settings.DHAN_RETRY_BASE_DELAY_SECONDS = 1
    settings.DHAN_RETRY_MAX_DELAY_SECONDS = 8

    class Response:
        def __init__(self, status_code, payload, headers=None):
            self.status_code = status_code
            self.payload = payload
            self.headers = headers or {}
            self.ok = 200 <= status_code < 300

        def json(self):
            return self.payload

    class Http:
        def __init__(self):
            self.responses = [
                requests.ConnectionError("temporary transport failure"),
                Response(200, {"status": "failure", "remarks": {"error_code": "DH-904"}}, {"Retry-After": "2"}),
                Response(200, {"data": ["2026-09-24"]}),
            ]

        def post(self, *args, **kwargs):
            result = self.responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

    delays = []
    client = DhanOptionChainClient(http=Http(), sleeper=delays.append)
    assert client.expiries(_underlying(), {}) == [date(2026, 9, 24)]
    assert delays == [1, 2]


def test_dhan_option_client_exhaustion_remains_retryable(settings):
    settings.DHAN_OPTIONS_ENABLED = True
    settings.DHAN_CLIENT_ID = "client"
    settings.DHAN_ACCESS_TOKEN = "token"
    settings.DHAN_MAX_ATTEMPTS = 2
    settings.DHAN_RETRY_BASE_DELAY_SECONDS = 0
    settings.DHAN_RETRY_MAX_DELAY_SECONDS = 0

    class Http:
        def post(self, *args, **kwargs):
            raise requests.ConnectionError("still unavailable")

    with pytest.raises(DhanOptionsTemporaryError, match="after 2 attempts") as raised:
        DhanOptionChainClient(http=Http(), sleeper=lambda delay: None).expiries(_underlying(), {})
    assert raised.value.retryable


def test_dhan_option_client_parses_nested_chain_and_normalizes_iv(settings):
    settings.DHAN_OPTIONS_ENABLED = True
    settings.DHAN_CLIENT_ID = "client"
    settings.DHAN_ACCESS_TOKEN = "token"
    settings.DHAN_MAX_ATTEMPTS = 1
    expiration = date(2026, 9, 24)

    class Response:
        status_code = 200
        ok = True
        headers = {}

        def json(self):
            return {"data": {"data": {"last_price": 22000, "oc": {"22000.000000": {
                "ce": {
                    "security_id": 777, "top_bid_price": 99, "top_ask_price": 101,
                    "last_price": 100, "implied_volatility": 20, "oi": 1200,
                    "volume": 300, "greeks": {"delta": 0.5},
                },
                "pe": {"last_price": 100, "oi": 1200, "volume": 300},
            }}}}}

    class Http:
        def post(self, *args, **kwargs):
            return Response()

    chain = DhanOptionChainClient(http=Http()).chain(_underlying(), {}, expiration)
    assert chain["spot"] == D(22000)
    assert len(chain["rows"]) == 1
    assert chain["rows"][0]["security_id"] == "777"
    assert chain["rows"][0]["price"] == D(100)
    assert chain["rows"][0]["iv"] == D("0.2")


def test_the_legacy_dhan_provider_cannot_authorize_an_approved_entry(monkeypatch):
    """Dhan quotes cannot run the approved engines, so they must not enter at all.

    The approved entry gate needs per-contract IV, executable bid/ask and Greeks
    observed at one timestamp, plus the contract multiplier and exchange lot
    size that size the payoff. Dhan supplies neither of the last two. Entering
    on a different, weaker rule instead is precisely the divergence this port
    exists to remove, so the legacy provider is refused and says why.
    """
    portfolio, underlying = _portfolio(), _underlying()
    expiration = date(2026, 9, 24)
    instance, _ = create_instance(
        name="NIFTY legacy provider", definition_key="PUT_CREDIT_SPREAD",
        portfolio=portfolio, instrument_id=underlying.pk, timeframe="1d",
        parameters={"lots": 1}, target_configuration={"target_weight": 0},
        execution_mode="PAPER", qualify=False,
    )
    version = instance.versions.get(version=instance.version)
    rows = [
        {"security_id": str(index), "strike": row["strike"], "right": row["right"],
         "price": row["price"], "bid": row["bid"], "ask": row["ask"], "oi": row["oi"],
         "volume": row["volume"], "delta": row["delta"], "iv": row["iv"],
         "expiration": expiration}
        for index, row in enumerate(_flat_chain(22000, expiration))
    ]

    class Dhan:
        def expiries(self, instrument, parameters):
            return [expiration]

        def chain(self, instrument, parameters, requested_expiration):
            return {"spot": D(22000), "expiration": expiration,
                    "observed_at": timezone.now(), "rows": rows}

    _daily_bars(underlying, datetime(2026, 9, 1, 10, tzinfo=datetime_timezone.utc))
    context = prepare_option_context(
        instance, version, {"bar_id": "legacy-dhan", "version": 1,
                            "window_end": "2026-09-01T10:00:00+00:00"},
        {}, dhan_client=Dhan(), gateway=None,
    )
    assert context["status"] == "UNSUPPORTED_PROVIDER"
    assert "IBKR" in context["reason"]
    assert not OptionSelectionSnapshot.objects.filter(strategy_instance=instance).exists()


def _daily_bars(underlying, now, sessions=140):
    from apps.market_streams.models import MarketBar
    price = 22000.0
    for index in range(sessions, 0, -1):
        end = now - timedelta(days=index)
        price += 30.0 if index % 2 else -30.0
        MarketBar.objects.create(
            instrument=underlying, interval="1d", bar_id=f"rps-daily-{index}",
            window_start=end - timedelta(days=1), window_end=end, produced_at=end,
            open=price - 5, high=price + 25, low=price - 25, close=price,
            volume=1_000_000, is_final=True, provider="IBKR",
        )


def _approved_entry(monkeypatch, settings):
    """An RPS auto-selector strategy whose IBKR chain always yields an entry."""
    settings.OPTION_CHAIN_PROVIDER = "IBKR"
    portfolio, underlying = _portfolio(), _underlying()
    portfolio.account.net_liquidation = 1000000
    portfolio.account.save()
    now = datetime(2026, 9, 1, 10, tzinfo=datetime_timezone.utc)
    expiration = (now + timedelta(days=21)).date()
    _daily_bars(underlying, now)
    instance, _ = create_instance(
        name="NIFTY approved entry", definition_key="RPS_OPTIONS_AUTO_SELECTOR",
        portfolio=portfolio, instrument_id=underlying.pk, timeframe="1d",
        parameters={"lots": 1, "rps_threshold": 10}, target_configuration={"target_weight": 0},
        execution_mode="PAPER", qualify=False,
    )
    version = instance.versions.get(version=instance.version)
    rows = _flat_chain(22000, expiration, step=100)
    for row in rows:
        row["chain_definition"] = {
            "exchange": "NSE", "trading_class": "NIFTY", "multiplier": "50",
            "expirations": [expiration.isoformat()],
            "strikes": [str(item["strike"]) for item in rows],
        }

    class Chain:
        provider = "IBKR"
        definitions = {"chains": [rows[0]["chain_definition"]]}

        def expiries(self, instrument, parameters):
            return [expiration]

        def chain(self, instrument, parameters, requested):
            return {"provider": "IBKR", "spot": D(22000), "expiration": expiration,
                    "observed_at": now, "rows": rows}

    qualified = []

    def qualify_option_contract(**payload):
        qualified.append(payload)
        strike = int(D(payload["strike"]))
        # Like the real qualification, a contract seen before resolves to its instrument.
        item = Instrument.objects.filter(
            symbol=f"NIFTY-{expiration:%Y%m%d}-{strike}-{payload['right']}",
        ).first() or _option(underlying, expiration, strike, payload["right"], len(qualified))
        return item, item.broker_contract, item.option_contract

    monkeypatch.setattr("apps.strategies.option_execution.IBKROptionChainClient",
                        lambda session, gateway=None: Chain())
    monkeypatch.setattr("apps.strategies.option_execution.qualify_option_contract", qualify_option_contract)
    monkeypatch.setattr("apps.strategies.option_execution._ensure_option_leases", lambda *args: None)
    return portfolio, instance, version, now


def test_prepare_option_context_runs_the_full_approved_entry_and_persists_its_evidence(monkeypatch, settings):
    _, instance, version, now = _approved_entry(monkeypatch, settings)

    context = prepare_option_context(
        instance, version,
        {"bar_id": "approved-entry", "version": 1, "window_end": now.isoformat()}, {},
    )
    snapshot = OptionSelectionSnapshot.objects.get(strategy_instance=instance)
    assert context["status"] in {"READY", "WAITING_FOR_OPTION_QUOTES"}
    observation = snapshot.source_snapshot["rps_observation"]
    # The boundary is the prior completed daily session's 0.75x Wilder ATR14,
    # and every RPS factor is recorded with the weights it was scored against.
    assert snapshot.model_version.startswith("rps-v2-framework")
    assert "daily-atr075-oi-top5" in snapshot.model_version
    assert snapshot.source_snapshot["atr_multiplier"] == "0.75"
    assert set(observation["component_scores"]) == set(rps_engine.WEIGHTS)
    assert snapshot.source_snapshot["decision_order"][0] == "RPS_FLOOR"
    assert snapshot.source_snapshot["decision_order"][1] == "APPROVED_RANGE_IV_BREACH_ENTRY"
    # The winner carries the approved selector's own verdict, and the entry
    # signal it was gated on is stored next to it.
    evidence = snapshot.source_snapshot["selection_evidence"]
    assert evidence["model"] == "aryan-options-selection-v2"
    assert evidence["entry_signal"]["entry_allowed"] is True
    assert evidence["entry_signal"]["breach_probability_method"].startswith("DETERMINISTIC_GBM_TERMINAL")
    assert snapshot.selected_strategy in {
        "PUT_CREDIT_SPREAD", "CALL_CREDIT_SPREAD", "IRON_CONDOR", "IRON_BUTTERFLY",
    }
    # The soft-exit baseline is fixed at entry and handed to the plugin.
    baseline = snapshot.source_snapshot["exit_baseline"]
    assert baseline["original_low"] == observation["atr_support"]
    assert baseline["original_high"] == observation["atr_resistance"]
    assert baseline["peak_pnl"] == 0.0
    assert context["state_data"]["exit_baseline"] == baseline


def test_option_plugin_emits_multi_leg_entry_and_only_strategy_or_expiry_exit():
    portfolio, underlying = _portfolio(), _underlying()
    expiration = date(2026, 9, 4)
    option_instruments = [
        _option(underlying, expiration, 21800, "P", 1),
        _option(underlying, expiration, 21900, "P", 2),
        _option(underlying, expiration, 22100, "C", 3),
        _option(underlying, expiration, 22200, "C", 4),
    ]
    instance, _ = create_instance(
        name="NIFTY automatic condor", definition_key="IRON_CONDOR",
        portfolio=portfolio, instrument_id=underlying.pk, timeframe="1d",
        parameters={"lots": 2}, target_configuration={"target_weight": 0},
        execution_mode="PAPER", qualify=False,
    )
    instance.enabled = True
    instance.state = "FLAT"
    instance.save(update_fields=["enabled", "state", "updated_at"])
    legs = [
        {"instrument_id": item.pk, "role": role, "side": side, "ratio": 1,
         "right": item.option_contract.right, "strike": str(item.option_contract.strike),
         "expiration": expiration.isoformat(), "multiplier": "50"}
        for item, role, side in zip(
            option_instruments,
            ("long_put", "short_put", "short_call", "long_call"),
            ("BUY", "SELL", "SELL", "BUY"),
        )
    ]
    entry = evaluate_instance(
        instance,
        bar={"bar_id": "option-entry", "window_end": "2026-09-01T10:00:00+00:00", "is_final": True,
             "option_context": {"status": "READY", "selected_strategy": "IRON_CONDOR",
                                "entry_score": "75", "reason": "ATR/OI gate passed", "legs": legs}},
        indicators={"weekly_atr": "1000", "weekly_close": "22000"}, event_id="option-entry",
    )
    assert entry.targets.count() == 4
    assert sorted(entry.targets.values_list("target_quantity", flat=True)) == [D(-2), D(-2), D(2), D(2)]
    instance.refresh_from_db()
    for target in entry.targets.all():
        StrategyAttributedPosition.objects.create(
            strategy_instance=instance, portfolio=portfolio,
            instrument=target.instrument, quantity=target.target_quantity,
        )
    hold = evaluate_instance(
        instance,
        bar={"bar_id": "option-hold", "window_end": "2026-09-03T10:00:00+00:00", "is_final": True},
        indicators={"weekly_atr": "1000", "weekly_close": "22000"}, event_id="option-hold",
    )
    assert hold.targets.count() == 4
    assert sorted(hold.targets.values_list("target_quantity", flat=True)) == [D(-2), D(-2), D(2), D(2)]
    expiry_exit = evaluate_instance(
        instance,
        bar={"bar_id": "option-expiry", "window_end": "2026-09-04T10:00:00+00:00", "is_final": True},
        indicators={"weekly_atr": "1000", "weekly_close": "22000"}, event_id="option-expiry",
    )
    assert expiry_exit.targets.count() == 4
    assert set(expiry_exit.targets.values_list("target_quantity", flat=True)) == {D(0)}


def test_a_refused_entry_shows_the_operator_which_gate_and_on_what_number(client):
    """Refusal is the normal outcome, so the detail endpoint must answer "why is
    nothing trading" for the bar just evaluated: the RPS score and its floor,
    the IV-score floor, and each candidate's refusal. A later, earlier-stage
    refusal must replace that evidence rather than leave it looking current."""
    portfolio, underlying = _portfolio(), _underlying()
    instance, _ = create_instance(
        name="NIFTY refused entry", definition_key="RPS_OPTIONS_AUTO_SELECTOR",
        portfolio=portfolio, instrument_id=underlying.pk, timeframe="1d",
        parameters={"lots": 1, "rps_threshold": 60, "rps_iv_score_threshold": 60},
        target_configuration={"target_weight": 0}, execution_mode="PAPER", qualify=False,
    )
    instance.enabled = True
    instance.state = "FLAT"
    instance.save(update_fields=["enabled", "state", "updated_at"])
    evidence = {
        "reason": "No structure passed the approved range/IV/breach entry gate",
        "rankings": [{"strategy_key": "PUT_CREDIT_SPREAD", "eligible": False,
                      "rejection_reason": "Approved range/IV/breach entry blocked: IV score 42.23 below 60.00"}],
    }
    refused = evaluate_instance(
        instance,
        bar={"bar_id": "refused", "window_end": "2026-09-01T10:00:00+00:00", "is_final": True,
             "option_context": {
                 "status": "NO_ELIGIBLE_STRUCTURE", "reason": evidence["reason"],
                 "entry_score": "71.5", "entry_threshold": "60",
                 "rps_observation": {"spot": 22000, "atr_support": 21800, "atr_resistance": 22200,
                                     "component_scores": {"distance_sr": 80.0}},
                 "selection_evidence": evidence,
                 "state_data": {"selection_evidence": evidence},
             }},
        indicators={}, event_id="refused",
    )
    assert refused.signal.signal_type == "NO_ACTION"
    option = client.get(f"/api/v1/strategy-instances/{instance.pk}/").json()["data"]["option_execution"]
    check = option["last_entry_check"]
    assert check["status"] == "NO_ELIGIBLE_STRUCTURE"
    assert check["evaluated_at"] == "2026-09-01T10:00:00+00:00"
    assert (option["entry_score"], check["entry_threshold"], check["iv_score_threshold"]) == ("71.5", "60", "60")
    assert option["entry_score_components"] == {"distance_sr": 80.0}
    assert option["atr_support"] == 21800
    assert option["selection_evidence"]["rankings"][0]["rejection_reason"].endswith("below 60.00")

    evaluate_instance(
        instance,
        bar={"bar_id": "window", "window_end": "2026-09-02T03:40:00+00:00", "is_final": True,
             "option_context": {"status": "ENTRY_WINDOW_CLOSED", "reason": "RPS entry window has not opened"}},
        indicators={}, event_id="window",
    )
    option = client.get(f"/api/v1/strategy-instances/{instance.pk}/").json()["data"]["option_execution"]
    assert option["last_entry_check"]["status"] == "ENTRY_WINDOW_CLOSED"
    assert option["last_entry_check"]["entry_score"] is None
    assert option["entry_threshold"] == 60
    assert option["selection_evidence"] == {}


def _open_condor(spot, expiration, *, entry_premiums):
    """One short iron condor, as the exit policy sees it."""
    roles = (("P", spot - 300, 1), ("P", spot - 200, -1), ("C", spot + 200, -1), ("C", spot + 300, 1))
    return [
        {"instrument_id": index, "right": right, "strike": D(str(strike)),
         "quantity": D(sign), "multiplier": D(50), "average_cost": D(str(entry_premiums[index]))}
        for index, (right, strike, sign) in enumerate(roles)
    ]


def test_price_leaving_the_range_never_closes_a_position_but_banking_profit_does():
    """Hold-to-expiry is the premise. The three surviving soft exits bank an
    edge that already exists; none of them watches the range, and the removed
    HARD_RANGE_BREACH must not come back by another name."""
    from apps.strategies.option_analytics.dynamic_exit import DynamicExitController, entry_baseline

    spot, expiration = 22000, date(2026, 10, 29)
    session = date(2026, 10, 8)
    premiums = {0: 40, 1: 90, 2: 90, 3: 40}
    positions = _open_condor(spot, expiration, entry_premiums=premiums)
    observation = {"atr_support": 21800.0, "atr_resistance": 22200.0}
    baseline = entry_baseline(
        [{"volume": 500, "iv": 0.18} for _ in positions], observation=observation, spot=float(spot),
    )
    assert baseline["original_low"] == 21800.0 and baseline["original_high"] == 22200.0
    controller = DynamicExitController({})

    def evaluate(*, marks, spot_now, iv, volume, obs=None, state=None):
        return controller.evaluate(
            positions=positions,
            quotes={index: {"close": D(str(marks[index])), "iv": iv, "volume": volume}
                    for index in range(4)},
            baseline=dict(state or baseline), observation=obs or observation,
            spot=spot_now, session_date=session, expiry=expiration,
        )

    # Spot has broken far below the range and the structure is deep underwater.
    # The old hard rule closed here; the approved policy holds.
    breached = evaluate(
        marks={0: 120, 1: 420, 2: 1, 3: 1}, spot_now=21300, iv=0.20, volume=500,
        obs={"atr_support": 21200.0, "atr_resistance": 21600.0},
    )
    assert breached["decision"] == "HOLD"
    assert breached["reason_codes"] == ["NO_EXIT_TRIGGER"]
    # The range move is still reported -- it just does not close anything.
    assert breached["metrics"]["max_boundary_shift_pct"] > 0
    assert breached["metrics"]["current_range"] == [21200.0, 21600.0]

    # Most of the credit has decayed away: profit is captured, little is left to
    # earn, and holding no longer pays for the risk.
    banked = evaluate(marks={0: 1, 1: 6, 2: 6, 3: 1}, spot_now=22010, iv=0.10, volume=500)
    assert banked["decision"] == "SOFT_EXIT"
    assert "SOFT_PROFIT_CAPTURE_RISK_REWARD" in banked["reason_codes"]
    assert banked["metrics"]["profit_capture_pct"] >= 0.70

    # Volume and IV expanded together -- a regime change, not a price move.
    regime = evaluate(marks={0: 30, 1: 80, 2: 80, 3: 30}, spot_now=22010, iv=0.30, volume=900)
    assert regime["decision"] == "SOFT_EXIT"
    assert "SOFT_VOLUME_IV_EXPANSION" in regime["reason_codes"]
    # Either one alone is not enough while both are required.
    assert evaluate(marks={0: 30, 1: 80, 2: 80, 3: 30}, spot_now=22010, iv=0.30, volume=500)["decision"] == "HOLD"
    assert evaluate(marks={0: 30, 1: 80, 2: 80, 3: 30}, spot_now=22010, iv=0.18, volume=900)["decision"] == "HOLD"

    # The running peak is what the giveback rules measure against, so it is
    # carried forward rather than recomputed from the current mark.
    rich = evaluate(marks={0: 1, 1: 10, 2: 10, 3: 1}, spot_now=22010, iv=0.12, volume=500)
    assert rich["baseline"]["peak_pnl"] > 0
    assert DynamicExitController({"dynamic_exit_enabled": False}).evaluate(
        positions=positions, quotes={}, baseline=baseline, observation=observation,
        spot=22010, session_date=session, expiry=expiration,
    ) is None


def test_the_plugin_flattens_on_a_soft_exit_and_holds_otherwise():
    portfolio, underlying = _portfolio(), _underlying()
    expiration = date(2026, 10, 29)
    instruments = [
        _option(underlying, expiration, 21800, "P", 11),
        _option(underlying, expiration, 21900, "P", 12),
    ]
    instance, _ = create_instance(
        name="NIFTY soft exit", definition_key="PUT_CREDIT_SPREAD",
        portfolio=portfolio, instrument_id=underlying.pk, timeframe="1d",
        parameters={"lots": 1}, target_configuration={"target_weight": 0},
        execution_mode="PAPER", qualify=False,
    )
    instance.enabled, instance.state = True, "FLAT"
    legs = [
        {"instrument_id": item.pk, "role": role, "side": side, "ratio": 1,
         "right": item.option_contract.right, "strike": str(item.option_contract.strike),
         "expiration": expiration.isoformat(), "multiplier": "50",
         "target_quantity": str(D(1) if side == "BUY" else D(-1))}
        for item, role, side in zip(instruments, ("long_put", "short_put"), ("BUY", "SELL"))
    ]
    instance.state_data = {
        "selected_strategy": "PUT_CREDIT_SPREAD", "opened_at": "2026-10-01",
        "expiration": expiration.isoformat(), "legs": legs,
        "exit_baseline": {"original_low": 21800.0, "original_high": 22200.0,
                          "baseline_volume": 1000.0, "baseline_iv": 0.18, "peak_pnl": 0.0},
    }
    instance.save()
    for item, leg in zip(instruments, legs):
        StrategyAttributedPosition.objects.create(
            strategy_instance=instance, portfolio=portfolio, instrument=item,
            quantity=D(leg["target_quantity"]), average_cost=100,
        )

    holding = evaluate_instance(
        instance,
        bar={"bar_id": "soft-hold", "window_end": "2026-10-08T10:00:00+00:00", "is_final": True,
             "option_context": {"status": "HOLDING", "reason": "No soft-exit trigger fired",
                                "exit_decision": {"decision": "HOLD", "reason_codes": ["NO_EXIT_TRIGGER"],
                                                  "metrics": {"profit_capture_pct": 0.1}},
                                "exit_baseline": {**instance.state_data["exit_baseline"], "peak_pnl": 250.0}}},
        indicators={}, event_id="soft-hold",
    )
    assert holding.signal.signal_type == "HOLD_OPTIONS"
    assert set(holding.targets.values_list("target_quantity", flat=True)) == {D(1), D(-1)}
    instance.refresh_from_db()
    # The refreshed peak is kept even though nothing fired.
    assert instance.state_data["exit_baseline"]["peak_pnl"] == 250.0

    exiting = evaluate_instance(
        instance,
        bar={"bar_id": "soft-exit", "window_end": "2026-10-09T10:00:00+00:00", "is_final": True,
             "option_context": {"status": "SOFT_EXIT",
                                "reason": "Most profit is captured",
                                "exit_decision": {
                                    "decision": "SOFT_EXIT",
                                    "reason_codes": ["SOFT_PROFIT_CAPTURE_RISK_REWARD"],
                                    "metrics": {"profit_capture_pct": 0.82}},
                                "exit_baseline": instance.state_data["exit_baseline"]}},
        indicators={}, event_id="soft-exit",
    )
    assert exiting.signal.signal_type == "EXIT_OPTIONS"
    assert set(exiting.targets.values_list("target_quantity", flat=True)) == {D(0)}
    instance.refresh_from_db()
    assert instance.state_data["exit_reason_codes"] == ["SOFT_PROFIT_CAPTURE_RISK_REWARD"]
    assert instance.state_data["exit_metrics"]["profit_capture_pct"] == 0.82
    assert "SOFT_PROFIT_CAPTURE_RISK_REWARD" in instance.state_data["exit_reason"]


def test_option_fills_apply_contract_multiplier_to_cash_pnl_and_position_value(client):
    portfolio, underlying = _portfolio(), _underlying()
    contract = _option(underlying, date.today() + timedelta(days=20), 22000, "C", 9)
    buy_intent = OrderIntent.objects.create(
        portfolio=portfolio, instrument=contract, side="BUY", quantity=1,
        reference_price=100, eligible=True, idempotency_key="option-ledger-buy",
    )
    buy_order = Order.objects.create(
        intent=buy_intent, internal_id="option-ledger-buy", status="ACKNOWLEDGED", quantity=1,
    )
    apply_execution(buy_order, {
        "execution_id": "option-ledger-buy-fill", "quantity": "1", "price": "100",
        "commission": "1", "currency": "INR", "executed_at": timezone.now(),
    })
    position = PortfolioPosition.objects.get(portfolio=portfolio, instrument=contract)
    assert position.quantity == 1 and position.average_cost == 100
    assert CashLedgerEntry.objects.get(reference="option-ledger-buy-fill").amount == -5001
    position_payload = client.get(f"/api/v1/positions/?portfolio={portfolio.pk}").json()["data"][0]
    assert D(position_payload["market_value"]) == 5000

    sell_intent = OrderIntent.objects.create(
        portfolio=portfolio, instrument=contract, side="SELL", quantity=1,
        reference_price=120, eligible=True, idempotency_key="option-ledger-sell",
    )
    sell_order = Order.objects.create(
        intent=sell_intent, internal_id="option-ledger-sell", status="ACKNOWLEDGED", quantity=1,
    )
    apply_execution(sell_order, {
        "execution_id": "option-ledger-sell-fill", "quantity": "1", "price": "120",
        "commission": "1", "currency": "INR", "executed_at": timezone.now(),
    })
    position.refresh_from_db()
    assert position.quantity == 0 and position.realized_pnl == 1000
    assert CashLedgerEntry.objects.get(reference="option-ledger-sell-fill").amount == 5999
    assert PositionLedgerEntry.objects.get(reference="option-ledger-sell-fill").realized_pnl == 1000


def test_exact_option_quantities_reach_rebalance_with_hedge_first_staging():
    portfolio, underlying = _portfolio(), _underlying()
    expiration = date.today() + timedelta(days=20)
    long_put = _option(underlying, expiration, 21900, "P", 11)
    short_put = _option(underlying, expiration, 22000, "P", 12)
    instance, _ = create_instance(
        name="NIFTY put spread", definition_key="PUT_CREDIT_SPREAD",
        portfolio=portfolio, instrument_id=underlying.pk, timeframe="1d",
        parameters={"lots": 1}, target_configuration={"target_weight": 0},
        execution_mode="PAPER", qualify=False,
    )
    instance.enabled = True; instance.state = "FLAT"; instance.save(update_fields=["enabled", "state", "updated_at"])
    evaluate_instance(
        instance,
        bar={"bar_id":"spread-entry","window_end":timezone.now().isoformat(),"is_final":True,
             "option_context":{"status":"READY","selected_strategy":"PUT_CREDIT_SPREAD","entry_score":"80","reason":"ATR/OI gate passed","legs":[
                 {"instrument_id":long_put.pk,"role":"long_put","side":"BUY","ratio":1,"right":"P","strike":"21900","expiration":expiration.isoformat(),"multiplier":"50"},
                 {"instrument_id":short_put.pk,"role":"short_put","side":"SELL","ratio":1,"right":"P","strike":"22000","expiration":expiration.isoformat(),"multiplier":"50"},
             ]}},
        indicators={"weekly_atr":"1000","weekly_close":"22000"},event_id="spread-entry",
    )
    stock = Instrument.objects.create(
        symbol="RELIANCE", asset_class="STK", exchange="NSE", currency="INR",
        trading_calendar="XNSE",
    )
    BrokerContract.objects.create(
        instrument=stock, conid=940000, primary_exchange="NSE", local_symbol="RELIANCE",
    )
    stock_instance, _ = create_instance(
        name="RELIANCE fixed target", definition_key="FIXED_WEIGHT_REBALANCE",
        portfolio=portfolio, instrument_id=stock.pk, timeframe="5m",
        parameters={"direction": "LONG"}, target_configuration={"target_weight": "0.10"},
        execution_mode="PAPER", qualify=False,
    )
    stock_instance.enabled = True
    stock_instance.state = "FLAT"
    stock_instance.save(update_fields=["enabled", "state", "updated_at"])
    evaluate_instance(
        stock_instance,
        bar={"bar_id": "stock-target", "window_end": timezone.now().isoformat(), "is_final": True, "close": "100"},
        indicators={}, event_id="stock-target",
    )
    for item, price in ((long_put, 80), (short_put, 100)):
        InstrumentMarketState.objects.create(
            instrument=item,status="FRESH",reference_price=price,
            latest_event_at=timezone.now(),stale_after_seconds=3600,
        )
    InstrumentMarketState.objects.create(
        instrument=stock, status="FRESH", reference_price=100,
        latest_event_at=timezone.now(), stale_after_seconds=3600,
    )
    RebalancePolicy.objects.create(
        portfolio=portfolio,mode="PAPER",instrument_drift_threshold=0,
        minimum_trade_notional=1,fee_buffer=0,cash_buffer_percent=0,maximum_turnover=2,
    )
    snapshot = build_portfolio_target_snapshot(portfolio)
    assert snapshot.status == "READY"
    assert D(snapshot.net_target_quantities[str(long_put.pk)]) == 1
    assert D(snapshot.net_target_quantities[str(short_put.pk)]) == -1
    assert str(stock.pk) not in snapshot.net_target_quantities
    rebalance = plan_rebalance(
        portfolio,"STRATEGY_TARGETS","option-spread-rebalance",
        target_snapshot=snapshot,automatic=True,strict_market_state=False,
    )
    intents = {intent.instrument_id:intent for intent in OrderIntent.objects.filter(rebalance=rebalance)}
    assert rebalance.phase == "OPTION_HEDGES"
    assert intents[long_put.pk].side == "BUY" and intents[long_put.pk].eligible
    assert intents[short_put.pk].side == "SELL" and not intents[short_put.pk].eligible
    assert intents[stock.pk].side == "BUY" and intents[stock.pk].eligible

    PortfolioPosition.objects.create(
        portfolio=portfolio, instrument=long_put, quantity=1,
        average_cost=80, market_price=80,
    )
    StrategyAttributedPosition.objects.create(
        strategy_instance=instance, portfolio=portfolio,
        instrument=long_put, quantity=1, average_cost=80,
    )
    # Broker sync then reports the filled hedge. Position rows carry no contract
    # details, and must not erase the hedge's underlying the short is matched by.
    from apps.broker_gateway.sync import ensure_instrument
    ensure_instrument({
        "conid": long_put.broker_contract.conid, "symbol": "NIFTY", "local_symbol": long_put.symbol,
        "asset_class": "OPT", "exchange": "NFO", "primary_exchange": "", "currency": "INR",
        "expiration": expiration.strftime("%Y%m%d"), "strike": "21900", "right": "P",
        "multiplier": "50", "trading_class": "NIFTY", "underlying_conid": None,
    })
    short_intent = intents[short_put.pk]
    short_intent.eligible = True
    short_intent.save(update_fields=["eligible"])
    decision, approved, checks = evaluate_intent(
        short_intent,
        {"connected": True, "reconciled": True, "mode": "paper"},
    )
    assert decision == "APPROVED" and approved == 1
    assert any(check.check_name == "option_short_coverage" for check in checks)
    assert CapitalReservation.objects.filter(
        order_intent=short_intent,
        reference_type="OPTION_SHORT_COLLATERAL",
        amount=5000,
    ).exists()


def test_single_sided_option_structure_does_not_wait_for_a_missing_hedge_stage():
    portfolio, underlying = _portfolio(), _underlying()
    expiration = date.today() + timedelta(days=20)
    short_put = _option(underlying, expiration, 21000, "P", 21)
    instance, _ = create_instance(
        name="NIFTY cash secured put", definition_key="CASH_SECURED_PUT",
        portfolio=portfolio, instrument_id=underlying.pk, timeframe="1d",
        parameters={"lots": 1}, target_configuration={"target_weight": 0},
        execution_mode="PAPER", qualify=False,
    )
    instance.enabled = True
    instance.state = "FLAT"
    instance.save(update_fields=["enabled", "state", "updated_at"])
    evaluate_instance(
        instance,
        bar={
            "bar_id": "cash-put-entry", "window_end": timezone.now().isoformat(),
            "is_final": True,
            "option_context": {
                "status": "READY", "selected_strategy": "CASH_SECURED_PUT",
                "entry_score": "80", "reason": "ATR/OI gate passed",
                "legs": [{
                    "instrument_id": short_put.pk, "role": "short_put", "side": "SELL",
                    "ratio": 1, "right": "P", "strike": "21000",
                    "expiration": expiration.isoformat(), "multiplier": "50",
                }],
            },
        },
        indicators={"weekly_atr": "1000", "weekly_close": "22000"},
        event_id="cash-put-entry",
    )
    InstrumentMarketState.objects.create(
        instrument=short_put, status="FRESH", reference_price=100,
        latest_event_at=timezone.now(), stale_after_seconds=3600,
    )
    RebalancePolicy.objects.create(
        portfolio=portfolio, mode="PAPER", instrument_drift_threshold=0,
        minimum_trade_notional=1, fee_buffer=0, cash_buffer_percent=0,
        maximum_turnover=2,
    )
    snapshot = build_portfolio_target_snapshot(portfolio)
    rebalance = plan_rebalance(
        portfolio, "STRATEGY_TARGETS", "cash-put-rebalance",
        target_snapshot=snapshot, automatic=True, strict_market_state=False,
    )
    intent = OrderIntent.objects.get(rebalance=rebalance, instrument=short_put)
    assert rebalance.phase == "OPTION_SHORTS"
    assert intent.eligible


def test_the_locked_selection_is_reused_only_while_its_entry_is_in_progress(monkeypatch, settings):
    from apps.allocation.models import OrderIntentAttribution, RebalanceRun

    portfolio, instance, version, now = _approved_entry(monkeypatch, settings)

    def bar(name, minutes):
        return {"bar_id": name, "version": 1, "window_end": (now + timedelta(minutes=minutes)).isoformat()}

    first = prepare_option_context(instance, version, bar("entry-1", 0), {})
    instance.state_data = first["state_data"]
    instance.save(update_fields=["state_data"])
    run = RebalanceRun.objects.create(
        portfolio=portfolio, trigger="STRATEGY_TARGETS", idempotency_key="entry-1", status="EXECUTING",
    )
    leg = first["legs"][0]
    intent = OrderIntent.objects.create(
        portfolio=portfolio, instrument_id=leg["instrument_id"], side=leg["side"], quantity=1,
        idempotency_key="entry-1-leg", rebalance=run, origin=OrderIntent.Origin.REBALANCE,
        operation_status="PENDING",
    )
    OrderIntentAttribution.objects.create(order_intent=intent, strategy_instance=instance, target_delta=1)

    # A bar that arrives mid-entry keeps targeting the locked contracts.
    during = prepare_option_context(instance, version, bar("entry-2", 5), {})
    assert during["selection_snapshot_id"] == first["selection_snapshot_id"]

    # The entry ends with a leg rejected and the strategy flat: the next bar
    # selects from the current chain instead of re-entering the old snapshot.
    intent.operation_status = "RISK_REJECTED"
    intent.save(update_fields=["operation_status"])
    run.status, run.phase = "PARTIALLY_COMPLETED", "BLOCKED"
    run.save(update_fields=["status", "phase"])
    after = prepare_option_context(instance, version, bar("entry-3", 10), {})
    assert after["selection_snapshot_id"] != first["selection_snapshot_id"]
    assert OptionSelectionSnapshot.objects.get(pk=after["selection_snapshot_id"]).market_bar_id == "entry-3"
