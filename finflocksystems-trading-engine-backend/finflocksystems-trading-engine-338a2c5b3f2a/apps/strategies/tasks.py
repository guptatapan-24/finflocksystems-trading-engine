import socket

from celery import shared_task

from apps.execution.readiness import record_worker_heartbeat

from .evaluation_jobs import (
    process_strategy_evaluation_jobs,
    recover_stuck_strategy_evaluation_jobs,
)
from .framework import StrategyActivationError, activate_instance


def _run_with_heartbeat(callback):
    role = "strategy_evaluation"
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
def execute_strategy_evaluation_jobs(limit=None):
    return _run_with_heartbeat(
        lambda: process_strategy_evaluation_jobs(limit=limit)
    )


@shared_task
def recover_strategy_evaluation_jobs():
    return _run_with_heartbeat(recover_stuck_strategy_evaluation_jobs)


@shared_task
def activate_strategy_instance(instance_id, action_id=None, construction_run_id=None):
    try:
        instance=activate_instance(
            instance_id,action_id=action_id,construction_run_id=construction_run_id)
        return {
            "strategy_instance_id":instance.pk,
            "activation_status":instance.state,
        }
    except StrategyActivationError as exc:
        return {
            "strategy_instance_id":instance_id,
            "activation_status":"BLOCKED",
            "block_reason":str(exc),
            "retryable":exc.retryable,
        }
