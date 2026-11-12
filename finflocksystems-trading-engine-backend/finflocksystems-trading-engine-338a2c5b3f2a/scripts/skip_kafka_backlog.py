#!/usr/bin/env python3
"""Move the Backend market consumer past an old backlog on selected topics.

The market consumer reads market.bars.v1, market.indicators.v1 and
market.quality.v1 in one process, one message at a time. A flood of old
market.quality.v1 messages (FRESH/STALE marks, where only the newest per
instrument matters) can hold new bars and indicators behind it for hours, so
strategy warm-up times out.

Run at Backend start, before supervisord launches the consumer: for each listed
topic it commits the consumer group's offset at the current end of every
partition. Only the listed topics are touched; bars and indicators are not
skipped. The consumer group must have no active members (the old container is
already stopped at this point), otherwise Kafka rejects the commit and this
script only logs a warning.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from confluent_kafka import Consumer, KafkaException, TopicPartition
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env", override=False)

GROUP_ID = "finflock-backend-market-persistence-v2"


def main() -> int:
    topics = [
        item.strip()
        for item in os.getenv("MARKET_CONSUMER_SKIP_BACKLOG_TOPICS", "market.quality.v1").split(",")
        if item.strip()
    ]
    if not topics:
        print("skip-backlog: no topics configured; nothing to do")
        return 0
    consumer = Consumer({
        "bootstrap.servers": os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092"),
        "group.id": GROUP_ID,
        "enable.auto.commit": False,
    })
    try:
        metadata = consumer.list_topics(timeout=15)
        offsets = []
        for topic in topics:
            if topic not in metadata.topics:
                print(f"skip-backlog: topic {topic} does not exist; skipped")
                continue
            partitions = [TopicPartition(topic, p) for p in metadata.topics[topic].partitions]
            committed = {
                item.partition: item.offset
                for item in consumer.committed(partitions, timeout=15)
            }
            for partition in partitions:
                _low, high = consumer.get_watermark_offsets(partition, timeout=15, cached=False)
                before = committed.get(partition.partition)
                skipped = (high - before) if before is not None and before >= 0 else "unknown"
                print(
                    f"skip-backlog: {topic}[{partition.partition}] committed={before} "
                    f"end={high} skipping={skipped}"
                )
                offsets.append(TopicPartition(topic, partition.partition, high))
        if offsets:
            consumer.commit(offsets=offsets, asynchronous=False)
            print(f"skip-backlog: committed end offsets for {len(offsets)} partition(s) of group {GROUP_ID}")
        return 0
    except KafkaException as exc:
        print(f"skip-backlog: WARNING could not move offsets ({exc}); continuing", file=sys.stderr)
        return 1
    finally:
        consumer.close()


if __name__ == "__main__":
    raise SystemExit(main())
