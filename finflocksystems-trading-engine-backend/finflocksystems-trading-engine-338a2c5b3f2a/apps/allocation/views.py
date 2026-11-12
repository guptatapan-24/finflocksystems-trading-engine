import json
from decimal import Decimal, InvalidOperation
from django.db import transaction
from apps.core.views import method_guard, response
from apps.core.validation import decimal_field, require_fields
from apps.portfolios.models import TradingPortfolio
from apps.strategies.models import StrategyAllocation
from .models import AllocationRun
from .services import create_flow


def policies(request):
    invalid=method_guard(request,"GET")
    if invalid:return invalid
    rows=[]
    for item in StrategyAllocation.objects.select_related("strategy_instance","portfolio"):
        rows.append({"id":item.pk,"portfolio_id":item.portfolio_id,"portfolio":item.portfolio.name,
            "strategy_id":item.strategy_instance_id,"strategy":item.strategy_instance.name,"target_share":item.weight,
            "minimum_share":item.minimum_share,"maximum_share":item.maximum_share,"capacity":item.capacity,
            "minimum_allocation":item.minimum_allocation,"priority":item.priority,"enabled":item.strategy_instance.enabled})
    return response(rows)


def flows(request):
    if request.method != "POST": return response(status=405,error={"code":"METHOD_NOT_ALLOWED","message":"POST required","details":{}})
    key=request.headers.get("Idempotency-Key")
    if not key:return response(status=400,error={"code":"IDEMPOTENCY_KEY_REQUIRED","message":"Idempotency-Key header is required","details":{}})
    try:
        payload=json.loads(request.body or b"{}")
        if not isinstance(payload,dict):raise ValueError("Request body must be a JSON object")
        unknown=set(payload)-{"portfolio_id","flow_type","amount","nav","liquidation_policy","allocation_mode"}
        if unknown:raise ValueError(f"Unsupported flow fields: {', '.join(sorted(unknown))}")
        require_fields(payload,"portfolio_id","flow_type","amount")
        amount=decimal_field(payload,"amount",required=True,positive=True,allow_zero=False)
        nav=decimal_field(payload,"nav",positive=True,allow_zero=False)
        run=create_flow(TradingPortfolio.objects.select_related("account").get(pk=payload["portfolio_id"]),
            payload["flow_type"].upper(),amount,key,nav=nav,
            liquidation_policy=payload.get("liquidation_policy","PROPORTIONAL"),allocation_mode=payload.get("allocation_mode","AUTO"),
            retry_failed=request.headers.get("Idempotency-Retry","").strip().lower() in {"1","true","yes"},defer_optimized=True)
        if run.status=="QUEUED":
            from .tasks import execute_flow_allocation_task
            transaction.on_commit(lambda:execute_flow_allocation_task.delay(run.pk))
        return response({"id":run.pk,"flow_id":run.flow_id,"status":run.status,"unallocated_amount":run.unallocated_amount,
            "allocation_mode":run.allocation_mode,"optimization_run_id":run.optimization_run_id},status=202 if run.status=="QUEUED" else 201)
    except (KeyError,ValueError,InvalidOperation,TradingPortfolio.DoesNotExist) as exc:
        return response(status=400,error={"code":"INVALID_FLOW","message":str(exc),"details":{}})


def _run(item, detail=False):
    row={"id":item.pk,"flow_id":item.flow_id,"portfolio_id":item.flow.portfolio_id,"flow_type":item.flow.flow_type,
        "amount":item.flow.amount,"approved_amount":item.approved_amount,"unallocated_amount":item.unallocated_amount,
        "liquidation_policy":item.liquidation_policy,"allocation_mode":item.allocation_mode,
        "optimization_run_id":item.optimization_run_id,"status":item.status,"created_at":item.created_at}
    if detail:
        row["snapshots"]=[{"id":record.pk,
            "strategy_id":record.strategy_instance_id or record.strategy_snapshot.get("strategy_instance_id") or record.strategy_snapshot.get("legacy_strategy_id"),
            "strategy":record.strategy_instance.name if record.strategy_instance else record.strategy_snapshot.get("strategy_name"),
            "capital_before":record.capital_before,"target_capital":record.target_capital,
            "deficit":record.deficit,"surplus":record.surplus,"idle_cash":record.idle_cash}
            for record in item.capital_snapshots.select_related("strategy_instance").all()]
        row["decisions"]=[{"id":record.pk,
            "strategy_id":record.strategy_instance_id or record.strategy_snapshot.get("strategy_instance_id") or record.strategy_snapshot.get("legacy_strategy_id"),
            "strategy":record.strategy_instance.name if record.strategy_instance else record.strategy_snapshot.get("strategy_name"),
            "source":record.source,"requested_amount":record.requested_amount,
            "approved_amount":record.approved_amount,"binding_constraint":record.binding_constraint,
            "liquidation_required":record.liquidation_required,"rank":record.rank,"details":record.details}
            for record in item.decisions.select_related("strategy_instance").all()]
    return row


def runs(request, run_id=None):
    invalid=method_guard(request,"GET")
    if invalid:return invalid
    if run_id:
        try:return response(_run(AllocationRun.objects.select_related("flow").get(pk=run_id),True))
        except AllocationRun.DoesNotExist:return response(status=404,error={"code":"NOT_FOUND","message":"Allocation run not found","details":{}})
    return response([_run(x) for x in AllocationRun.objects.select_related("flow").order_by("-created_at")[:100]])
