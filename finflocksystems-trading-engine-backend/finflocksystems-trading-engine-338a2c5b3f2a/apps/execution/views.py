import logging

from apps.core.views import method_guard, response

from .flink_diagnostics import collect_flink_diagnostics
from .readiness import collect_execution_readiness


LOGGER = logging.getLogger(__name__)


def readiness(request):
    invalid = method_guard(request, "GET")
    if invalid:
        return invalid
    try:
        result = collect_execution_readiness()
        return response(result, status=200 if result["ready"] else 503)
    except Exception as exc:
        return response(
            status=503,
            error={
                "code": "EXECUTION_READINESS_FAILED",
                "message": str(exc),
                "details": {},
            },
        )


def diagnostics(request):
    """Operator diagnostics are inspectable even while execution is not ready."""
    invalid = method_guard(request, "GET")
    if invalid:
        return invalid
    try:
        return response(collect_execution_readiness())
    except Exception as exc:
        return response(
            {
                "ready": False,
                "automatic_execution_ready": False,
                "status": "NOT_READY",
                "observed_at": None,
                "blockers": [
                    {
                        "code": "EXECUTION_DIAGNOSTICS_FAILED",
                        "message": str(exc),
                        "details": {},
                    }
                ],
                "signals": {},
            }
        )


def flink_diagnostics(request):
    """Inspect internal Flink state without exposing the internal REST service."""
    invalid = method_guard(request, "GET")
    if invalid:
        return invalid
    try:
        return response(collect_flink_diagnostics())
    except Exception:
        LOGGER.exception("Unexpected failure while collecting Flink diagnostics")
        return response(
            status=503,
            error={
                "code": "FLINK_DIAGNOSTICS_FAILED",
                "message": "Flink diagnostics could not be collected safely.",
                "details": {},
            },
        )
