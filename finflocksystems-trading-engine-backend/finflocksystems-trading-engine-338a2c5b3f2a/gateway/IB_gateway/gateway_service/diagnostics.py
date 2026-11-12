"""Safe, credential-free diagnostics for the private Gateway container."""

from __future__ import annotations

import os
import re
import socket
from pathlib import Path

from django.conf import settings
from django.db import DatabaseError

from .models import GatewayEvent, GatewayHealthSnapshot, GatewaySession
from .modes import tws_port_for_mode


# 8001 is gunicorn and 8080 is the nginx front door: without them the gateway
# serves nothing. 5900 (x11vnc) and 6080 (websockify) exist only so a human
# can watch the IB Gateway window, and the entrypoint leaves them stopped
# unless remote desktop is enabled, so requiring them unconditionally pinned
# /readyz at 503 forever in exactly the unattended runs it is meant to gate.
REQUIRED_INTERNAL_PORTS = (8001, 8080)
REMOTE_DESKTOP_PORTS = (5900, 6080)
INTERNAL_PORTS = tuple(sorted(REMOTE_DESKTOP_PORTS + REQUIRED_INTERNAL_PORTS))
KNOWN_BROKER_STATES = {"CONNECTING", "CONNECTED", "DISCONNECTED"}
SENSITIVE_ENVIRONMENT_NAME = re.compile(
    r"(?:PASSWORD|PASSWD|TOKEN|SECRET|CREDENTIAL|AUTH|COOKIE|SESSION|USERNAME|LOGIN)", re.I
)
SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(?:password|passwd|pwd|token|secret|credential|authorization|"
    r"ibloginid|ibpassword|username|login)\b\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
BEARER_VALUE = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
URL_CREDENTIALS = re.compile(r"(?i)(://)[^/@\s:]+(?::[^/@\s]*)?@")


def port_is_listening(port: int, timeout: float = 0.1) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            return connection.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False


def ibgateway_process_running(proc_root: Path = Path("/proc")) -> bool:
    """Inspect process command names without returning command-line content."""
    try:
        processes = list(proc_root.iterdir())
    except OSError:
        return False
    for process in processes:
        if not process.name.isdigit():
            continue
        try:
            command = (process / "cmdline").read_bytes()[:65536].replace(b"\0", b" ").lower()
        except OSError:
            continue
        if b"ibcalpha.ibc.ibcgateway" in command:
            return True
        if b"ibcstart.sh" in command and b"--gateway" in command:
            return True
    return False


def sanitize_broker_error(value: object) -> str | None:
    if value in (None, ""):
        return None
    text = " ".join(str(value).split())
    for name, secret in os.environ.items():
        if SENSITIVE_ENVIRONMENT_NAME.search(name) and secret:
            text = text.replace(secret, "[REDACTED]")
    text = URL_CREDENTIALS.sub(r"\1[REDACTED]@", text)
    text = BEARER_VALUE.sub("Bearer [REDACTED]", text)
    text = SENSITIVE_ASSIGNMENT.sub("[REDACTED]", text)
    return text[:500] or None


def _latest_broker_error() -> dict[str, str] | None:
    candidates: list[tuple[object, str, object, object]] = []
    try:
        events = GatewayEvent.objects.filter(
            event_type__in=("session.disconnected", "market.error")
        ).order_by("-created_at", "-id")[:20]
        for event in events:
            message = event.payload.get("error") or event.payload.get("error_message")
            code = event.payload.get("error_code")
            if message:
                candidates.append((event.created_at, event.event_type, message, code))

        snapshots = GatewayHealthSnapshot.objects.filter(connected=False).order_by(
            "-created_at", "-id"
        )[:20]
        for snapshot in snapshots:
            message = snapshot.details.get("error")
            if message:
                candidates.append((snapshot.created_at, "broker.health", message, None))
    except DatabaseError:
        return None

    if not candidates:
        return None
    occurred_at, source, raw_message, raw_code = max(candidates, key=lambda item: item[0])
    message = sanitize_broker_error(raw_message)
    if not message:
        return None
    result = {
        "source": source if source in {"session.disconnected", "market.error", "broker.health"} else "broker",
        "message": message,
        "occurred_at": occurred_at.isoformat(),
    }
    code = str(raw_code or "")
    if re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", code):
        result["code"] = code
    return result


def _broker_state() -> tuple[str, bool, bool]:
    try:
        session = GatewaySession.objects.filter(pk=1).only("state", "reconciled").first()
    except DatabaseError:
        return "UNKNOWN", False, False
    if not session:
        return "DISCONNECTED", False, True
    state = str(session.state or "").upper()
    if state not in KNOWN_BROKER_STATES:
        state = "UNKNOWN"
    return state, bool(session.reconciled), True


def collect_gateway_diagnostics() -> dict[str, object]:
    mode = settings.IBC_TRADING_MODE
    tws_port = tws_port_for_mode(mode)
    state, reconciled, database_available = _broker_state()
    return {
        "ib_gateway_process_running": ibgateway_process_running(),
        "expected_tws_api_port": tws_port,
        "tws_api_port_listening": port_is_listening(tws_port),
        "internal_ports": {str(port): port_is_listening(port) for port in INTERNAL_PORTS},
        "broker_connection_state": state,
        "broker_reconciled": reconciled,
        "database_available": database_available,
        "latest_broker_error": _latest_broker_error() if database_available else None,
    }


def readiness_state(
    diagnostics: dict[str, object],
    adapter: str,
    mode: str,
    remote_desktop_enabled: bool = False,
) -> tuple[bool, str, dict]:
    if not diagnostics["database_available"]:
        return False, "database_unavailable", {}
    ports = diagnostics["internal_ports"]
    required = REQUIRED_INTERNAL_PORTS + (REMOTE_DESKTOP_PORTS if remote_desktop_enabled else ())
    unavailable = [port for port in map(str, sorted(required)) if not ports.get(port, False)]
    if unavailable:
        return False, "internal_services_unavailable", {"unavailable_ports": unavailable}

    if adapter != "mock":
        if not diagnostics["ib_gateway_process_running"]:
            return False, "ib_gateway_not_running", {}
        if not diagnostics["tws_api_port_listening"]:
            waiting = "waiting_for_live_2fa" if mode == "live" else "waiting_for_login"
            return False, waiting, {}

    if diagnostics["broker_connection_state"] != "CONNECTED":
        return False, "broker_connecting", {}
    if not diagnostics["broker_reconciled"]:
        return False, "broker_reconciling", {}
    return True, "ready", {}
