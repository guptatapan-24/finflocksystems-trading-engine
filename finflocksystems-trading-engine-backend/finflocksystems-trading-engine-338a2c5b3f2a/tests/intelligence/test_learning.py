import json
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.intelligence import tasks
from apps.intelligence.llm.fake import FakeLLMClient
from apps.intelligence.models import (
    CalibrationStat, EpisodeMetrics, LearningEvent, Lesson, LessonEvidence, PerformanceStat, PromptTemplate,
    Suggestion, SuggestionOutcome, TradeEpisode, TradeReview,
)
from apps.intelligence.services import analytics, digest, dsl, facts as facts_module, learning, prompts, report, stats
from apps.intelligence.services.episodes import build_or_update_episodes
from apps.intelligence.services.reviews import review_episode
from apps.strategies.framework import update_instance
from apps.strategies.models import StrategyInstance
from tests.intelligence import helpers as h
from tests.intelligence.test_reviews import closed_stock, good_response

pytestmark = pytest.mark.django_db
D = Decimal
_n = {"i": 0}


@pytest.fixture(autouse=True)
def on(settings):
    settings.INTELLIGENCE_ENABLED = True
    settings.INTELLIGENCE_REVIEWS_ENABLED = True
    settings.INTELLIGENCE_LLM_PROVIDER = "fake"
    settings.INTELLIGENCE_MODEL_REVIEW = "fake-flash"


def make_episode(bps, *, rsi=20.0, mode="PAPER", currency="USD", key="SMA_CROSSOVER", offset_days=0, extra=None,
                 instance_id=1, version=1, symbol="AAPL", flags=None, pop=None):
    """A closed episode with chosen metrics, without going through fills (fast synthetic streams)."""
    _n["i"] += 1
    opened = timezone.now() + timedelta(days=offset_days, minutes=_n["i"])
    episode = TradeEpisode.objects.create(
        strategy_key=key, instance_id=instance_id, instance_name="syn", strategy_version=version, instrument_symbol=symbol,
        asset_family="STOCK", execution_mode=mode, currency=currency, direction="LONG", status="CLOSED",
        opened_at=opened, closed_at=opened + timedelta(hours=1), net_pnl=D(str(bps)) / 10, gross_pnl=D(str(bps)) / 10,
        first_fill_execution_id=f"syn-{_n['i']}", idempotency_key=f"syn:{_n['i']}", data_quality_flags=flags or [],
        exit_reason="SIGNAL")
    metrics = {"asset_family": "STOCK", "entry.rsi": rsi, "outcome.net_return_bps": float(bps), "outcome.is_closed": True}
    if pop is not None:
        metrics["opt.pop"] = pop
    metrics.update(extra or {})
    EpisodeMetrics.objects.create(episode=episode, metrics=metrics, metrics_version="1")
    return episode


PRED = {"all": [{"metric": "entry.rsi", "op": "<", "value": 25}]}
EFFECT = {"outcome_metric": "net_return_bps", "direction": "negative"}


def review_with(episode, proposals, fallback=False):
    return TradeReview.objects.create(
        episode=episode, strategy_key=episode.strategy_key, instance_id=episode.instance_id, instance_name="syn",
        instrument_symbol="AAPL", currency=episode.currency, execution_mode="PAPER", is_fallback=fallback,
        lesson_proposals=proposals, evidence_hash=f"h{episode.pk}", prompt_version="trade_review:v2", model="m")


def proposal(statement="Very oversold entries lose", predicate=PRED, effect=EFFECT):
    return {"statement": statement, "predicate": json.dumps(predicate), "expected_effect": json.dumps(effect)}


def new_lesson(origin_bps=-50, **kw):
    origin = make_episode(origin_bps, **kw)
    lessons = learning.process_proposals(review_with(origin, [proposal()]))
    assert len(lessons) == 1
    return origin, lessons[0]


# =====================================================================================================
# DSL
# =====================================================================================================
def test_dsl_normalises_and_hashes_independent_of_order():
    a = dsl.parse_predicate({"all": [{"metric": "regime.vol_pctile", "op": ">", "value": 80}, {"metric": "entry.rsi", "op": "<", "value": 25}]})
    b = dsl.parse_predicate(json.dumps({"all": [{"metric": "entry.rsi", "op": "<", "value": 25.0}, {"metric": "regime.vol_pctile", "op": ">", "value": 80}]}))
    assert a == b and dsl.canonical_hash(a, EFFECT) == dsl.canonical_hash(b, EFFECT)
    assert dsl.metrics_used(a) == {"entry.rsi", "regime.vol_pctile"}


@pytest.mark.parametrize("bad,fragment", [
    ({"all": [{"metric": "made.up", "op": "<", "value": 1}]}, "unknown metric"),
    ({"all": [{"metric": "entry.rsi", "op": "~", "value": 1}]}, "is not one of"),
    ({"all": [{"metric": "outcome.net_pnl", "op": "<", "value": 0}]}, "money-valued"),
    ({"all": [{"metric": "opt.max_loss", "op": "<", "value": 0}]}, "money-valued"),
    ({"all": [{"metric": "exit.reason", "op": "<", "value": "x"}]}, "numeric"),
    ({"all": [{"metric": "entry.rsi", "op": "<", "value": "high"}]}, "numeric value"),
    ({"all": [{"metric": "entry.rsi", "op": "in", "value": []}]}, "non-empty"),
    ({"all": []}, "at least one condition"),
    ({"all": [{"metric": "entry.rsi", "op": "<", "value": 1}] * 6}, "too many conditions"),
    ({"all": [{"metric": "entry.rsi", "op": "<", "value": 1}], "any": []}, "does not have enough|has too many|should not"),
    ({"all": [{"metric": "entry.rsi", "op": "<", "value": 1, "extra": 1}]}, "Additional properties"),
    ("not json", "not valid JSON"),
])
def test_dsl_rejects_unknown_money_and_malformed(bad, fragment):
    with pytest.raises(dsl.DslError) as info:
        dsl.parse_predicate(bad)
    import re
    assert re.search(fragment, str(info.value))


def test_dsl_accepts_indicator_patterns_and_text_equality():
    assert dsl.parse_predicate({"any": [{"metric": "entry.prev.sma_fast", "op": ">", "value": 1}, {"metric": "exit.rsi", "op": "<", "value": 70}]})
    assert dsl.parse_predicate({"all": [{"metric": "exit.reason", "op": "in", "value": ["SIGNAL", "FLATTEN"]}]})
    assert dsl.parse_predicate({"all": [{"metric": "opt.snapshot_linked", "op": "==", "value": True}]})


def test_dsl_evaluation_semantics():
    p_all = dsl.parse_predicate({"all": [{"metric": "entry.rsi", "op": "<", "value": 25}, {"metric": "exit.reason", "op": "==", "value": "SIGNAL"}]})
    assert dsl.evaluate(p_all, {"entry.rsi": 20, "exit.reason": "SIGNAL"}) is True
    assert dsl.evaluate(p_all, {"entry.rsi": 30, "exit.reason": "SIGNAL"}) is False
    assert dsl.evaluate(p_all, {"entry.rsi": 20}) is None            # a missing metric is "not applicable", not False
    assert dsl.evaluate(p_all, {"entry.rsi": 30}) is False           # one definite False decides
    p_any = dsl.parse_predicate({"any": [{"metric": "entry.rsi", "op": "<", "value": 25}, {"metric": "entry.close", "op": ">", "value": 100}]})
    assert dsl.evaluate(p_any, {"entry.close": 150}) is True and dsl.evaluate(p_any, {"entry.rsi": 40}) is None
    assert dsl.evaluate(p_any, {"entry.rsi": 40, "entry.close": 50}) is False


def test_dsl_effect_and_equivalence():
    assert dsl.parse_effect('{"outcome_metric": "cost_bps", "direction": "positive"}') == {"outcome_metric": "outcome.cost_bps", "direction": "positive"}
    for bad in ('{"outcome_metric": "net_pnl", "direction": "negative"}', '{"outcome_metric": "cost_bps", "direction": "up"}', '{"outcome_metric": "cost_bps"}', "nope"):
        with pytest.raises(dsl.DslError):
            dsl.parse_effect(bad)
    a = dsl.parse_predicate({"all": [{"metric": "entry.rsi", "op": "<", "value": 25}]})
    assert dsl.equivalent(a, dsl.parse_predicate({"all": [{"metric": "entry.rsi", "op": "<", "value": 26}]}))
    assert not dsl.equivalent(a, dsl.parse_predicate({"all": [{"metric": "entry.rsi", "op": "<", "value": 40}]}))
    assert not dsl.equivalent(a, dsl.parse_predicate({"all": [{"metric": "entry.rsi", "op": ">", "value": 25}]}))


# =====================================================================================================
# statistics helpers
# =====================================================================================================
def test_bootstrap_is_seeded_and_honest_about_tiny_samples():
    assert stats.bootstrap_ci([], 1) == (None, None, None)
    assert stats.bootstrap_ci([5.0], 1) == (5.0, None, None)
    a = stats.bootstrap_ci([1, 2, 3, 4, 5, 6], seed=7)
    assert a == stats.bootstrap_ci([1, 2, 3, 4, 5, 6], seed=7) and a[1] < a[0] < a[2]
    assert stats.brier([(0.7, 1), (0.7, 0)]) == pytest.approx((0.09 + 0.49) / 2)


# =====================================================================================================
# lesson creation
# =====================================================================================================
def test_lesson_created_from_a_matching_validated_proposal_excludes_its_origin_trade():
    origin, lesson = new_lesson()
    assert lesson.status == "HYPOTHESIS" and lesson.origin_episode_id == origin.pk
    assert lesson.support_n == 0 and lesson.contradict_n == 0       # the trade that proposed it is not evidence for it
    evidence = LessonEvidence.objects.get(lesson=lesson)
    assert evidence.is_origin and evidence.weight == 0
    assert LearningEvent.objects.filter(type="LESSON_CREATED", lesson=lesson).count() == 1


def test_invalid_nonmatching_duplicate_and_capped_proposals_are_rejected_and_recorded():
    origin = make_episode(-50, rsi=20.0)
    review = review_with(origin, [
        proposal("unknown metric", {"all": [{"metric": "made.up", "op": "<", "value": 1}]}),
        proposal("does not match", {"all": [{"metric": "entry.rsi", "op": ">", "value": 60}]}),
        proposal("good one"),
        proposal("same again", {"all": [{"metric": "entry.rsi", "op": "<", "value": 26}]}),   # equivalent to the previous
    ])
    created = learning.process_proposals(review)
    assert [l.statement for l in created] == ["good one"]
    reasons = " | ".join(" ".join(r["reasons"]) for r in TradeReview.objects.get(pk=review.pk).rejected_items)
    assert "unknown metric" in reasons and "does not match the trade it came from" in reasons and "duplicate of lesson" in reasons


def test_weekly_cap_on_new_lessons(settings):
    settings.INTELLIGENCE_LEARNING_THRESHOLDS = {"max_new_lessons_per_week": 2}
    origin = make_episode(-50, rsi=20.0, extra={"entry.close": 100.0})
    proposals = [proposal(f"lesson {i}", {"all": [{"metric": "entry.close", "op": ">", "value": value}]})
                 for i, value in enumerate((10, 25, 45, 70))]
    review = review_with(origin, proposals)
    created = learning.process_proposals(review)
    assert len(created) == 2
    rejected = TradeReview.objects.get(pk=review.pk).rejected_items
    assert len(rejected) == 2 and all("weekly cap" in " ".join(r["reasons"]) for r in rejected)


def test_fallback_reviews_create_no_lessons():
    origin = make_episode(-50)
    assert learning.process_proposals(review_with(origin, [proposal()], fallback=True)) == []
    assert Lesson.objects.count() == 0


# =====================================================================================================
# lifecycle on synthetic streams
# =====================================================================================================
def feed(lesson, rsi_hits, bps, *, mode="LIVE", offset_days=0):
    last = None
    for _ in range(rsi_hits):
        last = make_episode(bps, rsi=20.0, mode=mode, offset_days=offset_days)
    return last


def test_hypothesis_becomes_supported_only_with_enough_agreeing_trades():
    origin, lesson = new_lesson()
    for n in range(1, 9):                       # 8 losing matching trades: required n is 9 with one lesson under test
        episode = make_episode(-80, rsi=20.0, mode="LIVE")
        learning.evaluate_episode(episode)
    lesson.refresh_from_db()
    assert lesson.support_n == 8 and lesson.status == "HYPOTHESIS" and learning.required_n(learning.thresholds()) == 9
    make_episode(60, rsi=70.0, mode="LIVE")     # non-matching winners give the effect something to compare with
    make_episode(40, rsi=65.0, mode="LIVE")
    episode = make_episode(-80, rsi=20.0, mode="LIVE")
    learning.evaluate_episode(episode)
    lesson.refresh_from_db()
    assert lesson.status == "SUPPORTED" and lesson.support_n == 9 and lesson.supported_at is not None
    assert lesson.effect_size < 0 and lesson.effect_ci_low is not None and lesson.effect_ci_high < 0
    assert LearningEvent.objects.filter(type="LESSON_PROMOTED", lesson=lesson).count() == 1


def test_established_needs_forward_trades_confirmed_after_promotion():
    origin, lesson = new_lesson()
    make_episode(60, rsi=70.0, mode="LIVE")
    make_episode(40, rsi=65.0, mode="LIVE")
    for _ in range(25):                                         # lots of HISTORY: never enough on its own
        make_episode(-80, rsi=20.0, mode="LIVE", offset_days=-30)
    learning.evaluate_episode(TradeEpisode.objects.latest("pk"))
    lesson.refresh_from_db()
    assert lesson.status == "SUPPORTED" and lesson.support_n == 25 and lesson.n_forward == 0
    for _ in range(7):                                          # 7 forward trades: not enough yet
        make_episode(-80, rsi=20.0, mode="LIVE", offset_days=5)
    learning.evaluate_episode(TradeEpisode.objects.latest("pk"))
    lesson.refresh_from_db()
    assert lesson.status == "SUPPORTED" and lesson.n_forward == 7
    make_episode(-80, rsi=20.0, mode="LIVE", offset_days=5)
    learning.evaluate_episode(TradeEpisode.objects.latest("pk"))
    lesson.refresh_from_db()
    assert lesson.status == "ESTABLISHED" and lesson.established_at is not None and lesson.n_forward == 8


def test_a_supported_lesson_is_refuted_by_forward_trades_that_contradict_it():
    origin, lesson = new_lesson()
    make_episode(60, rsi=70.0, mode="LIVE")
    make_episode(40, rsi=65.0, mode="LIVE")
    for _ in range(10):
        make_episode(-80, rsi=20.0, mode="LIVE", offset_days=-10)
    learning.evaluate_episode(TradeEpisode.objects.latest("pk"))
    lesson.refresh_from_db()
    assert lesson.status == "SUPPORTED"
    for _ in range(8):                                          # forward reality disagrees: the pattern won
        make_episode(150, rsi=20.0, mode="LIVE", offset_days=5)
    learning.evaluate_episode(TradeEpisode.objects.latest("pk"))
    lesson.refresh_from_db()
    assert lesson.status == "REFUTED"
    kinds = list(LearningEvent.objects.filter(lesson=lesson).values_list("type", flat=True))
    assert "LESSON_PROMOTED" in kinds and "LESSON_RETIRED" in kinds and "LESSON_CONTRADICTED" in kinds
    assert LearningEvent.objects.get(type="LESSON_RETIRED", lesson=lesson).payload["to"] == "REFUTED"
    before = lesson.contradict_n
    learning.evaluate_episode(TradeEpisode.objects.latest("pk"))   # terminal: no further changes
    lesson.refresh_from_db()
    assert lesson.contradict_n == before and lesson.status == "REFUTED"


def test_a_hypothesis_contradicted_from_the_start_is_refuted():
    origin, lesson = new_lesson()
    for _ in range(9):
        make_episode(200, rsi=20.0, mode="LIVE")
    learning.evaluate_episode(TradeEpisode.objects.latest("pk"))
    lesson.refresh_from_db()
    assert lesson.status == "REFUTED" and lesson.contradict_n == 9


def test_paper_and_delayed_data_trades_are_down_weighted():
    origin, live_lesson = new_lesson(instance_id=11, key="LIVE_STRAT", mode="LIVE")
    paper_origin = make_episode(-50, key="PAPER_STRAT", instance_id=12, mode="PAPER")
    paper_lesson = learning.process_proposals(review_with(paper_origin, [proposal("paper lesson")]))[0]
    for key, mode in (("LIVE_STRAT", "LIVE"), ("PAPER_STRAT", "PAPER")):
        make_episode(60, rsi=70.0, key=key, mode=mode, instance_id=11 if mode == "LIVE" else 12)
        make_episode(40, rsi=65.0, key=key, mode=mode, instance_id=11 if mode == "LIVE" else 12)
        for _ in range(9):
            make_episode(-80, rsi=20.0, key=key, mode=mode, instance_id=11 if mode == "LIVE" else 12)
        make_episode(90, rsi=20.0, key=key, mode=mode, instance_id=11 if mode == "LIVE" else 12)   # one contradiction
    learning.evaluate_lesson(live_lesson)
    learning.evaluate_lesson(paper_lesson)
    live_lesson.refresh_from_db()
    paper_lesson.refresh_from_db()
    assert (live_lesson.support_n, live_lesson.contradict_n) == (paper_lesson.support_n, paper_lesson.contradict_n) == (9, 1)
    assert live_lesson.status == "SUPPORTED" and paper_lesson.status == "HYPOTHESIS"
    assert paper_lesson.support_weight == pytest.approx(4.5) and live_lesson.support_weight == pytest.approx(9.0)
    delayed = make_episode(-80, rsi=20.0, key="LIVE_STRAT", mode="LIVE", instance_id=11, flags=["PAPER_DELAYED_DATA"])
    learning.evaluate_lesson(live_lesson)
    assert LessonEvidence.objects.get(lesson=live_lesson, episode=delayed).weight == 0.5


def test_reprocessing_is_idempotent():
    origin, lesson = new_lesson()
    episodes = [make_episode(-80, rsi=20.0, mode="LIVE") for _ in range(5)]
    for _ in range(3):
        for ep in episodes:
            learning.evaluate_episode(ep)
    lesson.refresh_from_db()
    assert lesson.support_n == 5 and LessonEvidence.objects.filter(lesson=lesson).count() == 6   # 5 + the origin row
    assert LearningEvent.objects.filter(type="LESSON_REINFORCED", lesson=lesson).count() == 5


def test_multiple_testing_guard_raises_the_bar_as_lessons_accumulate():
    base = learning.required_n(learning.thresholds())
    origin = make_episode(-50, rsi=20.0, extra={"entry.close": 100.0})
    for i in range(7):
        Lesson.objects.create(strategy_key="OTHER", statement=f"l{i}", predicate={"all": []}, predicate_hash=f"h{i}", expected_effect=EFFECT)
    assert learning.required_n(learning.thresholds()) == base + 3


def test_stale_lessons_are_retired():
    origin, lesson = new_lesson()
    Lesson.objects.filter(pk=lesson.pk).update(last_matched_at=timezone.now() - timedelta(days=400))
    assert learning.retire_stale_lessons() == 1
    assert Lesson.objects.get(pk=lesson.pk).status == "RETIRED"
    assert LearningEvent.objects.filter(type="LESSON_RETIRED", lesson=lesson).count() == 1


# =====================================================================================================
# performance, calibration, suggestion outcomes
# =====================================================================================================
def test_performance_is_per_currency_with_n_and_intervals():
    for bps in (100, 200, -50, 150, 80, 60, 90, 120):
        make_episode(bps, currency="USD", instance_id=1)
    for bps in (-300, -100, 50):
        make_episode(bps, currency="INR", key="RPS_OPTIONS_AUTO_SELECTOR", instance_id=2, symbol="NIFTY")
    make_episode(40, currency="USD", key="DONCHIAN_BREAKOUT", instance_id=3, symbol="MSFT")
    analytics.refresh_performance()
    usd = PerformanceStat.objects.get(strategy_key="SMA_CROSSOVER", strategy_version=0, instrument="", regime="", structure="", mode="", risk_profile="", context="", currency="USD")
    assert usd.n == 8 and usd.wins == 7 and usd.mean_return_bps == pytest.approx(93.75)
    assert usd.ci_low < usd.mean_return_bps < usd.ci_high
    inr = PerformanceStat.objects.get(strategy_key="RPS_OPTIONS_AUTO_SELECTOR", strategy_version=0, instrument="", regime="", structure="", mode="", risk_profile="", context="", currency="INR")
    assert inr.n == 3 and inr.sum_net_pnl == D("-35.0")  # (-300 - 100 + 50) / 10, in rupees only
    assert not PerformanceStat.objects.filter(currency="INR", strategy_key="SMA_CROSSOVER").exists()
    solo = PerformanceStat.objects.get(strategy_key="DONCHIAN_BREAKOUT", strategy_version=0, instrument="", regime="", structure="", mode="", risk_profile="", context="", currency="USD")
    assert solo.n == 1 and solo.ci_low is None and solo.ci_high is None            # one trade has no interval
    assert PerformanceStat.objects.filter(instrument="AAPL", strategy_key="", currency="USD").exists()   # other dimensions exist too
    before = PerformanceStat.objects.count()
    analytics.refresh_performance()
    assert PerformanceStat.objects.count() == before


def test_stat_shift_event_only_for_well_sampled_means():
    for bps in (100,) * 8:
        make_episode(bps, mode="LIVE")
    analytics.refresh_performance()
    assert not LearningEvent.objects.filter(type="STAT_SHIFT").exists()
    for bps in (-400,) * 8:
        make_episode(bps, mode="LIVE")
    analytics.refresh_performance()
    shifts = LearningEvent.objects.filter(type="STAT_SHIFT")
    assert shifts.exists() and "small samples" in shifts.first().payload["note"]
    analytics.refresh_performance()
    count = shifts.count()
    analytics.refresh_performance()
    assert LearningEvent.objects.filter(type="STAT_SHIFT").count() == count


def test_calibration_bins_brier_and_no_alarm_on_tiny_samples():
    for i in range(5):
        make_episode(100 if i % 2 == 0 else -100, pop=0.7)
    analytics.refresh_calibration()
    assert not LearningEvent.objects.filter(type="CALIBRATION_SHIFT").exists()          # n = 5: never alarm
    for i in range(15):
        make_episode(-100 if i % 3 else 100, pop=0.7)
    analytics.refresh_calibration()
    row = CalibrationStat.objects.get(metric="POP", strategy_key="", bin="ALL")
    assert row.n == 20 and row.brier is not None and 0 < row.brier < 1
    bin_row = CalibrationStat.objects.get(metric="POP", strategy_key="", bin="0.7-0.8")
    assert bin_row.n == 20 and bin_row.predicted_mean == pytest.approx(0.7) and bin_row.observed_rate == pytest.approx(0.4)
    assert LearningEvent.objects.filter(type="CALIBRATION_SHIFT").count() >= 1
    analytics.refresh_calibration()
    assert LearningEvent.objects.filter(type="CALIBRATION_SHIFT", payload__bin="0.7-0.8", payload__strategy_key="ALL").count() == 1


def _applied_suggestion():
    pf = h.portfolio("USD", "DU-OUT")
    strat = h.stock_strategy(pf, h.aapl())
    update_instance(StrategyInstance.objects.get(pk=strat.pk), {"parameters": {**strat.parameters, "fast_window": 12}})
    v2 = StrategyInstance.objects.get(pk=strat.pk).versions.get(version=2)
    review = review_with(make_episode(0, instance_id=strat.pk, version=1), [])
    suggestion = Suggestion.objects.create(
        review=review, strategy_key="SMA_CROSSOVER", instance_id=strat.pk, instance_name="x", kind="PARAMETER",
        parameter_key="fast_window", current_value=20, proposed_value=12, status="APPLIED", applied_version_id=v2.pk,
        idempotency_key="sugg-out-1")
    return strat, suggestion


def test_suggestion_outcome_helped_hurt_inconclusive_and_waits_for_enough_trades():
    strat, suggestion = _applied_suggestion()
    for bps in (-100, -120, -80, -90):
        make_episode(bps, instance_id=strat.pk, version=1)
    for bps in (100, 120, 90, 110, 95, 105, 115):
        make_episode(bps, instance_id=strat.pk, version=2)
    assert analytics.resolve_suggestion_outcomes() == 0 and not SuggestionOutcome.objects.exists()   # only 7 trades after
    make_episode(100, instance_id=strat.pk, version=2)
    assert analytics.resolve_suggestion_outcomes() == 1
    outcome = SuggestionOutcome.objects.get()
    assert outcome.result == "HELPED" and outcome.n_after == 8 and outcome.n_before >= 4 and outcome.ci_low > 0
    assert LearningEvent.objects.filter(type="SUGGESTION_RESOLVED").count() == 1
    assert analytics.resolve_suggestion_outcomes() == 0                                   # idempotent
    other = Suggestion.objects.create(
        review=suggestion.review, strategy_key="SMA_CROSSOVER", instance_id=strat.pk, instance_name="x", kind="PARAMETER",
        parameter_key="fast_window", current_value=12, proposed_value=10, status="APPLIED",
        applied_version_id=suggestion.applied_version_id, idempotency_key="sugg-out-2")
    assert analytics.resolve_suggestion_outcomes() == 1 and other.outcome.n_after == 8


def test_suggestion_outcome_hurt_and_inconclusive_decisions():
    strat, suggestion = _applied_suggestion()
    for bps in (100, 120, 80, 90):
        make_episode(bps, instance_id=strat.pk, version=1)
    for bps in (-100, -90, -110, -95, -105, -120, -85, -100):
        make_episode(bps, instance_id=strat.pk, version=2)
    analytics.resolve_suggestion_outcomes()
    outcome = SuggestionOutcome.objects.get()
    assert outcome.result == "HURT" and outcome.ci_high < 0 and outcome.n_after == 8


def test_suggestion_outcome_noisy_data_is_inconclusive():
    strat, suggestion = _applied_suggestion()
    for bps in (100, -120, 80, -90):
        make_episode(bps, instance_id=strat.pk, version=1)
    for bps in (100, -100, 110, -95, 105, -120, 85, -100):
        make_episode(bps, instance_id=strat.pk, version=2)
    analytics.resolve_suggestion_outcomes()
    assert SuggestionOutcome.objects.get().result == "INCONCLUSIVE"


# =====================================================================================================
# integration: pipeline, prompt, facts, report, tasks
# =====================================================================================================
def test_a_newer_code_prompt_is_only_a_candidate_until_it_beats_the_active_one():
    PromptTemplate.objects.create(name="trade_review", version=1, system_prompt="old", active=True)
    active = prompts.get_active_template()
    assert active.version == 1 and active.active                       # the active prompt keeps being used
    candidate = PromptTemplate.objects.get(name="trade_review", version=prompts.TEMPLATE_VERSION)
    assert candidate.active is False and "DSL" in candidate.system_prompt and "predicate" in candidate.system_prompt
    assert prompts.get_active_template().version == 1                  # idempotent
    PromptTemplate.objects.all().delete()
    assert prompts.get_active_template().version == prompts.TEMPLATE_VERSION   # first use activates the code version


def test_end_to_end_review_creates_a_lesson_and_the_report_and_next_prompt_use_it():
    strat, episode = closed_stock()
    resp = good_response(lesson_proposals=[{
        "statement": "Entries when the fast average is above 200 have been profitable.",
        "predicate": json.dumps({"all": [{"metric": "entry.sma_fast", "op": ">", "value": 200}]}),
        "expected_effect": json.dumps({"outcome_metric": "net_return_bps", "direction": "positive"})}])
    result = review_episode(episode.pk, client=FakeLLMClient("valid", resp))
    lesson = Lesson.objects.get()
    assert not result.is_fallback and lesson.status == "HYPOTHESIS" and lesson.created_from_review_id == result.pk
    assert lesson.origin_episode_id == episode.pk and lesson.support_n == 0
    md = report.to_markdown(report.build_report(TradeEpisode.objects.get(pk=episode.pk)))
    assert "New lesson hypotheses proposed by this trade" in md and "fast average is above 200" in md
    facts, _, _ = facts_module.build_facts(TradeEpisode.objects.get(pk=episode.pk), 20000)
    assert facts["retrieved_lessons.count"] == 1 and facts["retrieved_lessons[0].status"] == "HYPOTHESIS"
    assert facts["strategy_stats.available"] is False        # nothing computed yet
    assert tasks.learn_from_episode(episode.pk)["performance_rows"] > 0
    facts, _, _ = facts_module.build_facts(TradeEpisode.objects.get(pk=episode.pk), 20000)
    assert facts["strategy_stats.n"] == 1 and "strategy_stats.ci_low_bps" in facts


def test_tasks_never_raise_and_respect_the_flag(settings, monkeypatch):
    strat, episode = closed_stock()
    assert tasks.learn_from_episode(episode.pk)["lessons"] == 0
    monkeypatch.setattr(learning, "evaluate_episode", lambda e: (_ for _ in ()).throw(RuntimeError("x")))
    assert tasks.learn_from_episode(episode.pk) == {"error": True}
    assert tasks.daily_learning()["retired"] == 0
    settings.INTELLIGENCE_ENABLED = False
    assert tasks.learn_from_episode(episode.pk) == {"skipped": "disabled"} and tasks.daily_learning() == {"skipped": "disabled"}


def test_closing_an_episode_queues_learning(monkeypatch, django_capture_on_commit_callbacks):
    queued = []
    monkeypatch.setattr(tasks.learn_from_episode, "apply_async", lambda args, **k: queued.append(args))
    monkeypatch.setattr(tasks.run_trade_review, "apply_async", lambda args, **k: None)
    pf = h.portfolio("USD", "DU-LRN")
    inst = h.aapl()
    strat = h.stock_strategy(pf, inst)
    h.fill(pf, inst, "BUY", 5, "10", "q1", instance=strat, when=h.at(3))
    h.fill(pf, inst, "SELL", 5, "12", "q2", instance=strat, when=h.at(2))
    with django_capture_on_commit_callbacks(execute=True):
        build_or_update_episodes(strat.pk)
    assert queued == [(TradeEpisode.objects.get().pk,)]


# =====================================================================================================
# API, digest, export
# =====================================================================================================
def staff():
    get_user_model().objects.create_user("learn-boss", password="pw", is_staff=True)
    client = Client()
    client.login(username="learn-boss", password="pw")
    return client


def test_learning_api_feed_lessons_performance_calibration_digest():
    origin, lesson = new_lesson()
    for _ in range(3):
        make_episode(-80, rsi=20.0, mode="LIVE", pop=0.7)
    learning.evaluate_episode(TradeEpisode.objects.latest("pk"))
    analytics.refresh_performance()
    analytics.refresh_calibration()
    client = staff()
    feed_body = client.get("/api/v1/intelligence/learning-feed/").json()
    types = {e["type"] for e in feed_body["data"]}
    assert {"LESSON_CREATED", "LESSON_REINFORCED"} <= types and "expected" in feed_body["meta"]["note"]
    cursor = feed_body["meta"]["cursor"]
    assert client.get(f"/api/v1/intelligence/learning-feed/?since={cursor}").json()["data"] == []
    assert client.get("/api/v1/intelligence/learning-feed/?since=x").status_code == 400
    listing = client.get("/api/v1/intelligence/lessons/?status=HYPOTHESIS").json()
    assert listing["meta"]["total"] == 1 and listing["data"][0]["n"] == 3 and listing["data"][0]["posterior_mean"] > 0.5
    detail = client.get(f"/api/v1/intelligence/lessons/{lesson.pk}/").json()["data"]
    assert any(e["origin"] and e["weight"] == 0 for e in detail["evidence"]) and "never counts as evidence" in detail["note"]
    assert client.get("/api/v1/intelligence/lessons/99999/").status_code == 404
    perf = client.get("/api/v1/intelligence/performance/?currency=USD&strategy_key=SMA_CROSSOVER").json()
    assert perf["data"] and all(r["currency"] == "USD" for r in perf["data"]) and "never summed across currencies" in perf["meta"]["note"]
    assert client.get("/api/v1/intelligence/calibration/").json()["data"]
    body = client.get("/api/v1/intelligence/digest/?days=1").json()["data"]
    assert body["closed_trades"]["total"] == 4 and body["lessons"]["created"] and any("noise" in n for n in body["notes"])
    text = client.get("/api/v1/intelligence/digest/?format=text")
    assert text["Content-Type"].startswith("text/plain") and "Closed trades: 4" in text.content.decode()
    assert client.get("/api/v1/intelligence/digest/?days=x").status_code == 400


def test_digest_is_honest_when_nothing_is_supported():
    text = digest.to_text(digest.build_digest(days=1))
    assert "Closed trades: 0" in text and "No lesson is supported yet" in text and "expected" in text


def test_export_dataset_writes_jsonl_with_manifest_and_no_secrets(tmp_path):
    strat, episode = closed_stock()
    review_episode(episode.pk, client=FakeLLMClient("valid", good_response()))
    call_command("export_intelligence_dataset", "--out", str(tmp_path), verbosity=0)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["files"]["episodes.jsonl"]["rows"] == 1 and manifest["files"]["reviews.jsonl"]["rows"] == 1
    import hashlib
    for name, info in manifest["files"].items():
        assert hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() == info["sha256"]
        assert len((tmp_path / name).read_text().splitlines()) == info["rows"]
    assert not (tmp_path / "evidence_snapshots.jsonl").exists() and not (tmp_path / "llm_run_log.jsonl").exists()
    everything = "".join(p.read_text() for p in tmp_path.glob("*.jsonl"))
    assert "DU-REVIEW-ACCT" not in everything and "265598" not in everything
    call_command("export_intelligence_dataset", "--out", str(tmp_path / "full"), "--include-evidence", verbosity=0)
    assert (tmp_path / "full" / "evidence_snapshots.jsonl").exists()
    assert "training" in manifest["note"] or "analysis" in manifest["note"]


def test_flush_guard_still_protects_lessons():
    from apps.intelligence.guard import flush_refusal_message
    new_lesson()
    message = flush_refusal_message()
    assert "intel_lesson" in message and "intel_trade_episode" in message
