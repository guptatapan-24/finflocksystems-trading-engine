from django.conf import settings
from collections import deque
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math
from types import SimpleNamespace
from .base import BrokerAdapter
from .option_quotes import OptionQuotesMixin, number

class IBAsyncBrokerAdapter(OptionQuotesMixin, BrokerAdapter):
    def __init__(self):
        from ib_async import IB
        self.ib = IB(); self.contracts = {}; self.trades = {}; self.market_subscriptions={};self.market_events=deque()
        # Without it ib_async localises zone-less execution times to the process
        # zone, which put every fill hours away from when it happened.
        self.ib.TimezoneTWS = settings.IBKR_TWS_TIMEZONE
        self.order_events=deque();self.operator_cancellations=set();self.market_request_ids={};self.recent_errors=deque(maxlen=50)
        self.ib.orderStatusEvent += self._on_order_status
        self.ib.errorEvent += self._on_error
    def connect(self):
        # Market-data streams die with the previous TWS connection and are not
        # restored by a reconnect. Keeping their entries made every later
        # subscribe for the same key answer "reused" while no bar ever came.
        self.market_subscriptions.clear();self.market_request_ids.clear()
        self.ib.connect("127.0.0.1", settings.TWS_PORT, clientId=settings.IBKR_CLIENT_ID, readonly=False, timeout=15)
        return {"connected":self.ib.isConnected()}
    def disconnect(self): self.ib.disconnect()
    def is_connected(self): return self.ib.isConnected()
    @contextmanager
    def _request_timeout(self, seconds):
        """Bound one synchronous ib_async request and restore the shared client setting."""
        previous = getattr(self.ib, "RequestTimeout", None)
        if previous is not None:
            self.ib.RequestTimeout = float(seconds)
        try:
            yield
        finally:
            if previous is not None:
                self.ib.RequestTimeout = previous
    def _contract(self, payload):
        from ib_async import Contract, Forex, Future, Index, Option, Stock
        if payload.get("conid"):
            return Contract(conId=int(payload["conid"]), exchange=payload.get("exchange", ""), currency=payload.get("currency", ""))
        asset = payload.get("asset_class", "STK")
        if asset == "CASH": return Forex(payload["symbol"], exchange=payload.get("exchange", "IDEALPRO"))
        if asset == "FUT": return Future(payload["symbol"], payload.get("expiry", ""), exchange=payload["exchange"], currency=payload.get("currency", "USD"))
        if asset == "IND": return Index(payload["symbol"], payload.get("exchange", "SMART"), payload.get("currency", "USD"))
        if asset == "OPT":
            expiry=str(payload.get("expiration") or payload.get("expiry") or "").replace("-", "")
            right=str(payload.get("right") or "").upper()
            right={"CALL":"C","PUT":"P"}.get(right,right)
            return Option(payload["symbol"],expiry,float(payload["strike"]),right,
                payload.get("exchange","SMART"),multiplier=str(payload.get("multiplier") or ""),
                currency=payload.get("currency","USD"),tradingClass=payload.get("trading_class") or "")
        return Stock(payload["symbol"], payload.get("exchange", "SMART"), payload.get("currency", "USD"))
    @staticmethod
    def _contract_data(contract, details=None):
        return {"conid":contract.conId,"symbol":contract.symbol,"local_symbol":contract.localSymbol,
            "asset_class":contract.secType,"exchange":contract.exchange or contract.primaryExchange,
            "primary_exchange":contract.primaryExchange,"currency":contract.currency,
            "description":getattr(details,"longName","") or getattr(details,"marketName","") or "",
            "expiration":getattr(contract,"lastTradeDateOrContractMonth","") or None,
            "strike":str(getattr(contract,"strike","") or "") or None,
            "right":getattr(contract,"right","") or None,
            "multiplier":getattr(contract,"multiplier","") or None,
            "trading_class":getattr(contract,"tradingClass","") or "",
            "underlying_conid":getattr(details,"underConId",None) if details else None,
            "min_tick":number(getattr(details,"minTick",None), positive=True),
            "min_size":number(getattr(details,"minSize",None), positive=True),
            "size_increment":number(getattr(details,"sizeIncrement",None), positive=True)}
    @staticmethod
    def _search_rank(contract, query):
        symbol = str(getattr(contract, "symbol", "") or "").upper()
        local_symbol = str(getattr(contract, "localSymbol", "") or "").upper()
        security_type = str(getattr(contract, "secType", "") or "").upper()
        exact = query in {symbol, local_symbol}
        prefix = symbol.startswith(query) or local_symbol.startswith(query)
        return (
            0 if security_type == "STK" and exact else
            1 if security_type == "STK" and prefix else
            2 if security_type == "STK" else
            3 if exact else
            4 if prefix else
            5,
            symbol,
            local_symbol,
            int(getattr(contract, "conId", 0) or 0),
        )
    def search_contracts(self, query, asset_classes=None):
        normalized = str(query).strip().upper()
        requested={str(value).upper() for value in (asset_classes or [])}
        with self._request_timeout(settings.GATEWAY_IBKR_REQUEST_TIMEOUT_SEARCH_CONTRACTS_SECONDS):
            matches = self.ib.reqMatchingSymbols(normalized)
        results = []
        seen = set()
        ranked = sorted(matches or [], key=lambda match: self._search_rank(match.contract, normalized))
        for match in ranked:
            contract = match.contract
            if contract.conId <= 0 or contract.conId in seen:
                continue
            if requested and str(contract.secType or "").upper() not in requested:
                continue
            seen.add(contract.conId)
            # reqMatchingSymbols already returns exact contract identities. Calling
            # reqContractDetails once per unbounded match serializes IBKR round
            # trips and can monopolize the sole broker worker for minutes.
            results.append(self._contract_data(contract))
            if len(results) >= settings.GATEWAY_CONTRACT_SEARCH_MAX_RESULTS:
                break
        return results
    def option_chain(self,payload):
        underlying_conid=int(payload["underlying_conid"])
        if underlying_conid<=0:raise ValueError("underlying_conid must be positive")
        underlying=self._contract({"conid":underlying_conid,"exchange":payload.get("exchange","") ,"currency":payload.get("currency","")})
        with self._request_timeout(settings.GATEWAY_IBKR_REQUEST_TIMEOUT_OPTION_CHAIN_SECONDS):
            details=self.ib.reqContractDetails(underlying)
        if not details:raise RuntimeError("Underlying contract details were not found")
        exact=next((item for item in details if int(item.contract.conId)==underlying_conid),None)
        if exact is None:raise RuntimeError("IBKR returned a different underlying contract")
        contract=exact.contract
        with self._request_timeout(settings.GATEWAY_IBKR_REQUEST_TIMEOUT_OPTION_CHAIN_SECONDS):
            parameters=self.ib.reqSecDefOptParams(contract.symbol,"",contract.secType,underlying_conid)
        chains=[]
        for item in parameters or []:
            expirations=sorted(str(value) for value in (item.expirations or []) if str(value))
            strikes=sorted(float(value) for value in (item.strikes or []) if float(value)>0)
            if not expirations or not strikes or not str(item.multiplier or ""):continue
            chains.append({"exchange":str(item.exchange or "SMART"),"trading_class":str(item.tradingClass or ""),
                "multiplier":str(item.multiplier),"expirations":expirations,"strikes":[str(value) for value in strikes]})
        return {"underlying":self._contract_data(contract,exact),"chains":chains}
    def qualify_contract(self, payload):
        contract = self._contract(payload)
        with self._request_timeout(settings.GATEWAY_IBKR_REQUEST_TIMEOUT_QUALIFY_SECONDS):
            qualified = self.ib.qualifyContracts(contract)
        if not qualified or qualified[0] is None:
            error = self.recent_errors[-1] if self.recent_errors else {}
            raise RuntimeError(error.get("error_message") or "IBKR contract qualification returned no result")
        contract = qualified[0]; self.contracts[str(contract.conId)] = contract
        with self._request_timeout(settings.GATEWAY_IBKR_REQUEST_TIMEOUT_QUALIFY_SECONDS):
            details=self.ib.reqContractDetails(contract)
        exact=next((item for item in details or [] if int(item.contract.conId)==int(contract.conId)),None)
        return {**self._contract_data(contract,exact), "qualified":True}
    def historical_bars(self, payload):
        qualified=self.ib.qualifyContracts(self._contract(payload))
        if not qualified:raise RuntimeError("Selected exact IBKR contract could not be qualified for historical data")
        contract=qualified[0]
        if int(contract.conId) != int(payload["conid"]):
            raise RuntimeError("IBKR returned a different contract identity")
        rows=self.ib.reqHistoricalData(
            contract,
            endDateTime=payload.get("end_time", ""),
            durationStr=payload["duration"],
            barSizeSetting=payload.get("bar_size", "1 day"),
            whatToShow=payload.get("what_to_show", "TRADES"),
            useRTH=bool(payload.get("use_rth", True)),
            formatDate=2,
            keepUpToDate=False,
        )
        bars=[]
        for row in rows:
            bars.append({
                "date":self._bar_time(row).date().isoformat() if payload.get("bar_size", "1 day")=="1 day" else self._bar_time(row).isoformat(),
                "open":str(row.open),"high":str(row.high),"low":str(row.low),"close":str(row.close),
                "volume":str(max(0, row.volume)),"bar_count":int(getattr(row,"barCount",0) or 0),
                "average":str(getattr(row,"average",0) or 0),
            })
        return {"conid":contract.conId,"symbol":contract.symbol,"provider":"IBKR",
                "what_to_show":payload.get("what_to_show","TRADES"),"bar_size":payload.get("bar_size","1 day"),"bars":bars}
    def historical_schedule(self, payload):
        qualified=self.ib.qualifyContracts(self._contract(payload))
        if not qualified or qualified[0] is None or int(qualified[0].conId)!=int(payload["conid"]):
            raise RuntimeError("Selected exact IBKR contract could not be qualified for schedule data")
        contract=qualified[0]
        schedule=self.ib.reqHistoricalSchedule(contract,numDays=int(payload["days"]),
            endDateTime=payload.get("end_time", ""),useRTH=bool(payload.get("use_rth",True)))
        sessions=[]
        for item in getattr(schedule,"sessions",[]) or []:
            sessions.append({"reference_date":str(item.refDate),"start":item.startDateTime.isoformat(),"end":item.endDateTime.isoformat()})
        return {"conid":contract.conId,"symbol":contract.symbol,"provider":"IBKR","timezone":str(getattr(schedule,"timeZone","")),"sessions":sessions}
    @staticmethod
    def _timeframe(value):
        mapping={"1m":("1 min",60),"5m":("5 mins",300),"15m":("15 mins",900),"1h":("1 hour",3600),"1d":("1 day",86400)}
        if value not in mapping:raise ValueError(f"Unsupported IBKR market-data timeframe {value}")
        return mapping[value]
    @staticmethod
    def _warmup_duration(count, seconds):
        # A daily bar represents one session, not 24 hours of intraday bars.
        count = max(1, count)
        trading_days = count if seconds == 86400 else max(1, (count * seconds + 23399) // 23400)
        days = max(4, (trading_days * 8 + 4) // 5 + 2)
        # IBKR rejects durations over 365 days expressed with the D unit.
        return f"{(days + 364) // 365} Y" if days > 365 else f"{days} D"
    @staticmethod
    def _bar_time(bar):
        value=getattr(bar,"date",None) or getattr(bar,"time",None)
        if isinstance(value,date) and not isinstance(value,datetime):value=datetime.combine(value,time.min,tzinfo=timezone.utc)
        if value.tzinfo is None:value=value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    def _market_payload(self,bar,payload,source,timeframe,seconds,market_data_type=None):
        start=self._bar_time(bar);end=start+timedelta(seconds=seconds)
        def field(primary,fallback=None):return getattr(bar,primary,getattr(bar,fallback,0) if fallback else 0)
        stable=f"{payload['conid']}:{payload['subscription_key']}:{timeframe}:{start.isoformat()}:{payload.get('provider_generation','')}"
        data_type=str(market_data_type or payload.get("market_data_type") or "LIVE").upper()
        return {"source_event_id":stable,"subscription_key":payload["subscription_key"],
            "instrument_id":int(payload["instrument_id"]),"conid":int(payload["conid"]),"symbol":payload["symbol"],
            "exchange":payload.get("exchange","SMART"),"currency":payload.get("currency","USD"),
            "event_kind":"BAR","timeframe":timeframe,"event_time":start.isoformat(),"window_start":start.isoformat(),
            "window_end":end.isoformat(),"open":str(field("open","open_")),"high":str(field("high","high")),
            "low":str(field("low","low")),"close":str(field("close","close")),
            "volume":str(max(0,field("volume","volume"))),"is_final":True,"source":source,
            "provider":"IBKR","provider_generation":str(payload.get("provider_generation") or ""),
            "market_data_type":data_type,"received_at":str(payload.get("_received_at") or datetime.now(timezone.utc).isoformat()),
            "delay_seconds":int(payload.get("_delay_seconds") or 0),
            "processing_mode":"WARMUP" if source=="ibkr_historical" else "LIVE"}
    @staticmethod
    def _delayed_price(ticker):
        market_price=getattr(ticker,"marketPrice",None)
        market_price=market_price() if callable(market_price) else market_price
        for value in (getattr(ticker,"last",None),market_price,
                      getattr(ticker,"close",None),getattr(ticker,"bid",None),getattr(ticker,"ask",None)):
            try:
                value=float(value)
                if math.isfinite(value) and value>0:return value
            except (TypeError,ValueError):
                continue
        return None
    def _capture_delayed_ticker(self,current,received_at=None,include_size=False):
        received_at=received_at or datetime.now(timezone.utc);ticker=current["live"]
        price=self._delayed_price(ticker)
        if price is None:return
        delay=int(current["delay_seconds"]);event_at=received_at-timedelta(seconds=delay)
        stamp=int(event_at.timestamp());start=datetime.fromtimestamp(stamp-stamp%5,tz=timezone.utc)
        bucket=current.get("bucket")
        if bucket and bucket.date!=start:
            payload={**current["payload"],"_received_at":current["bucket_received_at"].isoformat(),"_delay_seconds":delay}
            self.market_events.append(self._market_payload(bucket,payload,"ibkr_delayed","5s",5,"DELAYED"))
            bucket=None
        size=(getattr(ticker,"lastSize",0) or 0) if include_size else 0
        if bucket is None:
            current["bucket"]=SimpleNamespace(date=start,open=price,high=price,low=price,close=price,volume=max(0,float(size)))
        else:
            bucket.high=max(bucket.high,price);bucket.low=min(bucket.low,price);bucket.close=price
        current["bucket_received_at"]=received_at
    def _flush_delayed_tickers(self):
        now=datetime.now(timezone.utc)
        for current in self.market_subscriptions.values():
            if not current.get("delayed"):continue
            self._capture_delayed_ticker(current,now)
            bucket=current.get("bucket")
            if bucket and now >= bucket.date+timedelta(seconds=int(current["delay_seconds"])+5):
                payload={**current["payload"],"_received_at":current["bucket_received_at"].isoformat(),
                    "_delay_seconds":current["delay_seconds"]}
                self.market_events.append(self._market_payload(bucket,payload,"ibkr_delayed","5s",5,"DELAYED"))
                current["bucket"]=None
    def subscribe_market_data(self,payload):
        key=payload["subscription_key"];runtime_key=payload.get("gateway_subscription_key") or key
        held=self.market_subscriptions.get(runtime_key)
        if held and str(held["payload"].get("provider_generation") or "")!=str(payload.get("provider_generation") or ""):
            # A new generation supersedes the held stream: its bars carry the
            # old generation and the backend drops them. Replace it with a
            # full subscribe (warm-up history plus a stream on the new one).
            self.cancel_market_data({"subscription_key":key,"gateway_subscription_key":runtime_key})
        if runtime_key in self.market_subscriptions:return {"subscription_key":key,"gateway_subscription_key":runtime_key,
            "state":"ACTIVE","historical_bar_count":0,"reused":True,"probe":bool(payload.get("probe")),
            "provider_generation":str(payload.get("provider_generation") or "")}
        payload={**payload,"_gateway_subscription_key":runtime_key}
        qualified=self.ib.qualifyContracts(self._contract(payload))
        if not qualified:raise RuntimeError("Selected IBKR contract could not be qualified for market data")
        contract=qualified[0];self.contracts[str(contract.conId)]=contract
        delayed=settings.IBKR_MARKET_DATA_TYPE=="DELAYED"
        if delayed and settings.IBC_TRADING_MODE!="paper":
            raise RuntimeError("Delayed market data is restricted to paper mode")
        self.ib.reqMarketDataType(3 if delayed else 1)
        bar_size,seconds=self._timeframe(payload["timeframe"]);count=max(0,int(payload.get("historical_bars",0)))
        historical=[]
        if not payload.get("probe"):
            request_count=max(1,count)
            historical=self.ib.reqHistoricalData(contract,endDateTime="",durationStr=self._warmup_duration(request_count,seconds),barSizeSetting=bar_size,
                whatToShow=payload.get("what_to_show","TRADES"),useRTH=bool(payload.get("use_rth",False)),formatDate=2,keepUpToDate=False)
            if not historical:
                recent=next((item for item in reversed(self.recent_errors) if item.get("conid") in (None,contract.conId)),None)
                detail=f"IBKR error {recent['error_code']}: {recent['error_message']}" if recent else "IBKR historical request returned no bars"
                raise RuntimeError(detail)
            for bar in list(historical)[-request_count:]:self.market_events.append(self._market_payload(
                bar,payload,"ibkr_historical",payload["timeframe"],seconds,"DELAYED" if delayed else "LIVE"))
        if delayed:
            live=self.ib.reqMktData(contract,"",False,False)
        else:
            live=self.ib.reqRealTimeBars(contract,5,payload.get("what_to_show","TRADES"),bool(payload.get("use_rth",False)))
        request_id=getattr(live,"reqId",None)
        if request_id is not None:self.market_request_ids[int(request_id)]=dict(payload)
        recent=next((item for item in reversed(self.recent_errors) if request_id is not None and item.get("request_id")==request_id),None)
        if recent:
            self.market_request_ids.pop(int(request_id),None)
            (self.ib.cancelMktData(contract) if delayed else self.ib.cancelRealTimeBars(live))
            raise RuntimeError(f"IBKR error {recent['error_code']}: {recent['error_message']}")
        latest_historical_end=(self._bar_time(historical[-1])+timedelta(seconds=seconds)) if historical else None
        observed_delay=max(900,int((datetime.now(timezone.utc)-latest_historical_end).total_seconds())) if latest_historical_end else 900
        observed_delay=((observed_delay+4)//5)*5
        current={"live":live,"handler":None,"payload":dict(payload),"request_id":request_id,
            "delayed":delayed,"contract":contract,"delay_seconds":observed_delay,"bucket":None}
        def on_update(bars,*args):
            if delayed:
                self._capture_delayed_ticker(current,include_size=True)
            elif bars:
                self.market_events.append(self._market_payload(
                    bars[-1],payload,"ibkr_live","5s",5,"LIVE"))
        live.updateEvent += on_update
        current["handler"]=on_update;self.market_subscriptions[runtime_key]=current
        return {"subscription_key":key,"gateway_subscription_key":runtime_key,"state":"ACTIVE",
            "historical_bar_count":min(len(historical),count),"conid":contract.conId,"probe":bool(payload.get("probe")),
            "provider_generation":str(payload.get("provider_generation") or ""),
            "market_data_type":"DELAYED" if delayed else "LIVE","delay_seconds":observed_delay if delayed else 0}
    def cancel_market_data(self,payload):
        key=payload["subscription_key"]
        runtime_keys=[name for name,item in self.market_subscriptions.items()
            if name==payload.get("gateway_subscription_key") or item["payload"].get("subscription_key")==key]
        for runtime_key in runtime_keys:
            current=self.market_subscriptions.pop(runtime_key)
            (self.ib.cancelMktData(current["contract"]) if current.get("delayed")
             else self.ib.cancelRealTimeBars(current["live"]))
            if current.get("request_id") is not None:self.market_request_ids.pop(int(current["request_id"]),None)
        return {"subscription_key":key,"state":"INACTIVE","cancelled":len(runtime_keys)}
    def drain_market_events(self):
        self._flush_delayed_tickers()
        events=[]
        while self.market_events:events.append(self.market_events.popleft())
        return events
    def drain_order_events(self):
        events=[]
        while self.order_events:events.append(self.order_events.popleft())
        return events
    def _order(self, payload):
        from ib_async import MarketOrder, LimitOrder, StopOrder, StopLimitOrder
        action, qty, tif = payload["side"], float(payload["quantity"]), payload.get("time_in_force", "DAY")
        typ = payload.get("order_type", "MKT")
        if typ == "LMT": return LimitOrder(action, qty, float(payload["limit_price"]), tif=tif)
        if typ == "STP": return StopOrder(action, qty, float(payload["stop_price"]), tif=tif)
        if typ == "STP_LMT": return StopLimitOrder(action, qty, float(payload["limit_price"]), float(payload["stop_price"]), tif=tif)
        return MarketOrder(action, qty, tif=tif)
    def place_order(self, payload):
        contract = self.contracts.get(str(payload.get("conid"))) or self._contract(payload)
        order = self._order(payload); order.orderRef = payload["internal_id"]
        if payload.get("account"): order.account = payload["account"]
        trade = self.ib.placeOrder(contract, order); self.trades[payload["internal_id"]] = trade
        return {"internal_id":payload["internal_id"], "broker_order_id":str(trade.order.orderId), "permanent_id":str(trade.order.permId or ""), "status":trade.orderStatus.status}
    def place_option_order(self,payload):
        if str(payload.get("asset_class") or "").upper()!="OPT":raise ValueError("Option order requires asset_class OPT")
        if int(payload.get("conid") or 0)<=0:raise ValueError("Option order requires a qualified conId")
        with self._request_timeout(settings.GATEWAY_IBKR_REQUEST_TIMEOUT_QUALIFY_SECONDS):
            qualified=self.ib.qualifyContracts(self._contract(payload))
        if not qualified or qualified[0] is None or int(qualified[0].conId)!=int(payload["conid"]) or qualified[0].secType!="OPT":
            raise RuntimeError("Selected exact IBKR option contract could not be qualified")
        contract=qualified[0]
        expected_expiry=str(payload["expiration"]).replace("-","")
        expected_right=str(payload["right"]).upper()
        if (str(contract.lastTradeDateOrContractMonth or "")[:8]!=expected_expiry[:8]
                or str(contract.right or "").upper()!=expected_right
                or float(contract.strike)!=float(payload["strike"])
                or Decimal(str(contract.multiplier or "0"))!=Decimal(str(payload["multiplier"]))
                or str(contract.tradingClass or "")!=str(payload["trading_class"] or "")):
            raise RuntimeError("IBKR qualified option identity does not match the operator-selected contract")
        self.contracts[str(payload["conid"])]=contract
        return self.place_order(payload)
    def _find_trade(self, internal_id):
        trade = self.trades.get(internal_id)
        if trade: return trade
        for candidate in self.ib.trades():
            if candidate.order.orderRef == internal_id:
                self.trades[internal_id] = candidate
                return candidate
        raise RuntimeError(f"Broker order not found for {internal_id}")
    def modify_order(self, payload):
        trade = self._find_trade(payload["internal_id"])
        for source, dest in [("quantity","totalQuantity"),("limit_price","lmtPrice"),("stop_price","auxPrice")]:
            if source in payload: setattr(trade.order, dest, float(payload[source]))
        trade = self.ib.placeOrder(trade.contract, trade.order); return {"broker_order_id":str(trade.order.orderId), "status":trade.orderStatus.status}
    def cancel_order(self, payload):
        trade = self._find_trade(payload["internal_id"]);self.operator_cancellations.add(payload["internal_id"])
        self.ib.cancelOrder(trade.order); return {"broker_order_id":str(trade.order.orderId), "status":"PendingCancel"}
    @staticmethod
    def _log_data(entry):
        occurred=getattr(entry,"time",None)
        return {"time":occurred.isoformat() if occurred else None,"status":str(getattr(entry,"status","") or ""),
            "message":str(getattr(entry,"message","") or ""),"error_code":str(getattr(entry,"errorCode","") or "")}
    @staticmethod
    def _advanced_reject(trade):
        value=getattr(trade,"advancedError","") or ""
        if not value:return None
        try:return json.loads(value)
        except (TypeError,json.JSONDecodeError):return value
    def _order_event(self,trade,error_code=None,error_message=""):
        order,status=trade.order,trade.orderStatus;logs=[self._log_data(item) for item in list(trade.log)]
        latest=logs[-1] if logs else {};occurred_at=latest.get("time") or datetime.now(timezone.utc).isoformat()
        log_code=latest.get("error_code") or "";log_message=latest.get("message") or ""
        code=str(error_code if error_code not in (None,"") else log_code)
        message=str(error_message or log_message or "")
        why_held=str(getattr(status,"whyHeld","") or "")
        internal_id=str(order.orderRef or "")
        identity=json.dumps([internal_id,order.orderId,status.status,code,message,occurred_at],default=str,separators=(",",":"))
        source_event_id=hashlib.sha256(identity.encode()).hexdigest()
        return {**self._contract_data(trade.contract),"source_event_id":source_event_id,"internal_id":internal_id,
            "account":order.account,"broker_order_id":str(order.orderId),"permanent_id":str(order.permId or ""),
            "broker_status":str(status.status or ""),"error_code":code,"error_message":message,
            "why_held":why_held,"warning_text":log_message,"advanced_reject":self._advanced_reject(trade),
            "trade_log":logs,"occurred_at":occurred_at,"operator_requested":internal_id in self.operator_cancellations}
    def _on_order_status(self,trade):
        self.order_events.append(self._order_event(trade))
    def _on_error(self,req_id,error_code,error_string,contract=None,*_args):
        trade=next((candidate for candidate in self.ib.trades() if candidate.order.orderId==req_id),None)
        if trade:self.order_events.append(self._order_event(trade,error_code,error_string));return
        if int(error_code) in {2104,2106,2107,2108,2158}:return
        now=datetime.now(timezone.utc).isoformat();conid=getattr(contract,"conId",None) if contract else None
        self.recent_errors.append({"request_id":req_id,"error_code":str(error_code),"error_message":str(error_string),
            "conid":conid,"occurred_at":now})
        payload=self.market_request_ids.get(int(req_id)) if isinstance(req_id,int) and req_id>=0 else None
        if payload:
            self.market_request_ids.pop(int(req_id),None);self.market_subscriptions.pop(payload.get("_gateway_subscription_key") or payload["subscription_key"],None)
            identity=json.dumps([req_id,error_code,error_string,now],separators=(",",":"))
            self.market_events.append({**payload,"source_event_id":hashlib.sha256(identity.encode()).hexdigest(),
                "event_kind":"ERROR","error_code":str(error_code),"error_message":str(error_string),
                "occurred_at":now,"source":"ibkr"})
    def _trade_data(self, trade):
        order, status = trade.order, trade.orderStatus
        diagnostic=self._order_event(trade)
        return {**self._contract_data(trade.contract),"account":order.account,"internal_id":order.orderRef,"broker_order_id":str(order.orderId),"permanent_id":str(order.permId or ""),"side":order.action,"quantity":str(order.totalQuantity),"order_type":order.orderType,"limit_price":None if order.lmtPrice in (0,1.7976931348623157e308) else str(order.lmtPrice),"stop_price":None if order.auxPrice in (0,1.7976931348623157e308) else str(order.auxPrice),"time_in_force":order.tif,"status":status.status,"filled_quantity":str(status.filled),"remaining_quantity":str(status.remaining),"average_fill_price":str(status.avgFillPrice or 0),
            "broker_status":diagnostic["broker_status"],"error_code":diagnostic["error_code"],
            "error_message":diagnostic["error_message"],"why_held":diagnostic["why_held"],
            "warning_text":diagnostic["warning_text"],"advanced_reject":diagnostic["advanced_reject"],
            "trade_log":diagnostic["trade_log"],"occurred_at":diagnostic["occurred_at"],
            "operator_requested":diagnostic["operator_requested"]}
    def refresh_state(self):
        summary=[]
        for value in self.ib.accountValues(): summary.append({"account":value.account,"tag":value.tag,"value":value.value,"currency":value.currency,"model_code":value.modelCode})
        portfolio_prices={p.contract.conId:p.marketPrice for account in self.ib.managedAccounts() for p in self.ib.portfolio(account)}
        positions=[{"account":p.account,**self._contract_data(p.contract),"quantity":str(p.position),"average_cost":str(p.avgCost),"market_price":str(portfolio_prices.get(p.contract.conId,0) or 0)} for p in self.ib.positions()]
        open_trades=self.ib.openTrades(); open_keys={(t.order.clientId,t.order.orderId) for t in open_trades}
        completed=[t for t in self.ib.trades() if (t.order.clientId,t.order.orderId) not in open_keys]
        executions=[]
        for fill in self.ib.fills():
            report=fill.commissionReport; execution=fill.execution
            commission=report.commission if report and report.commission < 1e100 else 0
            executions.append({"execution_id":execution.execId,"broker_order_id":str(execution.orderId),"permanent_id":str(execution.permId or ""),"account":execution.acctNumber,"side":execution.side,"quantity":str(execution.shares),"price":str(execution.price),"executed_at":execution.time.isoformat() if execution.time else None,"commission":str(commission),"currency":report.currency if report else fill.contract.currency,**self._contract_data(fill.contract)})
        return {"accounts":[{"account_id":account} for account in self.ib.managedAccounts()],"account_summary":summary,"positions":positions,"open_orders":[self._trade_data(t) for t in open_trades],"completed_orders":[self._trade_data(t) for t in completed],"executions":executions,"reconciled":True}
    def wait(self, seconds): self.ib.sleep(seconds)
