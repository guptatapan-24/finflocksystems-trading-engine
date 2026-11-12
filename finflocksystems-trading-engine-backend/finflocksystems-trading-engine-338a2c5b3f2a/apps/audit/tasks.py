from celery import shared_task
from apps.event_bus.services import publish_batch

@shared_task
def dispatch_outbox():
    return publish_batch()
