"""Bounded option observations on the worker's existing IBKR connection."""

import asyncio
from datetime import datetime, timezone
from math import isfinite

from django.conf import settings


def number(value, *, positive=False):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if isfinite(value) and (value > 0 if positive else value >= 0) else None


#: IBKR tick types. 2 (frozen) and 4 (delayed-frozen) are last-known values
#: from a closed book and never authorize an entry, so neither is requestable.
LIVE_TICK_TYPE = 1
DELAYED_TICK_TYPE = 3


def option_tick_type():
    """The tick type this Gateway's trading mode entitles it to.

    The mode is the decision: a paper account reads delayed data, a live
    account reads real time. `IBKR_MARKET_DATA_TYPE` defaults from the mode
    and can only narrow a paper session, never widen a live one.
    """
    if settings.IBKR_MARKET_DATA_TYPE == "DELAYED":
        if settings.IBC_TRADING_MODE != "paper":
            raise RuntimeError("Delayed market data is restricted to paper mode")
        return DELAYED_TICK_TYPE
    return LIVE_TICK_TYPE


class OptionQuotesMixin:
    def option_quotes(self, payload):
        if payload["mode"] != settings.IBC_TRADING_MODE:
            raise ValueError("Option data mode must match the configured Gateway")
        tick_type = option_tick_type()
        # Fresh Contract objects prevent ib_async from reusing cached Ticker fields.
        contracts = [self._contract(row) for row in payload["contracts"]]
        async def details_for_contracts():
            return await asyncio.wait_for(asyncio.gather(*[
                self.ib.reqContractDetailsAsync(contract) for contract in contracts
            ]), timeout=20)

        details = self.ib.run(details_for_contracts())
        # A chain's strike/expiry cross product can contain unlisted contracts.
        # Keep only unambiguous identities returned by IBKR.
        exact = [items[0] for items in details if len(items) == 1]
        qualified = [item.contract for item in exact]
        metadata = {item.contract.conId: self._contract_data(item.contract, item) for item in exact}
        if not qualified:
            raise ValueError("No requested option contract qualified")
        tickers = []
        started = datetime.now(timezone.utc)
        try:
            self.ib.reqMarketDataType(tick_type)
            for contract in qualified:
                ticker = self.ib.reqMktData(contract, "100,101,104,106" if contract.secType == "OPT" else "", False, False)
                tickers.append(ticker)

            async def collect():
                deadline = asyncio.get_running_loop().time() + 10
                while asyncio.get_running_loop().time() < deadline:
                    if all(self._option_quote_ready(ticker, started, tick_type) for ticker in tickers):
                        break
                    await asyncio.sleep(0.2)

            self.ib.run(collect())
            rows = []
            for ticker in tickers:
                contract = ticker.contract
                greeks = ticker.modelGreeks
                rows.append({
                    **metadata[contract.conId],
                    "observed_at": ticker.time.isoformat() if ticker.time else None,
                    "market_data_type": ticker.marketDataType,
                    "bid": number(ticker.bid), "ask": number(ticker.ask),
                    "last": number(ticker.last, positive=True),
                    "oi": number(ticker.callOpenInterest if contract.right == "C" else ticker.putOpenInterest),
                    "volume": number(ticker.volume),
                    "iv": number(getattr(greeks, "impliedVol", None), positive=True),
                    "delta": getattr(greeks, "delta", None) if greeks and isfinite(greeks.delta) else None,
                })
            errors = [item for item in self.recent_errors
                if item.get("conid") in metadata and item.get("occurred_at", "") >= started.isoformat()]
            return {"provider": "IBKR", "mode": settings.IBC_TRADING_MODE, "rows": rows, "errors": errors}
        finally:
            for ticker in tickers:
                self.ib.cancelMktData(ticker.contract)
            self.ib.reqMarketDataType(tick_type)

    @staticmethod
    def _option_quote_ready(ticker, started, tick_type=LIVE_TICK_TYPE):
        # An exact match rejects the frozen fallbacks IBKR substitutes when a
        # book is closed, and rejects real-time data a paper session did not ask
        # for, so provenance always matches what the mode entitled.
        if not ticker.time or ticker.time < started or ticker.marketDataType != tick_type:
            return False
        if ticker.contract.secType != "OPT":
            return number(ticker.last, positive=True) is not None
        oi = ticker.callOpenInterest if ticker.contract.right == "C" else ticker.putOpenInterest
        return all(number(value) is not None for value in (ticker.bid, ticker.ask, ticker.volume, oi)) and ticker.modelGreeks is not None
