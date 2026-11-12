from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.utils import timezone

from apps.market_data.ibkr_options import IBKROptionChainClient, IBKROptionDataUnavailable
from apps.strategies.framework import create_instance
from apps.strategies.models import StrategyInstance
from tests.test_option_strategy_execution import _portfolio, _underlying

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("mode", ["paper", "live"])
def test_option_endpoint_derives_mode_and_resolves_nifty_as_index(client, monkeypatch, mode):
    from apps.strategies import framework
    portfolio = _portfolio()
    portfolio.gateway_session.mode = mode
    portfolio.gateway_session.save()
    seen = {}
    original = framework.resolve_instrument

    def resolve(**kwargs):
        seen.update(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(framework, "resolve_instrument", resolve)
    result = client.post("/api/v1/options/strategy-instances/", {
        "name": "NIFTY automatic options", "portfolio_id": portfolio.pk,
        "ticker": "NIFTY", "qualify": False,
    }, content_type="application/json")
    assert result.status_code == 201, result.json()
    row = result.json()["data"]
    assert row["execution_mode"] == mode.upper()
    assert seen["asset_class"] == "IND"
    assert seen["ticker"] == "NIFTY50"
    assert seen["exchange"] == "NSE" and seen["currency"] == "INR"
    assert StrategyInstance.objects.get(pk=row["id"]).definition.key == "RPS_OPTIONS_AUTO_SELECTOR"


@pytest.mark.parametrize("extra", [{"execution_mode": "LIVE"}, {"definition_key": "SMA_CROSSOVER"}, {"activate": "yes"}])
def test_option_endpoint_rejects_invalid_requests_without_creating_strategy(client, extra):
    portfolio = _portfolio()
    result = client.post("/api/v1/options/strategy-instances/", {
        "name": "Invalid", "portfolio_id": portfolio.pk, "ticker": "NIFTY", "qualify": False, **extra,
    }, content_type="application/json")
    assert result.status_code == 400
    assert not StrategyInstance.objects.exists()


def test_options_list_does_not_include_stocks(client):
    from apps.instruments.models import Instrument
    portfolio = _portfolio()
    stock = Instrument.objects.create(symbol="AAPL", asset_class="STK")
    item, _ = create_instance(name="Stock", definition_key="SMA_CROSSOVER", portfolio=portfolio,
        instrument_id=stock.pk, timeframe="1m", parameters={}, target_configuration={}, qualify=False)
    assert client.get("/api/v1/options/strategy-instances/").json()["data"] == []
    assert client.get(f"/api/v1/options/strategy-instances/{item.pk}/").status_code == 404


def test_option_quotes_reject_wrong_gateway_mode():
    gateway = SimpleNamespace(option_quotes=lambda contracts: {"mode": "live", "rows": []})
    client = IBKROptionChainClient(SimpleNamespace(mode="paper"), gateway=gateway)
    with pytest.raises(IBKROptionDataUnavailable, match="mode"):
        client._quotes([])


def _quote_rows(mode, age, data_type):
    gateway = SimpleNamespace(option_quotes=lambda contracts: {"mode": mode, "rows": [{
        "observed_at": (timezone.now() - timedelta(seconds=age)).isoformat(), "market_data_type": data_type,
    }]})
    return IBKROptionChainClient(SimpleNamespace(mode=mode), gateway=gateway)._quotes([])


# A live session may act only on real time; frozen and delayed-frozen are a
# closed book, and delayed is not what a live account is reading.
@pytest.mark.parametrize("age,data_type", [(60, 1), (0, 2), (0, 3), (0, 4), (-60, 1)])
def test_stale_frozen_delayed_and_future_quotes_cannot_enter_live(age, data_type):
    assert _quote_rows("live", age, data_type) == []


# A paper session reads the delayed feed, so real time is not what it asked for
# and the frozen variants remain refused.
@pytest.mark.parametrize("data_type", [1, 2, 4])
def test_paper_refuses_every_tick_type_but_delayed(data_type):
    assert _quote_rows("paper", 0, data_type) == []


def test_paper_accepts_delayed_quotes_within_the_delay_allowance():
    assert len(_quote_rows("paper", 600, 3)) == 1


def test_paper_still_rejects_delayed_quotes_beyond_the_allowance():
    assert _quote_rows("paper", settings.OPTION_QUOTE_DELAYED_MAX_AGE_SECONDS + 60, 3) == []


def test_paper_rejects_future_dated_delayed_quotes():
    assert _quote_rows("paper", -60, 3) == []


def test_live_accepts_realtime_quotes():
    assert len(_quote_rows("live", 5, 1)) == 1


# A refused underlying quote must say which check refused it; the bare message
# gave no way to tell a tick-type mismatch from a stale or zone-less stamp.
def test_missing_underlying_quote_names_why_the_quote_was_refused():
    gateway = SimpleNamespace(option_quotes=lambda contracts: {"mode": "paper", "rows": [{
        "conid": 51497778, "observed_at": timezone.now().isoformat(), "market_data_type": 1, "last": 25000,
    }]})
    client = IBKROptionChainClient(SimpleNamespace(mode="paper"), gateway=gateway)
    underlying = SimpleNamespace(broker_contract=SimpleNamespace(conid=51497778), asset_class="IND",
        exchange="NSE", currency="INR")
    with pytest.raises(IBKROptionDataUnavailable,
            match="conid 51497778: IBKR tick type 1, the session is set to delayed data and requires 3; .*answer yes"):
        client.chain(underlying, {}, date.today())


# A subscribed paper login is served real time even when it asks for delayed,
# so declaring the subscription is what lets its quotes through.
def test_paper_session_declared_live_accepts_realtime_and_refuses_delayed():
    def rows(data_type):
        gateway = SimpleNamespace(option_quotes=lambda contracts: {"mode": "paper", "rows": [{
            "observed_at": timezone.now().isoformat(), "market_data_type": data_type,
        }]})
        session = SimpleNamespace(mode="paper", market_data_type="LIVE")
        return IBKROptionChainClient(session, gateway=gateway)._quotes([])
    assert len(rows(1)) == 1
    assert rows(3) == []


def test_ibkr_chain_keeps_exact_multiplier_exchange_and_observed_greeks():
    underlying = _underlying()
    expiry = date.today() + timedelta(days=7)
    requests = []

    def quotes(contracts):
        requests.append(contracts)
        common = {"observed_at": timezone.now().isoformat(), "market_data_type": 3}
        rows = [{**common, "last": 22000}] if contracts[0]["asset_class"] == "IND" else [{
            **contract, **common, "conid": 1000 + index, "bid": 99, "ask": 101,
            "iv": 0.2, "delta": 0.5 if contract["right"] == "C" else -0.5,
            "oi": 5000, "volume": 100, "min_size": 1, "size_increment": 1,
        } for index, contract in enumerate(contracts)]
        return {"mode": "paper", "rows": rows}

    client = IBKROptionChainClient(SimpleNamespace(mode="paper"), gateway=SimpleNamespace(option_quotes=quotes))
    client.definitions = {"chains": [{"exchange": "NSE", "trading_class": "NIFTY", "multiplier": "65",
        "expirations": [expiry.isoformat()], "strikes": [str(x) for x in range(20000, 24050, 50)]}]}
    chain = client.chain(underlying, {}, expiry)
    assert len(requests[1]) == 42
    assert {row["multiplier"] for row in requests[1]} == {"65"}
    assert chain["rows"][0]["iv"] == Decimal("0.2")
    assert chain["rows"][0]["chain_definition"]["exchange"] == "NSE"
    assert chain["provider"] == "IBKR"


def test_ibkr_missing_open_interest_does_not_fabricate_data():
    underlying = _underlying()
    expiry = date.today() + timedelta(days=7)

    def quotes(contracts):
        return {"mode": "paper", "rows": [{**row, "last": 22000, "bid": 99, "ask": 101,
            "conid": 100, "volume": 10, "oi": None,
            "observed_at": timezone.now().isoformat(), "market_data_type": 3} for row in contracts]}

    client = IBKROptionChainClient(SimpleNamespace(mode="paper"), gateway=SimpleNamespace(option_quotes=quotes))
    client.definitions = {"chains": [{"exchange": "NSE", "trading_class": "NIFTY", "multiplier": "65",
        "expirations": [expiry.isoformat()], "strikes": ["22000"]}]}
    with pytest.raises(IBKROptionDataUnavailable, match="permissions"):
        client.chain(underlying, {}, expiry)


def _daily_history(underlying, now, sessions=120):
    from apps.market_streams.models import MarketBar
    price = 22000.0
    for index in range(sessions, 0, -1):
        end = now - timedelta(days=index)
        price += 40.0 if index % 2 else -35.0
        MarketBar.objects.create(
            instrument=underlying, interval="1d", bar_id=f"daily-{index}",
            window_start=end - timedelta(days=1), window_end=end, produced_at=end,
            open=price - 10, high=price + 30, low=price - 30, close=price,
            volume=1_000_000, is_final=True, provider="IBKR",
        )


def _observation(underlying, now, spot, rows):
    from apps.strategies.option_analytics import rps as rps_engine
    features = rps_engine.daily_features(
        list(
            __import__("apps.market_streams.models", fromlist=["MarketBar"]).MarketBar.objects
            .filter(instrument=underlying, interval="1d").order_by("window_end")
            .values("window_end", "open", "high", "low", "close", "volume")
        )
    )
    return rps_engine.build_observation(
        features=features, session_date=now.date(), spot=float(spot),
        chain_rows=rows, parameters={},
    )


def _fixed_now():
    from datetime import timezone as _tz
    return datetime(2026, 9, 1, 10, 0, 0, tzinfo=_tz.utc)


def _chain_rows(spot, expiry, *, iv=Decimal("0.20"), step=100, span=14):
    atm = round(float(spot) / step) * step
    rows = []
    for index in range(-span, span + 1):
        strike = Decimal(atm + index * step)
        for right in ("C", "P"):
            intrinsic = max(float(spot) - float(strike), 0) if right == "C" else max(float(strike) - float(spot), 0)
            price = Decimal(str(round(intrinsic + 120 * 0.85 ** abs(index) + 5, 2)))
            rows.append({
                "strike": strike, "right": right, "price": price,
                "bid": price - 1, "ask": price + 1, "oi": 5000 - 200 * abs(index),
                "volume": 500, "iv": iv,
                "delta": Decimal("0.25") if right == "C" else Decimal("-0.25"),
                "expiration": expiry, "lot_size": "65",
                "chain_definition": {"multiplier": "1"},
            })
    return rows


def test_source_selector_sizes_on_full_order_lots_and_reports_unmeasured_inputs():
    from apps.strategies.option_analytics.runtime import rank_candidates
    portfolio, underlying = _portfolio(), _underlying()
    portfolio.account.net_liquidation = 1000000
    portfolio.account.save()
    # `rps_threshold` is one knob: it is the standalone RPS floor and the entry
    # signal's own `min_rps`. Lowered here so this test measures sizing and the
    # hard gates rather than re-testing the floor.
    instance, _ = create_instance(name="Source selector", definition_key="RPS_OPTIONS_AUTO_SELECTOR",
        portfolio=portfolio, instrument_id=underlying.pk, timeframe="1d",
        parameters={"rps_threshold": 10}, target_configuration={}, qualify=False)
    # Pinned: the breach simulation is seeded from this timestamp.
    now = _fixed_now()
    expiry = now.date() + timedelta(days=7)
    _daily_history(underlying, now)
    rows = _chain_rows(22000, expiry)
    observation = _observation(underlying, now, 22000, rows)
    legs = [
        {**next(r for r in rows if r["right"] == "P" and r["strike"] == 21000), "side": "BUY", "role": "long_put", "ratio": 1},
        {**next(r for r in rows if r["right"] == "P" and r["strike"] == 21100), "side": "SELL", "role": "short_put", "ratio": 1},
    ]
    ranked, evidence = rank_candidates(
        instance, {"PUT_CREDIT_SPREAD": legs}, spot=Decimal(22000), observation=observation,
        chains={expiry: rows}, observed_at=now, expiration=expiry,
    )
    # One exchange lot is 65 broker units, and the cost is charged per leg per lot.
    assert evidence["rankings"][0]["rejection_reason"] is None, evidence["rankings"][0]
    assert evidence["rankings"][0]["units"] == 65
    assert evidence["rankings"][0]["round_trip_cost"] == 80
    # Nothing in this runtime measures IV rank or event risk, and both are named
    # rather than silently scored.
    assert "iv_rank" in evidence["unavailable_inputs"]
    assert "event_risk" in evidence["unavailable_inputs"]

    # Ten lots multiplies the loss by ten and must hit the absolute loss ceiling.
    instance.parameters["lots"] = 10
    ranked, evidence = rank_candidates(
        instance, {"PUT_CREDIT_SPREAD": legs}, spot=Decimal(22000), observation=observation,
        chains={expiry: rows}, observed_at=now, expiration=expiry,
    )
    assert not ranked
    assert "GATE_MAXLOSS" in evidence["rankings"][0]["gate_failures"]


def test_a_two_sided_structure_is_gated_on_its_combined_breach_not_each_side():
    """The reference's central breach rule: at expiry the two sides are mutually
    exclusive, so their union is their sum. Judging each side against its own
    ceiling would hand the structure one full budget per side."""
    from apps.strategies.option_analytics.entry_signal import DecisionEntrySignal, atm_surface
    from apps.strategies.option_analytics.payoff import profit_boundaries
    from apps.strategies.option_analytics.runtime import _payoff_legs, _strike_roles
    underlying = _underlying()
    # The simulation is seeded from the decision timestamp, so it is pinned here:
    # a moving `now` would reseed the draw on every run and make a borderline
    # probability flap rather than reproduce.
    now = _fixed_now()
    expiry = now.date() + timedelta(days=21)
    rows = _chain_rows(22000, expiry)
    observation = {
        "rps_base": 75.0, "support": 21800.0, "resistance": 22200.0, "hv20": 0.12,
        "data_quality_ok": True,
    }
    surface = atm_surface({expiry: rows}, Decimal(22000))
    by = {(row["right"], row["strike"]): row for row in rows}

    def evaluate(schema):
        legs = [{**by[(right, Decimal(22000 + offset))], "role": role, "side": side, "ratio": 1}
                for role, side, right, offset in schema]
        lower, upper = profit_boundaries(_payoff_legs(legs, 65), 0.0, 22000.0)
        candidate = {**observation, **{f"{k}_strike": v for k, v in _strike_roles(legs).items()},
                     "breakeven_lower": lower, "breakeven_upper": upper}
        return DecisionEntrySignal({}).evaluate(
            spot=Decimal(22000), observed_at=now, surface=surface, chains={expiry: rows},
            observation=candidate, expiry=expiry,
        )

    butterfly = evaluate([("long_put", "BUY", "P", -200), ("short_put", "SELL", "P", 0),
                          ("short_call", "SELL", "C", 0), ("long_call", "BUY", "C", 200)])
    assert butterfly["upper_breach_probability"] < 0.5
    assert butterfly["lower_breach_probability"] < 0.5
    # Each side alone would pass a 0.50 ceiling; their union is what is gated.
    assert butterfly["either_breach_probability"] == pytest.approx(
        butterfly["upper_breach_probability"] + butterfly["lower_breach_probability"], abs=1e-6
    )
    assert butterfly["either_breach_probability"] > 0.5
    assert butterfly["entry_allowed"] is False

    # A single-sided spread has no second side and is judged on its own, against
    # its own ceiling -- never the unrelated opposite one.
    spread = evaluate([("long_put", "BUY", "P", -900), ("short_put", "SELL", "P", -800)])
    assert spread["upper_breach_probability"] is None
    assert spread["either_breach_probability"] == spread["lower_breach_probability"]
    assert spread["breach_boundary_basis"] == "BREAKEVEN"


def test_the_breach_simulation_is_deterministic_and_measures_settlement():
    """Same inputs reproduce exactly, and a mid-life excursion is not a breach."""
    from apps.strategies.option_analytics.entry_signal import breach_probabilities
    now = _fixed_now()
    expiry = now.date() + timedelta(days=30)
    call = lambda cache=None: breach_probabilities(
        spot=22000.0, observed_at=now, observation={}, expiry=expiry, volatility=0.20,
        paths=10000, steps=50, short_put_strike=21000.0, short_call_strike=23000.0,
        drift=0.056, path_cache=cache,
    )
    first, second = call(), call()
    assert first["upper_breach_probability"] == second["upper_breach_probability"]
    assert first["breach_probability_method"] == "DETERMINISTIC_GBM_TERMINAL_CONTRACT_IV"
    assert first["breach_boundary_basis"] == "SHORT_STRIKE"
    # An at-the-money short settles in the money about half the time -- it is not
    # a certainty, which is what a barrier-touch measure would have said.
    atm = breach_probabilities(
        spot=22000.0, observed_at=now, observation={}, expiry=expiry, volatility=0.20,
        paths=20000, steps=50, short_call_strike=22000.0, drift=0.0,
    )
    assert 0.45 < atm["upper_breach_probability"] < 0.55
    assert atm["moneyness"]["short_call"]["position"] == "ATM"


def test_nse_contract_quantity_uses_broker_lot_size_separately_from_multiplier():
    from apps.instruments.services import qualify_option_contract
    from apps.strategies.framework import evaluate_instance
    from apps.strategies.models import StrategyTarget
    portfolio, underlying = _portfolio(), _underlying()
    expiry = timezone.now().date() + timedelta(days=7)

    def qualify(payload, key):
        return {**payload, "conid": 911876549, "local_symbol": "NIFTY-TEST-CALL",
            "asset_class": "OPT", "qualified": True,
            "min_size": 65, "size_increment": 65, "min_tick": 0.05}

    option, _, _ = qualify_option_contract(underlying_instrument=underlying,
        expiration=expiry.isoformat(), strike="25000", right="C", multiplier="1",
        trading_class="NIFTY", exchange="NSE",
        gateway=SimpleNamespace(qualify_contract_exact=qualify))
    assert option.lot_size == 65 and option.multiplier == 1
    assert option.min_tick == Decimal("0.05")
    instance, _ = create_instance(name="NSE lots", definition_key="RPS_OPTIONS_AUTO_SELECTOR",
        portfolio=portfolio, instrument_id=underlying.pk, timeframe="1d", parameters={"lots": 2},
        target_configuration={}, qualify=False)
    instance.enabled = True
    instance.state = "FLAT"
    instance.save()
    evaluate_instance(instance, bar={"bar_id": "nse-lot-entry", "window_end": timezone.now().isoformat(),
        "is_final": True, "option_context": {"status": "READY", "selected_strategy": "PUT_CREDIT_SPREAD",
            "entry_score": "80", "reason": "Fixture decision", "legs": [{"instrument_id": option.pk,
                "role": "long_call", "side": "BUY", "ratio": 1, "expiration": expiry.isoformat()}]}},
        indicators={"weekly_atr": 1000, "weekly_close": 25000}, event_id="nse-lot-entry")
    assert StrategyTarget.objects.get(strategy_instance=instance).target_quantity == 130


def test_options_creation_waits_for_underlying_qualification():
    from apps.instruments.models import Instrument
    portfolio = _portfolio()
    underlying = Instrument.objects.create(symbol="NIFTY50", asset_class="IND", exchange="NSE", currency="INR")
    observed = []

    class Gateway:
        def qualify_contract(self, payload, key):
            observed.append("queued")
            return {"command_id": 1, "status": "PENDING"}

        def wait_for_command(self, queued, *, operation):
            assert operation == "QUALIFY" and queued["command_id"] == 1
            observed.append("completed")
            return {"conid": 51497778, "symbol": "NIFTY50", "asset_class": "IND", "currency": "INR"}

    instance, _ = create_instance(name="Qualified option underlying", definition_key="RPS_OPTIONS_AUTO_SELECTOR",
        portfolio=portfolio, instrument_id=underlying.pk, timeframe="1d", parameters={},
        target_configuration={}, gateway=Gateway())
    assert observed == ["queued", "completed"]
    assert instance.instrument.broker_contract.conid == 51497778
    assert instance.block_reason == ""
