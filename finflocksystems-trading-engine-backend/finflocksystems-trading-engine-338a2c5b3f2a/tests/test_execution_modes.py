import pytest

from apps.execution.modes import (
    ExecutionModeError,
    normalize_execution_mode,
    require_execution_mode_chain,
)


@pytest.mark.parametrize("invalid_mode", ["DEMO", "ARCHIVE", "", None])
def test_noncanonical_execution_modes_are_rejected(invalid_mode):
    with pytest.raises(ExecutionModeError):
        normalize_execution_mode(invalid_mode)


def test_complete_execution_mode_chain_accepts_matching_live_values():
    assert require_execution_mode_chain(
        intent_mode="LIVE",
        command_mode="LIVE",
        session_mode="live",
        health_mode="live",
    ) == "LIVE"


@pytest.mark.parametrize(
    ("command_mode", "session_mode", "health_mode"),
    [
        ("LIVE", "paper", "paper"),
        ("PAPER", "live", "paper"),
        ("PAPER", "paper", "live"),
    ],
)
def test_execution_mode_chain_rejects_every_mismatch(
    command_mode, session_mode, health_mode
):
    with pytest.raises(ExecutionModeError, match="Execution mode mismatch"):
        require_execution_mode_chain(
            intent_mode="PAPER",
            command_mode=command_mode,
            session_mode=session_mode,
            health_mode=health_mode,
        )
