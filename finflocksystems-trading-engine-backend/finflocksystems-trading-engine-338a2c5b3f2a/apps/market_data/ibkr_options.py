"""Option chain observations from the portfolio's configured IBKR session."""

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_CEILING

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.broker_gateway.client import GatewayClient
from apps.instruments.services import option_chain


class IBKROptionDataUnavailable(ValueError):
    retryable = True


#: IBKR tick types. 2 (frozen) and 4 (delayed-frozen) are last-known values from
#: a closed book, so neither is accepted in either mode.
LIVE_TICK_TYPE = 1
DELAYED_TICK_TYPE = 3


def expected_tick_type(session):
    """The only tick type this session may act on.

    The session's declared market-data entitlement is the decision, and its
    child requests exactly that type. IBKR serves real time to a subscribed
    login even when delayed is requested, so a paper login with subscriptions
    declares LIVE. Accepting the other type would authorize an entry on
    provenance the session never declared. Without a declaration the mode
    decides: paper reads delayed, live reads real time.
    """
    declared = getattr(session, "market_data_type", None)
    if declared:
        return LIVE_TICK_TYPE if declared == "LIVE" else DELAYED_TICK_TYPE
    return DELAYED_TICK_TYPE if str(session.mode).lower() == "paper" else LIVE_TICK_TYPE


def decimal_value(value, *, positive=False):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() and (result > 0 if positive else result >= 0) else None


class IBKROptionChainClient:
    provider = "IBKR"

    def __init__(self, session, *, gateway=None):
        self.session = session
        self.gateway = gateway or GatewayClient(session, require_commands=True)
        self.definitions = None

    def expiries(self, instrument, parameters):
        self.definitions = option_chain(underlying_instrument=instrument, gateway=self.gateway)
        return sorted({date.fromisoformat(expiry) for row in self.definitions["chains"] for expiry in row["expirations"]})

    def _quotes(self, contracts):
        result = self.gateway.option_quotes(contracts)
        if result.get("mode") != self.session.mode:
            raise IBKROptionDataUnavailable("IBKR quote mode differs from the portfolio gateway session")
        now = timezone.now()
        tick_type = expected_tick_type(self.session)
        max_age = (settings.OPTION_QUOTE_DELAYED_MAX_AGE_SECONDS
                   if tick_type == DELAYED_TICK_TYPE else settings.OPTION_QUOTE_MAX_AGE_SECONDS)
        rows = []
        # Why each row was discarded, so an empty result names its cause.
        self.rejected_quotes = []
        for row in result.get("rows", []):
            stamp = parse_datetime(str(row.get("observed_at") or ""))
            if not stamp or timezone.is_naive(stamp):
                self.rejected_quotes.append(f"conid {row.get('conid')}: observed_at {row.get('observed_at')!r} is missing or zone-less")
                continue
            age = (now - stamp).total_seconds()
            if not 0 <= age <= max_age:
                self.rejected_quotes.append(f"conid {row.get('conid')}: observed {age:.0f}s ago, allowed 0-{max_age}s")
                continue
            if row.get("market_data_type") != tick_type:
                declared = "real-time" if tick_type == LIVE_TICK_TYPE else "delayed"
                reason = (f"conid {row.get('conid')}: IBKR tick type {row.get('market_data_type')}, "
                    f"the session is set to {declared} data and requires {tick_type}")
                if row.get("market_data_type") == LIVE_TICK_TYPE:
                    # Real time arriving on a delayed session means the login is subscribed.
                    reason += "; the login has real-time subscriptions, so re-enter its credentials and answer yes"
                self.rejected_quotes.append(reason)
                continue
            rows.append(row)
        if not rows and result.get("errors"):
            errors = "; ".join(f"{item.get('error_code')}: {item.get('error_message')}" for item in result["errors"])
            raise IBKROptionDataUnavailable(f"IBKR option market data unavailable: {errors}")
        return rows

    def underlying_price(self, instrument):
        """The fresh real-time underlying quote, or None when none is available."""
        rows = self._quotes([{
            "conid": instrument.broker_contract.conid, "asset_class": instrument.asset_class,
            "exchange": instrument.exchange, "currency": instrument.currency,
        }])
        return decimal_value(rows[0].get("last"), positive=True) if rows else None

    def contract_quotes(self, contracts):
        """Fresh quotes for already-qualified contracts, keyed by conid.

        Used to mark an open structure for the soft-exit policy, so it asks for
        exact conids rather than re-deriving a chain. `close` is the midpoint --
        the mark a position is valued at, and the analogue of the candle close
        the source policy reads.
        """
        rows = self._quotes([{**contract, "asset_class": "OPT"} for contract in contracts])
        quotes = {}
        for row in rows:
            bid, ask = decimal_value(row.get("bid")), decimal_value(row.get("ask"), positive=True)
            if bid is None or ask is None or ask < bid or not row.get("conid"):
                continue
            # Keyed by string: a conid crosses the gateway as JSON and comes back
            # as either a number or a string, while the caller holds the stored
            # integer. One representation on both sides keeps the join exact.
            quotes[str(row["conid"])] = {
                "conid": str(row["conid"]), "bid": bid, "ask": ask, "close": (bid + ask) / 2,
                "iv": decimal_value(row.get("iv"), positive=True),
                "volume": decimal_value(row.get("volume")),
            }
        return quotes

    def chain(self, instrument, parameters, expiration):
        spot = self.underlying_price(instrument)
        if spot is None:
            detail = "; ".join(getattr(self, "rejected_quotes", [])) or "IBKR returned no positive last price"
            raise IBKROptionDataUnavailable(f"A fresh real-time IBKR underlying quote is required ({detail})")
        if self.definitions is None:
            self.expiries(instrument, parameters)
        chains = [row for row in self.definitions["chains"] if expiration.isoformat() in row["expirations"]]
        if not chains:
            raise IBKROptionDataUnavailable("IBKR has no chain for the requested expiry")
        requested_class = parameters.get("option_trading_class")
        if requested_class:
            chains = [row for row in chains if row["trading_class"] == requested_class]
        if not chains:
            raise IBKROptionDataUnavailable("IBKR has no chain for the requested trading class")
        chain = min(chains, key=lambda row: (row["trading_class"] != instrument.symbol, row["exchange"] != instrument.exchange, row["exchange"] != "SMART", row["trading_class"]))
        count = int(parameters.get("option_strike_count", 21))
        strikes = sorted(sorted(chain["strikes"], key=lambda value: abs(Decimal(value) - spot))[:count], key=Decimal)
        contracts = [{
            "symbol": instrument.symbol, "asset_class": "OPT", "currency": instrument.currency,
            "exchange": chain["exchange"], "trading_class": chain["trading_class"],
            "multiplier": chain["multiplier"], "expiration": expiration.isoformat(),
            "strike": strike, "right": right,
        } for strike in strikes for right in ("C", "P")]
        rows = []
        for row in self._quotes(contracts):
            bid, ask = decimal_value(row.get("bid")), decimal_value(row.get("ask"), positive=True)
            oi, volume = decimal_value(row.get("oi")), decimal_value(row.get("volume"))
            strike = decimal_value(row.get("strike"), positive=True)
            if bid is None or ask is None or ask < bid or oi is None or volume is None or strike is None:
                continue
            if row.get("right") not in {"C", "P"} or not row.get("conid") or strike not in {Decimal(value) for value in strikes}:
                continue
            if str(row.get("expiration", "")).replace("-", "")[:8] != expiration.strftime("%Y%m%d"):
                continue
            if row.get("trading_class") != chain["trading_class"] or decimal_value(row.get("multiplier")) != Decimal(chain["multiplier"]):
                continue
            minimum = decimal_value(row.get("min_size"), positive=True)
            increment = decimal_value(row.get("size_increment"), positive=True)
            if minimum is None or increment is None:
                continue
            lot_size = (minimum / increment).to_integral_value(rounding=ROUND_CEILING) * increment
            rows.append({**row, "lot_size": str(lot_size), "strike": strike, "bid": bid, "ask": ask,
                "price": (bid + ask) / 2, "oi": int(oi), "volume": int(volume),
                "iv": decimal_value(row.get("iv"), positive=True),
                "delta": Decimal(str(row["delta"])) if row.get("delta") is not None else None,
                "expiration": expiration, "chain_definition": chain,
            })
        if not rows:
            raise IBKROptionDataUnavailable("IBKR returned no fresh option bid/ask, OI and volume observations; check market-data permissions")
        return {"rows": rows, "spot": spot, "expiration": expiration,
            "observed_at": min(parse_datetime(row["observed_at"]) for row in rows), "provider": self.provider}
