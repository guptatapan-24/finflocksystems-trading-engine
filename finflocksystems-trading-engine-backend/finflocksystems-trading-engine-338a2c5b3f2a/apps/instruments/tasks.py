from celery import shared_task
from .models import BrokerContract
from .services import publish_instrument_registry


@shared_task
def publish_instrument_registry_snapshot():
    """Re-emit every contract onto the compacted registry topic.

    This is the recovery path for a recreated `instrument.registry.v1`, so it
    forces: re-queueing rows that were already published is the whole point.
    """
    count=0
    for contract in BrokerContract.objects.select_related("instrument"):
        publish_instrument_registry(contract, force=True);count+=1
    return count
