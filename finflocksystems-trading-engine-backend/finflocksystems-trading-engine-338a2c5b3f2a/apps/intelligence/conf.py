"""Typed access to INTELLIGENCE_* settings so callers never touch os.environ."""
from decimal import Decimal

from django.conf import settings


def enabled():
    return bool(getattr(settings, "INTELLIGENCE_ENABLED", False))


def provider():
    return str(getattr(settings, "INTELLIGENCE_LLM_PROVIDER", "fake") or "fake").strip().lower()


def model_for(tier):
    """tier is 'review', 'deep' or 'fast'. Model ids come from env only; no code defaults (R11).
    'fast' is for latency-sensitive calls (live commentary) and falls back to the review model when unset."""
    name = {"deep": "INTELLIGENCE_MODEL_DEEP", "fast": "INTELLIGENCE_MODEL_FAST"}.get(tier, "INTELLIGENCE_MODEL_REVIEW")
    value = str(getattr(settings, name, "") or "").strip()
    if not value and tier == "fast":
        return model_for("review")
    return value


def daily_budget_usd():
    return Decimal(str(getattr(settings, "INTELLIGENCE_DAILY_BUDGET_USD", "0") or "0"))


def max_input_tokens():
    return int(getattr(settings, "INTELLIGENCE_MAX_INPUT_TOKENS", 24000))


def max_output_tokens():
    return int(getattr(settings, "INTELLIGENCE_MAX_OUTPUT_TOKENS", 4096))


def reviews_enabled():
    """LLM post-trade reviews. Separate from `enabled()` so episodes can run alone (Phase 2) before any LLM call."""
    return enabled() and bool(getattr(settings, "INTELLIGENCE_REVIEWS_ENABLED", False))


def rereview_pnl_delta_pct():
    return float(getattr(settings, "INTELLIGENCE_REREVIEW_PNL_DELTA_PCT", 20))


def session_records_enabled():
    """Phase 9 session records and the daily report. Separate from `enabled()` so it can be switched off alone."""
    return enabled() and bool(getattr(settings, "INTELLIGENCE_SESSION_RECORD_ENABLED", False))


def context_enabled():
    """Phase 10 outside context (calendars, news, announcements). Separate from `enabled()` so it can be switched off alone."""
    return enabled() and bool(getattr(settings, "INTELLIGENCE_CONTEXT_ENABLED", False))


def context_sources():
    raw = str(getattr(settings, "INTELLIGENCE_CONTEXT_SOURCES", "") or "")
    return [part.strip().lower() for part in raw.split(",") if part.strip()]
