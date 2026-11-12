import os
import stat

import pytest
from django.core.management import call_command


def test_ibc_configuration_is_atomic_private_and_does_not_log_credentials(tmp_path, monkeypatch, capsys):
    target = tmp_path / "runtime" / "config.ini"
    target.parent.mkdir()
    target.write_text("stale-runtime-configuration\n", encoding="utf-8")
    monkeypatch.setenv("IBC_CONFIG_PATH", str(target))
    monkeypatch.setenv("IB_USERNAME", "sensitive-unit-user")
    monkeypatch.setenv("IB_PASSWORD", "sensitive-unit-password")
    monkeypatch.setenv("IBC_TRADING_MODE", "live")
    monkeypatch.setenv("IBC_2FA_TIMEOUT", "240")
    monkeypatch.setenv("IBC_AUTO_RESTART_TIME", "10:30 pm")

    call_command("configure_ibc")

    content = target.read_text(encoding="utf-8")
    output = capsys.readouterr()
    assert "stale-runtime-configuration" not in content
    assert "IbLoginId=sensitive-unit-user" in content
    assert "IbPassword=sensitive-unit-password" in content
    assert "TradingMode=live" in content
    assert "SecondFactorAuthenticationTimeout=240" in content
    assert "AutoRestartTime=10:30 PM" in content
    assert "sensitive-unit-user" not in output.out + output.err
    assert "sensitive-unit-password" not in output.out + output.err
    assert not list(target.parent.glob(".config.ini.*"))

    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_ibc_configuration_rejects_newline_credentials_without_logging_them(tmp_path, monkeypatch, capsys):
    target = tmp_path / "config.ini"
    monkeypatch.setenv("IBC_CONFIG_PATH", str(target))
    monkeypatch.setenv("IB_USERNAME", "user-with-newline\nsecret-suffix")
    monkeypatch.setenv("IB_PASSWORD", "password")
    monkeypatch.setenv("IBC_TRADING_MODE", "paper")

    with pytest.raises(Exception, match="IB_USERNAME"):
        call_command("configure_ibc")

    output = capsys.readouterr()
    assert "secret-suffix" not in output.out + output.err
    assert not target.exists()
