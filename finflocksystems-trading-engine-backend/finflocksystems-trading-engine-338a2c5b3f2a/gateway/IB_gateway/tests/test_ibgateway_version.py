from pathlib import Path

import pytest

from ibgateway_version import (
    GatewayVersionError,
    configure_ibc_layout,
    detect_installed_gateway,
    repair_install4j_jre,
    validate_expected_major,
    verify_ibc_layout,
)


def gateway_installation(root: Path, major: str) -> Path:
    installation = root / "gateway"
    jars = installation / "jars"
    jars.mkdir(parents=True)
    (jars / f"jts4launch-{major}.jar").touch()
    return installation


def test_detects_installed_gateway_major_from_launcher_jar(tmp_path):
    installation = gateway_installation(tmp_path, "1047")

    detected = detect_installed_gateway(tmp_path)

    assert detected.major == "1047"
    assert detected.directory == installation.resolve()


def test_detection_rejects_multiple_installed_majors(tmp_path):
    gateway_installation(tmp_path / "first", "1045")
    gateway_installation(tmp_path / "second", "1047")

    with pytest.raises(GatewayVersionError, match="multiple installed IB Gateway majors"):
        detect_installed_gateway(tmp_path)


def test_configured_major_must_match_detected_installation():
    assert validate_expected_major("1047", "1047") == "1047"
    assert validate_expected_major("1047", "") == "1047"

    with pytest.raises(GatewayVersionError, match="does not match TWS_MAJOR_VRSN"):
        validate_expected_major("1047", "1045")


def test_missing_launcher_jar_fails_clearly(tmp_path):
    with pytest.raises(GatewayVersionError, match="could not detect an installed IB Gateway major"):
        detect_installed_gateway(tmp_path)


def install4j_jre(root: Path) -> Path:
    executable = root / "bin" / "java"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.touch()
    executable.chmod(0o755)
    return root


def record_installer_jre(installation: Path, recorded: str) -> Path:
    configured = installation / ".install4j" / "inst_jre.cfg"
    configured.parent.mkdir(parents=True, exist_ok=True)
    configured.write_text(recorded + "\n", encoding="utf-8")
    return configured


def test_dangling_installer_jre_is_repaired_to_the_bundled_jre(tmp_path):
    """The 1050 layout: a JRE inside the installation, nothing shared to fall back to."""
    installation = gateway_installation(tmp_path, "1050")
    record_installer_jre(installation, "/tmp/ibgateway.sh.22.dir/jre")
    bundled = install4j_jre(installation / "jre")

    resolved = repair_install4j_jre(installation, tmp_path / "absent-i4j-jres")

    assert resolved == bundled
    recorded = (installation / ".install4j" / "inst_jre.cfg").read_text(encoding="utf-8").strip()
    assert recorded == str(bundled)


def test_dangling_installer_jre_falls_back_to_the_shared_jre_root(tmp_path):
    """The 1045 layout: no JRE in the installation, one registered under /opt/i4j_jres."""
    installation = gateway_installation(tmp_path, "1045")
    record_installer_jre(installation, "/tmp/ibgateway.sh.22.dir/jre")
    shared_root = tmp_path / "i4j_jres"
    shared = install4j_jre(shared_root / "Oda-jK0QgTEmVssfllLP")

    assert repair_install4j_jre(installation, shared_root) == shared


def test_a_valid_recorded_jre_is_kept(tmp_path):
    installation = gateway_installation(tmp_path, "1050")
    existing = install4j_jre(tmp_path / "already-good-jre")
    record_installer_jre(installation, str(existing))
    install4j_jre(installation / "jre")

    assert repair_install4j_jre(installation, tmp_path / "absent-i4j-jres") == existing


def test_installation_without_any_usable_jre_fails_the_build(tmp_path):
    installation = gateway_installation(tmp_path, "1050")
    record_installer_jre(installation, "/tmp/ibgateway.sh.22.dir/jre")

    with pytest.raises(GatewayVersionError, match="no usable JRE was found"):
        repair_install4j_jre(installation, tmp_path / "absent-i4j-jres")


def test_install_repairs_the_jre_and_verify_rejects_a_broken_one(tmp_path):
    install_root = tmp_path / "opt-ibgateway"
    installation = gateway_installation(install_root, "1050")
    record_installer_jre(installation, "/tmp/ibgateway.sh.22.dir/jre")
    install4j_jre(installation / "jre")
    tws_root = tmp_path / "opt-Jts"
    version_file = install_root / ".tws-major-version"
    shared_root = tmp_path / "absent-i4j-jres"

    assert configure_ibc_layout(install_root, tws_root, version_file, shared_root) == "1050"
    assert verify_ibc_layout(install_root, tws_root, version_file, "1050", shared_root) == "1050"

    record_installer_jre(installation, "/tmp/ibgateway.sh.22.dir/jre")
    (installation / "jre" / "bin" / "java").unlink()
    with pytest.raises(GatewayVersionError, match="no usable JRE was found"):
        verify_ibc_layout(install_root, tws_root, version_file, "1050", shared_root)
