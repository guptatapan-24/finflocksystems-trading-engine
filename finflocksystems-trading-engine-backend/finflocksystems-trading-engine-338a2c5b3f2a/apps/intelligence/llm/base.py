"""Provider-agnostic LLM interface. The LLM never computes numbers (R3): callers validate its JSON."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class LlmError(Exception):
    """Any provider failure. Always caught by callers: intelligence fails open (R2)."""


class LlmTimeout(LlmError):
    pass


class LlmMalformedResponse(LlmError):
    """Response was not parseable JSON. `usage`/`cost_usd` carry what the call cost, so a failed call is still accounted."""

    def __init__(self, message, raw_text="", usage=None, cost_usd=None):
        super().__init__(message)
        self.raw_text = raw_text
        self.usage = usage
        self.cost_usd = cost_usd


class LlmCircuitOpen(LlmError):
    pass


class LlmBudgetExceeded(LlmError):
    pass


@dataclass
class LlmUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0


@dataclass
class LlmResult:
    parsed: object
    raw_text: str
    usage: LlmUsage = field(default_factory=LlmUsage)
    model: str = ""
    latency_ms: int = 0
    cost_usd: object = None  # provider-reported cost (Decimal) when the gateway returns one; None = compute from prices


@dataclass
class ChatResult:
    """One assistant turn from `LlmClient.chat`: text and/or tool calls (each {id, name, arguments: dict})."""
    content: str = ""
    tool_calls: list = field(default_factory=list)
    finish_reason: str = ""
    usage: LlmUsage = field(default_factory=LlmUsage)
    model: str = ""
    latency_ms: int = 0
    cost_usd: object = None


class LlmClient(ABC):
    provider = ""

    @abstractmethod
    def generate_json(self, system, user, schema, model) -> LlmResult:
        """Return parsed JSON for `user` under `system`. Raises LlmError subclasses."""

    def chat(self, messages, model, tools=None) -> ChatResult:
        """Tool-calling turn (chat copilot, Phase 12). Optional: providers that cannot do it raise LlmError."""
        raise LlmError(f"{self.provider or 'this provider'} does not support chat/tool calling")

    def stream(self, messages, model, tools=None):
        """Yield {"type": "text"|"tool_call"|"done", ...} events. Optional, like `chat`."""
        raise LlmError(f"{self.provider or 'this provider'} does not support streaming")


def validate_against_schema(parsed, schema):
    """Local re-validation (R11): the provider only supports a subset of JSON Schema."""
    from jsonschema import Draft202012Validator

    errors = sorted(Draft202012Validator(schema).iter_errors(parsed), key=lambda e: list(e.path))
    return [f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors]
