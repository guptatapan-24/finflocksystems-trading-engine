from celery import shared_task

from apps.execution.modes import RunType

from .models import PortfolioOptimizationRun
from .services import apply_optimization_run, plan_optimized_rebalance, run_optimization


@shared_task
def execute_optimization_run(run_id, refresh_history=True, available_cash=None, create_preview=True):
    if not PortfolioOptimizationRun.objects.filter(pk=run_id,status="QUEUED").update(status="DISPATCHED"):
        current=PortfolioOptimizationRun.objects.get(pk=run_id)
        return {"optimization_run_id":current.pk,"status":current.status}
    queued=PortfolioOptimizationRun.objects.select_related("portfolio__account").get(pk=run_id)
    run=run_optimization(queued.portfolio,queued.idempotency_key,trigger=queued.trigger,nav=queued.nav,
        available_cash=available_cash,refresh_history=refresh_history,flow_reference=queued.flow_reference,
        stored_request_hash=queued.request_hash)
    if create_preview and run.status=="COMPLETED" and not run.rebalances.exists():
        plan_optimized_rebalance(
            run,
            f"{run.idempotency_key}:rebalance",
            run_type=RunType.PREVIEW,
            strict_market_state=False,
        )
    return {"optimization_run_id":run.pk,"status":run.status}


@shared_task
def apply_optimization_run_task(run_id,idempotency_key,mode=None):
    run=PortfolioOptimizationRun.objects.get(pk=run_id)
    try:
        run,rebalance,_=apply_optimization_run(
            run,
            idempotency_key,
            mode=mode,
            strict_market_state=True,
        )
        return {"optimization_run_id":run.pk,"rebalance_run_id":rebalance.pk,"status":run.application_status}
    except Exception as exc:
        PortfolioOptimizationRun.objects.filter(pk=run_id).update(application_status="FAILED",last_error=str(exc)[:1000],retryable=True)
        raise
