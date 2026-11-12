import importlib
import sys
import types
import zipfile
from pathlib import Path

import pytest

from streaming.flink.jobs.python_worker import (
    WorkerPythonConfig,
    WorkerPythonConfigurationError,
    is_missing_python_executable_failure,
)


def _runtime_archive(path):
    with zipfile.ZipFile(path, "w") as archive:
        info = zipfile.ZipInfo("bin/python")
        info.external_attr = 0o100755 << 16
        archive.writestr(info, b"portable-python")
    return path


def test_archive_runtime_validates_matching_worker_interpreter(tmp_path):
    archive = _runtime_archive(tmp_path / "runtime.zip")
    config = WorkerPythonConfig.from_environment(
        {
            "FLINK_PYTHON_RUNTIME_MODE": "archive",
            "FLINK_PYTHON_ARCHIVE": str(archive),
            "FLINK_PYTHON_ARCHIVE_TARGET": "pyenv",
            "FLINK_PYTHON_EXECUTABLE": "pyenv/bin/python",
        }
    )

    config.validate()

    assert config.archive_configured is True


def test_archive_runtime_rejects_interpreter_outside_target(tmp_path):
    archive = _runtime_archive(tmp_path / "runtime.zip")
    config = WorkerPythonConfig(
        runtime_mode="archive",
        archive_path=archive,
        archive_target="pyenv",
        executable="other/bin/python",
    )

    with pytest.raises(
        WorkerPythonConfigurationError,
        match="must be inside FLINK_PYTHON_ARCHIVE_TARGET",
    ):
        config.validate()


def test_taskmanager_path_mode_requires_an_absolute_path():
    config = WorkerPythonConfig(
        runtime_mode="taskmanager-path",
        executable="python",
    )

    with pytest.raises(WorkerPythonConfigurationError, match="absolute TaskManager path"):
        config.validate()


@pytest.mark.parametrize(
    "message",
    [
        'java.io.IOException: Cannot run program "python": error=2, No such file or directory',
        "Python executable was not found on the TaskManager",
    ],
)
def test_missing_worker_python_classification(message):
    assert is_missing_python_executable_failure(message) is True


def test_unrelated_failure_is_not_classified_as_missing_python():
    assert is_missing_python_executable_failure("Kafka broker unavailable") is False


def test_shared_job_runtime_adds_archive_before_selecting_worker(
    tmp_path,
    monkeypatch,
):
    archive = _runtime_archive(tmp_path / "runtime.zip")
    common = types.ModuleType("pyflink.common")
    common.SimpleStringSchema = object
    datastream = types.ModuleType("pyflink.datastream")
    datastream.StreamExecutionEnvironment = object
    kafka = types.ModuleType("pyflink.datastream.connectors.kafka")
    for name in (
        "KafkaOffsetResetStrategy",
        "KafkaOffsetsInitializer",
        "KafkaSink",
        "KafkaSource",
        "KafkaRecordSerializationSchema",
    ):
        setattr(kafka, name, object)
    monkeypatch.setitem(sys.modules, "pyflink", types.ModuleType("pyflink"))
    monkeypatch.setitem(sys.modules, "pyflink.common", common)
    monkeypatch.setitem(sys.modules, "pyflink.datastream", datastream)
    monkeypatch.setitem(
        sys.modules,
        "pyflink.datastream.connectors",
        types.ModuleType("pyflink.datastream.connectors"),
    )
    monkeypatch.setitem(sys.modules, "pyflink.datastream.connectors.kafka", kafka)
    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[1] / "streaming" / "flink")
    )
    sys.modules.pop("jobs.runtime", None)
    runtime = importlib.import_module("jobs.runtime")

    class FakeEnvironment:
        def __init__(self):
            self.calls = []

        def add_python_archive(self, path, target):
            self.calls.append(("archive", path, target))

        def set_python_executable(self, executable):
            self.calls.append(("executable", executable))

    environment = FakeEnvironment()
    runtime.configure_worker_python(
        environment,
        "test-job",
        {
            "FLINK_PYTHON_RUNTIME_MODE": "archive",
            "FLINK_PYTHON_ARCHIVE": str(archive),
            "FLINK_PYTHON_ARCHIVE_TARGET": "pyenv",
            "FLINK_PYTHON_EXECUTABLE": "pyenv/bin/python",
        },
    )

    assert environment.calls == [
        ("archive", str(archive), "pyenv"),
        ("executable", "pyenv/bin/python"),
    ]
