"""Learning loop (Phase 5). "Learning" means validated lessons and statistics kept in the database and fed into
future prompts. There is no model training. Everything here is deterministic, seeded and idempotent: lesson counts
are recomputed from the evidence rows, never incremented, so reprocessing any episode any number of times is safe.

Lifecycle (thresholds configurable, defaults below):
  HYPOTHESIS -> SUPPORTED   n >= required_n, Beta posterior mean >= 0.8, effect has the expected sign, and (Phase 11) a
                            one-sided permutation / sign-flip p-value whose Benjamini-Hochberg q across all live lessons
                            is <= 0.10 (more lessons tested = stricter evidence for each one)
  SUPPORTED  -> ESTABLISHED n >= 20 AND >= 8 FORWARD trades (opened after promotion) with posterior >= 0.8
  any live   -> REFUTED     posterior <= 0.3 (n >= required_n), or forward posterior <= 0.4 with >= 8 forward trades
  HYPOTHESIS/SUPPORTED -> RETIRED  no matching trade for retire_days
The trade that proposed a lesson never counts as evidence for it. PAPER and delayed-data trades count at half weight.
required_n grows slowly with the number of lessons already being tested (multiple-testing guard).
"""
import logging
import math
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.intelligence.models import (
    Lesson, LearningEvent, LessonEvidence, TradeEpisode,
)

from . import dsl, stats
from .evidence import sanitize_text

logger = logging.getLogger(__name__)

DEFAULTS = {
    "min_n_supported": 8, "supported_posterior": 0.80, "refuted_posterior": 0.30,
    "established_n": 20, "forward_min": 8, "forward_refute_posterior": 0.40,
    "paper_weight": 0.5, "max_new_lessons_per_week": 5, "retire_days": 180, "bootstrap": 1000,
    "fdr_q": 0.10,  # Benjamini-Hochberg false-discovery rate a lesson must meet across all live lessons (Phase 11)
    "experiment_min_n": 8, "experiment_days": 90,  # playbook experiments: matching trades needed, and days before one expires
    "cusum_ref_n": 10, "cusum_k": 0.75, "cusum_h": 10.0, "cusum_clip": 3.0, "cusum_min_after": 5, "cusum_min_calibration": 15,  # edge-decay monitor
}
TERMINAL = {Lesson.Status.REFUTED, Lesson.Status.RETIRED}


def thresholds():
    merged = dict(DEFAULTS)
    merged.update(getattr(settings, "INTELLIGENCE_LEARNING_THRESHOLDS", {}) or {})
    return merged


def _episode_weight(episode, t):
    flags = set(episode.data_quality_flags or [])
    return t["paper_weight"] if (episode.execution_mode == "PAPER" or "PAPER_DELAYED_DATA" in flags) else 1.0


def required_n(t):
    """Stricter evidence as more predicates are being tested (a slow, logarithmic multiple-testing guard)."""
    tested = Lesson.objects.exclude(status=Lesson.Status.RETIRED).count()
    return t["min_n_supported"] + int(math.log2(1 + tested))


def recompute_q_values():
    """Benjamini-Hochberg adjusted p-values across every live lesson that has a p-value. A lesson whose test could not
    be run (too few trades) has no p-value and is not part of the family."""
    live = list(Lesson.objects.exclude(status__in=list(TERMINAL)).order_by("pk"))
    qs = stats.bh_adjust([l.p_value for l in live])
    for lesson, q in zip(live, qs):
        if lesson.q_value != q:
            Lesson.objects.filter(pk=lesson.pk).update(q_value=q)
    return len(live)


def _event(kind, *, episode=None, lesson=None, payload=None, key):
    try:
        with transaction.atomic():
            return LearningEvent.objects.get_or_create(
                dedupe_key=key, defaults={"type": kind, "episode": episode, "lesson": lesson, "payload": payload or {}})
    except IntegrityError:
        return LearningEvent.objects.filter(dedupe_key=key).first(), False


def _metrics_of(episode):
    row = getattr(episode, "metrics_row", None)
    return dict(row.metrics) if row is not None else {}


def _closed_episodes(strategy_key):
    return list(TradeEpisode.objects.filter(strategy_key=strategy_key, status__in=["CLOSED", "EXPIRED"])
                .select_related("metrics_row").order_by("opened_at", "pk"))


# ------------------------------------------------------------------------------------------------
# one lesson
# ------------------------------------------------------------------------------------------------
def evaluate_lesson(lesson, now=None):
    """Recompute a lesson's evidence, counts, effect size and status from scratch. Returns the lesson."""
    if lesson.status in TERMINAL:
        return lesson
    now = now or timezone.now()
    t = thresholds()
    effect = lesson.expected_effect
    outcome_key = effect["outcome_metric"]
    want_negative = effect["direction"] == "negative"
    episodes = _closed_episodes(lesson.strategy_key)
    matching, others = [], []
    for episode in episodes:
        metrics = _metrics_of(episode)
        hit = dsl.evaluate(lesson.predicate, metrics)
        value = metrics.get(outcome_key)
        if value is None or isinstance(value, bool):
            continue
        (matching if hit else others if hit is False else []).append((episode, float(value)))
    other_values = [v for _, v in others]
    if outcome_key == "outcome.net_return_bps":
        reference = 0.0
    else:
        reference = stats.mean(other_values) if len(other_values) >= 3 else None

    support = contradict = forward = 0
    s_w = c_w = f_s = f_c = 0.0
    counted_values = []
    reinforced, contradicted = [], []
    for episode, value in matching:
        origin = episode.pk == lesson.origin_episode_id
        if reference is None:
            continue
        verdict = (LessonEvidence.Verdict.SUPPORTS if ((value < reference) if want_negative else (value > reference))
                   else LessonEvidence.Verdict.CONTRADICTS)
        weight = 0.0 if origin else _episode_weight(episode, t)
        is_forward = bool(lesson.supported_at and episode.opened_at >= lesson.supported_at and not origin)
        _, created = LessonEvidence.objects.update_or_create(
            lesson=lesson, episode=episode, defaults={"verdict": verdict, "outcome_value": value, "weight": weight,
                                                      "forward_only": is_forward, "is_origin": origin})
        if origin:
            continue
        counted_values.append(value)
        if verdict == LessonEvidence.Verdict.SUPPORTS:
            support += 1
            s_w += weight
            f_s += weight if is_forward else 0
            reinforced.append(episode)
        else:
            contradict += 1
            c_w += weight
            f_c += weight if is_forward else 0
            contradicted.append(episode)
        forward += 1 if is_forward else 0

    delta, low, high = stats.diff_ci(counted_values, other_values, seed=lesson.pk or 0, resamples=t["bootstrap"])
    direction = "negative" if want_negative else "positive"
    if outcome_key == "outcome.net_return_bps":  # compared with zero, as the verdicts above are
        p_value = stats.signflip_pvalue(counted_values, direction, seed=lesson.pk or 0, resamples=t["bootstrap"] * 2)
    else:
        p_value = stats.permutation_pvalue(counted_values, other_values, direction, seed=lesson.pk or 0, resamples=t["bootstrap"] * 2)
    lesson.p_value = p_value
    Lesson.objects.filter(pk=lesson.pk).update(p_value=p_value)
    recompute_q_values()
    lesson.q_value = Lesson.objects.filter(pk=lesson.pk).values_list("q_value", flat=True).first()
    fdr_ok = lesson.q_value is not None and lesson.q_value <= t["fdr_q"]
    alpha, beta = 1.0 + s_w, 1.0 + c_w
    posterior = stats.beta_mean(alpha, beta)
    f_alpha, f_beta = 1.0 + f_s, 1.0 + f_c
    forward_posterior = stats.beta_mean(f_alpha, f_beta)
    total = support + contradict
    sign_ok = delta is not None and ((delta < 0) if want_negative else (delta > 0)) if outcome_key != "outcome.net_return_bps" \
        else (stats.mean(counted_values) is not None and ((stats.mean(counted_values) < 0) if want_negative else (stats.mean(counted_values) > 0)))

    previous = lesson.status
    status = lesson.status
    need = required_n(t)
    if status == Lesson.Status.HYPOTHESIS:
        if total >= need and posterior <= t["refuted_posterior"]:
            status = Lesson.Status.REFUTED
        elif total >= need and posterior >= t["supported_posterior"] and sign_ok and fdr_ok:
            status = Lesson.Status.SUPPORTED
    if status in (Lesson.Status.SUPPORTED, Lesson.Status.ESTABLISHED):
        if forward >= t["forward_min"] and forward_posterior <= t["forward_refute_posterior"]:
            status = Lesson.Status.REFUTED
        elif (status == Lesson.Status.SUPPORTED and total >= t["established_n"] and forward >= t["forward_min"]
              and forward_posterior >= t["supported_posterior"] and fdr_ok):
            status = Lesson.Status.ESTABLISHED

    lesson.support_n, lesson.contradict_n = support, contradict
    lesson.support_weight, lesson.contradict_weight = s_w, c_w
    lesson.beta_alpha, lesson.beta_beta = alpha, beta
    lesson.n_forward, lesson.forward_alpha, lesson.forward_beta = forward, f_alpha, f_beta
    lesson.effect_size, lesson.effect_ci_low, lesson.effect_ci_high = delta, low, high
    lesson.last_evaluated_at = now
    if matching:
        lesson.last_matched_at = max(e.opened_at for e, _ in matching)
    if status != previous:
        lesson.status = status
        if status == Lesson.Status.SUPPORTED and lesson.supported_at is None:
            lesson.supported_at = now
        if status == Lesson.Status.ESTABLISHED:
            lesson.established_at = now
    lesson.save()
    for episode in reinforced:
        _event(LearningEvent.Type.LESSON_REINFORCED, episode=episode, lesson=lesson, key=f"lesson:{lesson.pk}:reinforced:{episode.pk}",
               payload={"statement": lesson.statement, "n": total, "status": lesson.status})
    for episode in contradicted:
        _event(LearningEvent.Type.LESSON_CONTRADICTED, episode=episode, lesson=lesson, key=f"lesson:{lesson.pk}:contradicted:{episode.pk}",
               payload={"statement": lesson.statement, "n": total, "status": lesson.status})
    if status != previous:
        kind = LearningEvent.Type.LESSON_PROMOTED if status in (Lesson.Status.SUPPORTED, Lesson.Status.ESTABLISHED) \
            else LearningEvent.Type.LESSON_RETIRED
        _event(kind, lesson=lesson, key=f"lesson:{lesson.pk}:to:{status}",
               payload={"statement": lesson.statement, "from": previous, "to": status, "n": total,
                        "posterior": round(posterior, 4), "forward_n": forward,
                        "p_value": None if p_value is None else round(p_value, 4),
                        "q_value": None if lesson.q_value is None else round(lesson.q_value, 4)})
    return lesson


def evaluate_episode(episode):
    """Update every live lesson of this strategy with the (new) episode. Idempotent."""
    if episode.status == TradeEpisode.Status.OPEN:
        return 0
    count = 0
    for lesson in Lesson.objects.filter(strategy_key=episode.strategy_key).exclude(status__in=list(TERMINAL)):
        evaluate_lesson(lesson)
        count += 1
    return count


def retire_stale_lessons(now=None):
    now = now or timezone.now()
    cutoff = now - timedelta(days=thresholds()["retire_days"])
    retired = 0
    for lesson in Lesson.objects.filter(status__in=[Lesson.Status.HYPOTHESIS, Lesson.Status.SUPPORTED]):
        last = lesson.last_matched_at or lesson.created_at
        if last < cutoff:
            lesson.status = Lesson.Status.RETIRED
            lesson.save(update_fields=["status"])
            _event(LearningEvent.Type.LESSON_RETIRED, lesson=lesson, key=f"lesson:{lesson.pk}:to:RETIRED",
                   payload={"statement": lesson.statement, "to": "RETIRED", "reason": "no matching trade for a long time"})
            retired += 1
    return retired


# ------------------------------------------------------------------------------------------------
# creating lessons from review proposals
# ------------------------------------------------------------------------------------------------
def process_proposals(review):
    """Turn a review's raw lesson proposals into Lessons when they validate, match the episode and are new.
    Rejections are appended to review.rejected_items. Returns the lessons created."""
    episode = review.episode
    if review.is_fallback or episode is None or not review.lesson_proposals:
        return []
    t = thresholds()
    metrics = _metrics_of(episode)
    created, rejected = [], []
    week_ago = timezone.now() - timedelta(days=7)
    for index, proposal in enumerate((review.lesson_proposals or [])[:4]):
        reasons = []
        try:
            predicate = dsl.parse_predicate(proposal.get("predicate"))
            effect = dsl.parse_effect(proposal.get("expected_effect"))
        except (dsl.DslError, AttributeError) as exc:
            rejected.append({"item": f"lesson_proposals[{index}]", "reasons": [f"invalid: {exc}"]})
            continue
        if dsl.evaluate(predicate, metrics) is not True:
            reasons.append("the predicate does not match the trade it came from")
        statement = sanitize_text(proposal.get("statement"), 400)
        if not statement:
            reasons.append("empty statement")
        existing = [l for l in Lesson.objects.filter(strategy_key=episode.strategy_key)
                    if l.expected_effect == effect and dsl.equivalent(l.predicate, predicate)]
        if existing:
            reasons.append(f"duplicate of lesson {existing[0].pk}")
        if Lesson.objects.filter(created_at__gte=week_ago).count() >= t["max_new_lessons_per_week"]:
            reasons.append("weekly cap on new lessons reached")
        if reasons:
            rejected.append({"item": f"lesson_proposals[{index}]", "reasons": reasons})
            continue
        lesson = Lesson.objects.create(
            strategy_key=episode.strategy_key, statement=statement, predicate=predicate, expected_effect=effect,
            predicate_hash=dsl.canonical_hash(predicate, effect), created_from_review=review,
            origin_episode_id=episode.pk)
        _event(LearningEvent.Type.LESSON_CREATED, episode=episode, lesson=lesson, key=f"lesson:{lesson.pk}:created",
               payload={"statement": statement, "predicate": predicate, "expected_effect": effect})
        evaluate_lesson(lesson)
        created.append(lesson)
    if rejected:
        review.rejected_items = list(review.rejected_items or []) + rejected
        review.save(update_fields=["rejected_items"])
    return created


# ------------------------------------------------------------------------------------------------
# retrieval for prompts and the pre-trade view (Phase 6)
# ------------------------------------------------------------------------------------------------
def applicable_lessons(strategy_key, metrics, limit=5):
    """Live lessons (not refuted or retired) whose predicate matches these metrics, strongest first."""
    order = {Lesson.Status.ESTABLISHED: 0, Lesson.Status.SUPPORTED: 1, Lesson.Status.HYPOTHESIS: 2}
    rows = [l for l in Lesson.objects.filter(strategy_key=strategy_key).exclude(status__in=list(TERMINAL))
            if dsl.evaluate(l.predicate, metrics) is True]
    rows.sort(key=lambda l: (order[l.status], -(l.support_n + l.contradict_n)))
    return rows[:limit]


def schedule_learning(episode_id):
    """Queue learning for a just-closed episode after the surrounding transaction commits. Never raises."""
    try:
        from apps.intelligence import conf

        if not conf.enabled():
            return
        from apps.intelligence.tasks import learn_from_episode

        transaction.on_commit(lambda: _enqueue(learn_from_episode, episode_id))
    except Exception:
        logger.exception("intelligence: could not schedule learning for episode %s", episode_id)


def _enqueue(task, episode_id):
    try:
        task.apply_async((episode_id,), retry=False, ignore_result=True)
    except Exception:
        logger.warning("intelligence: learning enqueue failed for episode %s", episode_id, exc_info=True)
