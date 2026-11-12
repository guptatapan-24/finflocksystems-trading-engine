import uuid
import logging
from datetime import timedelta
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from apps.broker_gateway.client import GatewayClient,GatewayError
from apps.strategies.models import StrategyInstance
from apps.strategies.plugins import get_plugin
from .models import MarketDataConsumerLease,MarketDataSubscription

logger = logging.getLogger(__name__)


def _requirements(instrument,timeframe,gateway_session=None):
    query=StrategyInstance.objects.filter(enabled=True,instrument=instrument,timeframe=timeframe)
    if gateway_session is not None:query=query.filter(portfolio__gateway_session=gateway_session)
    instances=list(query.select_related("definition").prefetch_related(
        "input_bindings__requirement","input_bindings__strategy_version",
    ))
    required=0
    for item in instances:
        current=[
            binding.requirement.warmup_bars
            for binding in item.input_bindings.all()
            if binding.active and binding.strategy_version.version==item.version
        ]
        required=max(
            required,
            max(current,default=get_plugin(item.definition).warmup_bars(item.parameters)),
        )
    return instances,required+int(getattr(settings,"WARMUP_SAFETY_BARS",5)) if instances else 0


def subscription_demand(instrument,timeframe,gateway_session=None,at=None):
    """Return strategy requirements and all unexpired persisted consumer demand."""
    instances,history=_requirements(instrument,timeframe,gateway_session)
    leases=MarketDataConsumerLease.objects.filter(
        instrument=instrument,timeframe=timeframe,expires_at__gt=at or timezone.now())
    if gateway_session is not None:leases=leases.filter(gateway_session=gateway_session)
    return instances,history,leases.count()


def acquire_consumer_lease(*,gateway_session,instrument,timeframe,consumer_type,lease_key,ttl_seconds):
    """Idempotently create/refresh demand and reconcile both old and new routes."""
    now=timezone.now();expires_at=now+timedelta(seconds=ttl_seconds)
    with transaction.atomic():
        existing=MarketDataConsumerLease.objects.select_for_update().filter(
            gateway_session=gateway_session,consumer_type=consumer_type,lease_key=lease_key).first()
        old_route=(existing.instrument,existing.timeframe) if existing else None
        lease,created=MarketDataConsumerLease.objects.update_or_create(
            gateway_session=gateway_session,consumer_type=consumer_type,lease_key=lease_key,
            defaults={"instrument":instrument,"timeframe":timeframe,"expires_at":expires_at})
    if old_route and old_route!=(instrument,timeframe):
        reconcile_market_subscription(*old_route,gateway_session=gateway_session)
    subscription=reconcile_market_subscription(instrument,timeframe,gateway_session=gateway_session)
    return lease,subscription,created


def release_consumer_lease(*,gateway_session,consumer_type,lease_key):
    with transaction.atomic():
        lease=MarketDataConsumerLease.objects.select_for_update().filter(
            gateway_session=gateway_session,consumer_type=consumer_type,lease_key=lease_key).first()
        if lease is None:return False,None
        instrument,timeframe=lease.instrument,lease.timeframe
        lease.delete()
    subscription=reconcile_market_subscription(instrument,timeframe,gateway_session=gateway_session)
    return True,subscription


def refresh_market_subscription_counts(subscription):
    instances,history,lease_count=subscription_demand(
        subscription.instrument,subscription.timeframe,subscription.gateway_session)
    subscription.consumer_count=len(instances)+lease_count
    subscription.required_history_bars=history
    subscription.save(update_fields=["consumer_count","required_history_bars","updated_at"])
    return subscription


def _cancel_stale_gateway_subscription(client,subscription_key,command_key,subscription_id):
    """Queue a cancel ahead of every subscribe for the same key.

    The Gateway answers a subscribe for a key it already holds with
    "ACTIVE, reused": no warm-up history and no switch to the new provider
    generation. After an IBKR reconnect those held entries are dead streams,
    so the strategy never receives a bar; on a live stream every bar carries
    the superseded generation and is dropped. Commands run in id order, so
    this cancel clears the entry and the subscribe that follows is a full
    one. Best-effort: a failed cancel never blocks the subscribe.
    """
    cancel_key=command_key.replace("market-subscribe:","market-resubscribe-cancel:",1)
    try:
        client.cancel_market_data({"subscription_key":subscription_key,"resubscribe":True},cancel_key)
    except Exception as exc:
        logger.warning(
            "market_data stage=resubscribe_cancel_failed subscription_id=%s error_type=%s error=%s",
            subscription_id,type(exc).__name__,exc,
        )


def reconcile_market_subscription(instrument,timeframe,gateway=None,force=False,connection_generation=None,gateway_session=None):
    contract=getattr(instrument,"broker_contract",None)
    if not contract:raise ValueError("Instrument does not have a qualified IBKR contract")
    gateway_session=gateway_session or getattr(gateway,"gateway_session",None)
    instances,history,lease_count=subscription_demand(instrument,timeframe,gateway_session)
    count=len(instances)+lease_count
    if gateway is None:
        if gateway_session is None:raise ValueError("A broker gateway session is required for market-data subscription routing")
        client=GatewayClient(gateway_session,require_commands=True)
    else:client=gateway
    generation=connection_generation
    if count and generation is None:
        logger.info(
            "manual_quote stage=gateway_health_start session_id=%s instrument_id=%s timeframe=%s",
            gateway_session.pk if gateway_session else "", instrument.pk, timeframe,
        )
        health=client.health();generation=str(health.get("connection_generation") or "")
        logger.info(
            "manual_quote stage=gateway_health_succeeded session_id=%s instrument_id=%s connected=%s generation=%s",
            gateway_session.pk if gateway_session else "", instrument.pk,
            bool(health.get("connected")), generation,
        )
    action=None;payload=None;command_key=None
    with transaction.atomic():
        subscription,_=MarketDataSubscription.objects.select_for_update().get_or_create(gateway_session=gateway_session,instrument=instrument,timeframe=timeframe,
            defaults={"conid":contract.conid,"consumer_count":count,"required_history_bars":history})
        subscription.conid=contract.conid;subscription.consumer_count=count;subscription.required_history_bars=history
        if count:
            if subscription.active_provider=="FINNHUB" and not force:
                subscription.save(update_fields=["conid","consumer_count","required_history_bars","updated_at"])
                return subscription
            if not force and subscription.state in {"SUBSCRIBING","ACTIVE"} and subscription.gateway_connection_generation==generation:
                subscription.save(update_fields=["conid","consumer_count","required_history_bars","updated_at"])
                logger.debug(
                    "manual_quote stage=subscription_reused subscription_id=%s session_id=%s instrument_id=%s state=%s provider=%s last_event_at=%s last_error=%s",
                    subscription.pk, gateway_session.pk if gateway_session else "", instrument.pk,
                    subscription.state, subscription.active_provider,
                    subscription.last_event_at or "", subscription.last_error or "",
                )
                return subscription
            subscription.request_id=uuid.uuid4();subscription.provider_generation=uuid.uuid4()
            subscription.active_provider="IBKR";subscription.fallback_state="PRIMARY";subscription.fallback_reason=""
            subscription.state="SUBSCRIBING";subscription.requested_at=timezone.now()
            subscription.gateway_connection_generation=generation
            subscription.save()
            session_key=str(gateway_session.pk) if gateway_session is not None else "explicit"
            payload={"subscription_key":f"{session_key}:{instrument.pk}:{timeframe}","instrument_id":instrument.pk,"conid":contract.conid,
                "symbol":instrument.symbol,"local_symbol":contract.local_symbol,
                "asset_class":instrument.asset_class,"exchange":instrument.exchange,
                "primary_exchange":contract.primary_exchange,
                "currency":instrument.currency,"timeframe":timeframe,"historical_bars":history,
                "provider":"IBKR","provider_generation":str(subscription.provider_generation)}
            command_key=f"market-subscribe:{session_key}:{subscription.pk}:{subscription.request_id}";action="subscribe"
        elif subscription.state!="INACTIVE" or subscription.consumer_count:
            subscription.request_id=uuid.uuid4();subscription.state="CANCELLING";subscription.requested_at=timezone.now();subscription.save()
            session_key=str(gateway_session.pk) if gateway_session is not None else "explicit"
            payload={"subscription_key":f"{session_key}:{instrument.pk}:{timeframe}"}
            command_key=f"market-cancel:{session_key}:{subscription.pk}:{subscription.request_id}";action="cancel"
        else:
            subscription.save(update_fields=["conid","consumer_count","required_history_bars","updated_at"])
            return subscription
        subscription_id=subscription.pk
    try:
        logger.info(
            "manual_quote stage=subscription_command_start subscription_id=%s session_id=%s instrument_id=%s action=%s timeframe=%s",
            subscription_id, gateway_session.pk if gateway_session else "", instrument.pk,
            action, timeframe,
        )
        if action=="subscribe":
            _cancel_stale_gateway_subscription(client,payload["subscription_key"],command_key,subscription_id)
        queued=(client.subscribe_market_data(payload,command_key) if action=="subscribe"
            else client.cancel_market_data(payload,command_key))
        MarketDataSubscription.objects.filter(pk=subscription_id).update(gateway_command_id=queued.get("command_id"),last_error="")
        logger.info(
            "manual_quote stage=subscription_command_queued subscription_id=%s session_id=%s instrument_id=%s action=%s gateway_command_id=%s",
            subscription_id, gateway_session.pk if gateway_session else "", instrument.pk,
            action, queued.get("command_id") or "",
        )
    except GatewayError as exc:
        MarketDataSubscription.objects.filter(pk=subscription_id).update(state="ERROR",last_error=str(exc)[:2000])
        logger.warning(
            "manual_quote stage=subscription_command_failed subscription_id=%s session_id=%s instrument_id=%s action=%s error_type=%s error=%s",
            subscription_id, gateway_session.pk if gateway_session else "", instrument.pk,
            action, type(exc).__name__, str(exc),
        )
        if action=="subscribe":
            from apps.market_data.fallback import handle_ibkr_failure
            handle_ibkr_failure(MarketDataSubscription.objects.get(pk=subscription_id),message=str(exc),historical=True)
    return MarketDataSubscription.objects.get(pk=subscription_id)


def restore_market_subscriptions(gateway=None,gateway_session=None):
    if gateway is None and gateway_session is None:
        from apps.broker_gateway.models import BrokerGatewaySession
        total=0
        for session in BrokerGatewaySession.objects.filter(status=BrokerGatewaySession.Status.CONNECTED,commands_enabled=True):
            total+=restore_market_subscriptions(gateway_session=session)
        return total
    gateway_session=gateway_session or getattr(gateway,"gateway_session",None)
    client=gateway or GatewayClient(gateway_session);health=client.health()
    if not health.get("connected"):return 0
    generation=str(health.get("connection_generation") or "");restored=0
    instances=StrategyInstance.objects.filter(enabled=True)
    subscriptions=MarketDataSubscription.objects.all()
    if gateway_session is not None:
        instances=instances.filter(portfolio__gateway_session=gateway_session)
        subscriptions=subscriptions.filter(gateway_session=gateway_session)
    pairs=set(instances.values_list("instrument_id","timeframe"))
    leases=MarketDataConsumerLease.objects.filter(expires_at__gt=timezone.now())
    if gateway_session is not None:leases=leases.filter(gateway_session=gateway_session)
    pairs.update(leases.values_list("instrument_id","timeframe"))
    pairs.update(subscriptions.values_list("instrument_id","timeframe"))
    from apps.instruments.models import Instrument
    # This runs every MARKET_SUBSCRIPTION_RESTORE_INTERVAL_SECONDS over every
    # (instrument, timeframe) the session knows about. Fetching the instrument
    # and its subscription one pair at a time made that 2 queries per pair per
    # pass; an options run with a few hundred legs turned a housekeeping task
    # into the busiest reader on the database. Both are one query now.
    # (gateway_session, instrument, timeframe) is unique, so the map below is
    # exactly what the per-pair .first() returned.
    instruments=Instrument.objects.select_related("broker_contract").in_bulk(
        {instrument_id for instrument_id,_ in pairs})
    existing={
        (item.instrument_id,item.timeframe):item
        for item in MarketDataSubscription.objects.filter(gateway_session=gateway_session)
    }
    now=timezone.now()
    stall_seconds=float(getattr(settings,"MARKET_SUBSCRIPTION_STALL_SECONDS",180))
    retry_seconds=float(getattr(settings,"MARKET_SUBSCRIPTION_RETRY_SECONDS",60))
    for instrument_id,timeframe in pairs:
        instrument=instruments.get(instrument_id)
        if instrument is None:continue
        current=existing.get((instrument_id,timeframe))
        if current and current.active_provider=="FINNHUB" and current.consumer_count:
            continue
        reconnected=bool(current and current.consumer_count and current.gateway_connection_generation!=generation)
        since_request=(now-current.requested_at).total_seconds() if current and current.requested_at else None
        if (current and current.state=="ERROR" and not reconnected
                and since_request is not None and since_request<retry_seconds):
            # Every subscribe now runs a real IBKR history request; IBKR
            # rejects identical ones repeated within 15 s (pacing), which would
            # fail the retry and keep the subscription in ERROR.
            continue
        stalled=bool(
            current and current.consumer_count and not reconnected
            and current.state=="SUBSCRIBING" and current.active_provider=="IBKR"
            and since_request is not None and since_request>=stall_seconds
            and (current.last_event_at is None or current.last_event_at<current.requested_at)
        )
        if stalled:
            # The subscribe was accepted but no event arrived for it (lost
            # command, dead stream, restarted Gateway). Waiting is never going
            # to fix that; request it again under a fresh generation.
            logger.warning(
                "market_data stage=subscription_stalled subscription_id=%s instrument_id=%s timeframe=%s seconds_since_request=%.0f",
                current.pk,instrument_id,timeframe,since_request,
            )
        force=reconnected or stalled
        reconcile_market_subscription(instrument,timeframe,client,force=force,connection_generation=generation,gateway_session=gateway_session);restored+=int(force)
    return restored
