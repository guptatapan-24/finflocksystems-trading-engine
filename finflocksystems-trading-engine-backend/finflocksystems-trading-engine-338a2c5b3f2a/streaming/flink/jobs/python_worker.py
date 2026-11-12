"""Configuration shared by PyFlink job graphs and Backend bootstrap tooling."""

from __future__ import annotations

import os
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping


ARCHIVE_MODE = "archive"
TASKMANAGER_PATH_MODE = "taskmanager-path"
ALLOWED_RUNTIME_MODES = frozenset({ARCHIVE_MODE, TASKMANAGER_PATH_MODE})

_SAFE_TARGET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|pwd|token|secret|credential|authorization|"
    r"api[_-]?key)(\s*[=:]\s*)([^\s,;&]+)"
)
_MISSING_PYTHON_PATTERNS = (
    re.compile(
        r"cannot run program\s+[\"']?python[\"']?.{0,160}no such file or directory",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"python.{0,100}(?:executable|binary).{0,80}(?:not found|missing)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"python.{0,120}no such file or directory",
        re.IGNORECASE | re.DOTALL,
    ),
)


class WorkerPythonConfigurationError(ValueError):
    """A safe-to-log worker runtime configuration error."""


@dataclass(frozen=True)
class WorkerPythonConfig:
    runtime_mode: str
    executable: str
    archive_path: Path | None = None
    archive_target: str | None = None

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "WorkerPythonConfig":
        values = os.environ if environ is None else environ
        mode = values.get("FLINK_PYTHON_RUNTIME_MODE", ARCHIVE_MODE).strip().lower()
        executable = values.get(
            "FLINK_PYTHON_EXECUTABLE",
            "pyenv/bin/python",
        ).strip()
        archive_value = values.get(
            "FLINK_PYTHON_ARCHIVE",
            "/opt/flink-python-runtime/flink-python-runtime.zip",
        ).strip()
        target = values.get("FLINK_PYTHON_ARCHIVE_TARGET", "pyenv").strip()
        return cls(
            runtime_mode=mode,
            executable=executable,
            archive_path=Path(archive_value) if archive_value else None,
            archive_target=target or None,
        )

    @property
    def archive_configured(self) -> bool:
        return self.runtime_mode == ARCHIVE_MODE and self.archive_path is not None

    def validate(self, *, inspect_archive: bool = True) -> None:
        if self.runtime_mode not in ALLOWED_RUNTIME_MODES:
            allowed = ", ".join(sorted(ALLOWED_RUNTIME_MODES))
            raise WorkerPythonConfigurationError(
                f"FLINK_PYTHON_RUNTIME_MODE must be one of: {allowed}"
            )
        if not self.executable:
            raise WorkerPythonConfigurationError(
                "FLINK_PYTHON_EXECUTABLE must not be empty"
            )
        if self.runtime_mode == TASKMANAGER_PATH_MODE:
            if not PurePosixPath(self.executable).is_absolute():
                raise WorkerPythonConfigurationError(
                    "FLINK_PYTHON_EXECUTABLE must be an absolute TaskManager path "
                    "when FLINK_PYTHON_RUNTIME_MODE=taskmanager-path"
                )
            return

        if self.archive_path is None:
            raise WorkerPythonConfigurationError(
                "FLINK_PYTHON_ARCHIVE is required when "
                "FLINK_PYTHON_RUNTIME_MODE=archive"
            )
        if not self.archive_path.is_absolute():
            raise WorkerPythonConfigurationError(
                "FLINK_PYTHON_ARCHIVE must be an absolute Backend path"
            )
        if self.archive_path.suffix.lower() != ".zip":
            raise WorkerPythonConfigurationError(
                "FLINK_PYTHON_ARCHIVE must be a ZIP archive for PyFlink 1.20"
            )
        if not self.archive_target or not _SAFE_TARGET.fullmatch(self.archive_target):
            raise WorkerPythonConfigurationError(
                "FLINK_PYTHON_ARCHIVE_TARGET must be one safe directory name"
            )

        executable = PurePosixPath(self.executable)
        if executable.is_absolute() or ".." in executable.parts:
            raise WorkerPythonConfigurationError(
                "FLINK_PYTHON_EXECUTABLE must be a safe relative archive path"
            )
        if len(executable.parts) < 2 or executable.parts[0] != self.archive_target:
            raise WorkerPythonConfigurationError(
                "FLINK_PYTHON_EXECUTABLE must be inside "
                "FLINK_PYTHON_ARCHIVE_TARGET"
            )
        if not inspect_archive:
            return
        if not self.archive_path.is_file() or not os.access(self.archive_path, os.R_OK):
            raise WorkerPythonConfigurationError(
                f"Configured Python archive is not a readable file: '{self.archive_path}'"
            )
        self._validate_archive_member(PurePosixPath(*executable.parts[1:]))

    def _validate_archive_member(self, member_path: PurePosixPath) -> None:
        assert self.archive_path is not None
        try:
            with zipfile.ZipFile(self.archive_path) as archive:
                info_by_name = {
                    item.filename.rstrip("/"): item for item in archive.infolist()
                }
                member_name = member_path.as_posix()
                info = info_by_name.get(member_name)
                if info is None:
                    raise WorkerPythonConfigurationError(
                        "Configured worker executable is not present in the Python archive: "
                        f"'{member_name}'"
                    )
                mode = info.external_attr >> 16
                if not mode & 0o111:
                    raise WorkerPythonConfigurationError(
                        "Configured worker executable is not executable in the Python archive: "
                        f"'{member_name}'"
                    )
                if stat.S_ISLNK(mode):
                    target = archive.read(info).decode("utf-8")
                    target_path = PurePosixPath(target)
                    if target_path.is_absolute() or ".." in target_path.parts:
                        raise WorkerPythonConfigurationError(
                            "Configured worker executable is an unsafe archive symlink"
                        )
                    resolved = (member_path.parent / target_path).as_posix()
                    if resolved not in info_by_name:
                        raise WorkerPythonConfigurationError(
                            "Configured worker executable is a broken archive symlink"
                        )
        except zipfile.BadZipFile as exc:
            raise WorkerPythonConfigurationError(
                f"Configured Python archive is not a valid ZIP file: '{self.archive_path}'"
            ) from exc


def is_missing_python_executable_failure(exception_text: str | None) -> bool:
    text = str(exception_text or "")
    return bool(text) and any(pattern.search(text) for pattern in _MISSING_PYTHON_PATTERNS)


def safe_path_for_log(value: object, limit: int = 500) -> str:
    text = str(value or "").replace("\r", "").replace("\n", "")
    text = _SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", text)
    return text if len(text) <= limit else text[: limit - 3] + "..."
