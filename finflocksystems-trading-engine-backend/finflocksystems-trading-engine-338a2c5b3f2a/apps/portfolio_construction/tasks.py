from celery import shared_task
from django.utils import timezone

from apps.audit.models import OperationAttempt
from apps.execution.modes import RunType

from .models import PortfolioConstructionRun
from .services import apply_construction_run, plan_construction_rebalance, run_construction


@shared_task
def execute_construction_run(run_id, refresh_history=True, create_preview=True):
    if not PortfolioConstructionRun.objects.filter(pk=run_id, status="QUEUED").update(status="DISPATCHED"):
        current = PortfolioConstructionRun.objects.get(pk=run_id)
        return {"construction_run_id": current.pk, "status": current.status}
    run = run_construction(run_id, refresh_history=refresh_history)
    try:
        if create_preview and run.status == "COMPLETED" and not run.rebalances.exists():
            plan_construction_rebalance(
                run,
                f"{run.idempotency_key}:rebalance",
                run_type=RunType.PREVIEW,
                strict_market_state=False,
            )
    except Exception as exc:
        completed_at = timezone.now()
        PortfolioConstructionRun.objects.filter(pk=run.pk).update(
            status="FAILED",
            retryable=True,
            last_error=str(exc)[:1000],
            completed_at=completed_at,
        )
        OperationAttempt.objects.filter(
            operation_type="PORTFOLIO_CONSTRUCTION",
            operation_id=str(run.pk),
            attempt_number=run.attempt_count,
        ).update(status="FAILED", retryable=True, error=str(exc)[:1000], result={}, completed_at=completed_at)
        raise
    return {"construction_run_id": run.pk, "status": run.status}


@shared_task
def apply_construction_run_task(run_id, idempotency_key, mode=None):
    run = PortfolioConstructionRun.objects.get(pk=run_id)
    try:
        run, rebalance, _ = apply_construction_run(run, idempotency_key, mode=mode)
        return {
            "construction_run_id": run.pk,
            "rebalance_run_id": rebalance.pk,
            "status": run.application_status,
        }
    except Exception as exc:
        PortfolioConstructionRun.objects.filter(pk=run_id).update(
            application_status="FAILED",
            last_error=str(exc)[:1000],
            retryable=True,
        )
        raise
