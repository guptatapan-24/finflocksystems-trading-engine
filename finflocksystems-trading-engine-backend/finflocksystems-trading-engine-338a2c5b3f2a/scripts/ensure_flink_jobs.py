#!/usr/bin/env python3
"""Ensure the required PyFlink jobs are running in the external cluster."""

from __future__ import annotations

import argparse
import logging
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping, Sequence
from urllib.parse import urlsplit

import psycopg
import requests
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env", override=False)

from streaming.flink.jobs.python_worker import (  # noqa: E402
    TASKMANAGER_PATH_MODE,
    WorkerPythonConfig,
    WorkerPythonConfigurationError,
    is_missing_python_executable_failure,
    safe_path_for_log,
)

LOGGER = logging.getLogger("flink-job-bootstrap")

# The five market-data stages are submitted as one job. Five separate jobs cost
# five TaskManager slots, five user classloaders and -- because every stage runs
# PyFlink UDFs -- five Beam Python harness processes (~1.26GB measured resident).
# See streaming/flink/jobs/market_pipeline.py.
DEFAULT_REQUIRED_JOBS = ("market-pipeline-v1",)

# The per-stage names this job replaced. A cluster upgraded in place still has
# them RUNNING, and leaving them would double-consume every topic, so bootstrap
# cancels them before submitting the combined job.
SUPERSEDED_JOBS = (
    "market-normalization-v2",
    "bar-aggregation-v2",
    "indicator-computation-v2",
    "stale-price-detection-v1",
    "stream-health-v1",
)

# The individual stages stay submittable by name -- set
# EXECUTION_REQUIRED_FLINK_JOBS to one of them to run a single stage in
# isolation when debugging.
JOB_FILES = {
    "market-pipeline-v1": "market_pipeline.py",
    "market-normalization-v2": "market_normalization.py",
    "bar-aggregation-v2": "bar_aggregation.py",
    "indicator-computation-v2": "indicator_computation.py",
    "stale-price-detection-v1": "stale_price_detection.py",
    "stream-health-v1": "stream_health.py",
}

# These are the Flink states for which another copy can be started deliberately.
# Every unrecognized state is handled conservatively as non-terminal.
TERMINAL_STATES = frozenset({"FAILED", "CANCELED", "FINISHED", "SUSPENDED"})
RUNNING_STATE = "RUNNING"
ADVISORY_LOCK_KEY = 0x464C494E4B4A4F42  # ASCII "FLINKJOB"
JOB_ID_PATTERN = re.compile(r"\bJobID\s+([0-9a-fA-F]{32})\b")
# What Flink's REST API reports as ``external_path`` for a checkpoint that was
# never written to durable storage (no state.checkpoints.dir). It is not a path,
# so passing it to ``flink run -s`` can only fail.
NON_ADDRESSABLE_CHECKPOINT_POINTER = "<checkpoint-not-externally-addressable>"
KAFKA_CONNECTOR_JAR = Path(
    "/opt/flink/lib/flink-sql-connector-kafka-3.3.0-1.20.jar"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|pwd|token|secret|credential|authorization|"
    r"api[_-]?key)(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
)


def _safe_exception_summary(value: str | None, limit: int = 1_000) -> str:
    if not value:
        return "no root exception was returned"
    redacted = _SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", str(value))
    redacted = re.sub(
        r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer [REDACTED]",
        redacted,
    )
    if len(redacted) > limit:
        return redacted[: limit - 3] + "..."
    return redacted


def is_transient_resource_allocation_failure(value: str | None) -> bool:
    """Return whether Flink is restarting while canceled slots are released."""
    normalized = str(value or "").lower()
    return any(
        marker in normalized
        for marker in (
            "noresourceavailableexception",
            "could not acquire the minimum required resources",
            "not enough resources available",
        )
    )


class RepairStatusStore:
    """Persist only bounded, non-secret repair outcomes for diagnostics."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.jobs: dict[str, dict[str, object]] = {}

    def reset(self) -> None:
        self.jobs = {}
        self._write()

    def record(
        self,
        job_name: str,
        result: str,
        *,
        old_job_id: str | None = None,
        replacement_job_id: str | None = None,
    ) -> None:
        row: dict[str, object] = {
            "result": result,
            "classification": "PYTHON_EXECUTABLE_MISSING",
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
        if old_job_id:
            row["old_job_id"] = old_job_id
        if replacement_job_id:
            row["replacement_job_id"] = replacement_job_id
        self.jobs[job_name] = row
        self._write()

    def _write(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(
                json.dumps({"jobs": self.jobs}, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        except OSError as exc:
            LOGGER.warning(
                "Could not persist the bounded Flink repair status (%s)",
                type(exc).__name__,
            )


class BootstrapError(RuntimeError):
    """A safe-to-log Flink bootstrap failure."""


class ConfigurationError(BootstrapError):
    pass


class FlinkUnavailable(BootstrapError):
    pass


class JobTerminalError(BootstrapError):
    pass


class JobRestartingError(BootstrapError):
    def __init__(self, job: "Job", root_exception: str | None) -> None:
        self.job = job
        self.root_exception = root_exception
        super().__init__(
            f"Flink job '{job.name}' ({job.job_id}) returned to RESTARTING: "
            f"{_safe_exception_summary(root_exception)}"
        )


@dataclass(frozen=True)
class Job:
    job_id: str
    name: str
    state: str


@dataclass(frozen=True)
class CompletedCheckpoint:
    checkpoint_id: int
    external_path: str
    completed_at_millis: int


@dataclass(frozen=True)
class SubmissionDecision:
    action: str
    job: Job | None
    matches: tuple[Job, ...]


@dataclass(frozen=True)
class BootstrapConfig:
    enabled: bool
    rest_url: str
    cli_jobmanager: str
    required_jobs: tuple[str, ...]
    files_root: Path
    flink_binary: str
    python_client_executable: str
    kafka_connector_jar: Path
    database_url: str
    submission_timeout_seconds: float
    start_timeout_seconds: float
    poll_interval_seconds: float
    max_attempts: int
    rest_timeout_seconds: float
    worker_python: WorkerPythonConfig = field(
        default_factory=lambda: WorkerPythonConfig(
            runtime_mode=TASKMANAGER_PATH_MODE,
            executable="/usr/bin/python",
        )
    )
    auto_repair_python_executable_failure: bool = True
    cancel_timeout_seconds: float = 120
    checkpoint_timeout_seconds: float = 120
    repair_status_path: Path = Path("/tmp/flink-job-bootstrap-status.json")
    restore_terminal_checkpoints: bool = True

    @classmethod
    def from_environment(cls) -> "BootstrapConfig":
        rest_url, cli_jobmanager = _parse_rest_url(
            os.getenv("FLINK_REST_URL", "http://localhost:8081")
        )
        required_jobs = _required_jobs(
            os.getenv("EXECUTION_REQUIRED_FLINK_JOBS", "")
        )
        submission_timeout = _positive_float(
            "FLINK_JOB_SUBMISSION_TIMEOUT_SECONDS", 60
        )
        start_timeout = _positive_float("FLINK_JOB_START_TIMEOUT_SECONDS", 120)
        poll_interval = _positive_float("FLINK_JOB_POLL_INTERVAL_SECONDS", 2)
        max_attempts = _positive_integer("FLINK_JOB_BOOTSTRAP_MAX_ATTEMPTS", 30)
        cancel_timeout = _positive_float("FLINK_JOB_CANCEL_TIMEOUT_SECONDS", 120)
        checkpoint_timeout = _nonnegative_float(
            "FLINK_JOB_CHECKPOINT_TIMEOUT_SECONDS", 120
        )
        configured_binary = os.getenv("FLINK_BIN", "").strip()
        flink_binary = configured_binary or shutil.which("flink") or "/opt/flink/bin/flink"
        return cls(
            enabled=_environment_bool("FLINK_AUTO_SUBMIT_JOBS", True),
            rest_url=rest_url,
            cli_jobmanager=cli_jobmanager,
            required_jobs=required_jobs,
            files_root=Path(
                os.getenv("FLINK_JOB_FILES_ROOT", "/app/streaming/flink/jobs")
            ).expanduser(),
            flink_binary=flink_binary,
            python_client_executable=os.getenv(
                "PYFLINK_CLIENT_EXECUTABLE",
                "/opt/pyflink-venv/bin/python",
            ).strip(),
            kafka_connector_jar=KAFKA_CONNECTOR_JAR,
            database_url=os.getenv("DATABASE_URL", "").strip(),
            submission_timeout_seconds=submission_timeout,
            start_timeout_seconds=start_timeout,
            poll_interval_seconds=poll_interval,
            max_attempts=max_attempts,
            rest_timeout_seconds=min(submission_timeout, 10.0),
            worker_python=WorkerPythonConfig.from_environment(),
            auto_repair_python_executable_failure=_environment_bool(
                "FLINK_AUTO_REPAIR_PYTHON_EXECUTABLE_FAILURE", True
            ),
            cancel_timeout_seconds=cancel_timeout,
            checkpoint_timeout_seconds=checkpoint_timeout,
            repair_status_path=Path(
                os.getenv(
                    "FLINK_BOOTSTRAP_STATUS_PATH",
                    "/tmp/flink-job-bootstrap-status.json",
                )
            ),
            restore_terminal_checkpoints=not _environment_bool(
                "CLEANUP_DATABASE_ON_START", False
            ),
        )

    def validate(self) -> None:
        if not self.database_url:
            raise ConfigurationError(
                "DATABASE_URL is required for the distributed Flink bootstrap lock"
            )
        if not self.repair_status_path.is_absolute():
            raise ConfigurationError("FLINK_BOOTSTRAP_STATUS_PATH must be absolute")
        if len(set(self.required_jobs)) != len(self.required_jobs):
            raise ConfigurationError(
                "EXECUTION_REQUIRED_FLINK_JOBS contains duplicate names"
            )
        unknown = [name for name in self.required_jobs if name not in JOB_FILES]
        if unknown:
            raise ConfigurationError(
                "No Flink source mapping exists for configured job(s): "
                + ", ".join(unknown)
            )
        missing = [
            str(self.files_root / JOB_FILES[name])
            for name in self.required_jobs
            if not (self.files_root / JOB_FILES[name]).is_file()
        ]
        if missing:
            raise ConfigurationError(
                "Flink source file(s) are missing: " + ", ".join(missing)
            )
        if not Path(self.flink_binary).is_file() and shutil.which(self.flink_binary) is None:
            raise ConfigurationError(
                f"Flink CLI executable was not found at '{self.flink_binary}'"
            )
        if not Path(self.python_client_executable).is_file():
            raise ConfigurationError(
                "PyFlink client Python executable was not found at "
                f"'{self.python_client_executable}'"
            )
        if not self.kafka_connector_jar.is_file():
            raise ConfigurationError(
                "Flink Kafka connector JAR was not found at "
                f"'{self.kafka_connector_jar}'"
            )
        try:
            self.worker_python.validate()
        except WorkerPythonConfigurationError as exc:
            raise ConfigurationError(str(exc)) from exc


def _environment_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return value


def _nonnegative_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if value < 0:
        raise ConfigurationError(f"{name} must be zero or greater")
    return value


def _positive_integer(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value < 1:
        raise ConfigurationError(f"{name} must be greater than zero")
    return value


def _required_jobs(configured: str) -> tuple[str, ...]:
    if not configured.strip():
        return DEFAULT_REQUIRED_JOBS
    jobs = tuple(name.strip() for name in configured.split(",") if name.strip())
    if not jobs:
        raise ConfigurationError("EXECUTION_REQUIRED_FLINK_JOBS is empty")
    return jobs


def _parse_rest_url(value: str) -> tuple[str, str]:
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigurationError(
            "FLINK_REST_URL must be an http:// or https:// endpoint"
        )
    if parsed.username or parsed.password:
        raise ConfigurationError("FLINK_REST_URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ConfigurationError("FLINK_REST_URL must not contain a query or fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ConfigurationError("FLINK_REST_URL contains an invalid port") from exc
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    return normalized, f"{host}:{port}"


class FlinkRestClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        *,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()

    def _get_json(self, path: str, *, allow_not_found: bool = False):
        try:
            response = self.session.get(
                self.base_url + path,
                timeout=self.timeout_seconds,
            )
            if allow_not_found and response.status_code == 404:
                return None
            response.raise_for_status()
            payload = response.json()
        except requests.Timeout as exc:
            raise FlinkUnavailable("Flink REST request timed out") from exc
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            suffix = f" (HTTP {status})" if status is not None else ""
            raise FlinkUnavailable(
                f"Flink REST request failed with {type(exc).__name__}{suffix}"
            ) from exc
        except ValueError as exc:
            raise FlinkUnavailable("Flink REST returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise FlinkUnavailable("Flink REST returned an unexpected payload")
        return payload

    def list_jobs(self) -> list[Job]:
        payload = self._get_json("/jobs/overview")
        jobs = []
        for item in payload.get("jobs") or []:
            if not isinstance(item, Mapping):
                continue
            job_id = str(item.get("jid") or item.get("id") or "").strip()
            name = str(item.get("name") or "").strip()
            state = str(item.get("state") or "UNKNOWN").strip().upper()
            if job_id and name:
                jobs.append(Job(job_id=job_id, name=name, state=state))
        return jobs

    def get_job(self, job_id: str) -> Job | None:
        payload = self._get_json(f"/jobs/{job_id}", allow_not_found=True)
        if payload is None:
            return None
        resolved_id = str(payload.get("jid") or payload.get("id") or job_id).strip()
        name = str(payload.get("name") or "").strip()
        state = str(payload.get("state") or "UNKNOWN").strip().upper()
        if not name:
            raise FlinkUnavailable(
                f"Flink REST returned no name for submitted job ID {job_id}"
            )
        return Job(job_id=resolved_id, name=name, state=state)

    def root_exception(self, job_id: str) -> str | None:
        payload = self._get_json(
            f"/jobs/{job_id}/exceptions",
            allow_not_found=True,
        )
        if payload is None:
            return None
        root = payload.get("root-exception") or payload.get("root_exception")
        if root:
            return str(root)
        history = payload.get("all-exceptions") or payload.get("all_exceptions") or []
        for item in history:
            if not isinstance(item, Mapping):
                continue
            value = item.get("exception") or item.get("stacktrace")
            if value:
                return str(value)
        return None

    def cancel_job(self, job_id: str) -> None:
        try:
            response = self.session.patch(
                self.base_url + f"/jobs/{job_id}",
                params={"mode": "cancel"},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except requests.Timeout as exc:
            raise FlinkUnavailable("Flink job cancellation timed out") from exc
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            suffix = f" (HTTP {status})" if status is not None else ""
            raise FlinkUnavailable(
                f"Flink job cancellation failed with {type(exc).__name__}{suffix}"
            ) from exc

    def latest_completed_checkpoint(self, job_id: str) -> CompletedCheckpoint | None:
        payload = self._get_json(
            f"/jobs/{job_id}/checkpoints",
            allow_not_found=True,
        )
        if payload is None:
            return None
        latest = (payload.get("latest") or {}).get("completed")
        candidates = [latest] if isinstance(latest, Mapping) else []
        candidates.extend(
            item
            for item in (payload.get("history") or [])
            if isinstance(item, Mapping)
            and str(item.get("status") or "").upper() == "COMPLETED"
        )
        checkpoints = []
        for item in candidates:
            external_path = str(item.get("external_path") or "").strip()
            if external_path == NON_ADDRESSABLE_CHECKPOINT_POINTER:
                # Completed, but not restorable: report it as having no path so
                # callers resubmit without ``-s`` instead of retrying a restore
                # that always fails.
                external_path = ""
            try:
                checkpoint_id = int(item.get("id"))
                completed_at = int(
                    item.get("latest_ack_timestamp")
                    or item.get("trigger_timestamp")
                    or 0
                )
            except (TypeError, ValueError):
                continue
            checkpoints.append(
                CompletedCheckpoint(
                    checkpoint_id=checkpoint_id,
                    external_path=external_path,
                    completed_at_millis=completed_at,
                )
            )
        return max(
            checkpoints,
            key=lambda item: (item.completed_at_millis, item.checkpoint_id),
            default=None,
        )


def submission_decision(jobs: Iterable[Job], required_name: str) -> SubmissionDecision:
    matches = tuple(job for job in jobs if job.name == required_name)
    running = next((job for job in matches if job.state == RUNNING_STATE), None)
    if running:
        return SubmissionDecision("satisfied", running, matches)
    active = next((job for job in matches if job.state not in TERMINAL_STATES), None)
    if active:
        return SubmissionDecision("wait", active, matches)
    return SubmissionDecision("submit", None, matches)


def _format_jobs(jobs: Sequence[Job]) -> str:
    if not jobs:
        return "none"
    return ", ".join(
        f"{job.name}={job.state} ({job.job_id})"
        for job in sorted(jobs, key=lambda item: (item.name, item.job_id))
    )


def wait_for_flink(
    client: FlinkRestClient,
    config: BootstrapConfig,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Job]:
    for attempt in range(1, config.max_attempts + 1):
        LOGGER.info(
            "Checking Flink REST endpoint %s (attempt %d/%d)",
            config.rest_url,
            attempt,
            config.max_attempts,
        )
        try:
            jobs = client.list_jobs()
        except FlinkUnavailable as exc:
            if attempt == config.max_attempts:
                raise FlinkUnavailable(
                    f"Flink remained unavailable after {config.max_attempts} attempts: {exc}"
                ) from exc
            LOGGER.warning(
                "Flink availability check failed with %s; retrying in %g seconds",
                type(exc).__name__,
                config.poll_interval_seconds,
            )
            sleep(config.poll_interval_seconds)
            continue
        LOGGER.info("Existing Flink jobs: %s", _format_jobs(jobs))
        return jobs
    raise AssertionError("unreachable")


def submit_python_job(
    config: BootstrapConfig,
    required_name: str,
    restore_path: str | None = None,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    source_file = config.files_root / JOB_FILES[required_name]
    command = [
        config.flink_binary,
        "run",
        "-d",
        "-m",
        config.cli_jobmanager,
        "-py",
        str(source_file),
        "-pyfs",
        str(config.files_root.parent),
        "-pyclientexec",
        config.python_client_executable,
        "--jarfile",
        str(config.kafka_connector_jar),
    ]
    if restore_path:
        command.extend(["-s", restore_path])
    try:
        result = run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=config.submission_timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise BootstrapError(
            f"Flink CLI submission timed out for '{required_name}'"
        ) from exc
    except OSError as exc:
        raise BootstrapError(
            f"Flink CLI could not start for '{required_name}' ({type(exc).__name__})"
        ) from exc
    if result.returncode != 0:
        raise BootstrapError(
            f"Flink CLI exited with status {result.returncode} for '{required_name}'"
        )
    match = JOB_ID_PATTERN.search((result.stdout or "") + "\n" + (result.stderr or ""))
    if match is None:
        raise BootstrapError(
            f"Flink CLI did not return a job ID for '{required_name}'"
        )
    return match.group(1).lower()


def wait_for_job(
    client: FlinkRestClient,
    expected: Job,
    config: BootstrapConfig,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Job:
    deadline = monotonic() + config.start_timeout_seconds
    last_state = "NOT_VISIBLE"
    resource_wait_logged = False
    while True:
        try:
            observed = client.get_job(expected.job_id)
        except FlinkUnavailable as exc:
            observed = None
            if last_state != "REST_UNAVAILABLE":
                LOGGER.warning(
                    "Flink REST was temporarily unavailable while waiting for '%s' "
                    "(%s)",
                    expected.name,
                    type(exc).__name__,
                )
                last_state = "REST_UNAVAILABLE"
        if observed is not None:
            if observed.name != expected.name:
                raise BootstrapError(
                    f"Flink job ID {expected.job_id} resolved to unexpected name "
                    f"'{observed.name}' instead of '{expected.name}'"
                )
            if observed.state != last_state:
                LOGGER.info(
                    "Flink job '%s' (%s) state: %s",
                    observed.name,
                    observed.job_id,
                    observed.state,
                )
                last_state = observed.state
            if observed.state == RUNNING_STATE:
                return observed
            if observed.state == "RESTARTING":
                root_exception = client.root_exception(observed.job_id)
                if is_transient_resource_allocation_failure(root_exception):
                    if not resource_wait_logged:
                        LOGGER.warning(
                            "Flink job '%s' (%s) is waiting for TaskManager "
                            "resources released by canceled jobs; allowing it to "
                            "recover within the %g second start timeout",
                            observed.name,
                            observed.job_id,
                            config.start_timeout_seconds,
                        )
                        resource_wait_logged = True
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise BootstrapError(
                            f"Timed out waiting for Flink job '{observed.name}' "
                            f"({observed.job_id}) to recover from temporary resource "
                            "allocation pressure"
                        )
                    sleep(min(config.poll_interval_seconds, remaining))
                    continue
                LOGGER.error(
                    "Flink job '%s' (%s) returned to RESTARTING; root exception: %s",
                    observed.name,
                    observed.job_id,
                    _safe_exception_summary(root_exception),
                )
                raise JobRestartingError(observed, root_exception)
            if observed.state in TERMINAL_STATES:
                raise JobTerminalError(
                    f"Flink job '{observed.name}' ({observed.job_id}) reached "
                    f"terminal state {observed.state}"
                )
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise BootstrapError(
                f"Timed out waiting for Flink job '{expected.name}' "
                f"({expected.job_id}) to reach RUNNING; final state was {last_state}"
            )
        sleep(min(config.poll_interval_seconds, remaining))


def wait_for_terminal_state(
    client: FlinkRestClient,
    job: Job,
    config: BootstrapConfig,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Job:
    deadline = monotonic() + config.cancel_timeout_seconds
    last_state = job.state
    while True:
        try:
            observed = client.get_job(job.job_id)
        except FlinkUnavailable:
            observed = None
            last_state = "REST_UNAVAILABLE"
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise BootstrapError(
                    f"Timed out confirming cancellation of Flink job '{job.name}' "
                    f"({job.job_id}); final observation was {last_state}"
                )
            sleep(min(config.poll_interval_seconds, remaining))
            continue
        if observed is None:
            return Job(job.job_id, job.name, "CANCELED")
        if observed.name != job.name:
            raise BootstrapError(
                f"Flink job ID {job.job_id} changed name during cancellation"
            )
        last_state = observed.state
        if observed.state in TERMINAL_STATES:
            return observed
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise BootstrapError(
                f"Timed out waiting for Flink job '{job.name}' ({job.job_id}) to "
                f"reach a terminal state; final state was {last_state}"
            )
        sleep(min(config.poll_interval_seconds, remaining))


def wait_for_completed_checkpoint(
    client: FlinkRestClient,
    job: Job,
    config: BootstrapConfig,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> CompletedCheckpoint | None:
    if config.checkpoint_timeout_seconds == 0:
        LOGGER.warning(
            "Completed-checkpoint wait is disabled for newly submitted job '%s'",
            job.name,
        )
        return None
    deadline = monotonic() + config.checkpoint_timeout_seconds
    while True:
        try:
            observed = client.get_job(job.job_id)
        except FlinkUnavailable:
            observed = None
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise BootstrapError(
                    f"Timed out reaching Flink while waiting for newly submitted "
                    f"job '{job.name}' ({job.job_id}) to complete a checkpoint"
                )
            sleep(min(config.poll_interval_seconds, remaining))
            continue
        if observed is None or observed.state != RUNNING_STATE:
            final_state = "NOT_FOUND" if observed is None else observed.state
            root = client.root_exception(job.job_id) if observed is not None else None
            raise BootstrapError(
                f"Flink job '{job.name}' ({job.job_id}) stopped running while waiting "
                f"for a completed checkpoint; state={final_state}; root exception: "
                f"{_safe_exception_summary(root)}"
            )
        try:
            checkpoint = client.latest_completed_checkpoint(job.job_id)
        except FlinkUnavailable:
            checkpoint = None
        if checkpoint is not None:
            LOGGER.info(
                "Flink job '%s' (%s) completed checkpoint %s",
                job.name,
                job.job_id,
                checkpoint.checkpoint_id,
            )
            return checkpoint
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise BootstrapError(
                f"Timed out waiting for newly submitted Flink job '{job.name}' "
                f"({job.job_id}) to complete a checkpoint"
            )
        sleep(min(config.poll_interval_seconds, remaining))


def repair_restarting_job(
    client: FlinkRestClient,
    config: BootstrapConfig,
    job: Job,
    root_exception: str | None = None,
    *,
    reporter: Callable[..., None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> CompletedCheckpoint | None:
    root_exception = root_exception or client.root_exception(job.job_id)
    safe_root = _safe_exception_summary(root_exception)
    if not is_missing_python_executable_failure(root_exception):
        raise BootstrapError(
            f"Required Flink job '{job.name}' ({job.job_id}) is RESTARTING for an "
            f"unrecognized cause; it will not be canceled automatically. Root "
            f"exception: {safe_root}"
        )
    if not config.auto_repair_python_executable_failure:
        raise BootstrapError(
            f"Required Flink job '{job.name}' ({job.job_id}) has the known missing "
            "Python executable failure, but automatic repair is disabled"
        )

    checkpoint = client.latest_completed_checkpoint(job.job_id)
    if reporter:
        reporter(job.name, "CANCEL_REQUESTED", old_job_id=job.job_id)
    LOGGER.warning(
        "Canceling required Flink job '%s' (%s) after confirming "
        "PYTHON_EXECUTABLE_MISSING",
        job.name,
        job.job_id,
    )
    client.cancel_job(job.job_id)
    terminal = wait_for_terminal_state(client, job, config, sleep=sleep)
    LOGGER.info(
        "Affected Flink job '%s' (%s) reached terminal state %s",
        job.name,
        job.job_id,
        terminal.state,
    )
    if reporter:
        reporter(job.name, "CANCELED", old_job_id=job.job_id)
    return checkpoint


def ensure_job(
    client: FlinkRestClient,
    config: BootstrapConfig,
    required_name: str,
    *,
    submit: Callable[[BootstrapConfig, str, str | None], str] = submit_python_job,
    reporter: Callable[..., None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Job:
    repair_job: Job | None = None
    repair_checkpoint: CompletedCheckpoint | None = None

    def finish_repair(running: Job) -> Job:
        if repair_job is None:
            return running
        try:
            wait_for_completed_checkpoint(client, running, config, sleep=sleep)
        except BootstrapError:
            if reporter:
                reporter(
                    required_name,
                    "REPLACEMENT_UNHEALTHY",
                    old_job_id=repair_job.job_id,
                    replacement_job_id=running.job_id,
                )
            raise
        if reporter:
            reporter(
                required_name,
                "REPAIRED",
                old_job_id=repair_job.job_id,
                replacement_job_id=running.job_id,
            )
        return running

    for attempt in range(1, config.max_attempts + 1):
        # This REST re-query occurs immediately before every possible submission.
        try:
            jobs = client.list_jobs()
        except FlinkUnavailable as exc:
            if attempt == config.max_attempts:
                raise
            LOGGER.warning(
                "Could not re-query Flink before handling '%s' (%s); retrying in "
                "%g seconds",
                required_name,
                type(exc).__name__,
                config.poll_interval_seconds,
            )
            sleep(config.poll_interval_seconds)
            continue
        decision = submission_decision(jobs, required_name)
        if decision.action == "satisfied":
            LOGGER.info(
                "Required Flink job '%s' is already RUNNING as %s",
                required_name,
                decision.job.job_id,
            )
            return finish_repair(decision.job)
        if decision.action == "wait":
            LOGGER.info(
                "Required Flink job '%s' already exists in non-terminal state %s; "
                "waiting instead of submitting a duplicate",
                required_name,
                decision.job.state,
            )
            try:
                if decision.job.state == "RESTARTING":
                    root_exception = client.root_exception(decision.job.job_id)
                    if not is_transient_resource_allocation_failure(root_exception):
                        raise JobRestartingError(decision.job, root_exception)
                running = wait_for_job(client, decision.job, config, sleep=sleep)
                return finish_repair(running)
            except JobRestartingError as exc:
                repair_job = exc.job
                repair_checkpoint = repair_restarting_job(
                    client,
                    config,
                    exc.job,
                    exc.root_exception,
                    reporter=reporter,
                    sleep=sleep,
                )
                # Query the complete inventory again immediately before deciding
                # whether a replacement is safe to submit.
                jobs = client.list_jobs()
                decision = submission_decision(jobs, required_name)
                if decision.action == "satisfied":
                    LOGGER.info(
                        "A healthy concurrent replacement for '%s' is already RUNNING",
                        required_name,
                    )
                    return finish_repair(decision.job)
                if decision.action == "wait":
                    if attempt == config.max_attempts:
                        raise BootstrapError(
                            f"Required Flink job '{required_name}' still has a "
                            "non-terminal instance after controlled cancellation"
                        )
                    sleep(config.poll_interval_seconds)
                    continue
            except JobTerminalError:
                if attempt == config.max_attempts:
                    raise
                LOGGER.warning(
                    "Existing Flink job '%s' entered a terminal state; re-querying "
                    "before a deliberate resubmission decision",
                    required_name,
                )
                continue

        terminal_matches = [
            job for job in decision.matches if job.state in TERMINAL_STATES
        ]
        restore_checkpoint = (
            repair_checkpoint
            if config.restore_terminal_checkpoints
            and repair_checkpoint is not None
            and repair_checkpoint.external_path
            else None
        )
        if terminal_matches:
            LOGGER.info(
                "Required Flink job '%s' has only terminal instances (%s); "
                "deliberately resubmitting",
                required_name,
                _format_jobs(terminal_matches),
            )
            retained = []
            if config.restore_terminal_checkpoints:
                for terminal_job in terminal_matches:
                    checkpoint = client.latest_completed_checkpoint(
                        terminal_job.job_id
                    )
                    if checkpoint is not None and checkpoint.external_path:
                        retained.append((checkpoint, terminal_job))
                if restore_checkpoint is not None and repair_job is not None:
                    retained.append((restore_checkpoint, repair_job))
            if retained:
                restore_checkpoint, checkpoint_job = max(
                    retained,
                    key=lambda item: (
                        item[0].completed_at_millis,
                        item[0].checkpoint_id,
                    ),
                )
                LOGGER.info(
                    "Restoring '%s' from retained checkpoint %s of terminal job %s",
                    required_name,
                    restore_checkpoint.checkpoint_id,
                    checkpoint_job.job_id,
                )
            elif config.restore_terminal_checkpoints:
                LOGGER.info(
                    "No retained completed checkpoint is exposed for terminal job "
                    "'%s'; resubmitting with its preserved Kafka offset policy",
                    required_name,
                )
            else:
                LOGGER.info(
                    "Streaming cleanup is enabled; resubmitting '%s' without "
                    "restoring terminal checkpoints",
                    required_name,
                )
        else:
            LOGGER.info("Required Flink job '%s' is missing", required_name)
        LOGGER.info(
            "Submitting Flink job '%s' with the Flink CLI (attempt %d/%d)",
            required_name,
            attempt,
            config.max_attempts,
        )
        try:
            job_id = submit(
                config,
                required_name,
                restore_checkpoint.external_path if restore_checkpoint else None,
            )
        except BootstrapError as exc:
            if reporter and repair_job is not None:
                reporter(
                    required_name,
                    "RESUBMISSION_FAILED",
                    old_job_id=repair_job.job_id,
                )
            if attempt == config.max_attempts:
                raise
            LOGGER.warning(
                "Submission attempt for '%s' failed with %s; re-querying Flink "
                "before retrying in %g seconds",
                required_name,
                type(exc).__name__,
                config.poll_interval_seconds,
            )
            sleep(config.poll_interval_seconds)
            continue
        LOGGER.info("Submitted Flink job '%s' with job ID %s", required_name, job_id)
        submitted = Job(job_id=job_id, name=required_name, state="SUBMITTED")
        try:
            running = wait_for_job(client, submitted, config, sleep=sleep)
            if repair_job is not None:
                return finish_repair(running)
            wait_for_completed_checkpoint(client, running, config, sleep=sleep)
            return running
        except JobRestartingError:
            if reporter and repair_job is not None:
                reporter(
                    required_name,
                    "REPLACEMENT_RESTARTING",
                    old_job_id=repair_job.job_id,
                    replacement_job_id=job_id,
                )
            raise
        except JobTerminalError:
            if attempt == config.max_attempts:
                raise
            LOGGER.warning(
                "Submitted Flink job '%s' entered a terminal state; re-querying "
                "before retrying in %g seconds",
                required_name,
                config.poll_interval_seconds,
            )
            sleep(config.poll_interval_seconds)
    raise AssertionError("unreachable")


@contextmanager
def postgresql_bootstrap_lock(
    config: BootstrapConfig,
    *,
    connect: Callable[..., object] = psycopg.connect,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[None]:
    try:
        connection = connect(
            config.database_url,
            autocommit=True,
            connect_timeout=max(1, min(10, int(config.rest_timeout_seconds))),
        )
    except Exception as exc:
        raise BootstrapError(
            "Could not connect to PostgreSQL for the Flink bootstrap lock "
            f"({type(exc).__name__})"
        ) from exc

    acquired = False
    try:
        for attempt in range(1, config.max_attempts + 1):
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_try_advisory_lock(%s)",
                    (ADVISORY_LOCK_KEY,),
                )
                acquired = bool(cursor.fetchone()[0])
            if acquired:
                LOGGER.info("Acquired distributed Flink bootstrap lock")
                break
            if attempt < config.max_attempts:
                LOGGER.info(
                    "Another Backend replica holds the Flink bootstrap lock; "
                    "retrying in %g seconds (%d/%d)",
                    config.poll_interval_seconds,
                    attempt,
                    config.max_attempts,
                )
                sleep(config.poll_interval_seconds)
        if not acquired:
            raise BootstrapError(
                "Timed out waiting for the distributed Flink bootstrap lock"
            )
        yield
    finally:
        if acquired:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_unlock(%s)",
                        (ADVISORY_LOCK_KEY,),
                    )
            except Exception:
                LOGGER.warning("Could not explicitly release the Flink bootstrap lock")
        connection.close()


def ensure_required_flink_jobs(
    config: BootstrapConfig | None = None,
    *,
    client: FlinkRestClient | None = None,
    submit: Callable[[BootstrapConfig, str, str | None], str] = submit_python_job,
    lock: Callable[[BootstrapConfig], object] = postgresql_bootstrap_lock,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Job]:
    config = config or BootstrapConfig.from_environment()
    if not config.enabled:
        LOGGER.info("Flink automatic job submission is disabled")
        return []

    config.validate()
    LOGGER.info("Required Flink jobs: %s", ", ".join(config.required_jobs))
    LOGGER.info(
        "Python runtimes: client_executable=%s worker_mode=%s "
        "worker_executable=%s archive_configured=%s archive_target=%s",
        safe_path_for_log(config.python_client_executable),
        config.worker_python.runtime_mode,
        safe_path_for_log(config.worker_python.executable),
        config.worker_python.archive_configured,
        safe_path_for_log(config.worker_python.archive_target)
        if config.worker_python.archive_configured
        else None,
    )
    client = client or FlinkRestClient(config.rest_url, config.rest_timeout_seconds)
    status_store = RepairStatusStore(config.repair_status_path)
    status_store.reset()

    # Establish availability before taking the distributed mutation lock.
    wait_for_flink(client, config, sleep=sleep)

    with lock(config):
        initial_jobs = client.list_jobs()
        if cancel_superseded_jobs(client, config, initial_jobs, sleep=sleep):
            initial_jobs = client.list_jobs()
        missing_jobs = [
            name
            for name in config.required_jobs
            if not any(job.name == name for job in initial_jobs)
        ]
        LOGGER.info(
            "Missing required Flink jobs: %s",
            ", ".join(missing_jobs) if missing_jobs else "none",
        )
        bootstrap_failures = []
        for name in config.required_jobs:
            try:
                ensure_job(
                    client,
                    config,
                    name,
                    submit=submit,
                    reporter=status_store.record,
                    sleep=sleep,
                )
            except BootstrapError as exc:
                bootstrap_failures.append(
                    f"{name}={type(exc).__name__}: {_safe_exception_summary(str(exc))}"
                )
                LOGGER.error(
                    "Required Flink job '%s' could not be made healthy; continuing "
                    "with the remaining required jobs: %s",
                    name,
                    _safe_exception_summary(str(exc)),
                )

        final_jobs = client.list_jobs()
        LOGGER.info("Final Flink jobs: %s", _format_jobs(final_jobs))
        unresolved = []
        running = []
        for name in config.required_jobs:
            decision = submission_decision(final_jobs, name)
            if decision.action == "satisfied":
                running.append(decision.job)
            else:
                states = ",".join(job.state for job in decision.matches) or "MISSING"
                unresolved.append(f"{name}={states}")
        if unresolved:
            failure_details = (
                "; bootstrap failures: " + "; ".join(bootstrap_failures)
                if bootstrap_failures
                else ""
            )
            raise BootstrapError(
                "Required Flink jobs are not RUNNING after bootstrap: "
                + ", ".join(unresolved)
                + failure_details
            )
        return running


def cancel_superseded_jobs(
    client: FlinkRestClient,
    config: BootstrapConfig,
    jobs: Sequence[Job],
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Job]:
    """Cancel per-stage jobs that ``market-pipeline-v1`` replaced.

    An in-place upgrade leaves the old per-stage jobs RUNNING. They share the
    combined job's consumer groups and sink topics, so leaving them would both
    double-write every downstream topic and split each group's partitions. Only
    jobs that are genuinely superseded are touched: if the operator has pointed
    EXECUTION_REQUIRED_FLINK_JOBS back at the individual stages, a required
    name is never cancelled.
    """
    retired = [
        job
        for job in jobs
        if job.name in SUPERSEDED_JOBS
        and job.name not in config.required_jobs
        and job.state not in TERMINAL_STATES
    ]
    if not retired:
        return []
    canceled = []
    for job in retired:
        LOGGER.warning(
            "Canceling superseded Flink job '%s' (%s) in state %s; its stage now "
            "runs inside 'market-pipeline-v1'",
            job.name,
            job.job_id,
            job.state,
        )
        client.cancel_job(job.job_id)
    for job in retired:
        terminal = wait_for_terminal_state(client, job, config, sleep=sleep)
        LOGGER.info(
            "Superseded Flink job '%s' (%s) reached %s",
            terminal.name,
            terminal.job_id,
            terminal.state,
        )
        canceled.append(terminal)
    return canceled


def cancel_required_flink_jobs(
    config: BootstrapConfig | None = None,
    *,
    client: FlinkRestClient | None = None,
    lock: Callable[[BootstrapConfig], object] = postgresql_bootstrap_lock,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Job]:
    """Cancel required jobs so a destructive startup can begin with empty state."""
    config = config or BootstrapConfig.from_environment()
    if not config.enabled:
        LOGGER.info("Flink automatic job submission is disabled; skipping cleanup")
        return []
    config.validate()
    client = client or FlinkRestClient(config.rest_url, config.rest_timeout_seconds)

    wait_for_flink(client, config, sleep=sleep)
    canceled = []
    with lock(config):
        jobs = client.list_jobs()
        # Superseded per-stage jobs are cancelled here as well: a destructive
        # reset flushes the topics, and any survivor would immediately start
        # repopulating them from its own committed offsets.
        cancellable = set(config.required_jobs) | set(SUPERSEDED_JOBS)
        active = [
            job
            for job in jobs
            if job.name in cancellable and job.state not in TERMINAL_STATES
        ]
        if not active:
            LOGGER.info("No active required Flink jobs need cancellation")
            return []
        for job in active:
            LOGGER.warning(
                "Streaming cleanup: canceling Flink job '%s' (%s) in state %s",
                job.name,
                job.job_id,
                job.state,
            )
            client.cancel_job(job.job_id)
        for job in active:
            terminal = wait_for_terminal_state(client, job, config, sleep=sleep)
            LOGGER.info(
                "Streaming cleanup: Flink job '%s' (%s) reached %s",
                terminal.name,
                terminal.job_id,
                terminal.state,
            )
            canceled.append(terminal)

        remaining = [
            job
            for job in client.list_jobs()
            if job.name in cancellable and job.state not in TERMINAL_STATES
        ]
        if remaining:
            raise BootstrapError(
                "Required Flink jobs remained active after cleanup: "
                + _format_jobs(remaining)
            )
    return canceled


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bootstrap or cancel required PyFlink jobs."
    )
    parser.add_argument(
        "--cancel-required",
        action="store_true",
        help="Cancel active required jobs and wait for terminal state.",
    )
    options = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    try:
        if options.cancel_required:
            canceled = cancel_required_flink_jobs()
            LOGGER.info(
                "Flink cleanup completed; canceled %d required job(s)",
                len(canceled),
            )
            return 0
        ensure_required_flink_jobs()
    except BootstrapError as exc:
        LOGGER.error("Flink job bootstrap failed [%s]: %s", type(exc).__name__, exc)
        return 1
    LOGGER.info("Flink job bootstrap completed successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
