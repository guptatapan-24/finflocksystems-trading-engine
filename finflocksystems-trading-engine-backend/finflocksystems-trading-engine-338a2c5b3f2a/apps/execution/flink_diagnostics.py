"""Bounded, read-only operator diagnostics for the internal Flink REST API."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone as datetime_timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import SplitResult, urlsplit, urlunsplit

import requests
from django.conf import settings
from django.utils import timezone

from .readiness import DEFAULT_REQUIRED_FLINK_JOBS


DIAGNOSTIC_JOB_STATES = frozenset(
    {
        "RESTARTING",
        "FAILING",
        "FAILED",
        "CREATED",
        "INITIALIZING",
        "CANCELLING",
        "CANCELED",
        "SUSPENDED",
    }
)
FAILING_VERTEX_STATES = frozenset(
    {"RESTARTING", "FAILING", "FAILED", "CANCELLING", "CANCELED"}
)
MAX_EXCEPTION_LENGTH = 2_000
MAX_EXCEPTION_HISTORY_ITEMS = 5
MAX_HISTORY_EXCEPTION_LENGTH = 1_000

_URL_CREDENTIALS = re.compile(
    r"(?i)([a-z][a-z0-9+.-]*://)([^\s/:@]+):([^\s/@]+)@"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|pwd|token|secret|credential|authorization|"
    r"api[_-]?key|access[_-]?key|sasl\.jaas\.config)"
    r"(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
)
_BEARER_TOKEN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_AUTHORIZATION_HEADER = re.compile(
    r"(?i)\bauthorization\s*:\s*(?:bearer|basic)\s+[^\s,;]+"
)


@dataclass(frozen=True)
class FlinkDiagnosticError(Exception):
    code: str
    message: str
    endpoint: str
    status_code: int | None = None
    reached_jobmanager: bool = False


def _setting_tuple(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = getattr(settings, name, default)
    if isinstance(value, str):
        value = value.split(",")
    normalized = tuple(str(item).strip() for item in value if str(item).strip())
    return normalized or default


def _redact_text(value: Any, limit: int) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)
    text = _URL_CREDENTIALS.sub(r"\1[REDACTED]@", text)
    text = _AUTHORIZATION_HEADER.sub("Authorization: [REDACTED]", text)
    text = _BEARER_TOKEN.sub("Bearer [REDACTED]", text)
    text = _SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", text)
    if len(text) > limit:
        return text[: max(0, limit - 3)] + "..."
    return text


def _safe_runtime_path(value: Any) -> str | None:
    text = _redact_text(value, 500)
    if text is None:
        return None
    return text.replace("\r", "").replace("\n", "")


def _repair_status() -> dict[str, dict[str, Any]]:
    """Read only whitelisted fields from the bounded bootstrap status file."""
    configured = getattr(
        settings,
        "FLINK_BOOTSTRAP_STATUS_PATH",
        "/tmp/flink-job-bootstrap-status.json",
    )
    try:
        path = Path(str(configured))
        if not path.is_file() or path.stat().st_size > 65_536:
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    jobs = payload.get("jobs") if isinstance(payload, Mapping) else None
    if not isinstance(jobs, Mapping):
        return {}
    allowed_results = {
        "CANCEL_REQUESTED",
        "CANCELED",
        "REPAIRED",
        "RESUBMISSION_FAILED",
        "REPLACEMENT_RESTARTING",
        "REPLACEMENT_UNHEALTHY",
    }
    result: dict[str, dict[str, Any]] = {}
    for name, value in jobs.items():
        if not isinstance(name, str) or not isinstance(value, Mapping):
            continue
        repair_result = str(value.get("result") or "")
        if repair_result not in allowed_results:
            continue
        row: dict[str, Any] = {
            "result": repair_result,
            "classification": "PYTHON_EXECUTABLE_MISSING",
            "observed_at": _redact_text(value.get("observed_at"), 100),
        }
        for field in ("old_job_id", "replacement_job_id"):
            identifier = str(value.get(field) or "")
            if identifier and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier):
                row[field] = identifier
        result[name] = row
    return result


def _validated_parts(value: Any) -> SplitResult:
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("FLINK_REST_URL must be an http:// or https:// URL")
    # Accessing port validates malformed values such as ':not-a-port'.
    parsed.port
    return parsed


def sanitize_flink_rest_url(value: Any) -> str | None:
    """Return only the configured scheme, hostname, and explicit port."""
    try:
        parsed = _validated_parts(value)
    except (TypeError, ValueError):
        return None
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme.lower(), host, "", "", ""))


def _safe_address(value: Any) -> str | None:
    text = _redact_text(value, 500)
    if not text or "://" not in text:
        return text
    try:
        parsed = urlsplit(text)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except ValueError:
        return None


class FlinkRestClient:
    """Small Flink client that never includes the configured URL in errors."""

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        *,
        session: requests.Session | None = None,
    ) -> None:
        self.parts = _validated_parts(base_url)
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.session = session or requests.Session()

    def _url(self, endpoint: str) -> str:
        base_path = self.parts.path.rstrip("/")
        return urlunsplit(
            (
                self.parts.scheme,
                self.parts.netloc,
                base_path + "/" + endpoint.lstrip("/"),
                self.parts.query,
                "",
            )
        )

    def get_json(self, endpoint: str) -> dict[str, Any]:
        try:
            response = self.session.get(
                self._url(endpoint),
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except requests.Timeout as exc:
            raise FlinkDiagnosticError(
                "FLINK_ENDPOINT_TIMEOUT",
                "The Flink REST request timed out.",
                endpoint,
            ) from exc
        except requests.RequestException as exc:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
            job_endpoint = re.fullmatch(r"/jobs/[^/]+(?:/.*)?", endpoint)
            if status_code == 404 and job_endpoint:
                code = "FLINK_JOB_NOT_FOUND"
                message = "The Flink job endpoint returned HTTP 404."
            else:
                code = "FLINK_ENDPOINT_FAILURE"
                message = "The Flink REST endpoint request failed."
            raise FlinkDiagnosticError(
                code,
                message,
                endpoint,
                status_code=status_code,
                reached_jobmanager=response is not None,
            ) from exc
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise FlinkDiagnosticError(
                "MALFORMED_FLINK_RESPONSE",
                "The Flink REST endpoint returned invalid JSON.",
                endpoint,
                status_code=response.status_code,
                reached_jobmanager=True,
            ) from exc
        if not isinstance(payload, dict):
            raise FlinkDiagnosticError(
                "MALFORMED_FLINK_RESPONSE",
                "The Flink REST endpoint returned an unexpected payload type.",
                endpoint,
                status_code=response.status_code,
                reached_jobmanager=True,
            )
        return payload


def _integer(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _first_present(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _millis(value: Any) -> int | None:
    result = _integer(value)
    return result if result is not None and result >= 0 else None


def _timestamp(value: Any) -> str | None:
    milliseconds = _millis(value)
    if milliseconds is None or milliseconds == 0:
        return None
    try:
        return datetime.fromtimestamp(
            milliseconds / 1_000,
            tz=datetime_timezone.utc,
        ).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _age_seconds(value: Any, now: datetime) -> float | None:
    milliseconds = _millis(value)
    if milliseconds is None or milliseconds == 0:
        return None
    try:
        observed = datetime.fromtimestamp(
            milliseconds / 1_000,
            tz=datetime_timezone.utc,
        )
    except (OSError, OverflowError, ValueError):
        return None
    return round(max(0.0, (now - observed).total_seconds()), 3)


def _job_id(item: Mapping[str, Any]) -> str:
    value = str(item.get("jid") or item.get("id") or "").strip()
    if not value or len(value) > 128 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return ""
    return value


def _job_summary(item: Mapping[str, Any], required_jobs: set[str]) -> dict[str, Any]:
    name = str(item.get("name") or "").strip()
    return {
        "name": name,
        "job_id": _job_id(item),
        "state": str(item.get("state") or "UNKNOWN").strip().upper(),
        "start_time": _timestamp(item.get("start-time") or item.get("start_time")),
        "duration": _integer(item.get("duration")),
        "required": name in required_jobs,
        "failing_vertex": None,
        "root_exception": None,
        "exception_timestamp": None,
        "exception_history": [],
        "exception_history_truncated": False,
        "latest_completed_checkpoint": None,
        "latest_failed_checkpoint": None,
        "checkpoint_failure_reason": None,
        "checkpoint_age_seconds": None,
        "restart_count": None,
        "failure_count": None,
        "probable_cause": None,
        "automatic_repair_eligible": False,
        "repair_attempt_result": None,
        "endpoint_errors": [],
    }


def _failing_vertex(payload: Mapping[str, Any]) -> str | None:
    for vertex in payload.get("vertices") or []:
        if not isinstance(vertex, Mapping):
            continue
        status = str(vertex.get("status") or "").upper()
        if status in FAILING_VERTEX_STATES:
            return _redact_text(vertex.get("name"), 500)
    return None


def _restart_count(payload: Mapping[str, Any]) -> int | None:
    candidates = (payload, payload.get("job-config") or {})
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        for key in ("restart-count", "restart_count", "restartCount", "numRestarts"):
            if key in candidate:
                return _integer(candidate.get(key))
    return None


def _exception_details(payload: Mapping[str, Any]) -> dict[str, Any]:
    root = payload.get("root-exception") or payload.get("root_exception")
    root_timestamp = payload.get("timestamp")
    history: list[dict[str, Any]] = []
    history_payload = payload.get("exceptionHistory") or payload.get(
        "exception-history"
    )
    history_truncated = bool(payload.get("truncated"))
    source_items: list[Any] = []
    if isinstance(history_payload, Mapping):
        source_items.extend(history_payload.get("entries") or [])
        history_truncated = bool(history_payload.get("truncated"))
    legacy_items = payload.get("all-exceptions") or payload.get("all_exceptions")
    if isinstance(legacy_items, list):
        source_items.extend(legacy_items)
    if len(source_items) > MAX_EXCEPTION_HISTORY_ITEMS:
        history_truncated = True

    failing_task = None
    for item in source_items[:MAX_EXCEPTION_HISTORY_ITEMS]:
        if not isinstance(item, Mapping):
            continue
        exception = (
            item.get("exception")
            or item.get("stacktrace")
            or item.get("exceptionName")
        )
        task = item.get("task") or item.get("taskName")
        if failing_task is None and task:
            failing_task = _redact_text(task, 500)
        history.append(
            {
                "exception": _redact_text(exception, MAX_HISTORY_EXCEPTION_LENGTH),
                "timestamp": _timestamp(item.get("timestamp")),
                "task": _redact_text(task, 500),
            }
        )

    if not root and history:
        root = history[0]["exception"]
    if root_timestamp in (None, "") and source_items:
        first = source_items[0]
        if isinstance(first, Mapping):
            root_timestamp = first.get("timestamp")
    if source_items:
        failure_count = len(source_items)
    elif root:
        failure_count = 1
    elif "all-exceptions" in payload or "all_exceptions" in payload:
        failure_count = 0
    else:
        failure_count = None
    return {
        "root_exception": _redact_text(root, MAX_EXCEPTION_LENGTH),
        "exception_timestamp": _timestamp(root_timestamp),
        "exception_history": history,
        "exception_history_truncated": history_truncated,
        "failing_vertex": failing_task,
        "failure_count": failure_count,
    }


def _latest_checkpoint(
    payload: Mapping[str, Any], status: str
) -> Mapping[str, Any] | None:
    latest = payload.get("latest") or {}
    direct = latest.get(status.lower()) if isinstance(latest, Mapping) else None
    if isinstance(direct, Mapping):
        return direct
    candidates = [
        item
        for item in (payload.get("history") or [])
        if isinstance(item, Mapping)
        and str(item.get("status") or "").upper() == status
    ]
    return max(
        candidates,
        key=lambda item: (
            _millis(
                item.get("latest_ack_timestamp")
                or item.get("end_time")
                or item.get("trigger_timestamp")
            )
            or 0,
            _integer(item.get("id")) or -1,
        ),
        default=None,
    )


def _checkpoint_row(item: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if item is None:
        return None
    return {
        "id": _integer(item.get("id")),
        "status": str(item.get("status") or "UNKNOWN").upper(),
        "trigger_time": _timestamp(item.get("trigger_timestamp")),
        "latest_ack_time": _timestamp(item.get("latest_ack_timestamp")),
        "end_to_end_duration": _integer(item.get("end_to_end_duration")),
    }


def _checkpoint_details(payload: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    completed = _latest_checkpoint(payload, "COMPLETED")
    failed = _latest_checkpoint(payload, "FAILED")
    completed_at = None
    if completed:
        completed_at = completed.get("latest_ack_timestamp") or completed.get(
            "trigger_timestamp"
        )
    failure_reason = None
    if failed:
        failure_reason = (
            failed.get("failure-message")
            or failed.get("failure_message")
            or failed.get("failure-cause")
        )
    return {
        "latest_completed_checkpoint": _checkpoint_row(completed),
        "latest_failed_checkpoint": _checkpoint_row(failed),
        "checkpoint_failure_reason": _redact_text(
            failure_reason, MAX_EXCEPTION_LENGTH
        ),
        "checkpoint_age_seconds": _age_seconds(completed_at, now),
    }


def _taskmanagers(payload: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    items = payload.get("taskmanagers") or []
    if not isinstance(items, list):
        return None
    rows = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        heartbeat = item.get("timeSinceLastHeartbeat") or item.get(
            "time_since_last_heartbeat"
        )
        rows.append(
            {
                "taskmanager_id": _redact_text(item.get("id"), 200),
                "path_or_address": _safe_address(
                    item.get("path") or item.get("address")
                ),
                "total_slots": _integer(
                    _first_present(item, "slotsNumber", "slots-number")
                ),
                "free_slots": _integer(
                    _first_present(item, "freeSlots", "free-slots")
                ),
                "data_port": _integer(
                    _first_present(item, "dataPort", "data-port")
                ),
                "last_heartbeat": _timestamp(heartbeat),
            }
        )
    return rows


_CAUSE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "NO_TASKMANAGERS",
        (
            r"no task\s*managers? (?:are )?(?:available|registered|connected)",
            r"task\s*manager[^\n]{0,100}(?:not registered|unavailable)",
        ),
    ),
    (
        "NO_AVAILABLE_SLOTS",
        (
            r"no (?:available|free) slots",
            r"not enough (?:available|free) slots",
        ),
    ),
    (
        "JOBMANAGER_UNAVAILABLE",
        (
            r"job\s*manager[^\n]{0,100}(?:unavailable|not reachable|connection refused)",
            r"could not connect[^\n]{0,100}job\s*manager",
        ),
    ),
    (
        "PYTHON_EXECUTABLE_MISSING",
        (
            r"python[\s\S]{0,100}(?:executable|binary)[\s\S]{0,60}(?:not found|missing)",
            r"cannot run program[\s\S]{0,100}python[\s\S]{0,80}no such file",
            r"python[\s\S]{0,140}no such file or directory",
        ),
    ),
    (
        "PYTHON_DEPENDENCY_MISSING",
        (r"modulenotfounderror", r"no module named", r"importerror"),
    ),
    (
        "PYFLINK_WORKER_FAILURE",
        (
            r"pyflink[^\n]{0,120}(?:worker|process)[^\n]{0,80}(?:failed|exited|died)",
            r"python (?:sdk harness|worker)[^\n]{0,80}(?:failed|exited|died)",
            r"(?:failed|failure)[^\n]{0,100}python (?:sdk harness|worker)",
            r"failed to create stage bundle factory",
        ),
    ),
    (
        "KAFKA_CONNECTOR_MISSING",
        (
            r"kafka[^\n]{0,120}(?:connector|factory|jar)[^\n]{0,80}(?:missing|not found|could not find)",
            r"(?:missing|not found|could not find)[^\n]{0,100}kafka[^\n]{0,80}(?:connector|factory|jar)",
            r"could not find any factory for identifier[^\n]{0,40}kafka",
            r"(?:classnotfoundexception|noclassdeffounderror)[^\n]{0,160}kafka",
        ),
    ),
    (
        "KAFKA_TOPIC_MISSING",
        (
            r"unknowntopicorpartition",
            r"unknown_topic_or_partition",
            r"topic[^\n]{0,120}not present in metadata",
        ),
    ),
    (
        "KAFKA_UNREACHABLE",
        (
            r"(?:bootstrap broker|kafka broker)[^\n]{0,100}(?:disconnected|unavailable)",
            r"connection to node[^\n]{0,100}could not be established",
            r"broker may not be available",
            r"kafka[^\n]{0,120}timed out[^\n]{0,80}(?:metadata|broker|bootstrap)",
            r"(?:org\.apache\.kafka|kafka)[^\n]{0,160}timeout(?:exception| waiting)",
        ),
    ),
    (
        "CLASS_NOT_FOUND",
        (r"classnotfoundexception", r"noclassdeffounderror"),
    ),
    (
        "CHECKPOINT_FAILURE",
        (
            r"checkpoint[^\n]{0,160}(?:failed|failure|declined|expired|timed out)",
            r"(?:failed|failure|declined)[^\n]{0,120}checkpoint",
        ),
    ),
    (
        "RESOURCE_ALLOCATION_FAILURE",
        (
            r"noresourceavailableexception",
            r"resource requirements could not be fulfilled",
            r"slot request bulk is not fulfillable",
            r"could not allocate[^\n]{0,80}(?:resource|slot)",
            r"insufficient number of network buffers",
        ),
    ),
)


def classify_probable_cause(exception_texts: list[str | None]) -> str | None:
    text = "\n".join(str(item) for item in exception_texts if item).lower()
    if not text:
        return None
    for classification, patterns in _CAUSE_PATTERNS:
        if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns):
            return classification
    return "UNKNOWN_FLINK_FAILURE"


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def collect_flink_diagnostics(
    *,
    session: requests.Session | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Collect all available diagnostics without failing the whole inspection."""
    now = now or timezone.now()
    required_jobs = _setting_tuple(
        "EXECUTION_REQUIRED_FLINK_JOBS", DEFAULT_REQUIRED_FLINK_JOBS
    )
    configured_url = getattr(settings, "FLINK_REST_URL", "")
    runtime_mode = str(
        getattr(settings, "FLINK_PYTHON_RUNTIME_MODE", "archive")
    ).strip().lower()
    archive_path = str(getattr(settings, "FLINK_PYTHON_ARCHIVE", "") or "").strip()
    auto_repair_enabled = bool(
        getattr(settings, "FLINK_AUTO_REPAIR_PYTHON_EXECUTABLE_FAILURE", True)
    )
    repair_status = _repair_status()
    result: dict[str, Any] = {
        "flink_rest_url": sanitize_flink_rest_url(configured_url),
        "python_runtime": {
            "client_executable": _safe_runtime_path(
                getattr(settings, "PYFLINK_CLIENT_EXECUTABLE", "")
            ),
            "worker_runtime_mode": runtime_mode,
            "worker_executable": _safe_runtime_path(
                getattr(settings, "FLINK_PYTHON_EXECUTABLE", "")
            ),
            "archive_configured": runtime_mode == "archive" and bool(archive_path),
            "archive_target": (
                _safe_runtime_path(
                    getattr(settings, "FLINK_PYTHON_ARCHIVE_TARGET", "")
                )
                if runtime_mode == "archive" and archive_path
                else None
            ),
            "automatic_repair_enabled": auto_repair_enabled,
        },
        "reachable": False,
        "cluster": {
            "flink_version": None,
            "taskmanager_count": None,
            "total_slots": None,
            "available_slots": None,
            "running_job_count": None,
            "restarting_job_count": None,
            "failed_job_count": None,
        },
        "taskmanagers": [],
        "required_jobs": list(required_jobs),
        "jobs": [],
        "errors": [],
        "summary": {
            "job_inventory_complete": False,
            "missing_required_jobs": None,
            "required_jobs_not_running": None,
            "jobs_with_exceptions": [],
            "jobs_without_completed_checkpoints": [],
            "taskmanager_or_slot_problems": [],
            "probable_cause": None,
        },
    }
    timeout = getattr(settings, "FLINK_DIAGNOSTICS_HTTP_TIMEOUT_SECONDS", 5)
    try:
        client = FlinkRestClient(configured_url, timeout, session=session)
    except (TypeError, ValueError):
        result["errors"].append(
            {
                "code": "INVALID_FLINK_REST_URL",
                "endpoint": None,
                "message": "FLINK_REST_URL is not a valid HTTP(S) endpoint.",
                "status_code": None,
            }
        )
        result["summary"]["probable_cause"] = "JOBMANAGER_UNAVAILABLE"
        return result

    reached_jobmanager = False

    def request(endpoint: str, *, job: dict[str, Any] | None = None):
        nonlocal reached_jobmanager
        try:
            payload = client.get_json(endpoint)
            reached_jobmanager = True
            return payload
        except FlinkDiagnosticError as exc:
            reached_jobmanager = reached_jobmanager or exc.reached_jobmanager
            error = {
                "code": exc.code,
                "endpoint": endpoint,
                "message": exc.message,
                "status_code": exc.status_code,
            }
            result["errors"].append(error)
            if job is not None:
                job["endpoint_errors"].append(error.copy())
            return None

    overview = request("/overview")
    taskmanager_payload = request("/taskmanagers")
    jobs_payload = request("/jobs/overview")
    result["reachable"] = reached_jobmanager

    if overview is not None:
        cluster = result["cluster"]
        cluster.update(
            {
                "flink_version": _redact_text(
                    overview.get("flink-version") or overview.get("flink_version"),
                    100,
                ),
                "taskmanager_count": _integer(overview.get("taskmanagers")),
                "total_slots": _integer(
                    _first_present(overview, "slots-total", "slots_total")
                ),
                "available_slots": _integer(
                    _first_present(overview, "slots-available", "slots_available")
                ),
                "running_job_count": _integer(
                    _first_present(overview, "jobs-running", "jobs_running")
                ),
                "failed_job_count": _integer(
                    _first_present(overview, "jobs-failed", "jobs_failed")
                ),
            }
        )

    if taskmanager_payload is not None:
        taskmanager_rows = _taskmanagers(taskmanager_payload)
        if taskmanager_rows is None:
            error = {
                "code": "MALFORMED_FLINK_RESPONSE",
                "endpoint": "/taskmanagers",
                "message": "The Flink taskmanager list is not an array.",
                "status_code": None,
            }
            result["errors"].append(error)
        else:
            result["taskmanagers"] = taskmanager_rows
            if result["cluster"]["taskmanager_count"] is None:
                result["cluster"]["taskmanager_count"] = len(taskmanager_rows)

    inventory: list[Mapping[str, Any]] | None = None
    if jobs_payload is not None:
        jobs_value = jobs_payload.get("jobs") or []
        if isinstance(jobs_value, list):
            inventory = [item for item in jobs_value if isinstance(item, Mapping)]
            result["summary"]["job_inventory_complete"] = True
        else:
            result["errors"].append(
                {
                    "code": "MALFORMED_FLINK_RESPONSE",
                    "endpoint": "/jobs/overview",
                    "message": "The Flink job list is not an array.",
                    "status_code": None,
                }
            )

    if inventory is not None:
        states = [str(item.get("state") or "UNKNOWN").upper() for item in inventory]
        if result["cluster"]["running_job_count"] is None:
            result["cluster"]["running_job_count"] = states.count("RUNNING")
        result["cluster"]["restarting_job_count"] = states.count("RESTARTING")
        if result["cluster"]["failed_job_count"] is None:
            result["cluster"]["failed_job_count"] = states.count("FAILED")

        names = {str(item.get("name") or "").strip() for item in inventory}
        missing = [name for name in required_jobs if name not in names]
        not_running = []
        for required_name in required_jobs:
            matching_states = [
                str(item.get("state") or "UNKNOWN").upper()
                for item in inventory
                if str(item.get("name") or "").strip() == required_name
            ]
            if matching_states and "RUNNING" not in matching_states:
                not_running.append(required_name)
        result["summary"]["missing_required_jobs"] = missing
        result["summary"]["required_jobs_not_running"] = not_running

        required_set = set(required_jobs)
        for item in inventory:
            name = str(item.get("name") or "").strip()
            state = str(item.get("state") or "UNKNOWN").strip().upper()
            if name not in required_set and state not in DIAGNOSTIC_JOB_STATES:
                continue
            row = _job_summary(item, required_set)
            if not row["job_id"]:
                error = {
                    "code": "MALFORMED_FLINK_RESPONSE",
                    "endpoint": "/jobs/overview",
                    "message": "A relevant Flink job has an invalid or missing job ID.",
                    "status_code": None,
                }
                result["errors"].append(error)
                row["endpoint_errors"].append(error.copy())
                result["jobs"].append(row)
                continue

            job_id = row["job_id"]
            detail = request(f"/jobs/{job_id}", job=row)
            if detail is not None:
                row["name"] = str(detail.get("name") or row["name"]).strip()
                row["state"] = str(detail.get("state") or row["state"]).upper()
                row["start_time"] = _timestamp(
                    detail.get("start-time") or detail.get("start_time")
                ) or row["start_time"]
                row["duration"] = _integer(detail.get("duration")) or row["duration"]
                row["failing_vertex"] = _failing_vertex(detail)
                row["restart_count"] = _restart_count(detail)

            exceptions = request(f"/jobs/{job_id}/exceptions", job=row)
            if exceptions is not None:
                exception_fields = _exception_details(exceptions)
                exception_vertex = exception_fields.pop("failing_vertex")
                row.update(exception_fields)
                if row["failing_vertex"] is None:
                    row["failing_vertex"] = exception_vertex

            checkpoints = request(f"/jobs/{job_id}/checkpoints", job=row)
            if checkpoints is not None:
                row.update(_checkpoint_details(checkpoints, now))
            row["probable_cause"] = classify_probable_cause(
                [row["root_exception"]]
            )
            row["automatic_repair_eligible"] = bool(
                auto_repair_enabled
                and row["required"]
                and row["state"] == "RESTARTING"
                and row["probable_cause"] == "PYTHON_EXECUTABLE_MISSING"
            )
            row["repair_attempt_result"] = repair_status.get(row["name"])
            result["jobs"].append(row)

    summary = result["summary"]
    exception_texts: list[str | None] = []
    for job in result["jobs"]:
        if job["root_exception"]:
            summary["jobs_with_exceptions"].append(job["name"])
            exception_texts.append(job["root_exception"])
        exception_texts.append(job["checkpoint_failure_reason"])
        if job["latest_completed_checkpoint"] is None:
            summary["jobs_without_completed_checkpoints"].append(job["name"])
    summary["jobs_with_exceptions"] = _unique(summary["jobs_with_exceptions"])
    summary["jobs_without_completed_checkpoints"] = _unique(
        summary["jobs_without_completed_checkpoints"]
    )

    taskmanager_count = result["cluster"]["taskmanager_count"]
    available_slots = result["cluster"]["available_slots"]
    if taskmanager_count == 0:
        summary["taskmanager_or_slot_problems"].append("NO_TASKMANAGERS")
    elif available_slots == 0:
        summary["taskmanager_or_slot_problems"].append("NO_AVAILABLE_SLOTS")

    if not result["reachable"]:
        summary["probable_cause"] = "JOBMANAGER_UNAVAILABLE"
        result["errors"].append(
            {
                "code": "JOBMANAGER_UNAVAILABLE",
                "endpoint": None,
                "message": "The Backend could not reach the configured Flink JobManager.",
                "status_code": None,
            }
        )
    elif taskmanager_count == 0:
        summary["probable_cause"] = "NO_TASKMANAGERS"
    elif available_slots == 0 and any(
        job["state"] != "RUNNING" for job in result["jobs"]
    ):
        summary["probable_cause"] = "NO_AVAILABLE_SLOTS"
    else:
        summary["probable_cause"] = classify_probable_cause(exception_texts)
    return result
