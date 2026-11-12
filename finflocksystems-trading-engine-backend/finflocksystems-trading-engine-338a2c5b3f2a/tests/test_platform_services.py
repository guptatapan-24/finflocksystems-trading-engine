"""Kafka / Flink sub-containers. QCH is faked; no network."""
from types import SimpleNamespace

import pytest

from apps.broker_gateway import platform_services as ps
from apps.broker_gateway.configuration import parse_docker_hub_image_reference
from apps.broker_gateway.qch import QCHError


class FakeQCH:
    def __init__(self, existing=(), fail_on=None):
        self.existing, self.created, self.deleted, self.fail_on = set(existing), [], [], fail_on

    def find_by_name(self, name):
        return SimpleNamespace(name=name, status="RUNNING") if name in self.existing else None

    def create_container(self, *, name, image, command=None, env=None, network=None):
        if self.fail_on == name:
            raise QCHError("refused", status_code=422)
        self.created.append({"name": name, "image": image, "command": command, "env": env, "network": network})
        return SimpleNamespace(name=name, status="STARTING")

    def delete_container(self, name):
        self.deleted.append(name)


@pytest.fixture
def on(settings):
    settings.PLATFORM_SERVICES_ENABLED = True
    settings.PLATFORM_SERVICE_PREFIX = "finflock"
    settings.QCH_SUBCONTAINER_NETWORK = "qch-app-resources"
    return settings


def test_does_nothing_unless_enabled(settings):
    settings.PLATFORM_SERVICES_ENABLED = False
    qch = FakeQCH()
    assert ps.ensure_all(qch) == {"status": "disabled"} and qch.created == []


def test_creates_kafka_then_both_flink_services_with_the_compose_settings(on):
    qch = FakeQCH()
    result = ps.ensure_all(qch)
    assert result == {"kafka": "created:STARTING", "jobmanager": "created:STARTING", "taskmanager": "created:STARTING"}
    assert [c["name"] for c in qch.created] == ["finflock-kafka", "finflock-flink-jobmanager", "finflock-flink-taskmanager"]
    kafka, jm, tm = qch.created
    assert kafka["image"] == "docker.io/apache/kafka:3.9.0" and kafka["command"] is None and kafka["network"] == "qch-app-resources"
    assert kafka["env"]["KAFKA_ADVERTISED_LISTENERS"] == "PLAINTEXT://finflock-kafka:9092"
    assert kafka["env"]["KAFKA_CONTROLLER_QUORUM_VOTERS"] == "1@finflock-kafka:9093" and kafka["env"]["CLUSTER_ID"] == "MkU3OEVBNTcwNTJENDM2Qk"
    assert jm["command"] == ["jobmanager"] and tm["command"] == ["taskmanager"]
    assert jm["image"] == tm["image"] == "docker.io/library/flink:1.20.1-scala_2.12-java17"
    assert "jobmanager.rpc.address: finflock-flink-jobmanager" in jm["env"]["FLINK_PROPERTIES"]
    assert "jobmanager.rpc.address: finflock-flink-jobmanager" in tm["env"]["FLINK_PROPERTIES"]
    assert "taskmanager.memory.process.size: 3072m" in tm["env"]["FLINK_PROPERTIES"]


def test_existing_services_are_left_alone_and_nothing_is_ever_deleted(on):
    qch = FakeQCH(existing={"finflock-kafka", "finflock-flink-jobmanager"})
    result = ps.ensure_all(qch)
    assert result["kafka"] == "present:RUNNING" and result["taskmanager"] == "created:STARTING"
    assert [c["name"] for c in qch.created] == ["finflock-flink-taskmanager"] and qch.deleted == []


def test_a_refusal_for_kafka_stops_the_rest_and_raises_nothing(on):
    qch = FakeQCH(fail_on="finflock-kafka")
    result = ps.ensure_all(qch)
    assert result == {"kafka": "qch_error:422"} and qch.created == []


def test_a_refusal_for_flink_does_not_undo_kafka(on):
    qch = FakeQCH(fail_on="finflock-flink-jobmanager")
    result = ps.ensure_all(qch)
    assert result["kafka"] == "created:STARTING" and result["jobmanager"] == "qch_error:422" and result["taskmanager"] == "created:STARTING"


def test_a_bad_image_setting_is_refused_before_anything_is_created(on):
    on.PLATFORM_KAFKA_IMAGE = "docker.io/apache/kafka:latest"
    qch = FakeQCH()
    result = ps.ensure_all(qch)
    assert result["kafka"] == "invalid_image" and "finflock-kafka" not in [c["name"] for c in qch.created]


def test_the_default_images_pass_the_platforms_own_image_validation():
    for spec in ps.specs():
        assert parse_docker_hub_image_reference(spec.image) == spec.image


def test_backend_environment_points_at_the_service_names():
    assert ps.backend_environment() == {"KAFKA_ENABLED": "true", "KAFKA_BOOTSTRAP_SERVERS": "finflock-kafka:9092",
                                        "FLINK_REST_URL": "http://finflock-flink-jobmanager:8081"}
    assert ps.backend_environment("acme")["KAFKA_BOOTSTRAP_SERVERS"] == "acme-kafka:9092"


def test_no_secret_is_sent_to_the_services(on):
    qch = FakeQCH()
    ps.ensure_all(qch)
    blob = " ".join(str(c["env"]) for c in qch.created).upper()
    assert not any(word in blob for word in ("PASSWORD", "TOKEN", "SECRET", "DATABASE_URL", "REDIS"))
