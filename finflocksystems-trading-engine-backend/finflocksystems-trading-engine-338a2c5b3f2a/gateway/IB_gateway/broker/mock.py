import hashlib
import os
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from .base import BrokerAdapter


def _truthy(name, default="false"):
    return str(os.getenv(name, default)).strip().lower() in {"1", "true", "yes", "on"}


class MockBrokerAdapter(BrokerAdapter):
    def __init__(self):
        self.connected = False
        self.instance_id = uuid.uuid4().hex
        self.next_order_id = 1_000_000 + (int(self.instance_id[:8], 16) % 800_000_000)
        self.orders = {}
        self.subscriptions = {}
        self.market_events = []
        self.order_events = []
        self.executions = []
        self.positions = {}
        self.killed = False
        self.account_id = str(os.getenv("MOCK_BROKER_ACCOUNT_ID", "")).strip()
        self.auto_fill = _truthy("MOCK_BROKER_AUTO_FILL")
        self.market_heartbeat_seconds = max(
            0, int(os.getenv("MOCK_BROKER_MARKET_HEARTBEAT_SECONDS", "0"))
        )
        self.last_market_heartbeat = 0.0
        self.market_sequence = 0

    @staticmethod
    def _conid(symbol, exchange="SMART"):
        digest = hashlib.sha256(f"{symbol}:{exchange}".encode()).digest()
        return int.from_bytes(digest[:4], "big") % 2_000_000_000

    def connect(self): self.connected = True; return {"connected":True}
    def disconnect(self): self.connected = False
    def is_connected(self): return self.connected
    def search_contracts(self, query, asset_classes=None):
        symbol = str(query).strip().upper()
        rows = [{"symbol":symbol,"local_symbol":symbol,"conid":self._conid(symbol,"NASDAQ"),
            "asset_class":"STK","exchange":"SMART","primary_exchange":"NASDAQ","currency":"USD",
            "description":f"{symbol} mock corporation"}]
        if symbol == "BHP":
            rows.append({"symbol":"BHP","local_symbol":"BHP","conid":self._conid(symbol,"ASX"),
                "asset_class":"STK","exchange":"SMART","primary_exchange":"ASX","currency":"AUD",
                "description":"BHP Group Limited"})
        if symbol in {"NIFTY","SPX"}:
            rows.append({"symbol":symbol,"local_symbol":symbol,"conid":self._conid(symbol,"INDEX"),
                "asset_class":"IND","exchange":"NSE" if symbol=="NIFTY" else "CBOE",
                "primary_exchange":"NSE" if symbol=="NIFTY" else "CBOE",
                "currency":"INR" if symbol=="NIFTY" else "USD","description":f"{symbol} mock index"})
        requested={str(value).upper() for value in (asset_classes or [])}
        return [row for row in rows if not requested or row["asset_class"] in requested]
    def option_chain(self,payload):
        symbol=str(payload.get("symbol") or "OPT").upper()
        return {"underlying":{**payload,"conid":int(payload["underlying_conid"])},"chains":[{
            "exchange":"NFO" if symbol=="NIFTY" else "SMART","trading_class":symbol,
            "multiplier":"75" if symbol=="NIFTY" else "100","expirations":["20260826","20260930"],
            "strikes":["25000","25100"] if symbol=="NIFTY" else ["100","105"],
        }]}
    def qualify_contract(self, payload):
        symbol = payload["symbol"]
        is_option=str(payload.get("asset_class") or "").upper()=="OPT"
        local=(f"{symbol}{str(payload.get('expiration') or '').replace('-','')}{payload.get('strike')}{payload.get('right')}"
            if is_option else payload.get("local_symbol") or symbol)
        return {**payload, "local_symbol":local,
            "conid":int(payload.get("conid") or self._conid(symbol,payload.get("primary_exchange") or payload.get("exchange","SMART"))),
            "primary_exchange":payload.get("primary_exchange") or "NASDAQ",
            "description":payload.get("description") or f"{symbol} mock corporation","qualified":True}
    def historical_bars(self, payload):
        count=min(int(str(payload.get("duration", "30 D")).split()[0]), 30)
        end=date.today()
        bars=[]
        intraday = payload.get("bar_size", "1 day") != "1 day"
        for offset in range(count, 0, -1):
            day=end-timedelta(days=offset)
            if day.weekday() >= 5:
                continue
            starts = [datetime(day.year, day.month, day.day, 15, 0, tzinfo=timezone.utc) + timedelta(hours=hour) for hour in range(6)] if intraday else [day]
            for start in starts:
                price=100 + len(bars) / 10
                bars.append({"date":start.isoformat() if intraday else start.isoformat(),"open":str(price),"high":str(price+1),
                             "low":str(price-1),"close":str(price),"volume":"1000000",
                             "bar_count":1,"average":str(price)})
        return {"conid":int(payload["conid"]),"symbol":payload["symbol"],"provider":"IBKR",
                "what_to_show":payload.get("what_to_show","TRADES"),"bar_size":payload.get("bar_size", "1 day"),"bars":bars}
    def historical_schedule(self, payload):
        end=date.today();sessions=[]
        for offset in range(int(payload.get("days",5)),0,-1):
            day=end-timedelta(days=offset)
            if day.weekday()<5:sessions.append({"reference_date":day.isoformat(),"start":f"{day.isoformat()}T14:30:00+00:00","end":f"{day.isoformat()}T21:00:00+00:00"})
        return {"conid":int(payload["conid"]),"symbol":payload["symbol"],"provider":"IBKR","timezone":"UTC","sessions":sessions}

    @staticmethod
    def _timeframe_seconds(value):
        text=str(value or "1m").strip().lower()
        units={"s":1,"m":60,"h":3600,"d":86400}
        if len(text)<2 or text[-1] not in units or not text[:-1].isdigit():
            raise ValueError(f"Unsupported mock timeframe {value}")
        return int(text[:-1])*units[text[-1]]

    @staticmethod
    def _market_payload(payload,start,seconds,price,source,processing_mode):
        end=start+timedelta(seconds=seconds)
        stable=":".join([
            str(payload["conid"]),str(payload["subscription_key"]),
            str(payload["timeframe"]),start.isoformat(),
            str(payload.get("provider_generation") or ""),processing_mode,
        ])
        return {
            "source_event_id":hashlib.sha256(stable.encode()).hexdigest(),
            "subscription_key":payload["subscription_key"],
            "instrument_id":int(payload["instrument_id"]),
            "conid":int(payload["conid"]),
            "symbol":payload["symbol"],
            "exchange":payload.get("exchange","SMART"),
            "currency":payload.get("currency","USD"),
            "event_kind":"BAR",
            "timeframe":payload["timeframe"],
            "event_time":start.isoformat(),
            "window_start":start.isoformat(),
            "window_end":end.isoformat(),
            "open":str(price),
            "high":str(price+Decimal("1")),
            "low":str(price-Decimal("1")),
            "close":str(price),
            "volume":"1000000",
            "is_final":True,
            "source":source,
            "provider":"IBKR",
            "provider_generation":str(payload.get("provider_generation") or ""),
            "processing_mode":processing_mode,
        }

    def subscribe_market_data(self,payload):
        key=payload["subscription_key"];runtime_key=payload.get("gateway_subscription_key") or key
        self.subscriptions[runtime_key]=dict(payload)
        history=0 if payload.get("probe") else max(0,int(payload.get("historical_bars",0)))
        seconds=self._timeframe_seconds(payload.get("timeframe"))
        now=datetime.now(timezone.utc)
        live_start=datetime.fromtimestamp(
            int(now.timestamp())-(int(now.timestamp())%seconds),tz=timezone.utc,
        )
        for offset in range(history,0,-1):
            start=live_start-timedelta(seconds=seconds*offset)
            self.market_events.append(self._market_payload(
                payload,start,seconds,Decimal("100"),"ibkr_historical","WARMUP",
            ))
        self.market_events.append(self._market_payload(
            payload,live_start,seconds,Decimal("110"),"ibkr_live","LIVE",
        ))
        self.last_market_heartbeat=time.monotonic()
        return {"subscription_key":key,"gateway_subscription_key":runtime_key,"state":"ACTIVE","historical_bar_count":history,
            "provider_generation":str(payload.get("provider_generation") or ""),"probe":bool(payload.get("probe"))}
    def cancel_market_data(self,payload):
        key=payload["subscription_key"]
        matches=[name for name,item in self.subscriptions.items() if item.get("subscription_key")==key]
        for name in matches:self.subscriptions.pop(name,None)
        return {"subscription_key":key,"state":"INACTIVE","cancelled":len(matches)}
    def drain_market_events(self):
        now_monotonic=time.monotonic()
        if (
            self.market_heartbeat_seconds
            and self.subscriptions
            and now_monotonic-self.last_market_heartbeat>=self.market_heartbeat_seconds
        ):
            now=datetime.now(timezone.utc)
            for payload in self.subscriptions.values():
                seconds=self._timeframe_seconds(payload.get("timeframe"))
                live_start=datetime.fromtimestamp(
                    int(now.timestamp())-(int(now.timestamp())%seconds),
                    tz=timezone.utc,
                )
                event=self._market_payload(
                    payload,live_start,seconds,Decimal("110"),"ibkr_live","LIVE",
                )
                self.market_sequence+=1
                event["source_event_id"]=hashlib.sha256(
                    f"{event['source_event_id']}:{self.market_sequence}".encode()
                ).hexdigest()
                self.market_events.append(event)
            self.last_market_heartbeat=now_monotonic
        events,self.market_events=self.market_events,[];return events
    def drain_order_events(self):
        events,self.order_events=self.order_events,[];return events

    @staticmethod
    def _contract_fields(payload):
        symbol=str(payload.get("symbol") or "UNKNOWN")
        exchange=str(payload.get("exchange") or "SMART")
        primary=str(payload.get("primary_exchange") or "NASDAQ")
        conid=int(payload.get("conid") or MockBrokerAdapter._conid(symbol,primary))
        return {
            "conid":conid,
            "symbol":symbol,
            "local_symbol":str(payload.get("local_symbol") or symbol),
            "asset_class":str(payload.get("asset_class") or "STK"),
            "exchange":exchange,
            "primary_exchange":primary,
            "currency":str(payload.get("currency") or "USD"),
        }

    def _fill_order(self,current):
        quantity=Decimal(str(current["quantity"]))
        side=str(current.get("side") or "BUY").upper()
        price=Decimal(str(os.getenv("MOCK_BROKER_FILL_PRICE","110")))
        now=datetime.now(timezone.utc)
        current.update({
            "status":"Filled","broker_status":"Filled",
            "filled_quantity":str(quantity),"remaining_quantity":"0",
            "average_fill_price":str(price),"occurred_at":now.isoformat(),
        })
        execution_id=f"mock-fill:{self.instance_id}:{current['broker_order_id']}"
        self.executions.append({
            **self._contract_fields(current),
            "execution_id":execution_id,
            "broker_order_id":current["broker_order_id"],
            "permanent_id":current["permanent_id"],
            "account":current["account"],
            "side":"BOT" if side=="BUY" else "SLD",
            "quantity":str(quantity),
            "price":str(price),
            "executed_at":now.isoformat(),
            "commission":"0",
            "currency":current.get("currency","USD"),
        })
        position_key=(current["account"],int(current["conid"]))
        signed=quantity if side=="BUY" else -quantity
        existing=self.positions.get(position_key,{"quantity":Decimal(0),"average_cost":Decimal(0)})
        new_quantity=existing["quantity"]+signed
        average_cost=price if new_quantity else Decimal(0)
        self.positions[position_key]={
            **self._contract_fields(current),
            "account":current["account"],
            "quantity":new_quantity,
            "average_cost":average_cost,
            "market_price":price,
        }
        self.order_events.append({
            **self._contract_fields(current),
            "source_event_id":f"mock-fill-order:{self.instance_id}:{current['broker_order_id']}",
            "internal_id":current["internal_id"],
            "account":current["account"],
            "broker_order_id":current["broker_order_id"],
            "permanent_id":current["permanent_id"],
            "broker_status":"Filled",
            "error_code":"","error_message":"","why_held":"","warning_text":"",
            "advanced_reject":None,"trade_log":[],
            "occurred_at":now.isoformat(),"operator_requested":False,
        })

    def place_order(self, payload):
        if self.killed: raise RuntimeError("Gateway kill switch is active")
        if payload["internal_id"] in self.orders:
            return self.orders[payload["internal_id"]]
        self.next_order_id += 1; oid = str(self.next_order_id)
        account=str(payload.get("account") or self.account_id or "DU-MOCK")
        current={**payload,**self._contract_fields(payload),"account":account,
            "broker_order_id":oid,"permanent_id":f"9{oid}","status":"Submitted",
            "broker_status":"Submitted","filled_quantity":"0",
            "remaining_quantity":str(payload["quantity"]),"average_fill_price":"0",
            "error_code":"","error_message":"","why_held":"","warning_text":"",
            "advanced_reject":None,"trade_log":[],"operator_requested":False}
        self.orders[payload["internal_id"]]=current
        if self.auto_fill:
            self._fill_order(current)
        return current
    def place_option_order(self,payload):
        if str(payload.get("asset_class") or "").upper()!="OPT":raise ValueError("Option order requires asset_class OPT")
        if int(payload.get("conid") or 0)<=0:raise ValueError("Option order requires a qualified conId")
        return self.place_order(payload)
    def modify_order(self, payload):
        current = self.orders[payload["internal_id"]]; current.update(payload); current["status"]="Submitted"; return current
    def cancel_order(self, payload):
        current = self.orders[payload["internal_id"]]; current["status"]="Cancelled"
        self.order_events.append({"source_event_id":f"mock-cancel:{payload['internal_id']}","internal_id":payload["internal_id"],
            "broker_order_id":current["broker_order_id"],"permanent_id":"","broker_status":"Cancelled","error_code":"",
            "error_message":"","why_held":"","warning_text":"","advanced_reject":None,"trade_log":[],
            "occurred_at":None,"operator_requested":True})
        return current
    def refresh_state(self):
        accounts=[{"account_id":self.account_id}] if self.account_id else []
        summary=[]
        if self.account_id:
            summary=[
                {"account":self.account_id,"tag":"NetLiquidation","value":"100000","currency":"USD","model_code":""},
                {"account":self.account_id,"tag":"AvailableFunds","value":"100000","currency":"USD","model_code":""},
                {"account":self.account_id,"tag":"BuyingPower","value":"200000","currency":"USD","model_code":""},
            ]
        positions=[{
            **row,
            "quantity":str(row["quantity"]),
            "average_cost":str(row["average_cost"]),
            "market_price":str(row["market_price"]),
        } for row in self.positions.values()]
        open_orders=[row for row in self.orders.values() if row.get("status")!="Filled"]
        completed=[row for row in self.orders.values() if row.get("status")=="Filled"]
        return {"accounts":accounts,"account_summary":summary,"positions":positions,
            "open_orders":open_orders,"completed_orders":completed,
            "executions":list(self.executions),"reconciled":True}
    def wait(self, seconds): time.sleep(seconds)
