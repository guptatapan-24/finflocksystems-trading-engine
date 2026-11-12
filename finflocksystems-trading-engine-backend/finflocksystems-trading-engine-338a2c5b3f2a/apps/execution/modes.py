from django.conf import settings
from django.db import models


class ExecutionMode(models.TextChoices):
    PAPER = "PAPER", "Paper"
    LIVE = "LIVE", "Live"


class RunType(models.TextChoices):
    PREVIEW = "PREVIEW", "Preview"
    EXECUTION = "EXECUTION", "Execution"


class ExecutionModeError(ValueError):
    pass


class LiveTradingDisabled(ExecutionModeError):
    pass


def normalize_execution_mode(value):
    mode = str(value or "").strip().upper()
    if mode not in ExecutionMode.values:
        raise ExecutionModeError("Execution mode must be exactly PAPER or LIVE")
    return mode


def normalize_run_type(value):
    run_type = str(value or "").strip().upper()
    if run_type not in RunType.values:
        raise ExecutionModeError("Run type must be exactly PREVIEW or EXECUTION")
    return run_type


def execution_mode_for_gateway_mode(value):
    gateway_mode = str(value or "").strip().lower()
    if gateway_mode == "paper":
        return ExecutionMode.PAPER
    if gateway_mode == "live":
        return ExecutionMode.LIVE
    raise ExecutionModeError("Gateway mode must be exactly paper or live")


def gateway_mode_for_execution_mode(value):
    return normalize_execution_mode(value).lower()


def normalize_gateway_mode(value):
    return gateway_mode_for_execution_mode(execution_mode_for_gateway_mode(value))


def execution_mode_for_session(session):
    if session is None:
        raise ExecutionModeError("A BrokerGatewaySession is required to derive execution mode")
    return execution_mode_for_gateway_mode(session.mode)


def execution_mode_for_portfolio(portfolio, *, require_session=True):
    session = getattr(portfolio, "gateway_session", None)
    if session is None:
        if require_session:
            raise ExecutionModeError(
                "Portfolio must be assigned to a BrokerGatewaySession before execution"
            )
        return ExecutionMode.PAPER
    return execution_mode_for_session(session)


def require_portfolio_execution_mode(portfolio, requested=None, *, require_session=True):
    derived = execution_mode_for_portfolio(portfolio, require_session=require_session)
    if requested is not None and normalize_execution_mode(requested) != derived:
        raise ExecutionModeError(
            f"Execution mode must match the portfolio Gateway session ({derived})"
        )
    return derived


def require_live_trading_allowed(mode):
    normalized = normalize_execution_mode(mode)
    if normalized == ExecutionMode.LIVE and not settings.ALLOW_LIVE_TRADING:
        raise LiveTradingDisabled(
            "Live trading is disabled by ALLOW_LIVE_TRADING"
        )
    return normalized


def require_portfolio_execution_ready(portfolio):
    mode = execution_mode_for_portfolio(portfolio)
    session = getattr(portfolio, "gateway_session", None)
    state = session.last_gateway_state or {}
    health_mode = execution_mode_for_gateway_mode(state.get("mode"))
    if health_mode != mode:
        raise ExecutionModeError(
            "Gateway health mode does not match the portfolio Gateway session"
        )
    if (
        session.status != session.Status.CONNECTED
        or not session.commands_enabled
        or not state.get("connected")
    ):
        raise ExecutionModeError(
            "Portfolio Gateway session is not connected and command-ready"
        )
    if not state.get("reconciled") or not portfolio.account.is_reconciled:
        raise ExecutionModeError(
            "Portfolio account and Gateway session must be reconciled before execution"
        )
    return require_live_trading_allowed(mode)


def require_execution_mode_chain(
    *,
    intent_mode,
    command_mode,
    session_mode,
    health_mode,
):
    expected = normalize_execution_mode(intent_mode)
    actual = {
        "command": normalize_execution_mode(command_mode),
        "gateway session": execution_mode_for_gateway_mode(session_mode),
        "gateway health": execution_mode_for_gateway_mode(health_mode),
    }
    mismatches = [
        f"{label}={mode}"
        for label, mode in actual.items()
        if mode != expected
    ]
    if mismatches:
        raise ExecutionModeError(
            "Execution mode mismatch: "
            f"intent={expected}; " + "; ".join(mismatches)
        )
    return expected
