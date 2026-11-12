import logging
import socket

from celery import shared_task

from .readiness import record_worker_heartbeat
from .dispatch import (
    process_broker_commands,
    process_order_intents,
    recover_stuck_broker_commands,
    recover_stuck_order_intents,
)


logger = logging.getLogger(__name__)


def _run_with_heartbeat(role, callback):
    worker = socket.gethostname()
    logger.debug("order_execution stage=worker_task_start role=%s worker=%s", role, worker)
    # No RUNNING pre-write: at a 1s cadence it was overwritten by the HEALTHY
    # write below microseconds later, so nothing could ever observe it, and it
    # doubled the row's write rate. A task that hangs still ages the row out.
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
        logger.exception(
            "order_execution stage=worker_task_failed role=%s worker=%s error_type=%s",
            role, worker, type(exc).__name__,
        )
        raise
    record_worker_heartbeat(
        role,
        worker=worker,
        details={"last_result": result},
    )
    logger.debug(
        "order_execution stage=worker_task_completed role=%s worker=%s result=%s",
        role, worker, result,
    )
    return result


@shared_task
def execute_order_intents(limit=None):
    return _run_with_heartbeat(
        "intent_execution",
        lambda: process_order_intents(limit=limit),
    )


@shared_task
def dispatch_broker_commands(limit=None):
    return _run_with_heartbeat(
        "broker_commands",
        lambda: process_broker_commands(limit=limit),
    )


@shared_task
def recover_broker_commands():
    return _run_with_heartbeat(
        "broker_commands",
        recover_stuck_broker_commands,
    )


@shared_task
def recover_order_intents():
    return _run_with_heartbeat(
        "intent_execution",
        recover_stuck_order_intents,
    )
