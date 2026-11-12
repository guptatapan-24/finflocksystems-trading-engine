import uuid
import json
from copy import deepcopy
from datetime import timedelta
from decimal import InvalidOperation
from django.conf import settings
from django.db import DataError, IntegrityError, InterfaceError, OperationalError, transaction
from django.utils import timezone
from apps.audit.models import OutboxEvent
from .models import ConsumedEvent, DeadLetterEvent, ReplayRequest
from .schemas import decimal_safe, validate_envelope


def envelope_for(event):
    return decimal_safe({
        "event_id": str(event.event_id), "event_type": event.event_type,
        "schema_version": event.schema_version, "occurred_at": event.created_at.isoformat(),
        "produced_at": timezone.now().isoformat(), "producer": settings.KAFKA_CLIENT_ID,
        "aggregate_type": event.aggregate_type, "aggregate_id": event.aggregate_id,
        "correlation_id": str(event.correlation_id or event.event_id),
        "causation_id": str(event.causation_id) if event.causation_id else None,
        "idempotency_key": event.idempotency_key, "payload": event.payload,
    })


class KafkaPublisher:
    def __init__(self, producer=None):
        if producer is None:
            from confluent_kafka import Producer
            producer = Producer({"bootstrap.servers": settings.KAFKA_BOOTSTRAP_SERVERS,
                                 "client.id": settings.KAFKA_CLIENT_ID,
                                 "enable.idempotence": True, "acks": "all"})
        self.producer = producer

    def publish(self, event):
        error=self.publish_batch([event]).get(event.pk)
        if error:raise RuntimeError(error)

    def publish_batch(self, events):
        import json
        outcomes={event.pk:None for event in events}
        for event in events:
            try:
                envelope=envelope_for(event);validate_envelope(envelope)
                def delivered(err,_message,event_id=event.pk):
                    outcomes[event_id]=str(err) if err else ""
                self.producer.produce(event.topic,key=event.partition_key.encode(),
                    value=json.dumps(envelope,separators=(",",":")).encode(),callback=delivered)
            except Exception as exc:
                outcomes[event.pk]=str(exc)
        remaining=self.producer.flush(10)
        for event_id,outcome in outcomes.items():
            if outcome is None:
                outcomes[event_id]=(f"Kafka acknowledgement timeout with {remaining} message(s) pending"
                    if remaining else "Kafka delivery callback did not confirm the event")
        return outcomes


def publish_batch(publisher=None, limit=100):
    if not settings.KAFKA_ENABLED:
        return 0
    publisher = publisher or KafkaPublisher()
    now = timezone.now()
    with transaction.atomic():
        ids = list(OutboxEvent.objects.select_for_update(skip_locked=True).filter(
            status__in=["PENDING", "FAILED", "PUBLISHING"], available_at__lte=now).order_by("created_at").values_list("pk", flat=True)[:limit])
        OutboxEvent.objects.filter(pk__in=ids).update(status="PUBLISHING",available_at=now+timedelta(seconds=60))
    events=list(OutboxEvent.objects.filter(pk__in=ids).order_by("created_at"))
    if hasattr(publisher,"publish_batch"):
        try:outcomes=publisher.publish_batch(events)
        except Exception as exc:outcomes={event.pk:str(exc) for event in events}
    else:
        outcomes={}
        for event in events:
            try:publisher.publish(event);outcomes[event.pk]=""
            except Exception as exc:outcomes[event.pk]=str(exc)
    published=0;now=timezone.now()
    for event in events:
        error=outcomes[event.pk] if event.pk in outcomes else "Publisher returned no delivery result"
        event.attempt_count+=1
        if not error:
            event.status="PUBLISHED";event.published_at=now;event.last_error="";published+=1
        else:
            event.status="FAILED";event.published_at=None;event.last_error=error[:2000]
            event.available_at=now+timedelta(seconds=min(300,2**min(event.attempt_count,8)))
    if events:
        OutboxEvent.objects.bulk_update(events,["status","published_at","attempt_count","last_error","available_at"])
    return published


def consume_once(consumer_name, envelope, handler):
    validate_envelope(envelope)
    event_id = uuid.UUID(envelope["event_id"])
    try:
        with transaction.atomic():
            consumed=ConsumedEvent.objects.select_for_update().filter(
                consumer_name=consumer_name,event_id=event_id).first()
            if consumed and consumed.result.get("status")!="FAILED":
                return {"duplicate": True}
            if consumed:
                consumed.result={"status":"PROCESSING"};consumed.save(update_fields=["result"])
            else:
                try:
                    with transaction.atomic():
                        consumed=ConsumedEvent.objects.create(consumer_name=consumer_name,event_id=event_id,
                            result={"status":"PROCESSING"})
                except IntegrityError:
                    return {"duplicate": True}
            result = handler(envelope) or {}
            consumed.result=decimal_safe(result);consumed.save(update_fields=["result"])
            return result
    except Exception as exc:
        ConsumedEvent.objects.update_or_create(consumer_name=consumer_name,event_id=event_id,
            defaults={"result":{"status":"FAILED","retryable":True,"error":str(exc)[:2000]}})
        raise


def route_dead_letter(source_topic, envelope, reason, consumer_name=""):
    raw_id = envelope.get("event_id") if isinstance(envelope, dict) else None
    try: raw_id=uuid.UUID(str(raw_id)) if raw_id else None
    except (ValueError,TypeError,AttributeError): raw_id=None
    return DeadLetterEvent.objects.create(event_id=raw_id, source_topic=source_topic,
        consumer_name=consumer_name, reason=str(reason)[:255], envelope=decimal_safe(envelope))


def deterministic_invalid_event_error(exc):
    """Return True only when replaying the same bytes cannot make them valid."""
    if isinstance(exc,(OperationalError,InterfaceError,ConnectionError,TimeoutError)):
        return False
    try:
        from jsonschema import ValidationError as JsonSchemaValidationError
        schema_errors=(JsonSchemaValidationError,)
    except ImportError:
        schema_errors=()
    return isinstance(exc,(
        json.JSONDecodeError,
        ValueError,
        TypeError,
        KeyError,
        InvalidOperation,
        DataError,
        IntegrityError,
        UnicodeDecodeError,
        *schema_errors,
    ))


def replay_dead_letter(dead_letter):
    from apps.market_streams.services import consume_market_event
    if dead_letter.source_topic not in {
        "market.bars.v1","market.indicators.v1","market.quality.v1",
    }:
        raise ValueError(
            f"No Backend market replay handler is registered for {dead_letter.source_topic}"
        )
    envelope=deepcopy(dead_letter.envelope)
    envelope.setdefault("payload",{})["processing_mode"]="REPLAY"
    result=consume_market_event(
        dead_letter.consumer_name or "market-persistence-v2",
        envelope,
    )
    dead_letter.replayed_at=timezone.now()
    dead_letter.save(update_fields=["replayed_at"])
    return result


def request_replay(topic, consumer_name, idempotency_key, from_timestamp=None, to_timestamp=None):
    request, _ = ReplayRequest.objects.get_or_create(idempotency_key=idempotency_key, defaults={
        "topic": topic, "consumer_name": consumer_name, "from_timestamp": from_timestamp, "to_timestamp": to_timestamp})
    return request


def replay_envelopes(replay_request, envelopes, handler):
    replay_request.status = "RUNNING"; replay_request.save(update_fields=["status"])
    processed = 0
    try:
        for envelope in envelopes:
            replay_envelope = deepcopy(envelope)
            if replay_request.topic in {
                "market.bars.v1",
                "market.indicators.v1",
                "market.quality.v1",
            }:
                replay_envelope.setdefault("payload", {})["processing_mode"] = "REPLAY"
            handler(replay_request.consumer_name, replay_envelope)
            processed += 1
        replay_request.status = "COMPLETED"; replay_request.processed_count = processed
        replay_request.completed_at = timezone.now()
        replay_request.save(update_fields=["status","processed_count","completed_at"])
    except Exception:
        replay_request.status = "FAILED"; replay_request.processed_count = processed
        replay_request.save(update_fields=["status","processed_count"])
        raise
    return processed


def execute_replay(request_id, consumer=None):
    import json
    from confluent_kafka import Consumer, OFFSET_BEGINNING, TopicPartition
    from apps.market_streams.services import consume_market_event
    replay_request = ReplayRequest.objects.get(pk=request_id)
    supported = {"market.bars.v1", "market.indicators.v1", "market.quality.v1"}
    if replay_request.topic not in supported:
        replay_request.status="FAILED";replay_request.save(update_fields=["status"])
        raise ValueError("Replay handler is not registered for this topic")
    owned = consumer is None
    consumer = consumer or Consumer({"bootstrap.servers":settings.KAFKA_BOOTSTRAP_SERVERS,
        "group.id":f"finflock-replay-{replay_request.pk}","enable.auto.commit":False,"auto.offset.reset":"earliest"})
    metadata=consumer.list_topics(replay_request.topic,timeout=10)
    partitions=sorted(metadata.topics[replay_request.topic].partitions)
    starts=[TopicPartition(replay_request.topic,p,OFFSET_BEGINNING) for p in partitions]
    if replay_request.from_timestamp:
        stamp=int(replay_request.from_timestamp.timestamp()*1000)
        starts=consumer.offsets_for_times([TopicPartition(replay_request.topic,p,stamp) for p in partitions],timeout=10)
    ends={p:consumer.get_watermark_offsets(TopicPartition(replay_request.topic,p),timeout=10)[1] for p in partitions}
    consumer.assign(starts)
    def messages():
        completed=set()
        while len(completed)<len(partitions):
            message=consumer.poll(1)
            if message is None:
                for position in consumer.position([TopicPartition(replay_request.topic,p) for p in partitions]):
                    if position.offset>=ends[position.partition]:completed.add(position.partition)
                continue
            if message.error():raise RuntimeError(str(message.error()))
            if replay_request.to_timestamp and message.timestamp()[1] > int(replay_request.to_timestamp.timestamp()*1000):
                completed.add(message.partition());continue
            yield json.loads(message.value())
            if message.offset()+1>=ends[message.partition()]:completed.add(message.partition())
    try:
        return replay_envelopes(replay_request,messages(),consume_market_event)
    finally:
        if owned:consumer.close()
