"""Validate and normalize container runtime configuration without starting Django."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import sys
from collections.abc import Mapping
from pathlib import Path

from gateway_service.modes import normalize_trading_mode


VALID_BROKER_ADAPTERS = {"ib_async", "mock"}
PLACEHOLDER_VALUES = {
    "<password>",
    "<secret>",
    "<token>",
    "change-me",
    "change-me-too",
    "changeme",
    "replace-me",
}
KNOWN_PLACEHOLDER_HASHES = {
    "f2bb58dacc874fec3b553e1eec858314417af6fcce64dd69f3938174d5ac8131",
    "4c5dc9b7708905f77f5e5d16316b5dfb425e68cb326dcd55a860e90a7707031e",
    "9242adafa576c83657aa021a5c7e9fd0c60503c2ae5ff8f502cf58b51abf50c0",
    "4f11327cac97c517854b9b0b86e93c50f03834d805c270cdf8400b8dd39349d3",
    "b0060e76ae3488cac69d2e702fa366537fe241e19382ea731059e758fae07be1",
}
RESTART_TIME = re.compile(r"^(0?[1-9]|1[0-2]):[0-5][0-9] (AM|PM)$")
VNC_PASSWORD_BYTES = 8


class RuntimeConfigurationError(ValueError):
    def __init__(self, *, missing=(), invalid=()):
        self.missing = tuple(sorted(set(missing)))
        self.invalid = tuple(sorted(set(invalid)))
        details = []
        if self.missing:
            details.append(f"missing variables: {', '.join(self.missing)}")
        if self.invalid:
            details.append(f"invalid variables: {', '.join(self.invalid)}")
        super().__init__("Gateway runtime configuration error; " + "; ".join(details))


def normalize_broker_adapter(value: object) -> str:
    adapter = str(value or "").strip()
    if adapter not in VALID_BROKER_ADAPTERS:
        raise ValueError("BROKER_ADAPTER")
    return adapter


# The entrypoint gates x11vnc/websockify on GATEWAY_REMOTE_DESKTOP_ENABLED with
# this exact truthy set and exports the normalized result as
# GATEWAY_REMOTE_DESKTOP. Readiness has to agree with that decision, or it waits
# forever on viewer ports nothing was ever going to open.
REMOTE_DESKTOP_TRUTHY = {"1", "true", "yes", "on"}


def remote_desktop_enabled(environment: dict) -> bool:
    """Resolve whether the noVNC viewer pair is supposed to be running."""
    for name in ("GATEWAY_REMOTE_DESKTOP", "GATEWAY_REMOTE_DESKTOP_ENABLED"):
        value = environment.get(name)
        if value is not None and str(value).strip():
            return str(value).strip().casefold() in REMOTE_DESKTOP_TRUTHY
    return False


def normalize_vnc_password(value: object) -> str:
    """Return the exact eight ASCII bytes used by classic VNC authentication."""
    password = "" if value is None else str(value)
    if not password:
        raise ValueError("NOVNC_PASSWORD")
    if "\r" in password or "\n" in password:
        raise ValueError("NOVNC_PASSWORD")
    try:
        encoded = password.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError("NOVNC_PASSWORD") from None
    if len(encoded) < VNC_PASSWORD_BYTES:
        raise ValueError("NOVNC_PASSWORD")
    return encoded[:VNC_PASSWORD_BYTES].decode("ascii")


def _is_missing(value: object) -> bool:
    return not str(value or "").strip()


def _is_placeholder(value: object) -> bool:
    normalized = str(value or "").strip().casefold()
    return (
        normalized in PLACEHOLDER_VALUES
        or hashlib.sha256(normalized.encode("utf-8")).hexdigest() in KNOWN_PLACEHOLDER_HASHES
        or normalized.startswith("replace-me")
        or normalized.startswith("replace-with-")
        or normalized.startswith("change-me")
        or (normalized.startswith("<") and normalized.endswith(">"))
    )


def _positive_integer(environment: Mapping[str, str], name: str, default: str, maximum: int) -> str:
    raw = str(environment.get(name, default) or "").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(name) from exc
    if value < 1 or value > maximum or raw.startswith("+"):
        raise ValueError(name)
    return str(value)


def validate_environment(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = os.environ if environment is None else environment
    missing: list[str] = []
    invalid: list[str] = []

    try:
        adapter = normalize_broker_adapter(environment.get("BROKER_ADAPTER", "ib_async"))
    except ValueError:
        adapter = ""
        invalid.append("BROKER_ADAPTER")

    required = ["DJANGO_SECRET_KEY", "GATEWAY_SERVICE_TOKEN", "NOVNC_PASSWORD"]
    if adapter == "ib_async":
        required.extend(["IB_USERNAME", "IB_PASSWORD", "IBC_TRADING_MODE"])
    for name in required:
        value = environment.get(name, "")
        if _is_missing(value):
            missing.append(name)
        elif _is_placeholder(value) or "\n" in str(value) or "\r" in str(value):
            invalid.append(name)
    if "NOVNC_PASSWORD" not in missing and "NOVNC_PASSWORD" not in invalid:
        try:
            normalize_vnc_password(environment.get("NOVNC_PASSWORD"))
        except ValueError:
            invalid.append("NOVNC_PASSWORD")

    raw_mode = environment.get("IBC_TRADING_MODE", "paper" if adapter == "mock" else "")
    mode = ""
    if str(raw_mode or "").strip():
        try:
            mode = normalize_trading_mode(raw_mode)
        except ValueError:
            invalid.append("IBC_TRADING_MODE")

    normalized: dict[str, str] = {}
    integer_fields = {
        "PORT": ("8080", 65535),
        "IBC_2FA_TIMEOUT": ("180", 86400),
        "IBKR_CLIENT_ID": ("17", 999999),
        "BROKER_REFRESH_SECONDS": ("5", 86400),
        "GATEWAY_CONTRACT_SEARCH_MAX_RESULTS": ("12", 50),
        "GATEWAY_IBKR_REQUEST_TIMEOUT_SEARCH_CONTRACTS_SECONDS": ("12", 3600),
        "GATEWAY_IBKR_REQUEST_TIMEOUT_QUALIFY_SECONDS": ("15", 3600),
    }
    for name, (default, maximum) in integer_fields.items():
        try:
            normalized[name] = _positive_integer(environment, name, default, maximum)
        except ValueError:
            invalid.append(name)

    if str(environment.get("TWS_MAJOR_VRSN", "") or "").strip():
        try:
            normalized["TWS_MAJOR_VRSN"] = _positive_integer(
                environment, "TWS_MAJOR_VRSN", "", 99999
            )
        except ValueError:
            invalid.append("TWS_MAJOR_VRSN")

    restart_time = str(environment.get("IBC_AUTO_RESTART_TIME", "11:45 PM") or "").strip().upper()
    if not RESTART_TIME.fullmatch(restart_time):
        invalid.append("IBC_AUTO_RESTART_TIME")
    else:
        normalized["IBC_AUTO_RESTART_TIME"] = restart_time

    if missing or invalid:
        raise RuntimeConfigurationError(missing=missing, invalid=invalid)
    normalized["BROKER_ADAPTER"] = adapter
    normalized["IBC_TRADING_MODE"] = mode
    return normalized


def shell_exports(configuration: Mapping[str, str]) -> str:
    return "\n".join(
        f"export {name}={shlex.quote(value)}" for name, value in sorted(configuration.items())
    )


def _write_normalized_vnc_password(path: str, password: str) -> None:
    target = Path(path)
    target.write_text(normalize_vnc_password(password) + "\n", encoding="ascii")
    target.chmod(0o600)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    password_path = None
    if argv:
        if len(argv) != 2 or argv[0] != "--write-normalized-vnc-password":
            print("Gateway runtime configuration error; invalid command", file=sys.stderr)
            return 64
        password_path = argv[1]
    try:
        configuration = validate_environment()
    except RuntimeConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 64
    if password_path is not None:
        _write_normalized_vnc_password(
            password_path, os.environ.get("NOVNC_PASSWORD", "")
        )
    print(shell_exports(configuration))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
