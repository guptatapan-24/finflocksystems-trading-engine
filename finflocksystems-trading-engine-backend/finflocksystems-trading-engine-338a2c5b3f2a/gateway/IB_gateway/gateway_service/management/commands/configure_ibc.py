import os
import tempfile
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from gateway_service.modes import normalize_trading_mode
from runtime_config import RESTART_TIME


DEFAULT_IBC_CONFIG_PATH = Path("/home/ibgateway/ibc/config.ini")


def _positive_integer(name, default):
    try:
        value = int(str(os.getenv(name, default)).strip())
    except ValueError as exc:
        raise CommandError(f"Invalid configuration variable: {name}") from exc
    if value < 1:
        raise CommandError(f"Invalid configuration variable: {name}")
    return value


def _credential(name):
    value = os.getenv(name, "")
    if not value or "\n" in value or "\r" in value:
        raise CommandError(f"Invalid configuration variable: {name}")
    return value


def _ibgateway_identity(target):
    if os.name != "posix":
        return None
    try:
        import pwd

        user = pwd.getpwnam("ibgateway")
    except KeyError as exc:
        if target == DEFAULT_IBC_CONFIG_PATH:
            raise CommandError("The ibgateway runtime user is unavailable") from exc
        return None
    return user.pw_uid, user.pw_gid


def write_ibc_configuration(target, content):
    target.parent.mkdir(parents=True, exist_ok=True)
    identity = _ibgateway_identity(target)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".config.ini.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        temporary.chmod(0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if identity is not None:
            os.chown(temporary, *identity)
        os.replace(temporary, target)
        target.chmod(0o600)
        if identity is not None:
            os.chown(target, *identity)
    finally:
        temporary.unlink(missing_ok=True)


class Command(BaseCommand):
    help = "Write the runtime-only IBC configuration from environment secrets"

    def handle(self, *args, **options):
        target = Path(os.getenv("IBC_CONFIG_PATH", str(DEFAULT_IBC_CONFIG_PATH)))
        try:
            mode = normalize_trading_mode(os.getenv("IBC_TRADING_MODE", ""))
        except ValueError as exc:
            raise CommandError("Invalid configuration variable: IBC_TRADING_MODE") from exc
        restart_time = str(os.getenv("IBC_AUTO_RESTART_TIME", "11:45 PM")).strip().upper()
        if not RESTART_TIME.fullmatch(restart_time):
            raise CommandError("Invalid configuration variable: IBC_AUTO_RESTART_TIME")
        content = "\n".join(
            [
                f"IbLoginId={_credential('IB_USERNAME')}",
                f"IbPassword={_credential('IB_PASSWORD')}",
                f"TradingMode={mode}",
                "AcceptNonBrokerageAccountWarning=yes",
                "ReadOnlyApi=no",
                "ExistingSessionDetectedAction=primary",
                "AcceptIncomingConnectionAction=accept",
                "ReloginAfterSecondFactorAuthenticationTimeout=yes",
                f"SecondFactorAuthenticationTimeout={_positive_integer('IBC_2FA_TIMEOUT', '180')}",
                f"AutoRestartTime={restart_time}",
            ]
        ) + "\n"
        write_ibc_configuration(target, content)
        self.stdout.write("IBC runtime configuration created")
