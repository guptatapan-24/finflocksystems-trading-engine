"""Kafka and Flink as QCH sub-containers, like the IB Gateway children.

The hosted platform attaches only Postgres and Redis. The compose stack (docker-compose.options.yml) also runs Kafka and a Flink
JobManager + TaskManager, which are missing on the platform: the System page then shows KAFKA_UNAVAILABLE and the execution
readiness stays NOT READY. This module creates those three services as sub-containers from their PUBLIC Docker Hub images (no
image of our own is needed), with the same settings as the compose file.

Safety rules (the same ones the gateway children follow):
* off unless PLATFORM_SERVICES_ENABLED=true; a no-op otherwise
* creates a service ONLY when QCH lists it as absent, and `create_container` resolves a name conflict by returning the existing
  child, so a flapping listing cannot create a duplicate
* never deletes or replaces anything; never touches the IB Gateway children
* a QCH problem changes nothing and is reported, never raised into the Backend's startup

Known limits (see ingestionclaude.md section 19): sub-containers have no volumes here, so Kafka's data is ephemeral (the instrument
registry topic is republished by the Backend every minute) and Flink state is lost on a restart (jobs are resubmitted at Backend start).
"""
import logging
from dataclasses import dataclass

from django.conf import settings

logger = logging.getLogger(__name__)

KAFKA_CLUSTER_ID = "MkU3OEVBNTcwNTJENDM2Qk"   # same as the compose file


@dataclass(frozen=True)
class ServiceSpec:
    key: str
    name: str
    image: str
    command: list | None
    env: dict


def service_names(prefix=None):
    prefix = prefix or getattr(settings, "PLATFORM_SERVICE_PREFIX", "finflock")
    return {"kafka": f"{prefix}-kafka", "jobmanager": f"{prefix}-flink-jobmanager", "taskmanager": f"{prefix}-flink-taskmanager"}


def specs(prefix=None):
    names = service_names(prefix)
    kafka, jm = names["kafka"], names["jobmanager"]
    flink_image = getattr(settings, "PLATFORM_FLINK_IMAGE", "docker.io/library/flink:1.20.1-scala_2.12-java17")
    return [
        ServiceSpec("kafka", kafka, getattr(settings, "PLATFORM_KAFKA_IMAGE", "docker.io/apache/kafka:3.9.0"), None, {
            "KAFKA_NODE_ID": "1", "KAFKA_PROCESS_ROLES": "broker,controller",
            "KAFKA_LISTENERS": "PLAINTEXT://:9092,CONTROLLER://:9093",
            "KAFKA_ADVERTISED_LISTENERS": f"PLAINTEXT://{kafka}:9092",
            "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP": "CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT",
            "KAFKA_CONTROLLER_LISTENER_NAMES": "CONTROLLER", "KAFKA_CONTROLLER_QUORUM_VOTERS": f"1@{kafka}:9093",
            "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR": "1", "KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR": "1",
            "KAFKA_TRANSACTION_STATE_LOG_MIN_ISR": "1", "CLUSTER_ID": KAFKA_CLUSTER_ID, "KAFKA_HEAP_OPTS": "-Xmx512m -Xms256m",
        }),
        ServiceSpec("jobmanager", jm, flink_image, ["jobmanager"], {"FLINK_PROPERTIES": "\n".join([
            f"jobmanager.rpc.address: {jm}", "parallelism.default: 1", "execution.checkpointing.interval: 10s",
            "jobmanager.memory.process.size: 1024m"])}),
        ServiceSpec("taskmanager", names["taskmanager"], flink_image, ["taskmanager"], {"FLINK_PROPERTIES": "\n".join([
            f"jobmanager.rpc.address: {jm}", "taskmanager.numberOfTaskSlots: 2", "taskmanager.memory.jvm-metaspace.size: 384m",
            "taskmanager.memory.process.size: 3072m", "taskmanager.memory.task.off-heap.size: 256m",
            "taskmanager.memory.framework.off-heap.size: 256m", "parallelism.default: 1"])}),
    ]


def backend_environment(prefix=None):
    """The settings the Backend needs to find these services (for the operator to set; not secrets)."""
    names = service_names(prefix)
    return {"KAFKA_ENABLED": "true", "KAFKA_BOOTSTRAP_SERVERS": f"{names['kafka']}:9092", "FLINK_REST_URL": f"http://{names['jobmanager']}:8081"}


def ensure_all(qch_client=None):
    """Idempotent. Returns {service key: status}; never raises."""
    if not getattr(settings, "PLATFORM_SERVICES_ENABLED", False):
        return {"status": "disabled"}
    from .configuration import GatewayImageConfigurationError, parse_docker_hub_image_reference
    from .qch import QCHBrokerClient, QCHError

    try:
        qch = qch_client or QCHBrokerClient()
    except QCHError as exc:
        return {"status": "qch_unavailable", "message": str(exc)}
    network = str(getattr(settings, "QCH_SUBCONTAINER_NETWORK", "") or "").strip() or None
    result = {}
    for spec in specs():
        try:
            image = parse_docker_hub_image_reference(spec.image)
            existing = qch.find_by_name(spec.name)
            if existing is not None:
                result[spec.key] = f"present:{existing.status}"
                continue
            created = qch.create_container(name=spec.name, image=image, command=spec.command, env=spec.env, network=network)
            result[spec.key] = f"created:{created.status}"
            logger.warning("platform_services stage=created service=%s status=%s", spec.name, created.status)
        except GatewayImageConfigurationError:
            result[spec.key] = "invalid_image"
        except QCHError as exc:
            result[spec.key] = f"qch_error:{exc.status_code or 'unreachable'}"
            logger.warning("platform_services stage=qch_error service=%s http=%s", spec.name, exc.status_code)
            if spec.key == "kafka":
                break   # the others depend on it; try again on the next run
    return result
