import json
import os
import signal
import time
from django.conf import settings
from django.core.management.base import BaseCommand
from apps.core.db_outage import DbOutageBackoff
from apps.event_bus.services import deterministic_invalid_event_error,route_dead_letter
from apps.event_bus.models import StreamHealthMetric
from apps.market_streams.services import consume_market_event


class Command(BaseCommand):
    help="Persist Flink market outputs idempotently and commit offsets only after PostgreSQL commits."

    def add_arguments(self,parser):
        parser.add_argument("--once",action="store_true")

    def handle(self,*args,**options):
        if not settings.KAFKA_ENABLED:
            self.stdout.write("Kafka is disabled; market consumer exiting")
            return
        from confluent_kafka import Consumer, KafkaError
        consumer=Consumer({"bootstrap.servers":settings.KAFKA_BOOTSTRAP_SERVERS,
            "group.id":"finflock-backend-market-persistence-v2","enable.auto.commit":False,
            "auto.offset.reset":"earliest"})
        consumer.subscribe(["market.bars.v1","market.indicators.v1","market.quality.v1"])
        running=True
        failed=False
        last_heartbeat=0.0
        def heartbeat(status="HEALTHY",**value):
            StreamHealthMetric.objects.update_or_create(component="backend-market-consumer",metric="heartbeat",
                defaults={"status":status,"value":{"pid":os.getpid(),**value}})
        def stop(*_args):
            nonlocal running;running=False
        signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
        # A database outage is waited out in this process (same uncommitted
        # message, bounded backoff) instead of exiting for supervisord to respawn.
        # --once keeps raising immediately.
        outage=None if options["once"] else DbOutageBackoff("market-consumer",stop=lambda:not running)
        def guarded(operation):
            return operation() if outage is None else outage.run(operation)
        try:
            while running:
                now=time.monotonic()
                if now-last_heartbeat>=10:
                    guarded(heartbeat);last_heartbeat=now
                message=consumer.poll(1)
                if message is None:
                    if options["once"]:break
                    continue
                if message.error():
                    kafka_error = message.error()

                    if kafka_error.code() == KafkaError._PARTITION_EOF:
                        continue

                    error_message = str(kafka_error)

                    guarded(lambda: heartbeat(
                        "DEGRADED",
                        kafka_error_code=kafka_error.code(),
                        error=error_message[:255],
                        retryable=not kafka_error.fatal(),
                    ))

                    self.stderr.write(
                        self.style.WARNING(
                            f"Kafka consumer error: {error_message}"
                        )
                    )

                    if kafka_error.fatal():
                        raise RuntimeError(error_message)

                    if options["once"]:
                        raise RuntimeError(error_message)

                    time.sleep(2)
                    continue
                envelope=None
                processing_error=None
                dead_letter=None
                try:
                    envelope=json.loads(message.value())
                    guarded(lambda: consume_market_event("market-persistence-v2",envelope))
                except Exception as exc:
                    processing_error=str(exc)
                    if not deterministic_invalid_event_error(exc):
                        heartbeat(
                            "DEGRADED",
                            topic=message.topic(),
                            partition=message.partition(),
                            offset=message.offset(),
                            retryable=True,
                            error=str(exc)[:255],
                        )
                        raise
                    dead_letter=guarded(lambda: route_dead_letter(message.topic(),envelope or {"raw":message.value().decode(errors="replace")},
                        exc,"market-persistence-v2"))
                consumer.commit(message=message,asynchronous=False)
                guarded(lambda: StreamHealthMetric.objects.update_or_create(component="backend-market-consumer",metric="last_event",
                    defaults={"status":"DEGRADED" if processing_error else "HEALTHY","value":{"topic":message.topic(),
                        "partition":message.partition(),"offset":message.offset(),"processing_error":processing_error,
                        "dead_letter_id":dead_letter.pk if dead_letter else None}}))
                if options["once"]:break
        except Exception as exc:
            failed=True;heartbeat("DEGRADED",error=str(exc)[:255]);raise
        finally:
            consumer.close()
            if not failed:heartbeat("STOPPED")
