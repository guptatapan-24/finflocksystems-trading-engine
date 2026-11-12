import hashlib
import json
from datetime import datetime
from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from apps.broker_gateway.client import GatewayClient, GatewaySessionUnavailable
from apps.audit.models import OutboxEvent
from .models import BrokerContract, Instrument, OptionContract


TRADING_CALENDAR_BY_EXCHANGE = {
    "NSE": "XNSE",
    "XNSE": "XNSE",
    "BSE": "XBOM",
    "BOM": "XBOM",
    "XBOM": "XBOM",
}


def trading_calendar_for(exchange, primary_exchange="", currency=""):
    for value in (primary_exchange, exchange):
        normalized = str(value or "").strip().upper()
        if normalized in TRADING_CALENDAR_BY_EXCHANGE:
            return TRADING_CALENDAR_BY_EXCHANGE[normalized]
    return "XNSE" if str(currency or "").strip().upper() == "INR" else "XNYS"


def _gateway(gateway=None,gateway_session=None):
    if gateway is not None:return gateway
    if gateway_session is None:
        raise GatewaySessionUnavailable("A broker gateway session is required")
    return GatewayClient(gateway_session,require_commands=True)


def _qualification_key(payload, kind="contract"):
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:48]
    return f"qualify:{kind}:{digest}"


def _value(row, *names, default=""):
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return default


def _asset_class(row):
    value = str(_value(row, "asset_class", "sec_type", "secType", default="")).upper()
    return "OPT" if value in {"OPT", "OPTION"} else value


def _right(row):
    value = str(_value(row, "right", "option_right", default="")).upper()
    return {"CALL": "C", "PUT": "P"}.get(value, value)


def _expiration(row):
    raw = str(_value(row, "expiration", "expiry", "last_trade_date", "lastTradeDateOrContractMonth", default="")).strip()
    if not raw:
        return None
    compact = raw.replace("-", "")[:8]
    try:
        return datetime.strptime(compact, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError(f"Invalid option expiration returned by IBKR: {raw}") from exc


def _option_values(row):
    if _asset_class(row) != "OPT":
        return None
    expiration = _expiration(row)
    right = _right(row)
    strike = _value(row, "strike", default=None)
    if not expiration or right not in {"C", "P"} or strike in (None, ""):
        raise ValueError("IBKR option contract is missing expiration, strike, or call/put right")
    raw_multiplier = _value(row, "multiplier", default=None)
    if raw_multiplier in (None, ""):
        raise ValueError("IBKR option contract is missing its multiplier")
    multiplier = Decimal(str(raw_multiplier))
    if multiplier <= 0:
        raise ValueError("IBKR option contract multiplier must be positive")
    return {
        "expiration": expiration,
        "strike": Decimal(str(strike)),
        "right": right,
        "trading_class": str(_value(row, "trading_class", "tradingClass", default="")),
        "multiplier": multiplier,
        "underlying_conid": int(_value(row, "underlying_conid", "underConId", default=0)) or None,
        "style": str(_value(row, "style", default="UNKNOWN")).upper(),
        "settlement": str(_value(row, "settlement", default="UNKNOWN")).upper(),
    }


def _contract_row(row, existing=None):
    asset_class = _asset_class(row)
    option = _option_values(row)
    return {
        "symbol": str(_value(row, "symbol")),
        "local_symbol": str(_value(row, "local_symbol", "localSymbol", "symbol")),
        "conid": int(_value(row, "conid", "conId", default=0)),
        "asset_class": asset_class,
        "exchange": str(_value(row, "exchange")),
        "primary_exchange": str(_value(row, "primary_exchange", "primaryExchange")),
        "currency": str(_value(row, "currency")),
        "description": str(_value(row, "description", "long_name", default="")),
        "instrument_id": existing.instrument_id if existing else None,
        "expiration": option["expiration"].isoformat() if option else None,
        "strike": str(option["strike"]) if option else None,
        "right": option["right"] if option else None,
        "multiplier": str(option["multiplier"]) if option else str(_value(row, "multiplier", default="1")),
        "trading_class": option["trading_class"] if option else "",
        "underlying_conid": option["underlying_conid"] if option else None,
    }


def _validate_qualified_selection(requested, result):
    requested_conid = int(_value(requested, "conid", "conId", default=0))
    result_conid = int(_value(result, "conid", "conId", default=0))
    if not requested_conid or result_conid != requested_conid:
        raise ValueError("IBKR qualified a different contract than the selected conId")
    requested_class = _asset_class(requested)
    result_class = _asset_class(result)
    if requested_class and result_class != requested_class:
        raise ValueError("IBKR qualified a different security type than the selected contract")
    if requested_class == "OPT":
        expected = _option_values(requested)
        actual = _option_values(result)
        for field in ("expiration", "strike", "right", "multiplier", "trading_class"):
            if expected[field] != actual[field]:
                raise ValueError(f"IBKR qualified an option with a different {field}")
    requested_currency = str(_value(requested, "currency")).upper()
    result_currency = str(_value(result, "currency")).upper()
    if requested_currency and result_currency != requested_currency:
        raise ValueError("IBKR qualified a contract in a different currency")


def _validate_qualified_option(requested, result):
    if _asset_class(result) != "OPT":
        raise ValueError("IBKR did not qualify an option contract")
    expected = _option_values({**requested, "asset_class": "OPT"})
    actual = _option_values(result)
    for field in ("expiration", "strike", "right", "multiplier", "trading_class"):
        if expected[field] != actual[field]:
            raise ValueError(f"IBKR qualified an option with a different {field}")
    requested_underlying = expected["underlying_conid"]
    if requested_underlying and actual["underlying_conid"] != requested_underlying:
        raise ValueError("IBKR qualified an option for a different underlying")
    requested_currency = str(_value(requested, "currency")).upper()
    if requested_currency and str(_value(result, "currency")).upper() != requested_currency:
        raise ValueError("IBKR qualified an option in a different currency")
    if int(_value(result, "conid", "conId", default=0)) <= 0:
        raise ValueError("IBKR qualified option has no conId")


def search_broker_instruments(query, gateway=None, gateway_session=None, *, asset_classes=None,
                              country=None, currency=None):
    query=str(query or "").strip()
    if len(query)<2:raise ValueError("Instrument search query must contain at least 2 characters")
    requested = tuple(str(value).upper() for value in (asset_classes or ("STK", "OPT")))
    if any(value not in {"STK", "IND", "OPT"} for value in requested):
        raise ValueError("asset_classes may contain only STK, IND, and OPT")
    client = _gateway(gateway,gateway_session)
    try:
        rows=client.search_contracts(query,asset_classes=requested,country=country,currency=currency)
    except TypeError as exc:
        if "unexpected keyword" not in str(exc):
            raise
        # Compatibility with injected/older gateway clients; filtering remains authoritative here.
        rows=client.search_contracts(query)
    results=[]
    for row in rows:
        conid=int(_value(row,"conid","conId",default=0))
        asset_class=_asset_class(row)
        row_currency=str(_value(row,"currency")).upper()
        exchanges={str(_value(row,"exchange")).upper(),str(_value(row,"primary_exchange","primaryExchange")).upper()}
        if conid<=0 or not _value(row,"symbol") or asset_class not in requested:continue
        if currency and row_currency != str(currency).upper():continue
        if str(country or "").upper()=="IN" and not exchanges.intersection({"NSE","NFO","BSE","BFO","SMART"}):continue
        existing=BrokerContract.objects.select_related("instrument").filter(conid=conid).first()
        try:
            results.append(_contract_row(row,existing))
        except ValueError:
            if requested == ("OPT",):
                raise
            continue
    return results


def option_chain(*, underlying_instrument, gateway=None, gateway_session=None):
    contract = getattr(underlying_instrument, "broker_contract", None)
    if not contract or not contract.conid:
        raise ValueError("Qualify the underlying IBKR contract before loading its option chain")
    if underlying_instrument.asset_class not in {"STK", "IND"}:
        raise ValueError("Option-chain underlying must be a stock or index")
    payload = {
        "underlying_conid": contract.conid,
        "symbol": underlying_instrument.symbol,
        "asset_class": underlying_instrument.asset_class,
        "exchange": underlying_instrument.exchange,
        "currency": underlying_instrument.currency,
    }
    result = _gateway(gateway,gateway_session).option_chain(payload)
    chains = []
    for row in result.get("chains", []):
        multiplier = Decimal(str(_value(row, "multiplier", default="0")))
        expirations = sorted({value.isoformat() for value in filter(None, (_expiration({"expiration": item}) for item in row.get("expirations", [])))})
        strikes = sorted({Decimal(str(item)) for item in row.get("strikes", [])})
        if multiplier <= 0 or not expirations or not strikes:
            continue
        chains.append({
            "exchange": str(_value(row, "exchange")),
            "trading_class": str(_value(row, "trading_class", "tradingClass")),
            "multiplier": str(multiplier),
            "expirations": expirations,
            "strikes": [str(value) for value in strikes],
        })
    if not chains:
        raise ValueError("IBKR returned no usable option-chain definitions for this underlying")
    return {"underlying": _contract_row({
        "conid": contract.conid,"symbol": underlying_instrument.symbol,
        "local_symbol": contract.local_symbol,"asset_class": underlying_instrument.asset_class,
        "exchange": underlying_instrument.exchange,"primary_exchange": contract.primary_exchange,
        "currency": underlying_instrument.currency,"description": contract.description,
    },contract),"chains":chains}


@transaction.atomic
def qualify_option_contract(*, underlying_instrument, expiration, strike, right, multiplier,
                            trading_class, exchange, gateway=None, gateway_session=None):
    underlying = getattr(underlying_instrument, "broker_contract", None)
    if not underlying or not underlying.conid:
        raise ValueError("The underlying contract must be qualified first")
    requested = {
        "symbol": underlying_instrument.symbol,"asset_class": "OPT",
        "exchange": exchange or "SMART","currency": underlying_instrument.currency,
        "expiration": expiration,"strike": strike,"right": right,"multiplier": multiplier,
        "trading_class": trading_class,"underlying_conid": underlying.conid,
    }
    key = _qualification_key(requested, "option")
    result = _gateway(gateway,gateway_session).qualify_contract_exact(requested,key)
    _validate_qualified_option(requested,result)
    conid = int(_value(result,"conid","conId"))
    existing = BrokerContract.objects.select_related("instrument").filter(conid=conid).first()
    if existing:
        instrument = existing.instrument
        if instrument.asset_class != "OPT":
            raise ValueError("IBKR option conId is already assigned to a non-option instrument")
    else:
        local_symbol = str(_value(result,"local_symbol","localSymbol",default=f"OPT-{conid}")).upper()
        instrument = Instrument.objects.create(
            symbol=local_symbol,asset_class="OPT",exchange=str(_value(result,"exchange",default=exchange or "SMART")),
            primary_exchange=str(_value(result,"primary_exchange","primaryExchange")),
            currency=str(_value(result,"currency",default=underlying_instrument.currency)),
            multiplier=Decimal(str(_value(result,"multiplier"))),lot_size=1,fractional_support=False,
            trading_calendar=underlying_instrument.trading_calendar,
        )
    contract = record_qualified_contract(instrument,result)
    option = instrument.option_contract
    if option.underlying_id != underlying_instrument.pk:
        option.underlying = underlying_instrument
        option.save(update_fields=["underlying"])
    return instrument, contract, option


def publish_instrument_registry(contract, *, force=False):
    """Queue this contract's registry row for `instrument.registry.v1`.

    `force` re-queues a row that was already published. The registry topic is
    compacted and the Flink normalizer resolves every conId against it, so a
    topic that is recreated -- a broker restart without a durable volume, a
    cluster rebuild -- leaves the normalizer unable to map any conId and every
    market event dead-letters as UNKNOWN_CONID_TIMEOUT. Without `force` the
    idempotency key makes that unrecoverable: the row exists, so nothing is
    ever published again.
    """
    instrument=contract.instrument
    payload={"instrument_id":instrument.pk,
        "conid":contract.conid,"symbol":instrument.symbol,"local_symbol":contract.local_symbol,
        "asset_class":instrument.asset_class,"exchange":instrument.exchange,"primary_exchange":contract.primary_exchange,
        "currency":instrument.currency,"active":instrument.active and instrument.tradable}
    event,created=OutboxEvent.objects.get_or_create(idempotency_key=f"instrument-registry:{contract.conid}",defaults={
        "topic":"instrument.registry.v1","event_type":"instrument.registry.updated","aggregate_type":"instrument",
        "aggregate_id":str(instrument.pk),"partition_key":str(contract.conid),"payload":payload})
    if not created and force:
        event.payload=payload
        event.status="PENDING"
        event.published_at=None
        event.attempt_count=0
        event.last_error=""
        event.available_at=timezone.now()
        event.save(update_fields=[
            "payload","status","published_at","attempt_count","last_error","available_at",
        ])
    return event


def resolve_instrument(*, instrument_id=None, ticker=None, asset_class="STK", exchange="SMART", currency="USD",
                       primary_exchange=None, conid=None, local_symbol=None, description=None, qualify=True, gateway=None,
                       gateway_session=None, expiration=None, strike=None, right=None, multiplier=None,
                       trading_class=None, underlying_conid=None):
    """Resolve operator input to a canonical instrument and qualified IBKR contract."""
    selected_contract=BrokerContract.objects.select_related("instrument").filter(conid=int(conid)).first() if conid else None
    if selected_contract:
        if qualify:
            selected_instrument=selected_contract.instrument
            payload={"symbol":selected_instrument.symbol,"asset_class":selected_instrument.asset_class,
                     "exchange":selected_instrument.exchange,"currency":selected_instrument.currency,
                     "primary_exchange":primary_exchange or selected_contract.primary_exchange or selected_instrument.primary_exchange,
                     "local_symbol":local_symbol or selected_contract.local_symbol or selected_instrument.symbol,
                     "description":description or selected_contract.description}
            payload["conid"]=int(conid)
            if selected_instrument.asset_class == "OPT" and hasattr(selected_instrument,"option_contract"):
                option=selected_instrument.option_contract
                payload.update({"expiration":option.expiration.isoformat(),"strike":str(option.strike),
                    "right":option.right,"multiplier":str(option.multiplier),"trading_class":option.trading_class,
                    "underlying_conid":option.underlying_conid})
            result=_gateway(gateway,gateway_session).qualify_contract_exact(payload,_qualification_key(payload))
            _validate_qualified_selection(payload,result)
            selected_contract=record_qualified_contract(selected_instrument,result)
        publish_instrument_registry(selected_contract)
        return selected_contract.instrument,selected_contract,None
    if instrument_id:
        instrument = Instrument.objects.get(pk=instrument_id)
    else:
        asset_class=str(asset_class or "STK").upper()
        symbol = str((local_symbol if asset_class=="OPT" else ticker) or "").strip().upper()
        if not symbol:
            raise ValueError("ticker or instrument_id is required")
        choices = Instrument.objects.filter(symbol=symbol, asset_class=asset_class, currency=currency)
        if primary_exchange:
            choices = choices.filter(primary_exchange=primary_exchange)
        if choices.count() > 1 and not exchange:
            raise ValueError("Ticker is ambiguous; select an exchange")
        instrument = choices.filter(exchange=exchange).first() or (choices.first() if not conid else None)
        if instrument is None:
            instrument = Instrument.objects.create(symbol=symbol, asset_class=asset_class, exchange=exchange,
                primary_exchange=primary_exchange or "",currency=currency,
                trading_calendar=trading_calendar_for(exchange,primary_exchange,currency))
    if not instrument.active or not instrument.tradable:
        raise ValueError("Instrument is not active/tradable")
    contract = BrokerContract.objects.filter(instrument=instrument).first()
    if contract or not qualify:
        return instrument, contract, None
    client=_gateway(gateway,gateway_session)
    payload={"symbol":str(ticker or instrument.symbol).upper(),"asset_class":instrument.asset_class,"exchange":instrument.exchange,
        "currency":instrument.currency,"primary_exchange":primary_exchange or instrument.primary_exchange,
        "local_symbol":local_symbol or "","description":description or ""}
    if instrument.asset_class=="OPT":
        payload.update({"expiration":expiration,"strike":strike,"right":right,"multiplier":multiplier,
            "trading_class":trading_class,"underlying_conid":underlying_conid})
    if conid:
        payload["conid"]=int(conid)
        result=client.qualify_contract_exact(payload,_qualification_key(payload))
        _validate_qualified_selection(payload,result)
        return instrument,record_qualified_contract(instrument,result),None
    command = client.qualify_contract(payload,_qualification_key(payload))
    return instrument, None, command


@transaction.atomic
def record_qualified_contract(instrument, result):
    if not result.get("conid"):
        raise ValueError("IBKR qualification result has no conId")
    previous=BrokerContract.objects.filter(instrument=instrument).values(
        "conid","primary_exchange","local_symbol").first()
    contract=BrokerContract.objects.update_or_create(instrument=instrument, defaults={"conid":int(result["conid"]),
        "primary_exchange":result.get("primary_exchange", ""), "local_symbol":result.get("local_symbol", instrument.symbol),
        "description":result.get("description", ""),"qualified_at":timezone.now()})[0]
    option_values=_option_values(result)
    if instrument.asset_class=="OPT":
        if option_values is None:
            raise ValueError("IBKR qualified contract is not an option")
        recorded=OptionContract.objects.filter(instrument=instrument).first()
        if recorded is not None:
            # Position, execution and order rows describe the contract without
            # its contract details, so they carry no underConId, style or
            # settlement. Such a partial row must not erase what a full
            # qualification recorded: the option-short risk check matches a
            # hedge to its short by underlying_conid.
            if option_values["underlying_conid"] is None:
                option_values["underlying_conid"]=recorded.underlying_conid
            if not option_values["trading_class"]:
                option_values["trading_class"]=recorded.trading_class
            for field in ("style","settlement"):
                if option_values[field]=="UNKNOWN":
                    option_values[field]=getattr(recorded,field)
        underlying=None
        if option_values["underlying_conid"]:
            underlying_contract=BrokerContract.objects.select_related("instrument").filter(
                conid=option_values["underlying_conid"]
            ).first()
            underlying=underlying_contract.instrument if underlying_contract else None
            if underlying is None and recorded is not None and recorded.underlying_conid==option_values["underlying_conid"]:
                underlying=recorded.underlying
        OptionContract.objects.update_or_create(
            instrument=instrument,defaults={**option_values,"underlying":underlying}
        )
        if instrument.multiplier != option_values["multiplier"]:
            instrument.multiplier=option_values["multiplier"]
            instrument.save(update_fields=["multiplier"])
    primary_exchange=result.get("primary_exchange", "") or instrument.primary_exchange
    calendar=trading_calendar_for(instrument.exchange,primary_exchange,instrument.currency)
    updates=[]
    if instrument.asset_class == "OPT":
        from decimal import ROUND_CEILING
        minimum = result.get("min_size")
        increment = result.get("size_increment")
        if minimum is not None and increment is not None:
            minimum, increment = Decimal(str(minimum)), Decimal(str(increment))
            if not minimum.is_finite() or not increment.is_finite() or minimum <= 0 or increment <= 0:
                raise ValueError("IBKR option minimum size and size increment must be positive")
            instrument.lot_size = (minimum / increment).to_integral_value(rounding=ROUND_CEILING) * increment
            updates.append("lot_size")
        if result.get("min_tick") is not None:
            tick = Decimal(str(result["min_tick"]))
            if not tick.is_finite() or tick <= 0:
                raise ValueError("IBKR option minimum tick must be positive")
            instrument.min_tick = tick
            updates.append("min_tick")
    if primary_exchange and instrument.primary_exchange != primary_exchange:
        instrument.primary_exchange=primary_exchange;updates.append("primary_exchange")
    if instrument.trading_calendar != calendar:
        instrument.trading_calendar=calendar;updates.append("trading_calendar")
    if updates:
        instrument.save(update_fields=[*updates,"updated_at"] if hasattr(instrument,"updated_at") else updates)
    publish_instrument_registry(contract)
    from .models import InstrumentProviderMapping
    mapping,_=InstrumentProviderMapping.objects.get_or_create(instrument=instrument,provider="FINNHUB")
    identity=(contract.conid,contract.primary_exchange,contract.local_symbol)
    if previous and identity!=(previous["conid"],previous["primary_exchange"],previous["local_symbol"]):
        mapping.status="PENDING";mapping.verification_method="";mapping.verified_at=None
        mapping.last_error="IBKR contract identity changed; Finnhub mapping must be reverified"
        mapping.save(update_fields=["status","verification_method","verified_at","last_error","updated_at"])
    return contract
