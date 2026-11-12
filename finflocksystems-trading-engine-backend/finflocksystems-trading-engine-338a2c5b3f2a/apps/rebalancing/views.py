import json
from decimal import Decimal, InvalidOperation
from django.db import transaction
from apps.core.views import method_guard, response, _serialize
from apps.core.validation import decimal_field, require_fields
from apps.execution.modes import RunType, execution_mode_for_portfolio
from apps.portfolios.models import TradingPortfolio
from apps.allocation.models import RebalancePolicy, RebalanceRun
from .services import plan_rebalance


def policies(request):
    invalid=method_guard(request,"GET")
    if invalid:return invalid
    return response(_serialize(RebalancePolicy.objects.all(),["portfolio_id","instrument_drift_threshold","portfolio_drift_threshold",
        "minimum_trade_notional","minimum_trade_quantity","cash_buffer_percent","fee_buffer","maximum_turnover",
        "sell_before_buy","price_staleness_limit","partial_fill_threshold","mode","enabled","updated_at"]))


def _row(run, detail=False):
    row={"id":run.pk,"portfolio_id":run.portfolio_id,"trigger":run.trigger,"mode":run.mode,
        "run_type":run.run_type,"status":run.status,
        "phase":run.phase,"nav":run.nav,"total_drift":run.total_drift,"planned_turnover":run.planned_turnover,
        "target_source":run.target_source,"optimization_run_id":run.optimization_run_id,
        "construction_run_id":run.construction_run_id,
        "created_at":run.created_at,"last_recalculated_at":run.last_recalculated_at}
    if detail:
        row["targets"]=_serialize(run.targets.select_related("instrument"),["instrument_id","target_weight","current_weight","drift",
            "current_quantity","target_quantity","trade_quantity","reference_price","estimated_cost","suppressed","suppression_reason","rank"])
        row["intents"]=_serialize(run.orderintent_set.all(),["instrument_id","side","quantity","eligible","execution_priority","idempotency_key"])
    return row


def execute(request, preview=False):
    if request.method != "POST":return response(status=405,error={"code":"METHOD_NOT_ALLOWED","message":"POST required","details":{}})
    key=request.headers.get("Idempotency-Key")
    if not key:return response(status=400,error={"code":"IDEMPOTENCY_KEY_REQUIRED","message":"Idempotency-Key header is required","details":{}})
    try:
        payload=json.loads(request.body or b"{}")
        if not isinstance(payload,dict):raise ValueError("Request body must be a JSON object")
        unknown=set(payload)-{"portfolio_id","trigger","prices","nav"}
        if unknown:raise ValueError(f"Unsupported rebalance fields: {', '.join(sorted(unknown))}")
        require_fields(payload,"portfolio_id")
        portfolio=TradingPortfolio.objects.select_related("account","gateway_session").get(pk=payload["portfolio_id"])
        mode=execution_mode_for_portfolio(portfolio)
        run_type=RunType.PREVIEW if preview else RunType.EXECUTION
        if payload.get("prices") is not None and not isinstance(payload["prices"],dict):raise ValueError("prices must be an object")
        prices={}
        for key,value in payload.get("prices",{}).items():
            checked=decimal_field({"price":value},"price",required=True,positive=True,allow_zero=False)
            prices[int(key) if str(key).isdigit() else key]=str(checked)
        nav=decimal_field(payload,"nav",positive=True,allow_zero=False)
        run=plan_rebalance(portfolio,payload.get("trigger","MANUAL"),key,prices=prices or None,
            nav=nav,mode=mode,strict_market_state=not bool(prices),defer=True,
            run_type=run_type,
            retry_failed=request.headers.get("Idempotency-Retry","").strip().lower() in {"1","true","yes"})
        if run.status=="FAILED":
            return response(status=409,error={"code":"REBALANCE_RETRY_REQUIRED","message":run.last_error or "Failed rebalance requires an explicit retry","details":{"retryable":run.retryable}})
        from .tasks import execute_rebalance_run
        transaction.on_commit(lambda:execute_rebalance_run.delay(portfolio.pk,payload.get("trigger","MANUAL"),key,
            prices or None,nav,mode,not bool(prices),None,run_type))
        return response(_row(run,True),status=202)
    except (KeyError,ValueError,InvalidOperation,TradingPortfolio.DoesNotExist) as exc:
        return response(status=400,error={"code":"INVALID_REBALANCE","message":str(exc),"details":{}})


def runs(request, run_id=None):
    invalid=method_guard(request,"GET")
    if invalid:return invalid
    if run_id:
        try:return response(_row(RebalanceRun.objects.get(pk=run_id),True))
        except RebalanceRun.DoesNotExist:return response(status=404,error={"code":"NOT_FOUND","message":"Rebalance run not found","details":{}})
    return response([_row(x) for x in RebalanceRun.objects.order_by("-created_at")[:100]])
