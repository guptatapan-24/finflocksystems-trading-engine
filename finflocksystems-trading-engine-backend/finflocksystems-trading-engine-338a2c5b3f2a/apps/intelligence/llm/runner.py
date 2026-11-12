"""Idempotent, budget-aware LLM call that always leaves an LlmRunLog row.

Never raises for provider problems: returns the log row with a non-OK status so callers can fall back
to deterministic output (R2). Grounding validation (cited keys, quoted numbers) is added in Phase 3 on top
of the schema validation done here.
"""
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError
from django.db.models import Sum
from django.utils import timezone

from apps.intelligence import conf
from apps.intelligence.models import LlmRunLog

from .base import (
    LlmCircuitOpen, LlmError, LlmMalformedResponse, LlmTimeout, validate_against_schema,
)


def estimate_tokens(*texts):
    """Cheap, conservative estimate (about 4 characters per token)."""
    return sum(len(t or "") for t in texts) // 4 + 1


def spent_today_usd():
    start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    total = LlmRunLog.objects.filter(created_at__gte=start).aggregate(total=Sum("cost_usd"))["total"]
    return Decimal(total or 0)


def cost_usd(model, usage):
    """Prices come from env (USD per million tokens). Unset prices cost 0 and are reported as unknown by health."""
    price_in = Decimal(str(getattr(settings, "INTELLIGENCE_PRICE_INPUT_PER_MTOK_USD", "0") or "0"))
    price_out = Decimal(str(getattr(settings, "INTELLIGENCE_PRICE_OUTPUT_PER_MTOK_USD", "0") or "0"))
    billed_out = usage.output_tokens + usage.thinking_tokens  # thinking bills as output
    return ((Decimal(usage.input_tokens) * price_in + Decimal(billed_out) * price_out) / Decimal(1_000_000)).quantize(
        Decimal("0.000001"))


def run_llm(*, client, scope, idempotency_key, system, user, schema, model, prompt_template=None,
            evidence_hash="", episode_id=None):
    existing = LlmRunLog.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        return existing, False
    base = dict(scope=scope, provider=getattr(client, "provider", ""), model=model or "",
                prompt_template=prompt_template, evidence_hash=evidence_hash,
                idempotency_key=idempotency_key, episode_id=episode_id)

    def finish(status, **extra):
        try:
            return LlmRunLog.objects.create(status=status, **base, **extra), True
        except IntegrityError:  # concurrent duplicate: the other writer wins
            return LlmRunLog.objects.get(idempotency_key=idempotency_key), False

    if estimate_tokens(system, user) > conf.max_input_tokens():
        return finish(LlmRunLog.Status.BUDGET, validation_errors=["INPUT_TOKENS_OVER_LIMIT"])
    budget = conf.daily_budget_usd()
    if budget > 0 and spent_today_usd() >= budget:
        return finish(LlmRunLog.Status.BUDGET, validation_errors=["DAILY_BUDGET_EXHAUSTED"])
    try:
        result = client.generate_json(system, user, schema, model)
    except LlmMalformedResponse as exc:
        spent = {}
        if exc.usage is not None:  # a failed call still cost money: keep the budget guard honest
            spent = dict(input_tokens=exc.usage.input_tokens, output_tokens=exc.usage.output_tokens,
                         thinking_tokens=exc.usage.thinking_tokens,
                         cost_usd=exc.cost_usd if exc.cost_usd is not None else cost_usd(model, exc.usage))
        return finish(LlmRunLog.Status.SCHEMA_INVALID, raw_response=exc.raw_text[:20000],
                      validation_errors=["MALFORMED_JSON"], **spent)
    except LlmTimeout as exc:
        return finish(LlmRunLog.Status.TIMEOUT, validation_errors=[str(exc)[:300]])
    except LlmCircuitOpen as exc:
        return finish(LlmRunLog.Status.CIRCUIT_OPEN, validation_errors=[str(exc)[:300]])
    except LlmError as exc:
        return finish(LlmRunLog.Status.ERROR, validation_errors=[str(exc)[:300]])
    except Exception as exc:  # never let an unexpected client bug escape into callers
        return finish(LlmRunLog.Status.ERROR, validation_errors=[f"UNEXPECTED: {exc}"[:300]])
    usage = result.usage
    reported = getattr(result, "cost_usd", None)  # the gateway's own figure beats our price table
    common = dict(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                  thinking_tokens=usage.thinking_tokens, cost_usd=reported if reported is not None else cost_usd(model, usage),
                  latency_ms=result.latency_ms, raw_response=(result.raw_text or "")[:20000])
    errors = validate_against_schema(result.parsed, schema)
    if errors:
        return finish(LlmRunLog.Status.SCHEMA_INVALID, validation_errors=errors[:50], **common)
    row = finish(LlmRunLog.Status.OK, parsed_output=result.parsed, **common)
    from apps.intelligence.services.ops import check_budget_alarm

    check_budget_alarm()
    return row
