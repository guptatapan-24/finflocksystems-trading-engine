import socket

from celery import shared_task
from django.utils import timezone
from apps.execution.readiness import record_worker_heartbeat
from apps.execution.modes import RunType
from apps.portfolios.models import TradingPortfolio
from .coordinator import process_target_coordination
from .services import plan_rebalance, recover_incomplete


def _run_with_heartbeat(callback):
    role = "target_coordination"
    worker = socket.gethostname()
    # No RUNNING pre-write -- see apps.execution.tasks._run_with_heartbeat.
    try:
        result = callback()
    except Exception as exc:
        record_worker_heartbeat(
            role,
            status="DEGRADED",
            worker=worker,
            details={"error": str(exc)[:255]},
            force=True,
        )
        raise
    record_worker_heartbeat(
        role,
        worker=worker,
        details={"last_result": result},
    )
    return result


@shared_task
def recover_incomplete_rebalances():
    return _run_with_heartbeat(recover_incomplete)


@shared_task
def coordinate_portfolio_targets():
    return _run_with_heartbeat(process_target_coordination)


@shared_task
def execute_rebalance_run(portfolio_id,trigger,idempotency_key,prices=None,nav=None,mode=None,
                          strict_market_state=True,available_cash=None,run_type=RunType.EXECUTION):
    portfolio=TradingPortfolio.objects.select_related("account","gateway_session").get(pk=portfolio_id)
    try:
        from apps.allocation.models import RebalanceRun
        queued = RebalanceRun.objects.filter(idempotency_key=idempotency_key).select_related(
            "target_snapshot"
        ).first()
        run=plan_rebalance(portfolio,trigger,idempotency_key,prices=prices,nav=nav,mode=mode,
            strict_market_state=strict_market_state,available_cash=available_cash,
            run_type=run_type,
            target_snapshot=queued.target_snapshot if queued else None)
        return {"rebalance_run_id":run.pk,"status":run.status,"phase":run.phase}
    except Exception as exc:
        from apps.allocation.models import RebalanceRun
        RebalanceRun.objects.filter(idempotency_key=idempotency_key).update(status="FAILED",
            retryable=not isinstance(exc,ValueError),last_error=str(exc)[:1000],completed_at=timezone.now())
        raise
