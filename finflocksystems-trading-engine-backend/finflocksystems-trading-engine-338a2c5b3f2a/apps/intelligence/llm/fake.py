"""Deterministic client for tests (R9). No network, ever."""
import copy
import json

from .base import LlmClient, LlmMalformedResponse, LlmResult, LlmTimeout, LlmUsage


class FakeLLMClient(LlmClient):
    provider = "fake"
    MODES = {"valid", "schema_invalid", "malformed", "hallucinated_number", "timeout"}

    def __init__(self, mode="valid", valid_response=None, mutate_number=None):
        if mode not in self.MODES:
            raise ValueError(f"Unknown fake mode {mode}")
        self.mode = mode
        self.valid_response = valid_response if valid_response is not None else {}
        # (key path list, replacement value) applied by the hallucinated_number mode
        self.mutate_number = mutate_number
        self.calls = []

    def generate_json(self, system, user, schema, model):
        self.calls.append({"system": system, "user": user, "schema": schema, "model": model})
        usage = LlmUsage(input_tokens=max(1, len(system + user) // 4), output_tokens=50)
        if self.mode == "timeout":
            raise LlmTimeout("fake timeout")
        if self.mode == "malformed":
            raise LlmMalformedResponse("not json", raw_text="{this is not json")
        if self.mode == "schema_invalid":
            payload = {"unexpected": True}
        elif self.mode == "hallucinated_number":
            payload = copy.deepcopy(self.valid_response)
            if self.mutate_number:
                path, replacement = self.mutate_number
                node = payload
                for key in path[:-1]:
                    node = node[key]
                node[path[-1]] = replacement
        else:
            payload = copy.deepcopy(self.valid_response)
        return LlmResult(parsed=payload, raw_text=json.dumps(payload), usage=usage,
                         model=model or "fake-model", latency_ms=1)
