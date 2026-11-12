from celery import shared_task
from apps.broker_gateway.client import GatewayClient
from apps.broker_gateway.models import BrokerSessionAccount
from .services import reconcile

@shared_task
def run_scheduled_reconciliation():
    return [reconcile("scheduled",GatewayClient(mapping.session),broker_account=mapping.broker_account,
        gateway_session=mapping.session).pk for mapping in BrokerSessionAccount.objects.filter(
        available=True,session__status="CONNECTED").select_related("session","broker_account")]
