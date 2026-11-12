"""Fixtures shared by the Phase 2 tests: an AAPL SMA-crossover stock strategy and the NIFTY iron condor."""
from datetime import date, datetime, timedelta, timezone as datetime_timezone
from decimal import Decimal

from apps.accounts.models import BrokerAccount
from apps.allocation.models import OrderIntentAttribution
from apps.instruments.models import BrokerContract, Instrument, OptionContract
from apps.oms.models import Order, OrderIntent
from apps.oms.services import apply_execution
from apps.portfolios.models import TradingPortfolio
from apps.strategies.framework import create_instance
from apps.strategies.models import OptionSelectionSnapshot, StrategyRun, StrategySignal
from tests.managed_gateway import bind_gateway_mode

UTC = datetime_timezone.utc
D = Decimal
_counter = {"n": 0}


def at(days_ago=0, hours=0):
    return (datetime.now(UTC) - timedelta(days=days_ago)).replace(hour=14, minute=30, second=0, microsecond=0) \
        + timedelta(hours=hours)


def portfolio(currency="USD", account="DU-SECRET-ACCT"):
    acct = BrokerAccount.objects.create(account_id=account, net_liquidation=100000, available_cash=100000,
                                        buying_power=200000, base_currency=currency)
    item = TradingPortfolio.objects.create(name=f"P-{account}-{currency}", account=acct)
    bind_gateway_mode(item)
    return item


def aapl():
    item = Instrument.objects.create(symbol="AAPL", asset_class="STK", exchange="SMART", currency="USD")
    BrokerContract.objects.create(instrument=item, conid=265598, primary_exchange="NASDAQ", local_symbol="AAPL")
    return item


def stock_strategy(pf, instrument, name="AAPL SMA"):
    instance, _ = create_instance(
        name=name, definition_key="SMA_CROSSOVER", portfolio=pf, instrument_id=instrument.pk, timeframe="1d",
        parameters={"fast_window": 20, "slow_window": 50, "direction": "LONG"},
        target_configuration={"target_weight": "0.05"}, execution_mode="PAPER", qualify=False)
    instance.enabled = True
    instance.state = "FLAT"
    instance.save(update_fields=["enabled", "state", "updated_at"])
    return instance


def fill(pf, instrument, side, quantity, price, key, *, instance, when, commission="1", allocated=None,
         currency=None, reference=None, origin="STRATEGY", attribute=True, order_quantity=None):
    _counter["n"] += 1
    quantity = D(str(quantity))
    allocated = quantity if allocated is None else D(str(allocated))
    signed = allocated if side == "BUY" else -allocated
    intent = OrderIntent.objects.create(
        portfolio=pf, instrument=instrument, side=side, quantity=quantity, order_type="MKT",
        reference_price=D(str(reference if reference is not None else price)), eligible=True,
        idempotency_key=f"intent-{key}", origin=origin)
    OrderIntent.objects.filter(pk=intent.pk).update(created_at=when - timedelta(seconds=30))
    if attribute and instance is not None:
        OrderIntentAttribution.objects.create(order_intent=intent, strategy_instance=instance,
                                              strategy_version=instance.versions.first(), target_delta=signed,
                                              allocated_quantity=signed)
    order = Order.objects.create(intent=intent, internal_id=f"ord-{key}", status="ACKNOWLEDGED",
                                 quantity=order_quantity or quantity)
    execution = {"execution_id": f"exec-{key}", "quantity": str(quantity), "price": str(price),
                 "commission": str(commission), "currency": currency or instrument.currency, "executed_at": when}
    apply_execution(order, execution)
    return order, execution


def entry_signal_run(instance, when, *, snapshot=None, signal_type="ENTER_LONG", indicators=None, close="200"):
    option_context = {"selection_snapshot_id": snapshot.pk} if snapshot is not None else {}
    run = StrategyRun.objects.create(
        strategy_instance=instance, strategy_version=instance.versions.first(), input_hash=f"in-{when.isoformat()}",
        idempotency_key=f"run-{instance.pk}-{when.isoformat()}-{signal_type}", status="COMPLETED",
        context_snapshot={"bar": {"close": close, "option_context": option_context},
                          "indicators": indicators or {}, "previous_indicators": {}})
    StrategyRun.objects.filter(pk=run.pk).update(started_at=when - timedelta(minutes=5))
    StrategySignal.objects.create(run=run, strategy_instance=instance, strategy_version=instance.versions.first(),
                                  signal_type=signal_type, signal_time=when - timedelta(minutes=5),
                                  reason="test signal")
    return run


def exit_signal_run(instance, when, reason, signal_type="EXIT_OPTIONS", operator=False):
    context = {"operator_action": "FLATTEN"} if operator else {"bar": {}, "indicators": {}}
    run = StrategyRun.objects.create(
        strategy_instance=instance, strategy_version=instance.versions.first(), input_hash=f"x-{when.isoformat()}",
        idempotency_key=f"xrun-{instance.pk}-{when.isoformat()}", status="COMPLETED", context_snapshot=context)
    StrategyRun.objects.filter(pk=run.pk).update(started_at=when - timedelta(minutes=5))
    StrategySignal.objects.create(run=run, strategy_instance=instance, strategy_version=instance.versions.first(),
                                  signal_type=signal_type, signal_time=when - timedelta(minutes=5), reason=reason)
    return run


# ---- NIFTY iron condor ----------------------------------------------------------------------------------
def nifty_underlying():
    item = Instrument.objects.create(symbol="NIFTY", asset_class="IND", exchange="NSE", currency="INR",
                                     trading_calendar="XNSE")
    BrokerContract.objects.create(instrument=item, conid=810000, primary_exchange="NSE", local_symbol="NIFTY")
    return item


def nifty_option(underlying, strike, right, index, expiry):
    item = Instrument.objects.create(symbol=f"NIFTY-{strike}-{right}", asset_class="OPT", exchange="NFO",
                                     currency="INR", multiplier=1, lot_size=65, trading_calendar="XNSE")
    BrokerContract.objects.create(instrument=item, conid=820000 + index, primary_exchange="NFO",
                                  local_symbol=item.symbol)
    OptionContract.objects.create(instrument=item, underlying=underlying, underlying_conid=810000,
                                  expiration=expiry, strike=strike, right=right, trading_class="NIFTY", multiplier=1)
    return item


def condor_setup(expiry=None, currency="INR"):
    expiry = expiry or (date.today() + timedelta(days=7))
    pf = portfolio("INR", "DU-NIFTY-ACCT")
    underlying = nifty_underlying()
    instance, _ = create_instance(
        name="NIFTY condor", definition_key="RPS_OPTIONS_AUTO_SELECTOR", portfolio=pf,
        instrument_id=underlying.pk, timeframe="1d",
        parameters={"lots": 1, "rps_threshold": 60, "rps_iv_score_threshold": 60, "rps_tqs_threshold": 55},
        target_configuration={"target_weight": 0}, execution_mode="PAPER", qualify=False)
    instance.enabled = True
    instance.state = "FLAT"
    instance.save(update_fields=["enabled", "state", "updated_at"])
    legs = {
        "short_call": nifty_option(underlying, 24500, "C", 1, expiry),
        "long_call": nifty_option(underlying, 24600, "C", 2, expiry),
        "short_put": nifty_option(underlying, 22000, "P", 3, expiry),
        "long_put": nifty_option(underlying, 21900, "P", 4, expiry),
    }
    snapshot = OptionSelectionSnapshot.objects.create(
        strategy_instance=instance, strategy_version=instance.versions.first(), market_bar_id="bar-1",
        market_bar_version=1, observed_at=at(1), atr_reference=22500, atr_value=180, atr_support=22000,
        atr_resistance=24500, oi_support=22000, oi_resistance=24500, spot=22500, entry_score=61, expiration=expiry,
        selected_strategy="IRON_CONDOR",
        candidate_rankings=[{"strategy_key": "IRON_CONDOR", "eligible": True, "tqs": 58.0,
                             "tqs_factor_coverage": 0.8, "tqs_unmeasured_factors": ["C3_TERM", "B1_EVRISK"],
                             "decision_band": "WATCH", "size_multiplier": 0.5, "pop": 0.71,
                             "net_expected_pnl": 310.0, "max_profit": 1400, "max_loss": 1850,
                             "upper_breach_probability": 0.31, "lower_breach_probability": 0.12,
                             "entry_signal": {"iv_score": 64.0, "iv_rank": 42.0, "iv_percentile": 51.0}}],
        selected_legs=[], source_snapshot={"entry_threshold": "60", "rps_observation": {"spot": 22500}})
    return pf, underlying, instance, legs, snapshot, expiry
