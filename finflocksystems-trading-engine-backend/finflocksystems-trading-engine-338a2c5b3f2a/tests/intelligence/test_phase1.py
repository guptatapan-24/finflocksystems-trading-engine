import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import Client

from apps.intelligence import conf
from apps.intelligence.guard import flush_refusal_message
from apps.intelligence.llm import LlmError, get_client
from apps.intelligence.llm.base import LlmCircuitOpen, validate_against_schema
from apps.intelligence.llm.circuit import CircuitBreaker
from apps.intelligence.llm.fake import FakeLLMClient
from apps.intelligence.llm.runner import cost_usd, run_llm
from apps.intelligence.models import IntelCursor, LlmRunLog, PromptTemplate

pytestmark = pytest.mark.django_db

SCHEMA = {"type": "object", "required": ["verdict"], "properties": {"verdict": {"type": "string"}}}
GOOD = {"verdict": "ok"}


def _run(client, key="k1", **kw):
    return run_llm(client=client, scope="TRADE", idempotency_key=key, system="s", user="u",
                   schema=SCHEMA, model="m", **kw)


# ---- settings defaults -------------------------------------------------------------------------
def test_defaults_are_safe(settings):
    assert conf.enabled() is False
    assert conf.model_for("review") == "" and conf.model_for("deep") == ""
    assert settings.INTELLIGENCE_MIN_EPISODES_FOR_SUGGESTION == 8
    assert "apps.intelligence" in settings.INSTALLED_APPS


def test_intelligence_is_not_a_readiness_dependency(settings):
    from apps.execution.readiness import REQUIRED_WORKER_ROLES

    assert "intelligence" not in REQUIRED_WORKER_ROLES
    assert not any("intel" in t for t in settings.EXECUTION_REQUIRED_KAFKA_TOPICS)
    assert settings.CELERY_TASK_ROUTES["apps.intelligence.tasks.*"] == {"queue": "intelligence"}


# ---- constraints ---------------------------------------------------------------------------------
def test_prompt_template_unique_version_and_single_active():
    PromptTemplate.objects.create(name="trade_review", version=1, system_prompt="a", active=True)
    with pytest.raises(IntegrityError), transaction.atomic():
        PromptTemplate.objects.create(name="trade_review", version=1, system_prompt="b")
    with pytest.raises(IntegrityError), transaction.atomic():
        PromptTemplate.objects.create(name="trade_review", version=2, system_prompt="b", active=True)
    PromptTemplate.objects.create(name="trade_review", version=2, system_prompt="b", active=False)


def test_llm_run_log_idempotency_key_unique():
    LlmRunLog.objects.create(scope="TRADE", provider="fake", status="OK", idempotency_key="x")
    with pytest.raises(IntegrityError), transaction.atomic():
        LlmRunLog.objects.create(scope="TRADE", provider="fake", status="OK", idempotency_key="x")


def test_cursor_name_unique():
    IntelCursor.objects.create(name="fills")
    with pytest.raises(IntegrityError), transaction.atomic():
        IntelCursor.objects.create(name="fills")


def test_intel_tables_use_prefix():
    for model in (PromptTemplate, LlmRunLog, IntelCursor):
        assert model._meta.db_table.startswith("intel_")


# ---- fake client modes -----------------------------------------------------------------------------
def test_fake_valid_logs_ok_and_is_idempotent():
    client = FakeLLMClient("valid", GOOD)
    row, created = _run(client)
    assert created and row.status == "OK" and row.parsed_output == GOOD
    again, created2 = _run(client)
    assert not created2 and again.pk == row.pk and len(client.calls) == 1


def test_fake_schema_invalid():
    row, _ = _run(FakeLLMClient("schema_invalid", GOOD))
    assert row.status == "SCHEMA_INVALID" and row.validation_errors and row.parsed_output is None


def test_fake_malformed():
    row, _ = _run(FakeLLMClient("malformed"))
    assert row.status == "SCHEMA_INVALID" and row.validation_errors == ["MALFORMED_JSON"]


def test_fake_timeout():
    row, _ = _run(FakeLLMClient("timeout"))
    assert row.status == "TIMEOUT"


def test_fake_hallucinated_number_passes_schema_but_is_flagged_for_grounding_later():
    schema = {"type": "object", "properties": {"pnl": {"type": "number"}}}
    client = FakeLLMClient("hallucinated_number", {"pnl": 10}, mutate_number=(["pnl"], 999))
    row, _ = run_llm(client=client, scope="TRADE", idempotency_key="h", system="s", user="u", schema=schema, model="m")
    assert row.status == "OK" and row.parsed_output == {"pnl": 999}  # Phase 3 grounding rejects this


def test_unexpected_client_exception_never_escapes():
    class Boom(FakeLLMClient):
        def generate_json(self, *a, **k):
            raise RuntimeError("bug")

    row, _ = _run(Boom())
    assert row.status == "ERROR"


# ---- budget and limits ------------------------------------------------------------------------------
def test_daily_budget_exhausted_blocks_call(settings):
    settings.INTELLIGENCE_DAILY_BUDGET_USD = "1.00"
    LlmRunLog.objects.create(scope="TRADE", provider="fake", status="OK", idempotency_key="spent", cost_usd="1.5")
    client = FakeLLMClient("valid", GOOD)
    row, _ = _run(client)
    assert row.status == "BUDGET" and client.calls == []


def test_input_token_cap(settings):
    settings.INTELLIGENCE_MAX_INPUT_TOKENS = 5
    client = FakeLLMClient("valid", GOOD)
    row, _ = run_llm(client=client, scope="T", idempotency_key="big", system="x" * 400, user="y", schema=SCHEMA, model="m")
    assert row.status == "BUDGET" and client.calls == []


def test_cost_counts_thinking_as_output(settings):
    from apps.intelligence.llm.base import LlmUsage

    settings.INTELLIGENCE_PRICE_INPUT_PER_MTOK_USD = "1"
    settings.INTELLIGENCE_PRICE_OUTPUT_PER_MTOK_USD = "2"
    cost = cost_usd("m", LlmUsage(input_tokens=1_000_000, output_tokens=250_000, thinking_tokens=250_000))
    assert str(cost) == "2.000000"


# ---- circuit breaker -------------------------------------------------------------------------------------
class _OutageClient:
    provider = "qch"

    def __init__(self, breaker):
        self.breaker = breaker

    def generate_json(self, system, user, schema, model):
        if not self.breaker.allow():
            raise LlmCircuitOpen("QCH circuit breaker is open")
        self.breaker.record_failure()
        raise LlmError("outage")


def test_circuit_breaker_opens_and_blocks_calls():
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=1000)
    client = _OutageClient(breaker)
    for _ in range(2):
        with pytest.raises(LlmError):
            client.generate_json("s", "u", SCHEMA, "m")
    assert breaker.state == "OPEN"
    row, _ = run_llm(client=client, scope="T", idempotency_key="cb", system="s", user="u", schema=SCHEMA, model="m")
    assert row.status == "CIRCUIT_OPEN"


def test_qch_requires_key_and_base_url(settings):
    from apps.intelligence.llm.qch import QchClient

    settings.QCH_API_KEY = ""
    with pytest.raises(LlmError):
        QchClient()
    settings.QCH_BASE_URL = ""
    with pytest.raises(LlmError):
        QchClient(api_key="k")


def test_gemini_provider_is_gone(settings):
    settings.INTELLIGENCE_LLM_PROVIDER = "gemini"
    with pytest.raises(LlmError):
        get_client()


def test_factory(settings):
    settings.INTELLIGENCE_LLM_PROVIDER = "fake"
    assert isinstance(get_client(), FakeLLMClient)
    settings.INTELLIGENCE_LLM_PROVIDER = "nope"
    with pytest.raises(LlmError):
        get_client()


def test_validate_against_schema():
    assert validate_against_schema(GOOD, SCHEMA) == []
    assert validate_against_schema({}, SCHEMA)


# ---- flush guard (R6) ---------------------------------------------------------------------------------------
def test_flush_allowed_when_no_intel_rows():
    assert flush_refusal_message() is None
    call_command("check_intelligence_flush_guard")


def test_flush_refused_when_intel_rows_exist():
    IntelCursor.objects.create(name="fills")
    assert "intel_cursor=1" in flush_refusal_message()
    with pytest.raises(CommandError):
        call_command("check_intelligence_flush_guard")
    with pytest.raises(CommandError):
        call_command("flush", interactive=False)
    assert IntelCursor.objects.count() == 1


def test_flush_override_flag_allows_flush():
    IntelCursor.objects.create(name="fills")
    call_command("flush", "--i-understand-intelligence-data-loss", interactive=False, verbosity=0)
    assert IntelCursor.objects.count() == 0


# ---- health endpoint -------------------------------------------------------------------------------------------
def test_health_is_open_to_anyone():
    assert Client().get("/api/v1/intelligence/health/").status_code == 200


def test_health_for_staff_has_no_secrets(settings):
    settings.QCH_API_KEY = "super-secret-key"
    settings.QCH_BASE_URL = "https://qch.test/qch-ai"
    settings.INTELLIGENCE_LLM_PROVIDER = "qch"
    User = get_user_model()
    User.objects.create_user("boss", password="pw", is_staff=True)
    client = Client()
    client.login(username="boss", password="pw")
    response = client.get("/api/v1/intelligence/health/")
    body = response.json()
    assert response.status_code == 200 and body["ok"] is True
    assert body["data"]["api_key_configured"] is True and body["data"]["enabled"] is False
    assert "super-secret-key" not in response.content.decode()
    assert client.post("/api/v1/intelligence/health/").status_code == 405


def test_health_post_is_csrf_protected_not_exempt():
    from apps.intelligence import views

    assert not getattr(views.health, "csrf_exempt", False)
