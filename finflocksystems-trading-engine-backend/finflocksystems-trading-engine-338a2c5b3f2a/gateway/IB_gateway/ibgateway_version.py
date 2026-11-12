#!/usr/bin/env python3
"""Detect and validate the IB Gateway installation used by IBC."""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path


LAUNCHER_PATTERN = re.compile(r"^jts4launch-(?P<major>[0-9]+)\.jar$")


class GatewayVersionError(RuntimeError):
    pass


@dataclass(frozen=True)
class GatewayInstallation:
    major: str
    directory: Path


def detect_installed_gateway(install_root: Path) -> GatewayInstallation:
    """Find the single installed Gateway major from its versioned launcher jar."""
    root = install_root.resolve()
    matches: list[tuple[str, Path]] = []
    if root.is_dir():
        for launcher in root.rglob("jts4launch-*.jar"):
            match = LAUNCHER_PATTERN.fullmatch(launcher.name)
            if match and launcher.parent.name == "jars":
                matches.append((match.group("major"), launcher.parent.parent.resolve()))

    majors = {major for major, _ in matches}
    if not majors:
        raise GatewayVersionError(
            f"could not detect an installed IB Gateway major below {install_root}"
        )
    if len(majors) != 1:
        raise GatewayVersionError(
            "multiple installed IB Gateway majors were detected: " + ", ".join(sorted(majors))
        )

    major = majors.pop()
    directories = {directory for candidate, directory in matches if candidate == major}
    if len(directories) != 1:
        raise GatewayVersionError(
            f"IB Gateway major {major} was detected in multiple installation directories"
        )
    return GatewayInstallation(major=major, directory=directories.pop())


def validate_expected_major(installed_major: str, expected_major: str | None) -> str:
    expected = str(expected_major or "").strip()
    if expected and (not expected.isdigit() or int(expected) < 1):
        raise GatewayVersionError("TWS_MAJOR_VRSN must be a positive integer when configured")
    if expected and expected != installed_major:
        raise GatewayVersionError(
            f"installed IB Gateway major {installed_major} does not match TWS_MAJOR_VRSN {expected}"
        )
    return installed_major


def _is_java_home(candidate: Path) -> bool:
    executable = candidate / "bin" / "java"
    return executable.is_file() and os.access(executable, os.X_OK)


def resolve_install4j_jre(install_dir: Path, shared_jre_root: Path) -> Path:
    """Return a JRE the install4j launcher can actually execute.

    The IB Gateway installer records the JRE it used in `.install4j/inst_jre.cfg`
    as a path inside its own extraction directory (`/tmp/ibgateway.sh.<pid>.dir`),
    which no longer exists once the installer has exited. Whether that matters
    depends on the build the moving `stable` URL happens to serve: 1045 also
    registered a shared JRE under /opt/i4j_jres and the launcher fell back to it,
    while 1050 bundled its JRE into the installation directory and left nothing
    to fall back to, so every launch died with "No java executable found".
    """
    configured = install_dir / ".install4j" / "inst_jre.cfg"
    try:
        recorded = Path(configured.read_text(encoding="utf-8").strip())
    except OSError:
        recorded = None
    for candidate in (recorded, install_dir / "jre"):
        if candidate is not None and _is_java_home(candidate):
            return candidate
    if shared_jre_root.is_dir():
        for candidate in sorted(shared_jre_root.iterdir()):
            if _is_java_home(candidate):
                return candidate
    raise GatewayVersionError(
        f"no usable JRE was found for the IB Gateway installation at {install_dir}"
    )


def repair_install4j_jre(install_dir: Path, shared_jre_root: Path) -> Path:
    """Point `inst_jre.cfg` at a JRE that exists, failing loudly when none does."""
    jre = resolve_install4j_jre(install_dir, shared_jre_root)
    configured = install_dir / ".install4j" / "inst_jre.cfg"
    configured.parent.mkdir(parents=True, exist_ok=True)
    configured.write_text(str(jre) + "\n", encoding="utf-8")
    return jre


def configure_ibc_layout(
    install_root: Path,
    tws_root: Path,
    version_file: Path,
    shared_jre_root: Path = Path("/opt/i4j_jres"),
) -> str:
    installation = detect_installed_gateway(install_root)
    repair_install4j_jre(installation.directory, shared_jre_root)
    target_parent = tws_root / "ibgateway"
    target_parent.mkdir(parents=True, exist_ok=True)
    target = target_parent / installation.major

    if os.path.lexists(target):
        try:
            current = target.resolve(strict=True)
        except OSError as exc:
            raise GatewayVersionError(f"IBC Gateway path {target} cannot be resolved") from exc
        if current != installation.directory:
            raise GatewayVersionError(
                f"IBC Gateway path {target} does not select the detected installation"
            )
    else:
        target.symlink_to(installation.directory, target_is_directory=True)

    version_file.parent.mkdir(parents=True, exist_ok=True)
    version_file.write_text(installation.major + "\n", encoding="ascii")
    return installation.major


def verify_ibc_layout(
    install_root: Path,
    tws_root: Path,
    version_file: Path,
    expected_major: str | None = None,
    shared_jre_root: Path = Path("/opt/i4j_jres"),
) -> str:
    installation = detect_installed_gateway(install_root)
    resolve_install4j_jre(installation.directory, shared_jre_root)
    try:
        recorded = version_file.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise GatewayVersionError(f"installed Gateway version file {version_file} is unavailable") from exc
    if recorded != installation.major:
        raise GatewayVersionError(
            f"detected IB Gateway major {installation.major} does not match recorded major {recorded or 'missing'}"
        )

    target = tws_root / "ibgateway" / installation.major
    try:
        selected = target.resolve(strict=True)
    except OSError as exc:
        raise GatewayVersionError(f"IBC Gateway path {target} is unavailable") from exc
    if selected != installation.directory:
        raise GatewayVersionError(
            f"IBC Gateway path {target} does not select the detected installation"
        )
    return validate_expected_major(installation.major, expected_major)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("detect", "install", "verify"))
    parser.add_argument("--install-root", type=Path, default=Path("/opt/ibgateway"))
    parser.add_argument("--tws-root", type=Path, default=Path("/opt/Jts"))
    parser.add_argument(
        "--version-file", type=Path, default=Path("/opt/ibgateway/.tws-major-version")
    )
    parser.add_argument("--expected", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "detect":
            major = detect_installed_gateway(args.install_root).major
        elif args.action == "install":
            major = configure_ibc_layout(args.install_root, args.tws_root, args.version_file)
        else:
            major = verify_ibc_layout(
                args.install_root, args.tws_root, args.version_file, args.expected
            )
    except GatewayVersionError as exc:
        print(f"IB Gateway version error: {exc}", file=sys.stderr)
        return 64
    print(major)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
