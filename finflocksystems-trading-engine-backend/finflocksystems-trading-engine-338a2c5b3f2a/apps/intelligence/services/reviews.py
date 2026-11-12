"""Post-trade review orchestration (Phase 3): analyzers first, then one grounded LLM call, then persistence.

Fails open (R2): any LLM problem produces a deterministic fallback review built from the rule analyzers, never an
exception into callers and never anything that touches trading.
"""
import logging
from decimal import Decimal

from django.db import IntegrityError, transaction

from apps.intelligence import conf
from apps.intelligence.llm import get_client
from apps.intelligence.llm.runner import run_llm
from apps.intelligence.models import (
    LlmRunLog, MistakeFinding, Suggestion, TradeEpisode, TradeReview,
)

from . import facts as facts_module
from . import grounding, prompts

logger = logging.getLogger(__name__)
SCOPE = "TRADE_REVIEW"


def schedule_review(episode_id):
    """Queue a review after the surrounding transaction commits. Never raises."""
    try:
        if not conf.reviews_enabled():
            return
        from django.db import transaction as tx

        from apps.intelligence.tasks import run_trade_review

        tx.on_commit(lambda: _enqueue(run_trade_review, episode_id))
    except Exception:
        logger.exception("intelligence: could not schedule review for episode %s", episode_id)


def _enqueue(task, episode_id):
    try:
        task.apply_async((episode_id,), retry=False, ignore_result=True)
    except Exception:
        logger.warning("intelligence: review enqueue failed for episode %s", episode_id, exc_info=True)


def _audit(episode, reason, attempt):
    try:
        from apps.audit.models import AuditEvent

        AuditEvent.objects.get_or_create(
            idempotency_key=f"intelligence-review-fallback:{episode.pk}:{attempt}",
            defaults={"event_type": "intelligence.review.fallback", "actor": "intelligence",
                      "aggregate_type": "trade_episode", "aggregate_id": str(episode.pk), "data": {"reason": reason}})
    except Exception:
        logger.exception("intelligence: audit write failed")


def _num(value):
    text = format(Decimal(value).normalize(), "f")
    return text


def _fallback_summary(episode, reason):
    return (f"{episode.instrument_symbol} {episode.direction.replace('_', ' ').lower()} {episode.asset_family.lower()} "
            f"trade closed with net P&L {_num(episode.net_pnl)} {episode.currency} after {_num(episode.commissions)} in commissions "
            f"(exit: {episode.exit_reason or 'unknown'}). The language-model review was not available ({reason}); "
            "only the rule-based findings below are shown, and no suggestions were generated.")


def _persist(episode, instance, template, model, log, clean, findings, rejected, *, is_fallback, sample_n, snapshot):
    confidence = None
    if clean and clean.get("confidence") is not None:
        confidence = Decimal(str(min(1.0, max(0.0, float(clean["confidence"]))))).quantize(Decimal("0.0001"))
    with transaction.atomic():
        review = TradeReview.objects.create(
            scope=TradeReview.Scope.TRADE, episode=episode, strategy_key=episode.strategy_key,
            instance_id=episode.instance_id, instance_name=episode.instance_name,
            instrument_symbol=episode.instrument_symbol, currency=episode.currency,
            execution_mode=episode.execution_mode,
            verdict=(clean or {}).get("verdict", "Deterministic review only (language model unavailable)")[:400],
            process_grade=(clean or {}).get("process_grade", "") if not is_fallback else "",
            outcome_class=(clean or {}).get("outcome_class", "INCONCLUSIVE") if not is_fallback else "INCONCLUSIVE",
            summary=(clean or {}).get("summary", ""), confidence=confidence, is_fallback=is_fallback,
            strengths=(clean or {}).get("strengths", []), open_questions=(clean or {}).get("open_questions", []),
            lesson_proposals=(clean or {}).get("lesson_proposals", []), rejected_items=rejected,
            llm_run=log, prompt_version=f"{template.name}:v{template.version}", model=model,
            evidence_hash=snapshot.evidence_hash, net_pnl_at_review=episode.net_pnl, sample_n=sample_n)
        for finding in findings:
            MistakeFinding.objects.create(
                review=review, category=finding.category, severity=finding.severity, description=finding.description,
                evidence_refs=finding.refs, source=MistakeFinding.Source.RULE, rule_id=finding.rule_id,
                is_defect=finding.is_defect)
        for item in (clean or {}).get("mistakes", []):
            MistakeFinding.objects.create(
                review=review, category=item["category"], severity=item["severity"], description=item["description"],
                evidence_refs=item["evidence_refs"], source=MistakeFinding.Source.LLM)
        for index, item in enumerate((clean or {}).get("suggestions", [])):
            confidence_value = item.get("confidence")
            Suggestion.objects.create(
                review=review, strategy_key=episode.strategy_key, instance_id=episode.instance_id,
                instance_name=episode.instance_name, kind=item["kind"], parameter_key=item.get("parameter_key", ""),
                current_value=item.get("current_value"), proposed_value=item.get("proposed_value"),
                rationale=item.get("rationale", ""), expected_effect=item.get("expected_effect", ""),
                risk_of_change=item.get("risk_of_change", ""),
                confidence=None if confidence_value is None else Decimal(str(min(1.0, max(0.0, float(confidence_value))))).quantize(Decimal("0.0001")),
                evidence_needed=item.get("evidence_needed", ""), evidence_refs=item["evidence_refs"],
                is_hypothesis=bool(item.get("is_hypothesis", True)), idempotency_key=f"sugg:{review.pk}:{index}")
    try:  # the "try next time" measurement plans come from the rule findings (deterministic); never blocks a review
        from . import playbook

        with transaction.atomic():
            playbook.build_for_review(review)
    except Exception:
        logger.exception("intelligence: playbook failed for review %s", review.pk)
    return review


def _attempt_number(episode):
    return LlmRunLog.objects.filter(scope=SCOPE, episode_id=episode.pk).count() + 1


def review_episode(episode_id, *, client=None):
    """Create (or return) the review for a closed episode. Returns TradeReview or None."""
    from apps.strategies.models import StrategyInstance
    from apps.strategies.plugins import get_plugin

    episode = (TradeEpisode.objects.filter(pk=episode_id).select_related("evidence_snapshot").first())
    if episode is None or episode.status == TradeEpisode.Status.OPEN or not hasattr(episode, "evidence_snapshot"):
        return None
    snapshot = episode.evidence_snapshot
    template = prompts.get_active_template()
    model = conf.model_for("review")
    prompt_version = f"{template.name}:v{template.version}"
    existing = TradeReview.objects.filter(episode=episode, evidence_hash=snapshot.evidence_hash,
                                          prompt_version=prompt_version, model=model).first()
    if existing is not None and not existing.is_fallback:
        return existing
    attempt = _attempt_number(episode)
    if existing is not None:  # a fallback is replaceable by a real review (children first: PROTECT)
        existing.mistakes.all().delete()
        existing.suggestions.filter(status="PROPOSED").delete()
        if existing.suggestions.exists():
            return existing
        existing.delete()
    instance = StrategyInstance.objects.filter(pk=episode.instance_id).select_related("definition").first()
    facts, findings, _trimmed = facts_module.build_facts(episode, conf.max_input_tokens())
    sample_n = int(facts.get("sample.n_closed_same_instance_currency") or 0)
    defect_exists = any(f.is_defect for f in findings)

    def fallback(reason, log=None, rejected=None):
        _audit(episode, reason, attempt)
        try:
            return _persist(
                episode, instance, template, model, log,
                {"verdict": "Deterministic review only (language model unavailable)",
                 "summary": _fallback_summary(episode, reason), "mistakes": [], "suggestions": []},
                findings, rejected or [], is_fallback=True, sample_n=sample_n, snapshot=snapshot)
        except IntegrityError:
            return TradeReview.objects.filter(episode=episode, evidence_hash=snapshot.evidence_hash,
                                              prompt_version=prompt_version, model=model).first()

    if not model:
        return fallback("no model configured")
    try:
        client = client or get_client()
    except Exception as exc:
        return fallback(f"client unavailable: {str(exc)[:120]}")
    user = prompts.build_user_prompt(facts)
    key = f"review:{episode.pk}:{snapshot.evidence_hash}:{prompt_version}:{model}:a{attempt}"
    log, _created = run_llm(client=client, scope=SCOPE, idempotency_key=key, system=template.system_prompt,
                            user=user, schema=template.output_schema, model=model, prompt_template=template,
                            evidence_hash=snapshot.evidence_hash, episode_id=episode.pk)
    if log.status != LlmRunLog.Status.OK:
        return fallback(f"llm status {log.status}", log)
    if instance is None:
        return fallback("strategy instance no longer exists", log)
    plugin = get_plugin(instance.definition)
    clean, rejected, fatal = grounding.validate_review(log.parsed_output, facts, instance, plugin, defect_exists, sample_n)
    if fatal:
        repair_user = (user + "\nYour previous answer was rejected for these reasons: " + "; ".join(fatal)
                       + ". Answer again, quoting only numbers that appear in the facts.")
        log2, _ = run_llm(client=client, scope=SCOPE, idempotency_key=key + ":repair", system=template.system_prompt,
                          user=repair_user, schema=template.output_schema, model=model, prompt_template=template,
                          evidence_hash=snapshot.evidence_hash, episode_id=episode.pk)
        if log2.status == LlmRunLog.Status.OK:
            clean, rejected, fatal = grounding.validate_review(log2.parsed_output, facts, instance, plugin,
                                                               defect_exists, sample_n)
            log = log2
        if fatal:
            LlmRunLog.objects.filter(pk=log.pk).update(status=LlmRunLog.Status.GROUNDING_FAILED,
                                                       validation_errors=fatal + [str(r) for r in rejected][:20])
            return fallback("grounding failed: " + fatal[0][:120], log, rejected)
    if rejected:
        LlmRunLog.objects.filter(pk=log.pk).update(validation_errors=[str(r)[:300] for r in rejected][:30])
    try:
        review = _persist(episode, instance, template, model, log, clean, findings, rejected, is_fallback=False,
                          sample_n=sample_n, snapshot=snapshot)
        try:
            from . import learning

            learning.process_proposals(review)
        except Exception:
            logger.exception("intelligence: lesson proposals failed for review %s", review.pk)
        return review
    except IntegrityError:
        return TradeReview.objects.filter(episode=episode, evidence_hash=snapshot.evidence_hash,
                                          prompt_version=prompt_version, model=model).first()
