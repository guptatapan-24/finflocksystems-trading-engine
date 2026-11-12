import os
import re

from django.conf import settings


QCH_RUNTIME_VARIABLES = ("QCH_APP_ID", "QCH_API_HOST", "QCH_SERVICE_TOKEN")
DOCKER_HUB_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
DOCKER_HUB_IMAGE_RE = re.compile(
    rf"^docker\.io/(?P<namespace>{DOCKER_HUB_COMPONENT})/"
    rf"(?P<repository>{DOCKER_HUB_COMPONENT})"
    r"(?:(?:@sha256:(?P<digest>[0-9a-fA-F]{64}))|"
    r"(?::(?P<tag>[A-Za-z0-9_][A-Za-z0-9._-]{0,127})))$"
)
PLACEHOLDER_RE = re.compile(r"(?:^|[._-])(?:replace|placeholder)(?:[._-]|$)", re.IGNORECASE)
EXAMPLE_ENCRYPTION_KEYS = {"replace-with-a-long-random-encryption-key"}


class GatewayImageConfigurationError(ValueError):
    pass


def parse_docker_hub_image_reference(value):
    """Validate and normalize one explicit Docker Hub image reference."""
    image = str(value or "")
    if not image or image != image.strip() or re.search(r"\s", image):
        raise GatewayImageConfigurationError("IBKR_GATEWAY_IMAGE is not a valid Docker Hub image reference")
    match = DOCKER_HUB_IMAGE_RE.fullmatch(image)
    if match is None:
        raise GatewayImageConfigurationError("IBKR_GATEWAY_IMAGE is not a valid Docker Hub image reference")
    tag = match.group("tag")
    namespace = match.group("namespace")
    repository = match.group("repository")
    if len(f"docker.io/{namespace}/{repository}") > 255:
        raise GatewayImageConfigurationError("IBKR_GATEWAY_IMAGE repository name is too long")
    if PLACEHOLDER_RE.search(namespace) or PLACEHOLDER_RE.search(repository):
        raise GatewayImageConfigurationError("IBKR_GATEWAY_IMAGE contains a placeholder value")
    if tag and (tag.casefold() == "latest" or PLACEHOLDER_RE.search(tag)):
        raise GatewayImageConfigurationError("IBKR_GATEWAY_IMAGE must use a fixed non-latest version tag")
    if match.group("digest"):
        return f"docker.io/{namespace}/{repository}@sha256:{match.group('digest')}"
    return f"docker.io/{namespace}/{repository}:{tag}"


def configured_gateway_image():
    """Resolve the configured child image without exposing it in status output."""
    configured = str(_effective_value("IBKR_GATEWAY_IMAGE") or "")
    if "\r" in configured or "\n" in configured:
        raise GatewayImageConfigurationError("IBKR_GATEWAY_IMAGE contains an invalid line break")
    return parse_docker_hub_image_reference(configured)


def _effective_value(name):
    if name in QCH_RUNTIME_VARIABLES and name in os.environ:
        return os.environ[name]
    return getattr(settings, name, "")


def managed_broker_deployment_configuration():
    """Return non-secret availability metadata for managed IB Gateway sessions."""
    required = {
        "BROKER_SESSION_ENCRYPTION_KEY": _effective_value("BROKER_SESSION_ENCRYPTION_KEY"),
        "IBKR_GATEWAY_IMAGE": _effective_value("IBKR_GATEWAY_IMAGE"),
        "QCH_APP_ID": _effective_value("QCH_APP_ID"),
        "QCH_API_HOST": _effective_value("QCH_API_HOST"),
        "QCH_SERVICE_TOKEN": _effective_value("QCH_SERVICE_TOKEN"),
    }
    missing = sorted(name for name, value in required.items() if not str(value or "").strip())
    invalid = []
    if str(required["IBKR_GATEWAY_IMAGE"] or "").strip():
        try:
            configured_gateway_image()
        except GatewayImageConfigurationError:
            invalid.append("IBKR_GATEWAY_IMAGE")
    encryption_key = str(required["BROKER_SESSION_ENCRYPTION_KEY"] or "").strip().casefold()
    if encryption_key in EXAMPLE_ENCRYPTION_KEYS:
        invalid.append("BROKER_SESSION_ENCRYPTION_KEY")
    invalid.sort()
    available = not missing and not invalid
    return {
        "available": available,
        # Retain the established field for existing API clients.
        "ready": available,
        "missing": missing,
        "invalid": invalid,
    }


class ManagedBrokerGatewayUnavailable(RuntimeError):
    def __init__(self, configuration=None):
        self.configuration = configuration or managed_broker_deployment_configuration()
        super().__init__(
            "Managed IB Gateway is unavailable because QCH configuration is incomplete."
        )


def require_managed_broker_deployment():
    configuration = managed_broker_deployment_configuration()
    if not configuration["available"]:
        raise ManagedBrokerGatewayUnavailable(configuration)
    return configuration


def managed_broker_unavailable_error(configuration=None):
    configuration = configuration or managed_broker_deployment_configuration()
    return {
        "code": "BROKER_GATEWAY_NOT_CONFIGURED",
        "message": "Managed IB Gateway is unavailable because QCH configuration is incomplete.",
        "details": {
            "missing": configuration["missing"],
            "invalid": configuration["invalid"],
        },
    }


def managed_broker_disabled_task_result(configuration=None):
    configuration = configuration or managed_broker_deployment_configuration()
    return {
        "status": "disabled",
        "reason": "BROKER_GATEWAY_NOT_CONFIGURED",
        "broker_deployment": configuration,
    }
