from .base import (  # noqa: F401
    ChatResult, LlmBudgetExceeded, LlmCircuitOpen, LlmClient, LlmError, LlmMalformedResponse, LlmResult,
    LlmTimeout, LlmUsage, validate_against_schema,
)


def get_client():
    """Return the configured client. 'qch' needs QCH_API_KEY and QCH_BASE_URL; 'fake' is for tests only."""
    from apps.intelligence import conf

    name = conf.provider()
    if name == "qch":
        from .qch import QchClient

        return QchClient()
    if name == "fake":
        from .fake import FakeLLMClient

        return FakeLLMClient()
    raise LlmError(f"Unknown INTELLIGENCE_LLM_PROVIDER '{name}'")
