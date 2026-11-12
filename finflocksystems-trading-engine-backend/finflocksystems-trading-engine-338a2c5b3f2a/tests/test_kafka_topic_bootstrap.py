from types import SimpleNamespace

from scripts import ensure_kafka_topics


def kafka_environment(monkeypatch):
    monkeypatch.setenv("KAFKA_ENABLED", "true")
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
    monkeypatch.setenv("KAFKA_BOOTSTRAP_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("KAFKA_BOOTSTRAP_RETRY_SECONDS", "1")
    monkeypatch.setenv("KAFKA_ADMIN_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv(
        "EXECUTION_REQUIRED_KAFKA_TOPICS",
        "market.raw.v1,market.canonical.v1",
    )


def test_flush_existing_topics_advances_low_watermark_to_high(monkeypatch):
    kafka_environment(monkeypatch)
    state = {"flushed": False}

    class Future:
        def result(self, timeout):
            assert timeout == 5
            state["flushed"] = True

    class Admin:
        def __init__(self, configuration):
            self.configuration = configuration
            self.deleted = []

        def list_topics(self, timeout):
            assert timeout == 5
            return SimpleNamespace(
                topics={
                    "market.raw.v1": SimpleNamespace(partitions={0: object()}),
                    "market.canonical.v1": SimpleNamespace(
                        partitions={0: object(), 1: object()}
                    ),
                }
            )

        def delete_records(self, targets, **kwargs):
            self.deleted = list(targets)
            assert kwargs == {"operation_timeout": 5, "request_timeout": 5}
            assert sorted(
                (target.topic, target.partition, target.offset)
                for target in self.deleted
            ) == [
                ("market.canonical.v1", 0, 4),
                ("market.raw.v1", 0, 3),
            ]
            return {target: Future() for target in self.deleted}

    class CleanupConsumer:
        def __init__(self, configuration):
            assert configuration["enable.auto.commit"] is False
            self.closed = False

        def get_watermark_offsets(self, marker, **kwargs):
            assert kwargs == {"timeout": 5, "cached": False}
            highs = {
                ("market.raw.v1", 0): 3,
                ("market.canonical.v1", 0): 4,
                ("market.canonical.v1", 1): 0,
            }
            high = highs[(marker.topic, marker.partition)]
            return (high if state["flushed"] else 0, high)

        def close(self):
            self.closed = True

    monkeypatch.setattr(ensure_kafka_topics, "AdminClient", Admin)
    monkeypatch.setattr(ensure_kafka_topics, "Consumer", CleanupConsumer)

    ensure_kafka_topics.flush_existing_kafka_topics()

    assert state["flushed"] is True


def test_flush_topics_is_noop_when_required_topics_do_not_exist(
    monkeypatch, capsys
):
    kafka_environment(monkeypatch)

    class Admin:
        def __init__(self, _configuration):
            pass

        def list_topics(self, timeout):
            return SimpleNamespace(topics={})

    class CleanupConsumer:
        def __init__(self, _configuration):
            self.closed = False

        def close(self):
            self.closed = True

    monkeypatch.setattr(ensure_kafka_topics, "AdminClient", Admin)
    monkeypatch.setattr(ensure_kafka_topics, "Consumer", CleanupConsumer)

    ensure_kafka_topics.flush_existing_kafka_topics()

    assert "nothing to flush" in capsys.readouterr().out
