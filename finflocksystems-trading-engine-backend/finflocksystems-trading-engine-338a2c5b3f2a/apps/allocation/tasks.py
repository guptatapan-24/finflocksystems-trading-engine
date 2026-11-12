from celery import shared_task

from apps.audit.models import OperationAttempt
from .models import AllocationRun
from .services import execute_flow_allocation


@shared_task
def execute_flow_allocation_task(run_id):
    if not AllocationRun.objects.filter(pk=run_id,status="QUEUED").update(status="CALCULATING"):
        run=AllocationRun.objects.get(pk=run_id)
        return {"allocation_run_id":run.pk,"flow_id":run.flow_id,"status":run.status}
    queued=AllocationRun.objects.select_related("flow").get(pk=run_id)
    OperationAttempt.objects.filter(operation_type="PORTFOLIO_FLOW",operation_id=str(queued.flow_id),
        attempt_number=queued.flow.attempt_count,status="QUEUED").update(status="PROCESSING")
    run=execute_flow_allocation(queued)
    return {"allocation_run_id":run.pk,"flow_id":run.flow_id,"status":run.status}
