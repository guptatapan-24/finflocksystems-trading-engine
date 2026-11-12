import logging

from celery import shared_task

from datetime import timedelta
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .configuration import (
    ManagedBrokerGatewayUnavailable,
    managed_broker_deployment_configuration,
    managed_broker_disabled_task_result,
)
from .models import BrokerGatewaySession, BrokerGatewaySessionSecret
from .qch import QCHError
from .services import (
    inspect_gateway_session,
    provision_session,
    record_provision_failure,
    synchronize_local_paper_gateway,
)


logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=2, default_retry_delay=5)
def provision_broker_session(self, session_id):
    try:
        return provision_session(session_id)
    except ManagedBrokerGatewayUnavailable as exc:
        return managed_broker_disabled_task_result(exc.configuration)
    except QCHError as exc:
        if exc.retryable and self.request.retries < self.max_retries:
            raise self.retry(exc=exc)
        return record_provision_failure(session_id, exc, final=True)


@shared_task
def monitor_broker_sessions():
    deployment = managed_broker_deployment_configuration()
    if not deployment["available"]:
        return managed_broker_disabled_task_result(deployment)

    now = timezone.now()
    expired_session_ids = list(BrokerGatewaySessionSecret.objects.filter(
        expires_at__lte=now
    ).values_list("session_id", flat=True))
    BrokerGatewaySessionSecret.objects.filter(session_id__in=expired_session_ids).delete()
    BrokerGatewaySession.objects.filter(
        pk__in=expired_session_ids,
        status=BrokerGatewaySession.Status.CREATING,
    ).update(
        status=BrokerGatewaySession.Status.LOGIN_FAILED,
        commands_enabled=False,
        last_error="Temporary IBKR credentials expired before provisioning",
        last_checked_at=now,
    )

    stale_before = now - timedelta(seconds=int(settings.BROKER_SESSION_CREATING_STALE_SECONDS))
    stale_creating = BrokerGatewaySession.objects.filter(
        status=BrokerGatewaySession.Status.CREATING,
        deleted_at__isnull=True,
    ).filter(Q(last_checked_at__lt=stale_before) | Q(last_checked_at__isnull=True, updated_at__lt=stale_before))
    recovered = []
    for session_id in stale_creating.values_list("pk", flat=True):
        updated = BrokerGatewaySession.objects.filter(
            pk=session_id,
            status=BrokerGatewaySession.Status.CREATING,
        ).update(last_checked_at=now, last_error="Recovering stale broker-session provisioning")
        if updated:
            provision_broker_session.delay(str(session_id))
            recovered.append(str(session_id))

    results = {}
    sessions=BrokerGatewaySession.objects.exclude(
        status__in=[
            BrokerGatewaySession.Status.CREATING,
            BrokerGatewaySession.Status.LOGIN_FAILED,
            BrokerGatewaySession.Status.STOPPING,
            BrokerGatewaySession.Status.DELETED,
        ]
    )
    local_name=str(getattr(settings,"LOCAL_PAPER_GATEWAY_CONTAINER_NAME","") or "")
    if local_name:
        sessions=sessions.exclude(child_container_name=local_name)
    for session in sessions.order_by("created_at"):
        try:
            results[str(session.pk)] = inspect_gateway_session(session).status
        except QCHError as exc:
            if exc.retryable:
                # The observation failed, not the session. Overwriting a live
                # status with ERROR here is what made the frontend flap and
                # remount noVNC on every sync.
                logger.warning(
                    "broker_gateway stage=session_check_deferred session_id=%s container=%s status=%s reason=%s",
                    session.pk, session.child_container_name, session.status, exc,
                )
                results[str(session.pk)] = f"{session.status}+CHECK_DEFERRED"
                continue
            logger.exception(
                "broker_gateway stage=session_inspect_failed session_id=%s container=%s",
                session.pk, session.child_container_name,
            )
            BrokerGatewaySession.objects.filter(pk=session.pk).update(
                status=BrokerGatewaySession.Status.ERROR,
                commands_enabled=False,
                last_error=str(exc)[:4000],
            )
            results[str(session.pk)] = BrokerGatewaySession.Status.ERROR
        except Exception as exc:
            logger.exception(
                "broker_gateway stage=session_inspect_failed session_id=%s container=%s",
                session.pk, session.child_container_name,
            )
            BrokerGatewaySession.objects.filter(pk=session.pk).update(
                status=BrokerGatewaySession.Status.ERROR,
                commands_enabled=False,
                last_error=str(exc)[:4000],
            )
            results[str(session.pk)] = BrokerGatewaySession.Status.ERROR
    if recovered:
        results["recovered_creating"] = recovered
    return results


@shared_task
def sync_broker_events():
    local=synchronize_local_paper_gateway()
    managed=monitor_broker_sessions()
    return {
        "local_paper_session_id":str(local.pk) if local else None,
        "local_paper_status":local.status if local else "disabled",
        "managed":managed,
    }


@shared_task
def ensure_platform_services():
    """Beat entry `ensure-platform-services` (every 5 min): create the Kafka/Flink sub-containers if QCH lists them as absent. No-op unless enabled."""
    from .platform_services import ensure_all

    return ensure_all()
