import json
from decimal import Decimal, InvalidOperation
from django.views.decorators.csrf import csrf_exempt
from apps.core.views import method_guard, response
from apps.core.validation import decimal_field, require_fields
from apps.instruments.models import Instrument
from apps.portfolios.models import TradingPortfolio
from .models import PositionSizingDecision, PositionSizingPolicy
from .services import size_and_record


def _row(item):
    return {key:getattr(item,key) for key in ["id","policy_id","instrument_id","side","target_quantity","risk_quantity",
        "weight_quantity","liquidity_quantity","cash_quantity","broker_quantity","approved_quantity","entry_price",
        "stop_price","risk_budget","binding_constraint","limits","calculation_version","rejected_reason","created_at"]}


@csrf_exempt
def preview(request):
    if request.method != "POST":return response(status=405,error={"code":"METHOD_NOT_ALLOWED","message":"POST required","details":{}})
    key=request.headers.get("Idempotency-Key")
    if not key:return response(status=400,error={"code":"IDEMPOTENCY_KEY_REQUIRED","message":"Idempotency-Key header is required","details":{}})
    try:
        p=json.loads(request.body or b"{}")
        if not isinstance(p,dict):raise ValueError("Request body must be a JSON object")
        allowed={"portfolio_id","instrument_id","side","target_quantity","entry_price","stop_price","nav",
            "available_cash","adv","broker_limits"}
        unknown=set(p)-allowed
        if unknown:raise ValueError(f"Unsupported sizing fields: {', '.join(sorted(unknown))}")
        require_fields(p,"portfolio_id","instrument_id","target_quantity","entry_price")
        side=str(p.get("side") or "BUY").upper()
        if side not in {"BUY","SELL"}:raise ValueError("side must be BUY or SELL")
        target=decimal_field(p,"target_quantity",required=True,positive=True,allow_zero=False)
        entry=decimal_field(p,"entry_price",required=True,positive=True,allow_zero=False)
        stop=decimal_field(p,"stop_price",positive=True,allow_zero=False)
        nav=decimal_field(p,"nav",positive=True,allow_zero=False)
        available=decimal_field(p,"available_cash",positive=True,allow_zero=True)
        adv=decimal_field(p,"adv",positive=True,allow_zero=True)
        if p.get("broker_limits") is not None and not isinstance(p["broker_limits"],dict):raise ValueError("broker_limits must be an object")
        portfolio=TradingPortfolio.objects.select_related("account").get(pk=p["portfolio_id"])
        instrument=Instrument.objects.get(pk=p["instrument_id"])
        if not instrument.active or not instrument.tradable:raise ValueError("Instrument must be active and tradable")
        policy=PositionSizingPolicy.objects.filter(portfolio=portfolio,enabled=True).first() or PositionSizingPolicy.objects.create(portfolio=portfolio)
        item=size_and_record(policy,instrument,side,target,entry,stop,
            nav if nav is not None else Decimal(portfolio.account.net_liquidation),
            available if available is not None else Decimal(portfolio.account.available_cash),
            adv if adv is not None else Decimal(0),broker_limits=p.get("broker_limits",{}),idempotency_key=key)
        return response(_row(item),status=201)
    except (KeyError,ValueError,InvalidOperation,TradingPortfolio.DoesNotExist,Instrument.DoesNotExist) as exc:
        return response(status=400,error={"code":"INVALID_SIZING","message":str(exc),"details":{}})


def decision(request, decision_id):
    invalid=method_guard(request,"GET")
    if invalid:return invalid
    try:return response(_row(PositionSizingDecision.objects.get(pk=decision_id)))
    except PositionSizingDecision.DoesNotExist:return response(status=404,error={"code":"NOT_FOUND","message":"Sizing decision not found","details":{}})
