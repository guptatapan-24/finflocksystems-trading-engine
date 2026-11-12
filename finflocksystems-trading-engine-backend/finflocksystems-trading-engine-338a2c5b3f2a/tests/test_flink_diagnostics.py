from datetime import datetime, timezone
from urllib.parse import urlsplit

import pytest
import requests

from apps.execution.flink_diagnostics import (
    classify_probable_cause,
    collect_flink_diagnostics,
    sanitize_flink_rest_url,
)


pytestmark = pytest.mark.django_db


class FakeResponse:
    def __init__(self, payload=None, *, status_code=200, json_error=None):
        self.payload = payload
        self.status_code = status_code
        self.json_error = json_error

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)

    def json(self):
        if self.json_error:
            raise self.json_error
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, timeout):
        path = urlsplit(url).path
        self.calls.append((path, timeout))
        value = self.responses[path]
        if isinstance(value, Exception):
            raise value
        return value


def test_sanitize_flink_rest_url_removes_credentials_path_and_query():
    assert (
        sanitize_flink_rest_url(
            "https://operator:secret@flink.internal:8443/private?token=value#fragment"
        )
        == "https://flink.internal:8443"
    )
    assert sanitize_flink_rest_url("file:///tmp/flink") is None


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("No TaskManagers are registered", "NO_TASKMANAGERS"),
        ("No available slots for this job", "NO_AVAILABLE_SLOTS"),
        ("Could not connect to JobManager", "JOBMANAGER_UNAVAILABLE"),
        (
            "Python executable /opt/venv/bin/python was not found",
            "PYTHON_EXECUTABLE_MISSING",
        ),
        (
            'java.io.IOException: Cannot run program "python":\n'
            "error=2, No such file or directory",
            "PYTHON_EXECUTABLE_MISSING",
        ),
        ("ModuleNotFoundError: No module named 'numpy'", "PYTHON_DEPENDENCY_MISSING"),
        ("PyFlink worker process exited unexpectedly", "PYFLINK_WORKER_FAILURE"),
        ("Failed to start Python worker process", "PYFLINK_WORKER_FAILURE"),
        ("Could not find any factory for identifier 'kafka'", "KAFKA_CONNECTOR_MISSING"),
        ("UnknownTopicOrPartitionException", "KAFKA_TOPIC_MISSING"),
        ("Bootstrap broker kafka:9092 disconnected", "KAFKA_UNREACHABLE"),
        (
            "org.apache.kafka.common.errors.TimeoutException waiting for a node",
            "KAFKA_UNREACHABLE",
        ),
        ("java.lang.ClassNotFoundException: example.Missing", "CLASS_NOT_FOUND"),
        ("Checkpoint 3 failed because the coordinator declined it", "CHECKPOINT_FAILURE"),
        ("NoResourceAvailableException: no slots", "RESOURCE_ALLOCATION_FAILURE"),
        ("An unrecognized Flink exception", "UNKNOWN_FLINK_FAILURE"),
    ],
)
def test_classify_probable_cause(message, expected):
    assert classify_probable_cause([message]) == expected


def test_collects_relevant_jobs_and_redacts_diagnostics(settings):
    now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    checkpoint_at = int(now.timestamp() * 1_000) - 30_000
    settings.FLINK_REST_URL = (
        "http://operator:secret@flink-jobmanager:8081/internal?token=hidden"
    )
    settings.FLINK_DIAGNOSTICS_HTTP_TIMEOUT_SECONDS = 4
    settings.EXECUTION_REQUIRED_FLINK_JOBS = ("required-job",)
    settings.PYFLINK_CLIENT_EXECUTABLE = "/opt/pyflink-venv/bin/python"
    settings.FLINK_PYTHON_RUNTIME_MODE = "archive"
    settings.FLINK_PYTHON_ARCHIVE = "/opt/flink-python-runtime/runtime.zip"
    settings.FLINK_PYTHON_ARCHIVE_TARGET = "pyenv"
    settings.FLINK_PYTHON_EXECUTABLE = "pyenv/bin/python"
    settings.FLINK_AUTO_REPAIR_PYTHON_EXECUTABLE_FAILURE = True
    session = FakeSession(
        {
            "/internal/overview": FakeResponse(
                {
                    "flink-version": "1.20.1",
                    "taskmanagers": 1,
                    "slots-total": 4,
                    "slots-available": 2,
                    "jobs-running": 1,
                    "jobs-failed": 0,
                }
            ),
            "/internal/taskmanagers": FakeResponse(
                {
                    "taskmanagers": [
                        {
                            "id": "tm-1",
                            "path": "akka.tcp://flink@10.0.0.5:6122/user/rpc/taskmanager_0",
                            "slotsNumber": 4,
                            "freeSlots": 2,
                            "dataPort": 6121,
                            "timeSinceLastHeartbeat": int(now.timestamp() * 1_000),
                        }
                    ]
                }
            ),
            "/internal/jobs/overview": FakeResponse(
                {
                    "jobs": [
                        {
                            "jid": "abc123",
                            "name": "required-job",
                            "state": "RESTARTING",
                            "start-time": checkpoint_at - 20_000,
                            "duration": 50_000,
                        },
                        {
                            "jid": "healthy999",
                            "name": "unrelated-healthy-job",
                            "state": "RUNNING",
                        },
                    ]
                }
            ),
            "/internal/jobs/abc123": FakeResponse(
                {
                    "jid": "abc123",
                    "name": "required-job",
                    "state": "RESTARTING",
                    "restart-count": 3,
                    "vertices": [{"name": "Kafka Source", "status": "FAILED"}],
                }
            ),
            "/internal/jobs/abc123/exceptions": FakeResponse(
                {
                    "root-exception": (
                        "ModuleNotFoundError: No module named 'custom_package'; "
                        "password=hunter2"
                    ),
                    "timestamp": checkpoint_at,
                    "all-exceptions": [
                        {
                            "exception": f"failure-{index}",
                            "timestamp": checkpoint_at + index,
                            "task": "Kafka Source",
                        }
                        for index in range(6)
                    ],
                }
            ),
            "/internal/jobs/abc123/checkpoints": FakeResponse(
                {
                    "latest": {
                        "completed": {
                            "id": 7,
                            "status": "COMPLETED",
                            "trigger_timestamp": checkpoint_at - 1_000,
                            "latest_ack_timestamp": checkpoint_at,
                            "end_to_end_duration": 1_000,
                        },
                        "failed": {
                            "id": 8,
                            "status": "FAILED",
                            "trigger_timestamp": checkpoint_at + 5_000,
                            "failure-message": "checkpoint token=do-not-return",
                        },
                    }
                }
            ),
        }
    )

    result = collect_flink_diagnostics(session=session, now=now)

    assert result["flink_rest_url"] == "http://flink-jobmanager:8081"
    assert result["reachable"] is True
    assert result["cluster"] == {
        "flink_version": "1.20.1",
        "taskmanager_count": 1,
        "total_slots": 4,
        "available_slots": 2,
        "running_job_count": 1,
        "restarting_job_count": 1,
        "failed_job_count": 0,
    }
    assert result["taskmanagers"][0]["free_slots"] == 2
    assert len(result["jobs"]) == 1
    job = result["jobs"][0]
    assert job["name"] == "required-job"
    assert job["required"] is True
    assert job["restart_count"] == 3
    assert job["failure_count"] == 6
    assert job["failing_vertex"] == "Kafka Source"
    assert "hunter2" not in job["root_exception"]
    assert "[REDACTED]" in job["root_exception"]
    assert len(job["exception_history"]) == 5
    assert job["exception_history_truncated"] is True
    assert job["latest_completed_checkpoint"]["id"] == 7
    assert job["latest_failed_checkpoint"]["id"] == 8
    assert "do-not-return" not in job["checkpoint_failure_reason"]
    assert job["checkpoint_age_seconds"] == 30.0
    assert result["summary"]["probable_cause"] == "PYTHON_DEPENDENCY_MISSING"
    assert result["summary"]["required_jobs_not_running"] == ["required-job"]
    assert result["python_runtime"] == {
        "client_executable": "/opt/pyflink-venv/bin/python",
        "worker_runtime_mode": "archive",
        "worker_executable": "pyenv/bin/python",
        "archive_configured": True,
        "archive_target": "pyenv",
        "automatic_repair_enabled": True,
    }
    assert job["probable_cause"] == "PYTHON_DEPENDENCY_MISSING"
    assert job["automatic_repair_eligible"] is False
    assert job["repair_attempt_result"] is None
    assert not any("healthy999" in path for path, _ in session.calls)
    assert {timeout for _, timeout in session.calls} == {4.0}


def test_partial_endpoint_failures_are_reported_per_job(settings):
    settings.FLINK_REST_URL = "http://flink-jobmanager:8081"
    settings.EXECUTION_REQUIRED_FLINK_JOBS = ("required-job",)
    session = FakeSession(
        {
            "/overview": FakeResponse(
                {
                    "taskmanagers": 1,
                    "slots-total": 1,
                    "slots-available": 1,
                    "jobs-running": 0,
                    "jobs-failed": 0,
                }
            ),
            "/taskmanagers": FakeResponse({"taskmanagers": []}),
            "/jobs/overview": FakeResponse(
                {
                    "jobs": [
                        {
                            "jid": "abc123",
                            "name": "required-job",
                            "state": "FAILED",
                        }
                    ]
                }
            ),
            "/jobs/abc123": FakeResponse(status_code=404),
            "/jobs/abc123/exceptions": FakeResponse(
                json_error=ValueError("not json")
            ),
            "/jobs/abc123/checkpoints": requests.ConnectionError("offline"),
        }
    )

    result = collect_flink_diagnostics(session=session)

    assert result["reachable"] is True
    assert len(result["jobs"]) == 1
    assert {item["code"] for item in result["jobs"][0]["endpoint_errors"]} == {
        "FLINK_JOB_NOT_FOUND",
        "MALFORMED_FLINK_RESPONSE",
        "FLINK_ENDPOINT_FAILURE",
    }
    assert result["summary"]["probable_cause"] is None


def test_known_missing_python_job_reports_repair_eligibility_and_result(
    settings,
    tmp_path,
):
    settings.FLINK_REST_URL = "http://flink-jobmanager:8081"
    settings.EXECUTION_REQUIRED_FLINK_JOBS = ("required-job",)
    settings.FLINK_AUTO_REPAIR_PYTHON_EXECUTABLE_FAILURE = True
    status_path = tmp_path / "repair-status.json"
    status_path.write_text(
        '{"jobs":{"required-job":{"result":"CANCEL_REQUESTED",'
        '"classification":"PYTHON_EXECUTABLE_MISSING",'
        '"old_job_id":"abc123","observed_at":"2026-08-01T12:00:00+00:00"}}}',
        encoding="utf-8",
    )
    settings.FLINK_BOOTSTRAP_STATUS_PATH = str(status_path)
    session = FakeSession(
        {
            "/overview": FakeResponse(
                {
                    "taskmanagers": 1,
                    "slots-total": 4,
                    "slots-available": 3,
                }
            ),
            "/taskmanagers": FakeResponse({"taskmanagers": []}),
            "/jobs/overview": FakeResponse(
                {
                    "jobs": [
                        {
                            "jid": "abc123",
                            "name": "required-job",
                            "state": "RESTARTING",
                        }
                    ]
                }
            ),
            "/jobs/abc123": FakeResponse(
                {
                    "jid": "abc123",
                    "name": "required-job",
                    "state": "RESTARTING",
                }
            ),
            "/jobs/abc123/exceptions": FakeResponse(
                {
                    "root-exception": 'Cannot run program "python": No such file or directory'
                }
            ),
            "/jobs/abc123/checkpoints": FakeResponse({"history": []}),
        }
    )

    result = collect_flink_diagnostics(session=session)
    job = result["jobs"][0]

    assert job["probable_cause"] == "PYTHON_EXECUTABLE_MISSING"
    assert job["automatic_repair_eligible"] is True
    assert job["repair_attempt_result"]["result"] == "CANCEL_REQUESTED"
    assert job["repair_attempt_result"]["old_job_id"] == "abc123"


def test_unreachable_jobmanager_returns_controlled_diagnostics(settings):
    settings.FLINK_REST_URL = "http://flink-jobmanager:8081"
    session = FakeSession(
        {
            "/overview": requests.ConnectionError("password=must-not-leak"),
            "/taskmanagers": requests.Timeout("timed out"),
            "/jobs/overview": requests.ConnectionError("offline"),
        }
    )

    result = collect_flink_diagnostics(session=session)

    assert result["reachable"] is False
    assert result["summary"]["probable_cause"] == "JOBMANAGER_UNAVAILABLE"
    assert "password" not in str(result)
    assert {item["code"] for item in result["errors"]} >= {
        "FLINK_ENDPOINT_FAILURE",
        "FLINK_ENDPOINT_TIMEOUT",
        "JOBMANAGER_UNAVAILABLE",
    }


def test_flink_diagnostics_allows_unauthenticated_requests(client, monkeypatch):
    endpoint = "/api/v1/execution/flink-diagnostics/"
    collector_calls = []
    monkeypatch.setattr(
        "apps.execution.views.collect_flink_diagnostics",
        lambda: collector_calls.append(True) or {"reachable": True},
    )

    result = client.get(endpoint)

    assert result.status_code == 200
    assert result.json()["data"] == {"reachable": True}
    assert collector_calls == [True]


def test_endpoint_hides_unexpected_collector_error(
    client,
    monkeypatch,
):
    def fail():
        raise RuntimeError("password=do-not-return")

    monkeypatch.setattr("apps.execution.views.collect_flink_diagnostics", fail)
    response = client.get("/api/v1/execution/flink-diagnostics/")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "FLINK_DIAGNOSTICS_FAILED"
    assert "do-not-return" not in str(response.json())
