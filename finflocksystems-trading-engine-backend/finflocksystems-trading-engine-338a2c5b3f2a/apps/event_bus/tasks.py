from celery import shared_task
from django.conf import settings
from datetime import timedelta
from django.utils import timezone
from .models import StreamHealthMetric
from .services import publish_batch


@shared_task
def publish_outbox_events():
    try:
        published=publish_batch()
        from apps.audit.models import OutboxEvent
        failed=OutboxEvent.objects.filter(status="FAILED").count()
        StreamHealthMetric.objects.update_or_create(
            component="outbox-publisher",
            metric="heartbeat",
            defaults={
                "status":"HEALTHY" if failed==0 else "DEGRADED",
                "value":{"published":published,"failed":failed},
            },
        )
        return published
    except Exception as exc:
        StreamHealthMetric.objects.update_or_create(
            component="outbox-publisher",
            metric="heartbeat",
            defaults={"status":"DEGRADED","value":{"error":str(exc)[:255]}},
        )
        raise


@shared_task
def compact_operational_records():
    from apps.audit.models import OutboxEvent
    from apps.broker_gateway.models import BrokerPositionSnapshot
    from apps.event_bus.models import StreamHealthMetric
    now=timezone.now();limit=settings.OPERATIONAL_COMPACTION_BATCH_SIZE
    rules=[
        ("published_outbox",OutboxEvent.objects.filter(status="PUBLISHED",
            published_at__lt=now-timedelta(days=settings.OUTBOX_RETENTION_DAYS))),
        ("completed_broker_snapshots",BrokerPositionSnapshot.objects.filter(status="COMPLETED",
            completed_at__lt=now-timedelta(days=settings.BROKER_SNAPSHOT_RETENTION_DAYS))),
        ("stale_stream_health",StreamHealthMetric.objects.filter(
            observed_at__lt=now-timedelta(days=settings.STREAM_HEALTH_RETENTION_DAYS))),
    ]
    deleted={}
    for name,query in rules:
        ids=list(query.order_by("pk").values_list("pk",flat=True)[:limit])
        count,_=query.model.objects.filter(pk__in=ids).delete()
        deleted[name]=count
    return deleted


@shared_task
def check_stream_health():
    status, value = "DISABLED", {"enabled": False}
    if settings.KAFKA_ENABLED:
        try:
            from confluent_kafka.admin import AdminClient
            metadata = AdminClient({"bootstrap.servers": settings.KAFKA_BOOTSTRAP_SERVERS}).list_topics(timeout=5)
            topic_names=sorted(
                name for name,value in metadata.topics.items()
                if not getattr(value,"error",None)
            )
            status, value = "HEALTHY", {
                "enabled": True,
                "topic_count":len(topic_names),
                "topics":topic_names,
            }
        except Exception as exc:
            status, value = "DEGRADED", {"enabled": True, "error": str(exc)[:255]}
    metric, _ = StreamHealthMetric.objects.update_or_create(component="kafka", metric="connectivity",
                                                              defaults={"status": status, "value": value})
    lag_status="DISABLED";lag_value={"topics":{},"total":0}
    if settings.KAFKA_ENABLED and status=="HEALTHY":
        consumer=None
        try:
            from confluent_kafka import Consumer, TopicPartition
            topics=["market.bars.v1","market.indicators.v1","market.quality.v1"]
            consumer=Consumer({"bootstrap.servers":settings.KAFKA_BOOTSTRAP_SERVERS,
                "group.id":"finflock-backend-market-persistence-v2","enable.auto.commit":False})
            partitions=[]
            topic_partitions={}
            cluster=consumer.list_topics(timeout=5)
            for topic in topics:
                ids=sorted((cluster.topics.get(topic).partitions if cluster.topics.get(topic) else {}).keys())
                topic_partitions[topic]=[TopicPartition(topic,partition) for partition in ids]
                partitions.extend(topic_partitions[topic])
            committed={(item.topic,item.partition):item.offset for item in consumer.committed(partitions,timeout=5)} if partitions else {}
            for topic,items in topic_partitions.items():
                total=0
                for item in items:
                    _,high=consumer.get_watermark_offsets(item,timeout=5,cached=False)
                    offset=committed.get((item.topic,item.partition),-1)
                    total+=high if offset is None or offset<0 else max(0,high-offset)
                lag_value["topics"][topic]=total;lag_value["total"]+=total
            lag_status="DEGRADED" if lag_value["total"]>settings.KAFKA_LAG_DEGRADED_THRESHOLD else "HEALTHY"
        except Exception as exc:
            lag_status="DEGRADED";lag_value={"topics":{},"total":None,"error":str(exc)[:255]}
        finally:
            if consumer:consumer.close()
    StreamHealthMetric.objects.update_or_create(component="backend-market-consumer",metric="topic_lag",
        defaults={"status":lag_status,"value":lag_value})
    gateway_status="DEGRADED";gateway_value={"connected":False,"reconciled":False}
    try:
        from apps.broker_gateway.models import BrokerGatewaySession
        active=list(BrokerGatewaySession.objects.exclude(status__in=[BrokerGatewaySession.Status.STOPPING,
            BrokerGatewaySession.Status.DELETED]).values("id","status","last_gateway_state"))
        connected=[item for item in active if item["status"]==BrokerGatewaySession.Status.CONNECTED]
        gateway_value={"connected":bool(connected),
            "reconciled":bool(connected) and all(item["last_gateway_state"].get("reconciled") for item in connected),
            "session_count":len(active)}
        gateway_status="HEALTHY" if gateway_value.get("connected") and gateway_value.get("reconciled") else "DEGRADED"
    except Exception as exc:
        gateway_value={**gateway_value,"error":str(exc)[:255]}
    StreamHealthMetric.objects.update_or_create(component="gateway",metric="connectivity",
        defaults={"status":gateway_status,"value":gateway_value})
    return {"status":metric.status,"lag_status":lag_status,"gateway_status":gateway_status,
        "observed_at":timezone.now().isoformat()}


@shared_task
def execute_replay_request(request_id):
    from .services import execute_replay
    return execute_replay(request_id)
