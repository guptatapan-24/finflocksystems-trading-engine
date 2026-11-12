"""Phase 7: QCH gateway client, concurrency limiter, provider wiring. HTTP is mocked with `responses` (R9: no network);
the response shapes are the ones observed on the live gateway on 2026-10-23."""
import json
import logging
from decimal import Decimal

import pytest
import responses
from django.test import Client

from apps.intelligence import conf
from apps.intelligence.llm import LlmError, get_client
from apps.intelligence.llm.base import LlmCircuitOpen, LlmMalformedResponse, LlmTimeout
from apps.intelligence.llm.circuit import CircuitBreaker, breaker_for, qch_breaker
from apps.intelligence.llm.limiter import ConcurrencyLimiter, LlmBusy
from apps.intelligence.llm.qch import QchClient
from apps.intelligence.llm.runner import run_llm
from apps.intelligence.models import LlmRunLog

pytestmark = pytest.mark.django_db

BASE = "https://qch.test/qch-ai"
URL = f"{BASE}/v1/chat/completions"
KEY = "sk-secret-test-key-123"
SCHEMA = {"type": "object", "required": ["verdict"], "properties": {"verdict": {"type": "string"}}}


def completion(content, *, reasoning=40, completion_tokens=60, finish="stop", tool_calls=None):
    message = {"role": "assistant", "content": content, "reasoning_content": "thinking..."}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"id": "x", "model": "deepseek-v4-flash", "choices": [{"index": 0, "finish_reason": finish, "message": message}],
            "usage": {"prompt_tokens": 100, "completion_tokens": completion_tokens,
                      "completion_tokens_details": {"reasoning_tokens": reasoning}}}


class FakeRedis:
    """Just enough of redis-py for the limiter: runs the semantics of the Lua script in Python."""

    def __init__(self):
        self.members = {}

    def eval(self, script, numkeys, key, now, lease, maximum, token):
        live = {t: exp for t, exp in self.members.items() if exp > float(now)}
        self.members = live
        if len(live) < int(maximum):
            self.members[token] = float(now) + float(lease)
            return 1
        return 0

    def zrem(self, key, token):
        self.members.pop(token, None)


@pytest.fixture
def client(settings):
    settings.QCH_BASE_URL = BASE
    settings.INTELLIGENCE_LLM_MAX_ATTEMPTS = 3
    settings.INTELLIGENCE_LLM_RETRY_BASE_SECONDS = 0
    slept = []
    limiter = ConcurrencyLimiter("test", max_concurrency=2, redis_client=FakeRedis())
    c = QchClient(api_key=KEY, breaker=CircuitBreaker(), limiter=limiter, sleep=slept.append)
    c.slept = slept
    return c


# ---- generate_json -----------------------------------------------------------------------------
@responses.activate
def test_generate_json_parses_usage_and_gateway_cost(client):
    responses.post(URL, json=completion('{"verdict":"ok"}'), headers={"x-litellm-response-cost": "0.000123"})
    result = client.generate_json("system", "user", SCHEMA, "deepseek-v4-flash")
    assert result.parsed == {"verdict": "ok"}
    assert result.cost_usd == Decimal("0.000123")
    # reasoning tokens are carried inside completion_tokens: output excludes them, thinking holds them
    assert (result.usage.input_tokens, result.usage.output_tokens, result.usage.thinking_tokens) == (100, 20, 40)
    sent = json.loads(responses.calls[0].request.body)
    assert sent["response_format"] == {"type": "json_object"}  # json_schema is refused by the gateway
    assert "JSON Schema" in sent["messages"][0]["content"] and '"verdict"' in sent["messages"][0]["content"]
    assert sent["max_tokens"] == 4096 + 4096  # output cap plus thinking headroom
    assert responses.calls[0].request.headers["Authorization"] == f"Bearer {KEY}"


@responses.activate
def test_code_fenced_json_is_accepted(client):
    responses.post(URL, json=completion('```json\n{"verdict":"ok"}\n```'))
    assert client.generate_json("s", "u", SCHEMA, "m").parsed == {"verdict": "ok"}


@responses.activate
def test_thinking_disabled_sets_extra_body_and_drops_headroom(client, settings):
    settings.QCH_THINKING = "disabled"
    responses.post(URL, json=completion('{"verdict":"ok"}', reasoning=0))
    client.generate_json("s", "u", SCHEMA, "m")
    sent = json.loads(responses.calls[0].request.body)
    assert sent["extra_body"] == {"thinking": {"type": "disabled"}}
    assert sent["max_tokens"] == 4096


@responses.activate
def test_answer_cut_off_by_the_token_limit_is_retried_with_a_doubled_budget(client):
    responses.post(URL, json=completion('{"verdict":"par', reasoning=8000, completion_tokens=8100, finish="length"),
                   headers={"x-litellm-response-cost": "0.01"})
    responses.post(URL, json=completion('{"verdict":"ok"}', reasoning=9000, completion_tokens=9020),
                   headers={"x-litellm-response-cost": "0.02"})
    result = client.generate_json("s", "u", SCHEMA, "m")
    assert result.parsed == {"verdict": "ok"}
    assert [json.loads(c.request.body)["max_tokens"] for c in responses.calls] == [8192, 16384]
    # both calls are paid for, and both are accounted
    assert result.cost_usd == Decimal("0.03") and result.usage.thinking_tokens == 17000


@responses.activate
def test_persistently_cut_off_answer_gives_up_at_the_ceiling_and_reports_the_spend(client):
    responses.post(URL, json=completion("", reasoning=40, completion_tokens=40, finish="length"),
                   headers={"x-litellm-response-cost": "0.01"})
    with pytest.raises(LlmMalformedResponse) as info:
        client.generate_json("s", "u", SCHEMA, "m")
    assert "finish_reason=length" in str(info.value)
    assert [json.loads(c.request.body)["max_tokens"] for c in responses.calls] == [8192, 16384, 32768]
    assert info.value.cost_usd == Decimal("0.03") and info.value.usage.thinking_tokens == 120


@responses.activate
def test_runner_records_tokens_and_cost_of_a_call_that_returned_garbage(client):
    responses.post(URL, json=completion("not json at all", reasoning=10, completion_tokens=30),
                   headers={"x-litellm-response-cost": "0.005"})
    row, _ = run_llm(client=client, scope="TRADE", idempotency_key="q3", system="s", user="u", schema=SCHEMA, model="m")
    assert row.status == LlmRunLog.Status.SCHEMA_INVALID and row.validation_errors == ["MALFORMED_JSON"]
    assert row.cost_usd == Decimal("0.005000") and row.thinking_tokens == 10 and row.input_tokens == 100


@responses.activate
def test_non_json_text_is_malformed_and_keeps_raw_text(client):
    responses.post(URL, json=completion("sorry, I cannot"))
    with pytest.raises(LlmMalformedResponse) as info:
        client.generate_json("s", "u", SCHEMA, "m")
    assert info.value.raw_text == "sorry, I cannot"


def test_missing_key_or_model_is_an_error(settings):
    settings.QCH_BASE_URL = BASE
    with pytest.raises(LlmError):
        QchClient(api_key="")
    c = QchClient(api_key=KEY, limiter=ConcurrencyLimiter("t", redis_client=FakeRedis()))
    with pytest.raises(LlmError):
        c.generate_json("s", "u", SCHEMA, "")


# ---- retries and errors ------------------------------------------------------------------------
@responses.activate
def test_429_is_retried_with_retry_after_and_does_not_trip_the_breaker(client):
    responses.post(URL, json={"error": {"message": "slow down"}}, status=429, headers={"Retry-After": "2"})
    responses.post(URL, json=completion('{"verdict":"ok"}'))
    assert client.generate_json("s", "u", SCHEMA, "m").parsed == {"verdict": "ok"}
    assert client.slept == [2.0]
    assert client.breaker.state == "CLOSED"


@responses.activate
def test_5xx_is_retried_then_succeeds(client):
    responses.post(URL, json={"error": {"message": "bad gateway"}}, status=502)
    responses.post(URL, json=completion('{"verdict":"ok"}'))
    assert client.generate_json("s", "u", SCHEMA, "m").parsed == {"verdict": "ok"}
    assert len(responses.calls) == 2


@responses.activate
def test_4xx_is_not_retried_and_the_key_never_appears_in_the_error(client):
    responses.post(URL, json={"error": {"message": f"Invalid token {KEY} rejected", "code": "401"}}, status=401)
    with pytest.raises(LlmError) as info:
        client.generate_json("s", "u", SCHEMA, "m")
    assert len(responses.calls) == 1
    assert KEY not in str(info.value) and "***" in str(info.value)
    assert client.breaker.state == "CLOSED"  # our key/request is wrong; that is not an outage


@responses.activate
def test_persistent_5xx_exhausts_attempts_and_opens_the_breaker(client, settings):
    settings.INTELLIGENCE_LLM_MAX_ATTEMPTS = 2
    client.breaker = CircuitBreaker(failure_threshold=2)
    responses.post(URL, json={"error": {"message": "down"}}, status=503)
    with pytest.raises(LlmError):
        client.generate_json("s", "u", SCHEMA, "m")
    assert client.breaker.state == "OPEN"
    with pytest.raises(LlmCircuitOpen):
        client.generate_json("s", "u", SCHEMA, "m")
    assert len(responses.calls) == 2  # the open breaker made no further call


@responses.activate
def test_timeout_maps_to_llm_timeout(client, settings):
    import requests

    settings.INTELLIGENCE_LLM_MAX_ATTEMPTS = 1
    responses.post(URL, body=requests.Timeout("read timed out"))
    with pytest.raises(LlmTimeout):
        client.generate_json("s", "u", SCHEMA, "m")


# ---- tool calling and streaming ----------------------------------------------------------------
@responses.activate
def test_chat_returns_parsed_tool_calls(client):
    calls = [{"index": 0, "id": "call_1", "type": "function",
              "function": {"name": "get_episode_report", "arguments": '{"episode_id": 42}'}}]
    responses.post(URL, json=completion("", finish="tool_calls", tool_calls=calls))
    tools = [{"type": "function", "function": {"name": "get_episode_report", "parameters": {"type": "object"}}}]
    result = client.chat([{"role": "user", "content": "report 42"}], "m", tools=tools)
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls == [{"id": "call_1", "name": "get_episode_report", "arguments": {"episode_id": 42}}]
    assert json.loads(responses.calls[0].request.body)["tools"] == tools


@responses.activate
def test_chat_rejects_tool_arguments_that_are_not_json(client):
    calls = [{"id": "c", "type": "function", "function": {"name": "t", "arguments": "{oops"}}]
    responses.post(URL, json=completion("", finish="tool_calls", tool_calls=calls))
    with pytest.raises(LlmMalformedResponse):
        client.chat([{"role": "user", "content": "x"}], "m")


@responses.activate
def test_stream_yields_text_assembles_tool_calls_and_hides_reasoning(client):
    def sse(payload):
        return f"data: {json.dumps(payload)}\n\n"

    body = (sse({"choices": [{"index": 0, "delta": {"reasoning_content": "secret thoughts"}}]})
            + sse({"choices": [{"index": 0, "delta": {"content": "One, "}}]})
            + sse({"choices": [{"index": 0, "delta": {"content": "two"}}]})
            + sse({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "t", "arguments": '{"a":'}}]}}]})
            + sse({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]}, "finish_reason": "tool_calls"}]})
            + sse({"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 9, "completion_tokens_details": {"reasoning_tokens": 4}}})
            + "data: [DONE]\n\n")
    responses.post(URL, body=body, content_type="text/event-stream")
    events = list(client.stream([{"role": "user", "content": "x"}], "m"))
    assert "".join(e["text"] for e in events if e["type"] == "text") == "One, two"
    assert "secret thoughts" not in json.dumps(events, default=str)
    assert [e for e in events if e["type"] == "tool_call"] == [{"type": "tool_call", "id": "c1", "name": "t", "arguments": {"a": 1}}]
    done = events[-1]
    assert done["type"] == "done" and done["finish_reason"] == "tool_calls"
    assert (done["usage"].input_tokens, done["usage"].output_tokens, done["usage"].thinking_tokens) == (7, 5, 4)
    assert json.loads(responses.calls[0].request.body)["stream_options"] == {"include_usage": True}


# ---- limiter -----------------------------------------------------------------------------------
def test_limiter_caps_concurrency_and_releases_slots():
    redis = FakeRedis()
    limiter = ConcurrencyLimiter("cap", max_concurrency=2, redis_client=redis, sleep=lambda s: None)
    with limiter.slot():
        with limiter.slot():
            with pytest.raises(LlmBusy):
                with limiter.slot(wait_seconds=0):
                    pass
    assert redis.members == {}  # both slots released
    with limiter.slot():
        assert len(redis.members) == 1


def test_limiter_lease_expires_so_a_crashed_worker_cannot_leak_a_slot():
    redis = FakeRedis()
    redis.members = {"dead-worker": 1.0}  # lease ended long ago
    with ConcurrencyLimiter("lease", max_concurrency=1, redis_client=redis).slot():
        assert "dead-worker" not in redis.members


def test_limiter_falls_back_to_a_local_semaphore_when_redis_is_down():
    class Broken:
        def eval(self, *a, **k):
            raise ConnectionError("redis down")

    limiter = ConcurrencyLimiter("local", max_concurrency=1, redis_client=Broken(), sleep=lambda s: None)
    with limiter.slot():  # does not raise: intelligence fails open (R2)
        with pytest.raises(LlmBusy):
            with limiter.slot(wait_seconds=0):
                pass
    with limiter.slot():
        pass


# ---- runner, factory, health, metrics ----------------------------------------------------------
@responses.activate
def test_runner_logs_gateway_cost_and_thinking_tokens(client):
    responses.post(URL, json=completion('{"verdict":"ok"}'), headers={"x-litellm-response-cost": "0.0042"})
    row, created = run_llm(client=client, scope="TRADE", idempotency_key="q1", system="s", user="u",
                           schema=SCHEMA, model="deepseek-v4-flash")
    assert created and row.status == "OK" and row.provider == "qch"
    assert row.cost_usd == Decimal("0.004200") and row.thinking_tokens == 40 and row.output_tokens == 20


@responses.activate
def test_runner_turns_a_gateway_outage_into_a_logged_error_not_an_exception(client, settings):
    settings.INTELLIGENCE_LLM_MAX_ATTEMPTS = 1
    responses.post(URL, json={"error": {"message": "down"}}, status=503)
    row, _ = run_llm(client=client, scope="TRADE", idempotency_key="q2", system="s", user="u",
                     schema=SCHEMA, model="m")
    assert row.status == LlmRunLog.Status.ERROR and "503" in row.validation_errors[0]


def test_factory_returns_qch_only_when_configured(settings):
    settings.INTELLIGENCE_LLM_PROVIDER = "qch"
    settings.QCH_API_KEY = ""
    with pytest.raises(LlmError):
        get_client()
    settings.QCH_API_KEY = KEY
    assert get_client().provider == "qch"
    assert conf.provider() == "qch"
    assert breaker_for("qch") is qch_breaker


def test_health_reports_qch_without_the_key(settings, django_user_model):
    settings.INTELLIGENCE_LLM_PROVIDER = "qch"
    settings.QCH_API_KEY = KEY
    settings.QCH_BASE_URL = BASE
    staff = django_user_model.objects.create_user("s", password="p", is_staff=True)
    http = Client()
    http.force_login(staff)
    body = http.get("/api/v1/intelligence/health/").json()
    assert body["data"]["provider"] == "qch" and body["data"]["api_key_configured"] is True
    assert KEY not in json.dumps(body)
    assert body["data"]["circuit_breaker"] in ("OPEN", "CLOSED")


def test_key_is_never_logged(client, caplog):
    caplog.set_level(logging.DEBUG)
    with responses.RequestsMock() as mock:
        mock.post(URL, json=completion('{"verdict":"ok"}'))
        client.generate_json("s", "u", SCHEMA, "m")
    assert KEY not in caplog.text


def test_provider_flag_off_means_nothing_calls_the_gateway(settings):
    """With the default fake provider no QCH client is ever built."""
    assert conf.provider() == "fake"
    assert get_client().provider == "fake"


@responses.activate
def test_deep_model_gets_the_longer_read_timeout(client, settings):
    settings.INTELLIGENCE_MODEL_DEEP = "deepseek-v4-pro"
    settings.INTELLIGENCE_LLM_TIMEOUT_SECONDS = 90
    assert client._timeouts("deepseek-v4-flash") == (5.0, 90.0)
    assert client._timeouts("deepseek-v4-pro") == (5.0, 300.0)
    responses.post(URL, json=completion('{"verdict":"ok"}'))
    client.generate_json("s", "u", SCHEMA, "deepseek-v4-pro")  # the request path passes the model through
