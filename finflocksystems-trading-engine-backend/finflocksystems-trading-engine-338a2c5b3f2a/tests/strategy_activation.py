from django.test import override_settings

from apps.broker_gateway.sync import process_snapshot
from apps.market_streams.models import MarketDataSubscription
from apps.market_streams.services import current_warmup_required
from apps.strategies.framework import enable_instance
from apps.strategies.models import StrategyInstance


class FakeActivationGateway:
    def __init__(self, generation="test-activation-generation"):
        self.generation=generation
        self.subscribes=[]
        self.cancels=[]

    def health(self):
        return {"connected":True,"connection_generation":self.generation}

    def subscribe_market_data(self,payload,key):
        self.subscribes.append((payload,key))
        return {"command_id":len(self.subscribes),"status":"PENDING"}

    def cancel_market_data(self,payload,key):
        self.cancels.append((payload,key))
        return {"command_id":100+len(self.cancels),"status":"PENDING"}


def activate_strategy(instance, *, gateway=None, ready=True):
    gateway=gateway or FakeActivationGateway()
    with override_settings(KAFKA_ENABLED=True):
        enable_instance(instance,gateway)
    subscription=MarketDataSubscription.objects.get(
        gateway_session=instance.portfolio.gateway_session,
        instrument=instance.instrument,
        timeframe=instance.timeframe,
    )
    process_snapshot({
        "event_type":"command.subscribe_market_data.completed",
        "payload":{
            "subscription_key":f"{instance.portfolio.gateway_session_id}:{instance.instrument_id}:{instance.timeframe}",
            "provider_generation":str(subscription.provider_generation),
        },
    },gateway_session=instance.portfolio.gateway_session)
    if ready:
        instance.refresh_from_db()
        StrategyInstance.objects.filter(pk=instance.pk).update(
            state="READY_WAITING_FOR_LIVE_BAR",
            warmup_progress=current_warmup_required(instance),
            block_reason="",
        )
    instance.refresh_from_db()
    subscription.refresh_from_db()
    return instance,gateway,subscription
