import uuid
import json
import sys
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
import pytest
from confluent_kafka import KafkaError
from django.core.management import call_command
from django.db import OperationalError
from django.test import override_settings
from apps.accounts.models import BrokerAccount
from apps.audit.models import AuditEvent, OutboxEvent
from apps.broker_gateway.models import BrokerPositionSnapshot
from apps.event_bus.models import ConsumedEvent, DeadLetterEvent, ReplayRequest, StreamHealthMetric
from apps.event_bus.schemas import decimal_safe, validate_envelope
from apps.event_bus.services import (
    consume_once,
    deterministic_invalid_event_error,
    envelope_for,
    publish_batch,
    replay_envelopes,
    route_dead_letter,
)
from apps.event_bus.tasks import compact_operational_records
from django.utils import timezone

pytestmark = pytest.mark.django_db


class Publisher:
    def __init__(self, fail=False): self.fail=fail; self.events=[]
    def publish(self,event):
        if self.fail: raise RuntimeError("kafka down")
        self.events.append(event.event_id)


def event(key="one"):
    return OutboxEvent.objects.create(topic="system.health.v1",aggregate_id="backend",payload={"value":str(Decimal("1.20"))},idempotency_key=key)


def test_envelope_schema_and_decimal_serialization():
    item=event(); envelope=envelope_for(item)
    assert validate_envelope(envelope) and decimal_safe({"x":Decimal("1.20")})=={"x":"1.20"}


@override_settings(KAFKA_ENABLED=True)
def test_outbox_ack_and_retry():
    item=event(); assert publish_batch(Publisher())==1
    item.refresh_from_db(); assert item.status=="PUBLISHED" and item.published_at
    failed=event("two"); assert publish_batch(Publisher(True))==0
    failed.refresh_from_db(); assert failed.status=="FAILED" and failed.attempt_count==1 and "kafka down" in failed.last_error


@override_settings(KAFKA_ENABLED=True)
def test_outbox_produces_a_batch_once_and_preserves_per_event_results():
    first=event("batch-one");second=event("batch-two")
    class BatchPublisher:
        calls=0
        def publish_batch(self,events):
            self.calls+=1
            return {item.pk:("broker rejected" if item.pk==second.pk else "") for item in events}
    publisher=BatchPublisher()
    assert publish_batch(publisher)==1 and publisher.calls==1
    first.refresh_from_db();second.refresh_from_db()
    assert first.status=="PUBLISHED" and first.published_at is not None
    assert second.status=="FAILED" and second.published_at is None and "broker rejected" in second.last_error


def test_duplicate_consumer_is_idempotent():
    envelope=envelope_for(event()); calls=[]
    assert consume_once("bars",envelope,lambda x:calls.append(x) or {"ok":True})=={"ok":True}
    assert consume_once("bars",envelope,lambda x:calls.append(x))=={"duplicate":True}
    assert len(calls)==1 and ConsumedEvent.objects.count()==1


def test_failed_consumer_remains_visible_and_can_be_retried():
    envelope=envelope_for(event("failed-consumer"));calls=[]
    def fail_once(value):
        calls.append(value)
        if len(calls)==1:raise RuntimeError("strategy processing exploded")
        return {"ok":True}
    with pytest.raises(RuntimeError,match="strategy processing exploded"):
        consume_once("bars",envelope,fail_once)
    failed=ConsumedEvent.objects.get()
    assert failed.result=={"status":"FAILED","retryable":True,"error":"strategy processing exploded"}
    assert consume_once("bars",envelope,fail_once)=={"ok":True}
    failed.refresh_from_db()
    assert failed.result=={"ok":True} and len(calls)==2


def test_dead_letter_retains_original_event():
    envelope=envelope_for(event()); route_dead_letter("market.raw.v1",envelope,"malformed","normalizer")
    row=DeadLetterEvent.objects.get(); assert row.envelope["event_id"]==envelope["event_id"] and row.reason=="malformed"


class ConsumerMessage:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def value(self): return self.payload
    def error(self): return None
    def topic(self): return "market.bars.v1"
    def partition(self): return 0
    def offset(self): return 7


class OneMessageConsumer:
    def __init__(self, payload):
        self.message = ConsumerMessage(payload)
        self.commits = []
        self.closed = False

    def subscribe(self, topics): self.topics = topics
    def poll(self, timeout):
        message, self.message = self.message, None
        return message
    def commit(self, **kwargs): self.commits.append(kwargs)
    def close(self): self.closed = True


@override_settings(KAFKA_ENABLED=True)
def test_market_consumer_does_not_commit_transient_database_failure(monkeypatch):
    fake=OneMessageConsumer({"event_id":str(uuid.uuid4())})
    monkeypatch.setitem(
        sys.modules,
        "confluent_kafka",
        # The command imports `Consumer, KafkaError` and tests error codes
        # against KafkaError._PARTITION_EOF, so the stub carries the real class
        # rather than a placeholder that would silently compare unequal.
        SimpleNamespace(Consumer=lambda _configuration:fake, KafkaError=KafkaError),
    )
    monkeypatch.setattr(
        "apps.market_streams.management.commands.consume_market_streams.consume_market_event",
        lambda *_args,**_kwargs:(_ for _ in ()).throw(
            OperationalError("database temporarily unavailable")
        ),
    )
    with pytest.raises(OperationalError,match="temporarily unavailable"):
        call_command("consume_market_streams","--once")
    assert fake.commits == []
    assert fake.closed
    assert DeadLetterEvent.objects.count() == 0
    assert not deterministic_invalid_event_error(OperationalError("retry"))


@override_settings(KAFKA_ENABLED=True)
def test_market_consumer_commits_deterministic_invalid_only_after_dlq(monkeypatch):
    payload={"event_id":str(uuid.uuid4()),"payload":{"bad":True}}
    fake=OneMessageConsumer(payload)
    monkeypatch.setitem(
        sys.modules,
        "confluent_kafka",
        # The command imports `Consumer, KafkaError` and tests error codes
        # against KafkaError._PARTITION_EOF, so the stub carries the real class
        # rather than a placeholder that would silently compare unequal.
        SimpleNamespace(Consumer=lambda _configuration:fake, KafkaError=KafkaError),
    )
    monkeypatch.setattr(
        "apps.market_streams.management.commands.consume_market_streams.consume_market_event",
        lambda *_args,**_kwargs:(_ for _ in ()).throw(ValueError("invalid schema")),
    )
    call_command("consume_market_streams","--once")
    row=DeadLetterEvent.objects.get()
    assert row.envelope == payload and row.reason == "invalid schema"
    assert len(fake.commits) == 1
    assert fake.commits[0]["message"].offset() == 7
    assert fake.closed


def test_market_dlq_replay_command_forces_replay_mode(monkeypatch):
    row=DeadLetterEvent.objects.create(
        source_topic="market.bars.v1",
        consumer_name="market-persistence-v2",
        reason="previous deterministic failure",
        envelope={"event_id":str(uuid.uuid4()),"payload":{"processing_mode":"LIVE"}},
    )
    consumed=[]
    monkeypatch.setattr(
        "apps.market_streams.services.consume_market_event",
        lambda consumer,envelope:consumed.append((consumer,envelope)) or {"ok":True},
    )
    call_command("replay_market_dlq","--id",str(row.pk))
    row.refresh_from_db()
    assert row.replayed_at is not None
    assert consumed[0][0] == "market-persistence-v2"
    assert consumed[0][1]["payload"]["processing_mode"] == "REPLAY"


def test_replay_uses_consumer_idempotency():
    request=ReplayRequest.objects.create(topic="market.bars.v1",consumer_name="rebuild-bars",idempotency_key="replay-1")
    values=[envelope_for(event("replay-event"))];calls=[]
    replay_envelopes(request,values,lambda name,item:consume_once(name,item,lambda x:calls.append(x)))
    request.refresh_from_db();assert request.status=="COMPLETED" and request.processed_count==1 and len(calls)==1


def test_market_replay_forces_non_live_processing_mode():
    request=ReplayRequest.objects.create(topic="market.bars.v1",consumer_name="safe-replay",idempotency_key="safe-replay")
    values=[envelope_for(event("safe-replay-event"))];calls=[]
    replay_envelopes(request,values,lambda _name,item:calls.append(item))
    assert calls[0]["payload"]["processing_mode"]=="REPLAY"


def test_replay_status_endpoint_is_pollable(client):
    request=ReplayRequest.objects.create(topic="market.bars.v1",consumer_name="poll-replay",idempotency_key="poll-replay")
    body=client.get(f"/api/v1/streaming/replay/{request.pk}/").json()
    assert body["data"]["id"]==request.pk and body["data"]["status"]=="REQUESTED"
    assert client.post(f"/api/v1/streaming/replay/{request.pk}/").status_code==405


@override_settings(OUTBOX_RETENTION_DAYS=1,BROKER_SNAPSHOT_RETENTION_DAYS=1,STREAM_HEALTH_RETENTION_DAYS=1,
    OPERATIONAL_COMPACTION_BATCH_SIZE=100)
def test_compaction_deletes_only_expired_operational_records():
    old=timezone.now()-timedelta(days=2)
    published=event("compact-published");OutboxEvent.objects.filter(pk=published.pk).update(status="PUBLISHED",published_at=old)
    failed=event("compact-failed");OutboxEvent.objects.filter(pk=failed.pk).update(status="FAILED",available_at=old)
    account=BrokerAccount.objects.create(account_id="DU-COMPACT")
    snapshot=BrokerPositionSnapshot.objects.create(broker_account=account,snapshot_key="compact",status="COMPLETED",complete=True)
    BrokerPositionSnapshot.objects.filter(pk=snapshot.pk).update(completed_at=old)
    audit=AuditEvent.objects.create(event_type="immutable",actor="test",aggregate_type="test",aggregate_id="1",
        idempotency_key="audit-immutable")
    metric=StreamHealthMetric.objects.create(component="retired-worker",metric="heartbeat",status="STALE")
    StreamHealthMetric.objects.filter(pk=metric.pk).update(observed_at=old)
    compact_operational_records()
    assert not OutboxEvent.objects.filter(pk=published.pk).exists()
    assert not BrokerPositionSnapshot.objects.filter(pk=snapshot.pk).exists()
    assert OutboxEvent.objects.filter(pk=failed.pk).exists()
    assert not StreamHealthMetric.objects.filter(pk=metric.pk).exists()
    assert AuditEvent.objects.filter(pk=audit.pk).exists()
