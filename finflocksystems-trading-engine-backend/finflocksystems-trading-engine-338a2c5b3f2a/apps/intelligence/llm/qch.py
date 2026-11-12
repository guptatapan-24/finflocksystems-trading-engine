"""QCH gateway client (LiteLLM proxy in front of DeepSeek, OpenAI-compatible).

Behaviour verified against the live gateway on 2026-10-23 and recorded in CLAUDE.md R11:
* `response_format={"type":"json_schema"}` is refused (HTTP 400): the schema goes into the prompt, JSON mode is on, and
  callers re-validate every answer locally (R3).
* The model "thinks" first (`reasoning_content`); those tokens count inside `completion_tokens`, so `max_tokens` carries
  headroom, and an answer that ran out of tokens while thinking comes back with empty content (reported as malformed).
* The exact cost of a call is in the `x-litellm-response-cost` header.
The key is read from settings only, is never logged, and is scrubbed from every error message.
"""
import json
import random
import re
import time
from decimal import Decimal, InvalidOperation

from django.conf import settings

from .base import (
    ChatResult, LlmCircuitOpen, LlmClient, LlmError, LlmMalformedResponse, LlmResult, LlmTimeout, LlmUsage,
)
from .circuit import qch_breaker
from .limiter import ConcurrencyLimiter, LlmBusy

RETRYABLE_STATUS = {408, 409, 425, 500, 502, 503, 504}
MAX_RETRY_AFTER_SECONDS = 30.0
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _setting(name, default):
    return getattr(settings, name, default)


def _parse_json(text):
    cleaned = _FENCE.sub("", text or "").strip()
    return json.loads(cleaned)


def _cost(headers):
    raw = headers.get("x-litellm-response-cost")
    if raw in (None, ""):
        return None
    try:
        return Decimal(str(raw)).quantize(Decimal("0.000001"))
    except (InvalidOperation, ValueError):
        return None


def _usage(body):
    usage = (body or {}).get("usage") or {}
    reasoning = int(((usage.get("completion_tokens_details") or {}).get("reasoning_tokens")) or 0)
    completion = int(usage.get("completion_tokens") or 0)
    return LlmUsage(input_tokens=int(usage.get("prompt_tokens") or 0),
                    output_tokens=max(0, completion - reasoning), thinking_tokens=reasoning)


class QchClient(LlmClient):
    provider = "qch"

    def __init__(self, api_key=None, base_url=None, breaker=None, limiter=None, session=None, sleep=time.sleep):
        self.api_key = api_key if api_key is not None else _setting("QCH_API_KEY", "")
        if not self.api_key:
            raise LlmError("QCH_API_KEY is not configured")
        self.base_url = (base_url or _setting("QCH_BASE_URL", "")).rstrip("/")
        if not self.base_url:
            raise LlmError("QCH_BASE_URL is not configured")
        self.breaker = breaker or qch_breaker
        self.limiter = limiter or ConcurrencyLimiter("qch")
        self._session = session
        self._sleep = sleep

    # ---- plumbing -------------------------------------------------------------------------------
    def _http(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def _scrub(self, message):
        return str(message).replace(self.api_key, "***")[:300]

    def _timeouts(self, model=""):
        """The deep (pro) model thinks for minutes on a long evidence pack, so it gets its own read timeout."""
        from apps.intelligence import conf

        deep = bool(model) and model == conf.model_for("deep")
        read = _setting("QCH_TIMEOUT_DEEP_SECONDS", 300) if deep else _setting("INTELLIGENCE_LLM_TIMEOUT_SECONDS", 60)
        return (float(_setting("QCH_CONNECT_TIMEOUT_SECONDS", 5)), float(read))

    def _payload(self, model, messages, *, json_mode=False, tools=None, stream=False):
        thinking_off = str(_setting("QCH_THINKING", "")).strip().lower() == "disabled"
        headroom = 0 if thinking_off else int(_setting("QCH_REASONING_HEADROOM_TOKENS", 4096))
        payload = {"model": model, "messages": messages,
                   "max_tokens": int(_setting("INTELLIGENCE_MAX_OUTPUT_TOKENS", 4096)) + headroom}
        temperature = _setting("INTELLIGENCE_TEMPERATURE", None)
        if temperature is not None:
            payload["temperature"] = float(temperature)
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if tools:
            payload["tools"] = tools
        if thinking_off:
            payload["extra_body"] = {"thinking": {"type": "disabled"}}
        if stream:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
        return payload

    def _error_message(self, response):
        try:
            detail = (response.json().get("error") or {}).get("message") or response.text
        except Exception:
            detail = response.text
        return self._scrub(f"HTTP {response.status_code}: {detail}")

    def _post(self, payload, stream=False):
        """POST with retries on 429 / 5xx / network errors. Returns the response (open, when streaming)."""
        import requests

        if not self.breaker.allow():
            raise LlmCircuitOpen("QCH circuit breaker is open")
        attempts = max(1, int(_setting("INTELLIGENCE_LLM_MAX_ATTEMPTS", 3)))
        base_delay = float(_setting("INTELLIGENCE_LLM_RETRY_BASE_SECONDS", 1.0))
        last = None
        for attempt in range(attempts):
            delay = base_delay * (2 ** attempt) + random.uniform(0, base_delay / 4)
            try:
                with self.limiter.slot():
                    response = self._http().post(
                        f"{self.base_url}/v1/chat/completions", json=payload, stream=stream,
                        headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                        timeout=self._timeouts(payload.get("model", "")))
            except LlmBusy:
                raise
            except (requests.Timeout, requests.ConnectionError) as exc:
                self.breaker.record_failure()
                last = LlmTimeout(self._scrub(exc)) if isinstance(exc, requests.Timeout) else LlmError(self._scrub(exc))
            else:
                status = response.status_code
                if status == 200:
                    self.breaker.record_success()
                    return response
                message = self._error_message(response)
                if status == 429:  # rate limited: not an outage, so the breaker is not touched
                    retry_after = response.headers.get("Retry-After", "")
                    try:
                        delay = min(float(retry_after), MAX_RETRY_AFTER_SECONDS)
                    except ValueError:
                        pass
                    last = LlmError(message)
                elif status in RETRYABLE_STATUS:
                    self.breaker.record_failure()
                    last = LlmError(message)
                else:  # 400/401/403/404: our request or key is wrong; retrying cannot help
                    raise LlmError(message)
            if attempt + 1 < attempts:
                self._sleep(delay)
        raise last

    # ---- JSON generation ------------------------------------------------------------------------
    def generate_json(self, system, user, schema, model):
        if not model:
            raise LlmError("No model id configured (set INTELLIGENCE_MODEL_REVIEW / _DEEP)")
        instructions = (f"{system}\n\nReturn ONLY one JSON object, no prose and no code fences, that validates against "
                        f"this JSON Schema:\n{json.dumps(schema, separators=(',', ':'), sort_keys=True)}")
        started = time.monotonic()
        payload = self._payload(
            model, [{"role": "system", "content": instructions}, {"role": "user", "content": user}], json_mode=True)
        ceiling = int(_setting("QCH_MAX_TOKENS_CEILING", 32768))
        spent_usage, spent_cost = LlmUsage(), Decimal("0")
        while True:
            response = self._post(payload)
            try:
                body = response.json()
                choice = body["choices"][0]
                text = (choice.get("message") or {}).get("content") or ""
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise LlmMalformedResponse("QCH returned an unexpected body", raw_text=self._scrub(response.text),
                                           usage=spent_usage, cost_usd=spent_cost) from exc
            usage, cost = _usage(body), _cost(response.headers)
            spent_usage = LlmUsage(spent_usage.input_tokens + usage.input_tokens, spent_usage.output_tokens + usage.output_tokens,
                                   spent_usage.thinking_tokens + usage.thinking_tokens)
            spent_cost += cost or Decimal("0")
            if choice.get("finish_reason") == "length":
                # The model ran out of tokens, mostly while thinking: one retry with double the budget, then give up.
                if payload["max_tokens"] * 2 <= ceiling:
                    payload = {**payload, "max_tokens": payload["max_tokens"] * 2}
                    continue
                raise LlmMalformedResponse(f"QCH answer was cut off at {payload['max_tokens']} tokens (finish_reason=length)",
                                           raw_text=text, usage=spent_usage, cost_usd=spent_cost)
            break
        if not text.strip():
            raise LlmMalformedResponse(f"QCH returned no content (finish_reason={choice.get('finish_reason')})", raw_text="",
                                       usage=spent_usage, cost_usd=spent_cost)
        try:
            parsed = _parse_json(text)
        except ValueError as exc:
            raise LlmMalformedResponse("QCH returned non-JSON text", raw_text=text, usage=spent_usage,
                                       cost_usd=spent_cost) from exc
        return LlmResult(parsed=parsed, raw_text=text, usage=spent_usage, model=body.get("model") or model,
                         latency_ms=int((time.monotonic() - started) * 1000),
                         cost_usd=spent_cost if cost is not None else None)

    # ---- chat with tools (Phase 12) -------------------------------------------------------------
    def chat(self, messages, model, tools=None):
        if not model:
            raise LlmError("No model id configured")
        started = time.monotonic()
        response = self._post(self._payload(model, messages, tools=tools))
        try:
            body = response.json()
            choice = body["choices"][0]
            message = choice.get("message") or {}
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LlmMalformedResponse("QCH returned an unexpected body", raw_text=self._scrub(response.text)) from exc
        calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except ValueError as exc:
                raise LlmMalformedResponse("QCH tool call arguments were not JSON",
                                           raw_text=str(function.get("arguments"))[:500]) from exc
            calls.append({"id": call.get("id", ""), "name": function.get("name", ""), "arguments": arguments})
        return ChatResult(content=message.get("content") or "", tool_calls=calls, finish_reason=choice.get("finish_reason") or "",
                          usage=_usage(body), model=body.get("model") or model,
                          latency_ms=int((time.monotonic() - started) * 1000), cost_usd=_cost(response.headers))

    def stream(self, messages, model, tools=None):
        """Yield {"type":"text","text"}, {"type":"tool_call",...} (complete, at the end) and {"type":"done",...}.
        `reasoning_content` deltas are never yielded."""
        if not model:
            raise LlmError("No model id configured")
        response = self._post(self._payload(model, messages, tools=tools, stream=True), stream=True)
        pending, usage, finish = {}, LlmUsage(), ""
        try:
            for raw in response.iter_lines(decode_unicode=True):
                if not raw or not raw.startswith("data:"):
                    continue
                data = raw[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if chunk.get("usage"):
                    usage = _usage(chunk)
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        yield {"type": "text", "text": delta["content"]}
                    for call in delta.get("tool_calls") or []:
                        slot = pending.setdefault(call.get("index", 0), {"id": "", "name": "", "arguments": ""})
                        slot["id"] = call.get("id") or slot["id"]
                        function = call.get("function") or {}
                        slot["name"] = function.get("name") or slot["name"]
                        slot["arguments"] += function.get("arguments") or ""
                    finish = choice.get("finish_reason") or finish
        except Exception as exc:  # connection dropped mid-stream
            raise LlmError(self._scrub(f"stream interrupted: {exc}")) from exc
        finally:
            response.close()
        for _, call in sorted(pending.items()):
            try:
                arguments = json.loads(call["arguments"] or "{}")
            except ValueError as exc:
                raise LlmMalformedResponse("QCH streamed tool call arguments were not JSON", raw_text=call["arguments"][:500]) from exc
            yield {"type": "tool_call", "id": call["id"], "name": call["name"], "arguments": arguments}
        yield {"type": "done", "finish_reason": finish, "usage": usage, "model": model}
