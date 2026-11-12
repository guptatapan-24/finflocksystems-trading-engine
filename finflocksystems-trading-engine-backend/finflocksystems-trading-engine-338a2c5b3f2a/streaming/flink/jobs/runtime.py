import logging
import os
from pyflink.common import SimpleStringSchema
from pyflink.datastream import StreamExecutionEnvironment
from pyflink.datastream.connectors.kafka import (
    KafkaOffsetResetStrategy,
    KafkaOffsetsInitializer,
    KafkaSink,
    KafkaSource,
)
from pyflink.datastream.connectors.kafka import KafkaRecordSerializationSchema

from jobs.identity import starting_offset_policy
from jobs.python_worker import ARCHIVE_MODE, WorkerPythonConfig, safe_path_for_log


LOGGER = logging.getLogger("pyflink-job-runtime")


def configure_worker_python(env, job_name, environ=None):
    """Embed and select the worker-side Python runtime for a submitted graph."""
    config = WorkerPythonConfig.from_environment(environ)
    config.validate()
    if config.runtime_mode == ARCHIVE_MODE:
        env.add_python_archive(str(config.archive_path), config.archive_target)
    env.set_python_executable(config.executable)
    LOGGER.info(
        "Configured worker Python for job '%s': mode=%s executable=%s "
        "archive_configured=%s archive_target=%s",
        job_name,
        config.runtime_mode,
        safe_path_for_log(config.executable),
        config.archive_configured,
        safe_path_for_log(config.archive_target)
        if config.archive_configured
        else None,
    )
    return config


def _offset_initializer(policy):
    if policy == "earliest":
        return KafkaOffsetsInitializer.earliest()
    if policy == "latest":
        return KafkaOffsetsInitializer.latest()
    return KafkaOffsetsInitializer.committed_offsets(KafkaOffsetResetStrategy.LATEST)


def environment(job_name):
    env = StreamExecutionEnvironment.get_execution_environment()
    configure_worker_python(env, job_name)
    env.enable_checkpointing(int(os.getenv("FLINK_CHECKPOINT_INTERVAL_MS", "30000")))
    env.get_checkpoint_config().set_checkpoint_timeout(int(os.getenv("FLINK_CHECKPOINT_TIMEOUT_MS", "120000")))
    env.set_parallelism(int(os.getenv("FLINK_PARALLELISM", "1")))
    return env


def source(env, topic, group, starting_offsets=None):
    policy = starting_offset_policy(group, starting_offsets, os.environ)
    return env.from_source(KafkaSource.builder().set_bootstrap_servers(
        os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")).set_topics(topic).set_group_id(group).set_starting_offsets(
        _offset_initializer(policy)).set_value_only_deserializer(SimpleStringSchema()).build(),
        __import__("pyflink.common", fromlist=["WatermarkStrategy"]).WatermarkStrategy.no_watermarks(), group + "-source").uid(group + "-source-v1")


def sink(stream, topic, uid):
    target = KafkaSink.builder().set_bootstrap_servers(os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")).set_record_serializer(
        KafkaRecordSerializationSchema.builder().set_topic(topic).set_value_serialization_schema(SimpleStringSchema()).build()).build()
    stream.sink_to(target).uid(uid)
