from contextlib import contextmanager
from datetime import timezone as dt_timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import logging
import uuid
from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from apps.accounts.models import BrokerAccount
from apps.execution.models import Fill
from apps.instruments.models import BrokerContract, Instrument
from apps.oms.models import Order, OrderIntent, OrderStatusHistory
from apps.oms.services import ALLOWED, apply_execution
from apps.portfolios.models import PortfolioPosition, TradingPortfolio
from apps.market_streams.models import MarketDataSubscription
from apps.core.idempotency import canonical_request_hash
from apps.execution.modes import execution_mode_for_portfolio
from .client import GatewayClient
from .models import BrokerPositionSnapshot, BrokerSessionAccount, BrokerSyncCursor

logger = logging.getLogger(__name__)

TERMINAL={"FILLED","CANCELLED","REJECTED","EXPIRED"}
SURVEILLANCE_CONFIRMATION_CODE="201"
STATUS_MAP={
    "PendingSubmit":"SUBMITTED","ApiPending":"SUBMITTED","PreSubmitted":"ACKNOWLEDGED",
    "Submitted":"ACKNOWLEDGED","PendingCancel":"CANCEL_PENDING","ApiCancelled":"CANCELLED",
    "Cancelled":"CANCELLED","Inactive":"REJECTED","ValidationError":"REJECTED","Filled":"FILLED",
    "Expired":"EXPIRED","Unknown":"UNKNOWN",
}

def dec(value, default="0"):
    try: return Decimal(str(value if value not in (None,"") else default).replace(",",""))
    except (InvalidOperation,ValueError): return Decimal(default)

def ensure_account(account_id, gateway_session=None):
    account,_=BrokerAccount.objects.get_or_create(account_id=account_id or "UNKNOWN",defaults={"alias":f"IBKR {account_id or 'UNKNOWN'}"})
    if gateway_session is not None:
        BrokerSessionAccount.objects.update_or_create(
            session=gateway_session,broker_account=account,defaults={"available":True})
        portfolio=TradingPortfolio.objects.filter(account=account,gateway_session=gateway_session).order_by("pk").first()
        if portfolio is None:
            portfolio=TradingPortfolio.objects.create(
                account=account,gateway_session=gateway_session,
                name=f"{gateway_session.display_name} · {account.account_id}"[:128])
    else:
        portfolio,_=TradingPortfolio.objects.get_or_create(account=account,name=f"IBKR {account.account_id}")
    return account,portfolio

def ensure_instrument(row):
    conid=int(row.get("conid") or 0)
    if conid:
        existing=BrokerContract.objects.select_related("instrument").filter(conid=conid).first()
        if existing:
            from apps.instruments.services import record_qualified_contract
            record_qualified_contract(existing.instrument,row)
            return existing.instrument
    asset_class=str(row.get("asset_class") or row.get("sec_type") or "STK").upper()
    if asset_class=="OPTION":asset_class="OPT"
    symbol=(row.get("local_symbol") if asset_class=="OPT" else row.get("symbol")) or row.get("local_symbol") or (f"CONID-{conid}" if conid else "UNKNOWN")
    defaults={"asset_class":asset_class,"exchange":row.get("exchange") or row.get("primary_exchange") or "SMART",
        "primary_exchange":row.get("primary_exchange") or "","currency":row.get("currency") or "USD"}
    instrument=Instrument.objects.filter(symbol=symbol,asset_class=defaults["asset_class"],exchange=defaults["exchange"],
        currency=defaults["currency"],primary_exchange="",broker_contract__isnull=True).first()
    if instrument and defaults["primary_exchange"]:
        instrument.primary_exchange=defaults["primary_exchange"];instrument.save(update_fields=["primary_exchange"])
    if not instrument:instrument,_=Instrument.objects.get_or_create(symbol=symbol,**defaults)
    if conid:
        contract,_=BrokerContract.objects.get_or_create(instrument=instrument,defaults={"conid":conid,
        "primary_exchange":row.get("primary_exchange") or "","local_symbol":row.get("local_symbol") or symbol,
        "description":row.get("description") or "","qualified_at":timezone.now()})
        from apps.instruments.services import record_qualified_contract
        contract=record_qualified_contract(instrument,{**row,"conid":conid,"asset_class":asset_class})
    return instrument

def sync_accounts(rows, gateway_session=None):
    for row in rows:
        account_id=row if isinstance(row,str) else row.get("account_id") or row.get("account")
        if account_id: ensure_account(account_id,gateway_session)

def sync_account_summary(rows, gateway_session=None):
    grouped={}
    for row in rows:
        account_id=row.get("account"); tag=row.get("tag")
        if account_id and tag: grouped.setdefault(account_id,{})[tag]=row
    for account_id,values in grouped.items():
        account,_=ensure_account(account_id,gateway_session)
        def value(*tags):
            for tag in tags:
                if tag in values: return dec(values[tag].get("value"))
            return Decimal(0)
        base=values.get("NetLiquidation",{}).get("currency") or values.get("TotalCashValue",{}).get("currency") or account.base_currency
        account.base_currency=base if base and base != "BASE" else account.base_currency
        account.net_liquidation=value("NetLiquidation")
        account.available_cash=value("AvailableFunds","TotalCashValue","CashBalance")
        account.buying_power=value("BuyingPower")
        account.daily_pnl=value("DailyPnL","RealizedPnL")
        account.save(update_fields=["base_currency","net_liquidation","available_cash","buying_power","daily_pnl","updated_at"])
        from apps.risk.models import CapitalReservation
        CapitalReservation.objects.filter(account=account,status="CONSUMED").update(status="RELEASED",released_at=timezone.now())

def _position_snapshot_key(account_id, rows, snapshot_key=None, gateway_session=None):
    session_key=str(gateway_session.pk) if gateway_session is not None else ""
    if snapshot_key:
        return (f"{session_key}:{snapshot_key}" if session_key else str(snapshot_key))[:160]
    body = json.dumps({"account": account_id, "positions": rows}, sort_keys=True, default=str, separators=(",", ":"))
    prefix=f"{session_key}:" if session_key else ""
    return f"positions:{prefix}{account_id}:{hashlib.sha256(body.encode()).hexdigest()}"[:160]


def _sync_account_positions(account_id, rows, *, complete, snapshot_key, gateway_session=None):
    account, default_portfolio = ensure_account(account_id,gateway_session)
    key = _position_snapshot_key(account.account_id, rows, snapshot_key,gateway_session)
    snapshot, _ = BrokerPositionSnapshot.objects.get_or_create(
        snapshot_key=key,
        defaults={
            "broker_account": account,
            "session": gateway_session,
            "complete": bool(complete),
            "row_count": len(rows),
            "positions": rows,
        },
    )
    if snapshot.broker_account_id != account.pk:
        raise ValueError("Position snapshot key was reused for another broker account")
    if snapshot.status == "COMPLETED":
        return snapshot
    if not complete:
        snapshot.complete = False
        snapshot.status = "INCOMPLETE"
        snapshot.row_count = len(rows)
        snapshot.positions = rows
        snapshot.attempt_count += 1
        snapshot.last_error = "Snapshot was not confirmed complete and was not applied"
        snapshot.save(update_fields=["complete", "status", "row_count", "positions", "attempt_count", "last_error"])
        return snapshot
    try:
        with transaction.atomic():
            snapshot = BrokerPositionSnapshot.objects.select_for_update().get(pk=snapshot.pk)
            if snapshot.status == "COMPLETED":
                return snapshot
            snapshot.status = "PROCESSING"
            snapshot.complete = True
            snapshot.row_count = len(rows)
            snapshot.positions = rows
            snapshot.attempt_count += 1
            snapshot.last_error = ""
            snapshot.save(update_fields=["status", "complete", "row_count", "positions", "attempt_count", "last_error"])

            positions_by_instrument = {}
            for row in rows:
                row_account = str(row.get("account") or account.account_id)
                if row_account != account.account_id:
                    raise ValueError(
                        f"Position snapshot for {account.account_id} contains row for {row_account}"
                    )
                instrument = ensure_instrument(row)
                if instrument.pk in positions_by_instrument:
                    raise ValueError(
                        f"Position snapshot contains duplicate contract for instrument {instrument.pk}"
                    )
                positions_by_instrument[instrument.pk] = (instrument, row)

            portfolios_query=TradingPortfolio.objects.select_for_update().filter(account=account)
            if gateway_session is not None:portfolios_query=portfolios_query.filter(gateway_session=gateway_session)
            portfolios = list(portfolios_query)
            if not portfolios:
                portfolios = [default_portfolio]
            authoritative_ids = set(positions_by_instrument)
            missing = PortfolioPosition.objects.select_for_update().filter(portfolio__in=portfolios)
            if authoritative_ids:
                missing = missing.exclude(instrument_id__in=authoritative_ids)
            missing.update(quantity=0, average_cost=0, market_price=0)
            for portfolio in portfolios:
                for instrument, row in positions_by_instrument.values():
                    PortfolioPosition.objects.update_or_create(
                        portfolio=portfolio,
                        instrument=instrument,
                        defaults={
                            "quantity": dec(row.get("quantity")),
                            "average_cost": dec(row.get("average_cost")),
                            "market_price": dec(row.get("market_price")),
                        },
                    )
            snapshot.status = "COMPLETED"
            snapshot.completed_at = timezone.now()
            snapshot.save(update_fields=["status", "completed_at"])
            return snapshot
    except Exception as exc:
        BrokerPositionSnapshot.objects.filter(pk=snapshot.pk).update(
            status="FAILED", last_error=str(exc)[:1000], completed_at=None,
            attempt_count=F("attempt_count") + 1,
        )
        raise


def sync_positions(rows, *, account_id=None, complete=False, snapshot_key=None, gateway_session=None):
    grouped = {}
    if account_id:
        grouped[str(account_id)] = []
    for row in rows:
        row_account = str(row.get("account") or account_id or "")
        if not row_account:
            raise ValueError("Position snapshot row is missing a broker account")
        grouped.setdefault(row_account, []).append(row)
    snapshots = []
    for grouped_account, account_rows in sorted(grouped.items()):
        account_key = f"{snapshot_key}:{grouped_account}" if snapshot_key and len(grouped) > 1 else snapshot_key
        snapshots.append(
            _sync_account_positions(
                grouped_account,
                account_rows,
                complete=bool(complete),
                snapshot_key=account_key,
                gateway_session=gateway_session,
            )
        )
    return snapshots

def _session_prefix(gateway_session):
    return str(gateway_session.pk) if gateway_session is not None else "legacy"


def _order_query(gateway_session):
    query=Order.objects.all()
    return query.filter(intent__portfolio__gateway_session=gateway_session) if gateway_session is not None else query


def _matching_order(row, gateway_session):
    # internal_id is the orderRef we minted (uuid4), so it names our order no
    # matter which session placed it. A replacement session on the same IBKR
    # account still receives the earlier session's orders; a session-scoped
    # lookup missed them and re-imported them under the same internal_id.
    # Broker order ids are only unique per connection, so they stay scoped.
    order=Order.objects.filter(internal_id=row["internal_id"]).first() if row.get("internal_id") else None
    query=_order_query(gateway_session)
    if not order and row.get("permanent_id"):order=query.filter(broker_permanent_id=str(row["permanent_id"])).first()
    if not order and row.get("broker_order_id"):order=query.filter(broker_order_id=str(row["broker_order_id"])).first()
    return order


def _subscription_identity(key):
    parts=str(key or "").split(":")
    if len(parts)<2:return None,None
    return parts[-2],parts[-1]


def _external_order(row, instrument, portfolio, gateway_session=None):
    identity=row.get("permanent_id") or row.get("broker_order_id")
    session_key=_session_prefix(gateway_session)
    internal=(row.get("internal_id") or f"IBKR-{session_key[:12]}-{portfolio.account.account_id}-{identity}")[:64]
    intent_payload={"portfolio_id":portfolio.pk,"instrument_id":instrument.pk,
        "side":"BUY" if row.get("side") in {"BUY","BOT"} else "SELL","quantity":dec(row.get("quantity")),
        "order_type":row.get("order_type") or "MKT","limit_price":row.get("limit_price"),
        "stop_price":row.get("stop_price"),"time_in_force":row.get("time_in_force") or "DAY"}
    intent,_=OrderIntent.objects.get_or_create(idempotency_key=f"broker-import:{session_key}:{portfolio.account.account_id}:{identity}"[:128],
        defaults={"request_hash":canonical_request_hash("broker_order_import",intent_payload),"portfolio":portfolio,
        "instrument":instrument,"side":intent_payload["side"],"quantity":intent_payload["quantity"],
        "order_type":intent_payload["order_type"],"limit_price":intent_payload["limit_price"],
        "stop_price":intent_payload["stop_price"],"time_in_force":intent_payload["time_in_force"],
        "source":"BROKER_IMPORT","origin":OrderIntent.Origin.BROKER_IMPORT,
        "mode":execution_mode_for_portfolio(portfolio),"operation_status":"COMPLETED"})
    if intent.origin==OrderIntent.Origin.BROKER_IMPORT and intent.operation_status!="COMPLETED":
        intent.operation_status="COMPLETED";intent.operation_error="";intent.retryable=False
        intent.save(update_fields=["operation_status","operation_error","retryable"])
    order,created=Order.objects.get_or_create(intent=intent,defaults={"internal_id":internal,"quantity":intent.quantity,"status":"ACKNOWLEDGED","broker_order_id":str(row.get("broker_order_id") or ""),"broker_permanent_id":str(row.get("permanent_id") or "")})
    if created: OrderStatusHistory.objects.create(order=order,from_status="",to_status="ACKNOWLEDGED",source="broker_import",reason="Discovered at IBKR",event_key=f"broker-import:{session_key}:{portfolio.account.account_id}:{identity}:ack"[:128])
    return order

def _broker_reason(row):
    return str(row.get("error_message") or row.get("why_held") or row.get("warning_text") or "")[:255]


def _complete_broker_import_intent(order):
    intent=order.intent
    if intent.origin==OrderIntent.Origin.BROKER_IMPORT and intent.operation_status!="COMPLETED":
        intent.operation_status="COMPLETED";intent.operation_error="";intent.retryable=False
        intent.save(update_fields=["operation_status","operation_error","retryable"])


def _surveillance_confirmation(row, order):
    message=str(row.get("error_message") or row.get("warning_text") or row.get("why_held") or "")
    return (
        str(row.get("error_code") or "").strip()==SURVEILLANCE_CONFIRMATION_CODE
        and "security is under surveillance measure" in message.lower()
        and "would you like to continue" in message.lower()
        and order.intent.origin==OrderIntent.Origin.MANUAL
        and order.filled_quantity==0
    )

def _advanced_override_codes(row):
    return [option["code"] for option in _advanced_override_options(row)]

def _advanced_override_options(row):
    supplied=row.get("advanced_override_options") or []
    options=[]
    def add(code,text=""):
        import re
        code=str(code or "").strip()
        if code and re.fullmatch(r"[A-Za-z0-9_.:-]+",code) and code not in {item["code"] for item in options}:
            options.append({"code":code,"text":str(text or "").strip()})
    for option in supplied:
        if isinstance(option,dict):add(option.get("code"),option.get("text"))
    for code in row.get("advanced_override_codes") or []:
        add(code)
    if options:return options
    advanced=row.get("advanced_reject")
    error_data=advanced.get("errorData") if isinstance(advanced,dict) else None
    raw=error_data.get("rejectEventCode") if isinstance(error_data,dict) else None
    values=raw if isinstance(raw,list) else str(raw or "").split(",")
    for code in values:add(code)
    for reject in advanced.get("rejects",[]) if isinstance(advanced,dict) else []:
        for button in reject.get("buttons",[]) if isinstance(reject,dict) else []:
            for option in button.get("options",[]) if isinstance(button,dict) else []:
                if not isinstance(option,dict):continue
                for field in str(option.get("fixstr") or "").split(";"):
                    tag,separator,codes=field.strip().partition("=")
                    if separator and tag.strip()=="8229":
                        for code in codes.split(","):add(code,option.get("text"))
    return options

def _record_broker_status(order,row,event_key,source="ibkr",target_override=None):
    broker_status=str(row.get("broker_status") or row.get("status") or "")
    target=target_override or STATUS_MAP.get(broker_status)
    error_code=str(row.get("error_code") or "").strip()
    confirmation_candidate=_surveillance_confirmation(row,order)
    details={"error_message":str(row.get("error_message") or ""),"why_held":str(row.get("why_held") or ""),
        "warning_text":str(row.get("warning_text") or ""),"advanced_reject":row.get("advanced_reject"),
        "trade_log":row.get("trade_log") or [],"broker_order_id":str(row.get("broker_order_id") or ""),
        "permanent_id":str(row.get("permanent_id") or ""),
        "original_broker_order_id":str(row.get("broker_order_id") or order.broker_order_id or ""),
        "advanced_override_codes":_advanced_override_codes(row),
        "advanced_override_options":_advanced_override_options(row)}
    occurred=parse_datetime(str(row.get("occurred_at") or "")) or timezone.now()
    # A single IBKR rejection is reported through error/order-status callbacks
    # and later repeated in open/completed-order snapshots.  Broker order id plus
    # error code identifies that logical rejection across all of those sources.
    if confirmation_candidate:
        broker_order_id=details["original_broker_order_id"]
        existing=order.status_history.filter(
            reason_code=error_code,
            details__original_broker_order_id=broker_order_id,
        ).order_by("occurred_at","pk").first()
        if existing:
            return existing
    history,created=OrderStatusHistory.objects.get_or_create(event_key=event_key[:128],defaults={"order":order,
        "from_status":order.status,"to_status":"BROKER_BLOCKED" if confirmation_candidate else target or order.status,"source":source,"broker_status":broker_status,
        "reason_code":str(row.get("error_code") or "")[:64],"reason":_broker_reason(row),"details":details,
        "occurred_at":occurred,"operator_requested":bool(row.get("operator_requested"))})
    requires_confirmation=created and confirmation_candidate
    if requires_confirmation:
        warning=str(row.get("error_message") or row.get("warning_text") or row.get("why_held")
            or f"IBKR precautionary warning {error_code}")[:1000]
        if order.status not in TERMINAL and "BROKER_BLOCKED" in ALLOWED.get(order.status,set()):
            order.status="BROKER_BLOCKED";order.save(update_fields=["status","updated_at"])
        OrderIntent.objects.filter(pk=order.intent_id).update(
            operation_status="CONFIRMATION_REQUIRED",operation_error=warning,retryable=False,
        )
        logger.warning(
            "order_execution stage=confirmation_required application_order_id=%s ibkr_order_id=%s warning_code=%s warning=%s advancedOrderRejectJson=%r override_codes=%s",
            order.internal_id, order.broker_order_id or row.get("broker_order_id") or "",
            error_code, warning, row.get("advanced_reject"), details["advanced_override_codes"],
        )
        return history
    if target and target!="FILLED" and order.status not in TERMINAL and target in ALLOWED.get(order.status,set()):
        order.status=target;order.save(update_fields=["status","updated_at"])
        logger.info(
            "order_execution stage=broker_status internal_id=%s broker_order_id=%s warning_code=%s final_status=%s broker_status=%s",
            order.internal_id, order.broker_order_id or row.get("broker_order_id") or "",
            error_code, order.status, broker_status,
        )
        if target in TERMINAL:
            from apps.risk.services import settle_order_reservation
            settle_order_reservation(order,target)
            if order.intent.rebalance_id:
                from apps.rebalancing.services import advance_rebalance
                advance_rebalance(order.intent.rebalance)
    return history

def sync_orders(rows, snapshot, gateway_session=None):
    for row in rows:
        _,portfolio=ensure_account(row.get("account"),gateway_session); instrument=ensure_instrument(row)
        order=_matching_order(row,gateway_session) or _external_order(row,instrument,portfolio,gateway_session)
        _complete_broker_import_intent(order)
        order.broker_order_id=str(row.get("broker_order_id") or order.broker_order_id)
        order.broker_permanent_id=str(row.get("permanent_id") or order.broker_permanent_id)
        order.quantity=max(order.quantity,dec(row.get("quantity")))
        order.save(update_fields=["broker_order_id","broker_permanent_id","quantity","updated_at"])
        identity=json.dumps([snapshot,order.internal_id,row.get("status"),row.get("filled_quantity"),row.get("error_code"),
            row.get("error_message"),row.get("why_held"),row.get("occurred_at")],default=str,separators=(",",":"))
        _record_broker_status(order,row,f"broker-snapshot:{_session_prefix(gateway_session)}:{hashlib.sha256(identity.encode()).hexdigest()}"[:128])

def sync_order_event(row, gateway_session=None):
    order=_matching_order(row,gateway_session)
    if not order:
        _,portfolio=ensure_account(row.get("account"),gateway_session);instrument=ensure_instrument(row)
        order=_external_order({**row,"status":row.get("broker_status")},instrument,portfolio,gateway_session)
    order.broker_order_id=str(row.get("broker_order_id") or order.broker_order_id)
    order.broker_permanent_id=str(row.get("permanent_id") or order.broker_permanent_id)
    order.save(update_fields=["broker_order_id","broker_permanent_id","updated_at"])
    from apps.execution.dispatch import record_broker_order_observation
    record_broker_order_observation(order,row)
    source_id=str(row.get("source_event_id") or "")
    if not source_id:
        source_id=hashlib.sha256(json.dumps(row,sort_keys=True,default=str).encode()).hexdigest()
    event_key=(f"broker-order:{gateway_session.pk}:{source_id}" if gateway_session is not None
        else f"broker-order:{source_id}")
    return _record_broker_status(order,row,event_key[:128])

def record_gateway_command_failure(payload, gateway_session=None):
    from apps.execution.dispatch import record_gateway_command_failed
    command=record_gateway_command_failed(payload,gateway_session)
    if command is not None:return
    command_payload=payload.get("payload") or {};internal_id=str(command_payload.get("internal_id") or "")
    order=_order_query(gateway_session).filter(internal_id=internal_id).first()
    if not order:return
    command_type=str(payload.get("command_type") or "");target={"PLACE_ORDER":"BROKER_BLOCKED",
        "MODIFY_ORDER":"UNKNOWN","CANCEL_ORDER":"UNKNOWN"}.get(command_type)
    row={"broker_status":"GatewayCommandFailed","error_code":"GATEWAY_COMMAND_FAILED",
        "error_message":str(payload.get("error") or "Gateway order command failed"),
        "operator_requested":command_type=="CANCEL_ORDER","occurred_at":payload.get("occurred_at")}
    _record_broker_status(order,row,f"gateway-command:{_session_prefix(gateway_session)}:{payload.get('command_id')}:{command_type}",source="gateway",
        target_override=target)

def sync_executions(rows, gateway_session=None):
    for row in rows:
        execution_id=f"{_session_prefix(gateway_session)}:{row.get('execution_id')}" if gateway_session is not None else row.get("execution_id")
        if not row.get("execution_id"): continue
        existing=Fill.objects.select_related("order").filter(execution_id=execution_id).first()
        if existing:
            order=existing.order
        else:
            _,portfolio=ensure_account(row.get("account"),gateway_session); instrument=ensure_instrument(row)
            query=_order_query(gateway_session)
            order=None
            if row.get("permanent_id"): order=query.filter(broker_permanent_id=str(row["permanent_id"])).first()
            if not order and row.get("broker_order_id"): order=query.filter(broker_order_id=str(row["broker_order_id"])).first()
            if not order:
                synthetic={**row,"internal_id":"","quantity":row.get("quantity"),"order_type":"MKT","time_in_force":"DAY","status":"Submitted"}
                order=_external_order(synthetic,instrument,portfolio,gateway_session)
        _complete_broker_import_intent(order)
        if order.status in {"CREATED","RISK_APPROVED","QUEUED","BROKER_BLOCKED","SUBMITTED","UNKNOWN"}:
            OrderStatusHistory.objects.get_or_create(event_key=f"broker-execution-ready:{order.internal_id}",defaults={"order":order,"from_status":order.status,"to_status":"ACKNOWLEDGED","source":"broker_sync","reason":"Execution received from IBKR"})
            order.status="ACKNOWLEDGED"; order.quantity=max(order.quantity,order.filled_quantity+dec(row.get("quantity"))); order.save(update_fields=["status","quantity","updated_at"])
        reported_at=parse_datetime(row.get("executed_at") or "")
        if reported_at is not None and timezone.is_naive(reported_at):
            # The Gateway reports UTC; the process zone must not be applied.
            reported_at=timezone.make_aware(reported_at,dt_timezone.utc)
        if existing and reported_at is not None and existing.executed_at!=reported_at:
            # The broker's execution time is authoritative. Fills recorded while
            # the Gateway mislocalised zone-less times are corrected when it
            # reports them again.
            Fill.objects.filter(pk=existing.pk).update(executed_at=reported_at)
            existing.executed_at=reported_at
        executed_at=reported_at or (existing.executed_at if existing else timezone.now())
        apply_execution(order,{**row,"execution_id":execution_id,
            "quantity":str(dec(row.get("quantity"),existing.quantity if existing else "0")),
            "price":str(dec(row.get("price"),existing.price if existing else "0")),
            "commission":str(dec(row.get("commission"),existing.commission if existing else "0")),
            "currency":row.get("currency") or (existing.currency if existing else "USD"),
            "executed_at":executed_at})

def process_snapshot(event, gateway_session=None):
    event_type=event.get("event_type","");payload=event.get("payload",{})
    if event_type=="command.qualify.completed":
        ensure_instrument(payload);return
    if event_type=="broker.order":
        sync_order_event(payload,gateway_session);return
    if event_type=="market.error":
        key=str(payload.get("subscription_key") or "");reason=f"IBKR error {payload.get('error_code')}: {payload.get('error_message')}"[:2000]
        if ":" in key:
            instrument_id,timeframe=_subscription_identity(key)
            subscription=MarketDataSubscription.objects.select_related("instrument__broker_contract").filter(
                gateway_session=gateway_session,instrument_id=instrument_id,timeframe=timeframe).first()
            if subscription:
                event_generation=str(payload.get("provider_generation") or "")
                if subscription.active_provider in {"FINNHUB","NONE"}:
                    if payload.get("probe") and event_generation==str(subscription.primary_probe_generation or ""):
                        MarketDataSubscription.objects.filter(pk=subscription.pk).update(
                            primary_probe_generation=None,primary_probe_started_at=None,primary_probe_event_count=0,
                            fallback_state="FALLBACK" if subscription.active_provider=="FINNHUB" else "FAILED",
                            last_error=f"IBKR probe failed: {reason}"[:2000])
                    return
                if event_generation and event_generation!=str(subscription.provider_generation):return
                if not event_generation and settings.MARKET_DATA_FALLBACK_ENABLED:return
                from apps.market_data.fallback import handle_ibkr_failure
                handle_ibkr_failure(subscription,payload.get("error_code"),payload.get("error_message") or "",historical=False)
                subscription.refresh_from_db()
                from apps.market_streams.services import sync_subscription_strategy_lifecycle
                sync_subscription_strategy_lifecycle(subscription)
        return
    if event_type=="market.raw":
        source_key=str(payload.get("source_event_id") or "")
        if not source_key:return
        from apps.market_data.fallback import publish_provider_event
        publish_provider_event(payload,received_at=parse_datetime(str(event.get("created_at") or "")) or timezone.now())
        return
    if event_type in {"command.subscribe_market_data.completed","command.cancel_market_data.completed"}:
        key=str(payload.get("subscription_key") or "")
        if ":" in key:
            instrument_id,timeframe=_subscription_identity(key)
            subscription=MarketDataSubscription.objects.filter(gateway_session=gateway_session,instrument_id=instrument_id,timeframe=timeframe).first()
            if not subscription:return
            if event_type=="command.cancel_market_data.completed":
                if subscription.state=="CANCELLING" or subscription.consumer_count==0:
                    subscription.state="INACTIVE";subscription.active_provider="NONE";subscription.last_error=""
                    subscription.save(update_fields=["state","active_provider","last_error","updated_at"])
            elif payload.get("probe"):
                if str(subscription.primary_probe_generation or "")==str(payload.get("provider_generation") or ""):
                    subscription.fallback_state="RECOVERING";subscription.last_error=""
                    subscription.save(update_fields=["fallback_state","last_error","updated_at"])
            elif not payload.get("provider_generation") or str(subscription.provider_generation)==str(payload.get("provider_generation")):
                subscription.state="ACTIVE"
                subscription.save(update_fields=["state","updated_at"])
                from apps.market_streams.services import sync_subscription_strategy_lifecycle
                sync_subscription_strategy_lifecycle(subscription)
        return
    if event_type in {"command.place_order.completed","command.modify_order.completed","command.cancel_order.completed"}:
        from apps.execution.dispatch import record_gateway_command_completed
        record_gateway_command_completed(payload,gateway_session);return
    if event_type=="command.failed" and payload.get("command_type") in {"SUBSCRIBE_MARKET_DATA","CANCEL_MARKET_DATA"}:
        command_payload=payload.get("payload") or {};key=str(command_payload.get("subscription_key") or "")
        if payload.get("command_type")=="CANCEL_MARKET_DATA" and command_payload.get("resubscribe"):
            # The cancel queued ahead of a resubscribe only clears a stale
            # Gateway entry; its failure says nothing about market data.
            logger.info("market_data stage=resubscribe_cancel_command_failed subscription_key=%s error=%s",
                key,str(payload.get("error") or "")[:500])
            return
        if ":" in key:
            instrument_id,timeframe=_subscription_identity(key);reason=str(payload.get("error") or "Gateway market-data command failed")[:2000]
            subscription=MarketDataSubscription.objects.select_related("instrument__broker_contract").filter(
                gateway_session=gateway_session,instrument_id=instrument_id,timeframe=timeframe).first()
            if subscription and command_payload.get("probe"):
                MarketDataSubscription.objects.filter(pk=subscription.pk).update(
                    primary_probe_generation=None,primary_probe_started_at=None,primary_probe_event_count=0,
                    fallback_state="FALLBACK",last_error=f"IBKR probe failed: {reason}"[:2000])
            elif subscription:
                command_generation=str(command_payload.get("provider_generation") or "")
                if subscription.active_provider!="IBKR":return
                if command_generation and command_generation!=str(subscription.provider_generation):return
                if not command_generation and settings.MARKET_DATA_FALLBACK_ENABLED:return
                from apps.market_data.fallback import handle_ibkr_failure
                handle_ibkr_failure(subscription,message=reason,historical=True)
                subscription.refresh_from_db()
                from apps.market_streams.services import sync_subscription_strategy_lifecycle
                sync_subscription_strategy_lifecycle(subscription)
        return
    if event_type=="command.failed" and payload.get("command_type") in {"PLACE_ORDER","MODIFY_ORDER","CANCEL_ORDER"}:
        record_gateway_command_failure(payload,gateway_session);return
    if event_type=="session.disconnected":
        reason=str(payload.get("error") or "IBKR connection lost")
        occurred=payload.get("occurred_at")
        orders=_order_query(gateway_session).filter(status__in=["SUBMITTED","ACKNOWLEDGED","PARTIALLY_FILLED","CANCEL_PENDING"])
        for order in orders:
            _record_broker_status(order,{"broker_status":"Unknown","error_code":"CONNECTION_LOST",
                "error_message":reason,"occurred_at":occurred},f"broker-disconnect:{event.get('id')}:{order.internal_id}")
        from apps.market_data.fallback import handle_ibkr_failure
        disconnected_at=parse_datetime(str(occurred or ""))or parse_datetime(str(event.get("created_at") or ""))
        if disconnected_at is not None and timezone.is_naive(disconnected_at):
            disconnected_at=timezone.make_aware(disconnected_at,dt_timezone.utc)
        for subscription in MarketDataSubscription.objects.filter(gateway_session=gateway_session,consumer_count__gt=0,active_provider="IBKR").select_related(
                "instrument__broker_contract"):
            if disconnected_at is not None and subscription.requested_at and subscription.requested_at>disconnected_at:
                # The subscription was (re)requested after this disconnect, on
                # a later connection: a replayed old disconnect must not fail it.
                logger.info(
                    "market_data stage=stale_disconnect_ignored subscription_id=%s event_id=%s disconnected_at=%s requested_at=%s",
                    subscription.pk,event.get("id"),disconnected_at.isoformat(),subscription.requested_at.isoformat(),
                )
                continue
            handle_ibkr_failure(subscription,"1100",reason,historical=False)
        return
    rows=payload.get("value",[])
    if event_type=="snapshot.accounts": sync_accounts(rows,gateway_session)
    elif event_type=="snapshot.account_summary": sync_account_summary(rows,gateway_session)
    elif event_type=="snapshot.positions":
        sync_positions(
            rows,
            account_id=payload.get("account"),
            complete=payload.get("complete") is True,
            snapshot_key=payload.get("snapshot_id") or f"gateway-event:{event.get('id')}",
            gateway_session=gateway_session,
        )
    elif event_type=="snapshot.open_orders": sync_orders(rows,"open",gateway_session)
    elif event_type=="snapshot.completed_orders": sync_orders(rows,"completed",gateway_session)
    elif event_type=="snapshot.executions": sync_executions(rows,gateway_session)

# A sync normally takes 0.2-7 s. The expiry only matters if a release is ever
# lost, so it is kept short: at worst one or two 5 s beat ticks are skipped.
_SYNC_LOCK_SECONDS=30


@contextmanager
def _session_sync_lock(gateway_session):
    """Keep two overlapping beat runs from applying the same gateway events twice.

    The lock expires on its own, so a crashed worker can never wedge the sync.
    If Redis is unreachable the sync runs unlocked, exactly as it did before.
    """
    if gateway_session is None:
        yield True;return
    key=f"{settings.REDIS_KEY_PREFIX}broker-gateway:sync-events:{gateway_session.pk}"
    token=uuid.uuid4().hex;redis_client=None
    try:
        import redis
        redis_client=redis.Redis.from_url(settings.REDIS_URL,socket_connect_timeout=2,socket_timeout=2)
        acquired=bool(redis_client.set(key,token,nx=True,ex=_SYNC_LOCK_SECONDS))
    except Exception as exc:
        logger.warning("gateway_sync stage=lock_unavailable session_id=%s error=%s",gateway_session.pk,exc)
        redis_client=None;acquired=True
    try:
        yield acquired
    finally:
        if redis_client is not None and acquired:
            # Plain GET/DEL, not a Lua script: the platform-managed Redis runs
            # with an ACL user, and a silently refused EVAL left the lock held
            # until expiry, skipping every sync in between.
            try:
                if redis_client.get(key)==token.encode():
                    redis_client.delete(key)
            except Exception as exc:
                logger.warning("gateway_sync stage=lock_release_failed session_id=%s error=%s",gateway_session.pk,exc)


def sync_events(client, gateway_session=None, connection_generation=None):
    gateway_session=gateway_session or getattr(client,"gateway_session",None)
    with _session_sync_lock(gateway_session) as acquired:
        if not acquired:
            logger.info("gateway_sync stage=skipped_overlap session_id=%s",gateway_session.pk)
            return 0
        return _sync_events(client,gateway_session,connection_generation)


def _sync_events(client, gateway_session, connection_generation):
    cursor,_=BrokerSyncCursor.objects.get_or_create(session=gateway_session,name="gateway-events")
    generation=str(connection_generation or "").strip()
    if generation and cursor.connection_generation!=generation:
        # A new connection generation means the Gateway reconnected to IBKR.
        # Its event log keeps counting in the same database, so the cursor is
        # kept: rewinding to 0 replays days of history, including the old
        # session.disconnected that re-fails every fresh subscription. A
        # Gateway that restarted with a fresh database is detected below from
        # the sequence it returns.
        with transaction.atomic():
            cursor=BrokerSyncCursor.objects.select_for_update().get(pk=cursor.pk)
            if cursor.connection_generation!=generation:
                cursor.connection_generation=generation
                cursor.last_error=""
                cursor.save(update_fields=["connection_generation","last_error"])
    requested_after=cursor.last_sequence
    events=client.events(requested_after) or []
    if events and requested_after and events[0]["id"]<=requested_after:
        # The Gateway only returns ids at or below `after` when its sequence
        # restarted (a fresh database): it then pages from the beginning.
        logger.warning(
            "gateway_sync stage=sequence_reset session_id=%s previous_sequence=%s first_event_id=%s",
            gateway_session.pk if gateway_session else "",requested_after,events[0]["id"],
        )
        with transaction.atomic():
            cursor=BrokerSyncCursor.objects.select_for_update().get(pk=cursor.pk)
            cursor.last_sequence=0
            cursor.last_synced_at=None
            cursor.save(update_fields=["last_sequence","last_synced_at"])
    for event in events:
        if event["id"] <= cursor.last_sequence:
            continue
        event_type=str(event.get("event_type") or "")
        order_event=event_type.startswith("order.") or event_type.startswith("execution.") or event_type in {
            "command.place_order.completed","command.modify_order.completed",
            "command.cancel_order.completed","command.failed","snapshot.open_orders",
            "snapshot.completed_orders","snapshot.executions",
        }
        if order_event:
            logger.info(
                "order_execution stage=broker_event_received session_id=%s event_id=%s event_type=%s",
                gateway_session.pk if gateway_session else "", event.get("id"), event_type,
            )
        try:
            process_snapshot(event,gateway_session)
        except Exception as exc:
            BrokerSyncCursor.objects.filter(pk=cursor.pk).update(last_error=str(exc)[:1000])
            if order_event:
                logger.exception(
                    "order_execution stage=broker_event_failed session_id=%s event_id=%s event_type=%s",
                    gateway_session.pk if gateway_session else "", event.get("id"), event_type,
                )
            raise
        if order_event:
            logger.info(
                "order_execution stage=broker_event_applied session_id=%s event_id=%s event_type=%s",
                gateway_session.pk if gateway_session else "", event.get("id"), event_type,
            )
        with transaction.atomic():
            cursor=BrokerSyncCursor.objects.select_for_update().get(pk=cursor.pk)
            if event["id"] > cursor.last_sequence:
                cursor.last_sequence=event["id"]
                cursor.last_synced_at=timezone.now()
                cursor.last_error=""
                cursor.save(update_fields=["last_sequence","last_synced_at","last_error"])
    if events: client.ack_events(cursor.last_sequence)
    return len(events)
