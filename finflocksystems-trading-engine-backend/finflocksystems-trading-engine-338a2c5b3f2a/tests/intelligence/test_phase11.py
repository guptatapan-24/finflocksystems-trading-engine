"""Phase 11: multiple-testing control, partial pooling, edge-decay monitor, context-aware lessons and the "try next time" playbook.

Statistics are checked against hand-computed values and by simulation (false-alarm and detection rates); the lifecycle
tests use the real services on synthetic closed episodes (the same helpers the Phase 5 tests use), FakeLLM only (R9)."""
import json
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from apps.intelligence import tasks
from apps.intelligence.models import (
    ChangePointState, LearningEvent, Lesson, MistakeFinding, PerformanceStat, Suggestion, SuggestionOutcome, TradeEpisode)
from apps.intelligence.services import analytics, changepoint, counterfactuals, digest, dsl, learning, playbook, stats
from tests.intelligence.test_learning import PRED, make_episode, proposal, review_with

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def on(settings):
    settings.INTELLIGENCE_ENABLED = True
    settings.INTELLIGENCE_REVIEWS_ENABLED = True
    settings.INTELLIGENCE_LLM_PROVIDER = "fake"
    settings.INTELLIGENCE_MODEL_REVIEW = "fake-flash"


# =================================================================================================== statistics
def test_benjamini_hochberg_matches_the_hand_computed_textbook_example():
    p = [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, 0.212, 0.216]
    q = stats.bh_adjust(p)
    expected = [0.01, 0.04, 0.084, 0.084, 0.084, 0.1, 0.1057, 0.216, 0.216, 0.216]
    assert [round(x, 4) for x in q] == pytest.approx(expected, abs=1e-4)
    assert stats.benjamini_hochberg(p, 0.05) == [True, True] + [False] * 8
    assert all(q[i] >= p[i] for i in range(10))  # adjusting never makes a result look better


def test_a_test_that_could_not_be_run_is_not_part_of_the_family():
    assert stats.bh_adjust([0.01, None, 0.04]) == [pytest.approx(0.02), None, pytest.approx(0.04)]
    assert stats.benjamini_hochberg([None, None]) == [False, False]


def test_permutation_pvalue_is_directional_seeded_and_refuses_tiny_samples():
    a, b = [10, 12, 11, 13, 12, 14, 13, 15], [1, 2, 3, 2, 1, 3, 2, 1]
    assert stats.permutation_pvalue(a, b, "positive", seed=1) < 0.01
    assert stats.permutation_pvalue(a, b, "negative", seed=1) > 0.95
    assert stats.permutation_pvalue(a, b, "positive", seed=1) == stats.permutation_pvalue(a, b, "positive", seed=1)
    assert stats.permutation_pvalue([1], b) is None and stats.permutation_pvalue(a, [2]) is None


def test_permutation_test_does_not_cry_wolf_on_pure_noise():
    import numpy as np

    rng = np.random.default_rng(7)
    hits = sum(1 for i in range(60)
               if stats.permutation_pvalue(list(rng.normal(0, 1, 10)), list(rng.normal(0, 1, 20)), "positive", seed=i, resamples=400) <= 0.05)
    assert hits <= 8  # about 5% expected (3 of 60); well below a lenient bound


def test_signflip_pvalue():
    assert stats.signflip_pvalue([5, 6, 7, 8, 9, 6], "positive", seed=1) < 0.03
    assert stats.signflip_pvalue([5, -6, 7, -8, 9, -6], "positive", seed=1) > 0.3
    assert stats.signflip_pvalue([1, 2], "positive") is None


def test_partial_pooling_shrinks_small_slices_more_and_leaves_big_ones_alone():
    cells = [{"key": i, "n": n, "mean": m, "var": 100.0} for i, (n, m) in enumerate([(2, 80.0), (40, 10.0), (35, 12.0), (30, 8.0)])]
    pooled = stats.partial_pool(cells)
    family_mean = sum(c["mean"] * c["n"] for c in cells) / sum(c["n"] for c in cells)
    assert pooled[0]["shrinkage"] < pooled[1]["shrinkage"] and pooled[1]["shrinkage"] > 0.9  # n = 2 is pulled hard, n = 40 barely
    assert family_mean < pooled[0]["shrunk_mean"] < 80.0  # lands between the raw mean and the family
    assert abs(pooled[1]["shrunk_mean"] - 10.0) < 1.0
    assert all(v["shrunk_sd"] > 0 for v in pooled.values())


def test_partial_pooling_needs_three_slices_and_handles_identical_slices():
    two = stats.partial_pool([{"key": "a", "n": 3, "mean": 5.0, "var": 4.0}, {"key": "b", "n": 3, "mean": 9.0, "var": 4.0}])
    assert two["a"] == {"shrunk_mean": 5.0, "shrunk_sd": None, "shrinkage": None}
    same = stats.partial_pool([{"key": i, "n": 10, "mean": 7.0, "var": 4.0} for i in range(4)])
    assert all(v["shrunk_mean"] == pytest.approx(7.0) for v in same.values())


def test_cusum_is_quiet_on_stable_data_and_finds_a_decay_close_to_where_it_started():
    stable = [1.0, -1.0] * 15
    assert stats.cusum(stable)["alarm"] is False
    series = [1.0, -1.0] * 5 + [-3.0, -2.5, -3.5, -3.0, -2.0, -3.0]
    result = stats.cusum(series)
    assert result["alarm"] and result["direction"] == "DOWN" and result["index"] == 16 and result["change_index"] == 11
    up = stats.cusum([1.0, -1.0] * 5 + [4.0, 3.5, 4.5, 4.0, 3.0, 4.0])
    assert up["alarm"] and up["direction"] == "UP"


def test_cusum_refuses_short_series_and_flat_baselines():
    assert stats.cusum([1, 2, 3]) == {"insufficient": True, "n": 3, "need": 15}
    flat = stats.cusum([5.0] * 10 + [4.0] * 8)
    assert flat["insufficient"] and "no spread" in flat["reason"]


def test_cusum_false_alarm_and_detection_rates_by_simulation():
    import numpy as np

    rng = np.random.default_rng(11)
    false_alarms = sum(stats.cusum(list(rng.normal(0, 1, 60)))["alarm"] for _ in range(300))
    delays = []
    for _ in range(200):
        series = list(rng.normal(0, 1, 10)) + list(rng.normal(-2.0, 1, 20))  # the edge falls by two standard deviations
        r = stats.cusum(series)
        if r["alarm"]:
            delays.append(r["index"] - 10)
    assert false_alarms / 300 < 0.12  # about 5% measured: stable strategies rarely trigger within 50 monitored trades
    assert len(delays) >= 190 and sum(delays) / len(delays) < 13  # a real two-sigma decay is caught in roughly ten trades
    heavy = sum(stats.cusum(list(rng.standard_t(4, 60) / 2 ** 0.5))["alarm"] for _ in range(300))
    assert heavy / 300 < 0.12  # fat-tailed returns (clipped standardised values) do not inflate false alarms


# ===================================================================================== lessons and the FDR gate
def make_cost_lesson(n_match=10, cost_match=-1.0, others=(0, 100, -100, 50, -50, 80, -80, 30, -30, 60, -60, 10)):
    """A lesson about cost_bps. The matching trades are all a hair below the mean of the others, which are very spread out:
    the old rule (posterior >= 0.8 and the right sign) would promote it, but the effect is indistinguishable from noise."""
    for value in others:
        make_episode(10, rsi=40.0, extra={"outcome.cost_bps": float(value)})
    for _ in range(n_match - 1):
        make_episode(10, rsi=20.0, extra={"outcome.cost_bps": float(cost_match)})
    origin = make_episode(10, rsi=20.0, extra={"outcome.cost_bps": float(cost_match)})
    effect = {"outcome_metric": "cost_bps", "direction": "negative"}
    (lesson,) = learning.process_proposals(review_with(origin, [proposal("Oversold entries cost less", PRED, effect)]))
    return Lesson.objects.get(pk=lesson.pk)


def test_a_consistent_but_tiny_effect_does_not_become_a_supported_lesson():
    lesson = make_cost_lesson()
    assert lesson.support_n >= 9 and lesson.contradict_n == 0  # unanimous direction: posterior is high
    assert lesson.p_value is not None and lesson.p_value > 0.2  # but the difference is well inside the noise
    assert lesson.q_value is not None and lesson.q_value > learning.thresholds()["fdr_q"]
    assert lesson.status == Lesson.Status.HYPOTHESIS  # the FDR gate holds it back


def test_a_strong_effect_is_promoted_with_its_p_and_q_values_in_the_event():
    for _ in range(12):
        make_episode(25, rsi=40.0)
    for _ in range(9):
        make_episode(-45, rsi=20.0)
    origin = make_episode(-50, rsi=20.0)
    (lesson,) = learning.process_proposals(review_with(origin, [proposal()]))
    lesson = Lesson.objects.get(pk=lesson.pk)
    assert lesson.status == Lesson.Status.SUPPORTED and lesson.p_value < 0.01 and lesson.q_value <= 0.10
    event = LearningEvent.objects.get(type=LearningEvent.Type.LESSON_PROMOTED, lesson=lesson)
    assert event.payload["p_value"] is not None and event.payload["q_value"] <= 0.10


def test_q_values_are_recomputed_across_all_live_lessons_so_more_tests_mean_stricter_evidence():
    for _ in range(12):
        make_episode(25, rsi=40.0)
    for _ in range(9):
        make_episode(-45, rsi=20.0)
    origin = make_episode(-50, rsi=20.0)
    (first,) = learning.process_proposals(review_with(origin, [proposal()]))
    q_alone = Lesson.objects.get(pk=first.pk).q_value
    second_origin = make_episode(-50, rsi=20.0, extra={"entry.sma_gap": 1.0})
    learning.process_proposals(review_with(second_origin, [proposal("Another pattern", {"all": [{"metric": "entry.sma_gap", "op": ">", "value": 0.5}]})]))
    learning.recompute_q_values()
    assert Lesson.objects.get(pk=first.pk).q_value >= q_alone  # adding a hypothesis to the family never loosens another's q
    live = Lesson.objects.exclude(status__in=["REFUTED", "RETIRED"])
    assert all(l.p_value is None or (l.q_value is not None and l.q_value >= l.p_value - 1e-12) for l in live)


def test_a_lesson_whose_test_cannot_be_run_has_no_q_value_and_is_never_promoted():
    make_episode(25, rsi=40.0)  # a single other trade: no comparison is possible
    origin = make_episode(-50, rsi=20.0)
    (lesson,) = learning.process_proposals(review_with(origin, [proposal()]))
    lesson = Lesson.objects.get(pk=lesson.pk)
    assert lesson.status == Lesson.Status.HYPOTHESIS and lesson.q_value is None


# ===================================================================================== context-aware lessons
def test_the_lesson_language_accepts_context_and_operator_keys_and_still_rejects_money_and_unknown_keys():
    ok = dsl.parse_predicate({"all": [{"metric": "ctx.event_in_hold", "op": "==", "value": True},
                                      {"metric": "operator.hours_since_last_change_before_entry", "op": "<=", "value": 24},
                                      {"metric": "selection.risk_profile", "op": "==", "value": "NEUTRAL"}]})
    assert dsl.metrics_used(ok) == {"ctx.event_in_hold", "operator.hours_since_last_change_before_entry", "selection.risk_profile"}
    for bad in ("ctx.made_up", "operator.password", "ctx.event_in_hold.extra"):
        with pytest.raises(dsl.DslError):
            dsl.parse_predicate({"all": [{"metric": bad, "op": "==", "value": True}]})
    assert dsl.evaluate(ok, {"ctx.event_in_hold": True, "operator.hours_since_last_change_before_entry": 3, "selection.risk_profile": "NEUTRAL"}) is True
    assert dsl.evaluate(ok, {"ctx.event_in_hold": True}) is None  # a missing metric makes the predicate not applicable


def test_a_context_lesson_is_tested_only_on_trades_where_the_context_was_measured():
    for _ in range(6):
        make_episode(30, extra={"ctx.available": True, "ctx.event_in_hold": False})
    for _ in range(6):
        make_episode(30)  # no ctx keys: not applicable, must not count as "no event"
    for _ in range(9):
        make_episode(-45, extra={"ctx.available": True, "ctx.event_in_hold": True})
    origin = make_episode(-50, extra={"ctx.available": True, "ctx.event_in_hold": True})
    pred = {"all": [{"metric": "ctx.event_in_hold", "op": "==", "value": True}]}
    (lesson,) = learning.process_proposals(review_with(origin, [proposal("Holding through events loses", pred)]))
    lesson = Lesson.objects.get(pk=lesson.pk)
    assert lesson.support_n == 9 and lesson.contradict_n == 0
    others = [e for e in TradeEpisode.objects.all() if "ctx.event_in_hold" not in e.metrics_row.metrics]
    assert len(others) == 6 and lesson.status == Lesson.Status.SUPPORTED


def test_operator_metrics_and_risk_profile_are_added_for_closed_trades_only(monkeypatch):
    from types import SimpleNamespace

    from apps.intelligence.services import evidence, metrics as metrics_module

    block = {"hours_since_last_change_before_entry": 3.5, "changes_during_hold": 1, "manual_orders_during_hold": 0,
             "profile_changed_during_hold": False, "profile": {"name": "AGGRESSIVE"}}
    monkeypatch.setattr(evidence, "operator_block", lambda *a, **k: block)
    inst = SimpleNamespace(instrument=SimpleNamespace(symbol="NIFTY"))
    out = {}
    metrics_module._operator(inst, {"closed": True, "opened_at": None, "closed_at": None, "version_number": 2}, out)
    assert out == {"operator.hours_since_last_change_before_entry": 3.5, "operator.changes_during_hold": 1,
                   "operator.manual_orders_during_hold": 0, "operator.profile_changed_during_hold": False, "selection.risk_profile": "AGGRESSIVE"}
    still_open = {}
    metrics_module._operator(inst, {"closed": False}, still_open)
    assert still_open == {} and metrics_module.METRICS_VERSION == "3"


# ============================================================================== performance slices and shrinkage
def test_performance_has_risk_profile_and_context_slices_with_shrinkage_but_skips_unprofiled_strategies():
    for symbol, count, bps in (("AAPL", 14, 20), ("MSFT", 12, 18), ("NVDA", 10, 22), ("AMD", 2, 120)):
        for i in range(count):
            make_episode(bps + (i % 3), symbol=symbol, extra={"selection.risk_profile": "NEUTRAL", "ctx.available": True, "ctx.event_in_hold": i % 4 == 0})
    make_episode(5, key="OTHER_STRATEGY")  # created without a risk profile
    analytics.refresh_performance()
    profile = PerformanceStat.objects.filter(strategy_key="SMA_CROSSOVER", risk_profile="NEUTRAL", instrument="", context="")
    assert profile.count() == 1 and profile.get().n == 38
    assert not PerformanceStat.objects.filter(strategy_key="OTHER_STRATEGY").exclude(risk_profile="").exists()  # no profile slice
    buckets = set(PerformanceStat.objects.exclude(context="").values_list("context", flat=True))
    assert buckets == {"EVENT_IN_HOLD", "NO_EVENT", "NO_CONTEXT"}
    by_symbol = {s.instrument: s for s in PerformanceStat.objects.filter(strategy_key="SMA_CROSSOVER", instrument__in=["AAPL", "MSFT", "NVDA", "AMD"], risk_profile="", context="")}
    assert by_symbol["AMD"].shrinkage < by_symbol["AAPL"].shrinkage  # two trades are pulled towards the family, fourteen are not
    assert by_symbol["AMD"].shrunk_mean_bps < by_symbol["AMD"].mean_return_bps  # the lucky small slice is pulled down
    assert by_symbol["AAPL"].shrunk_sd_bps is not None
    again = PerformanceStat.objects.count()
    analytics.refresh_performance()
    assert PerformanceStat.objects.count() == again  # idempotent


def test_shrinkage_never_pools_across_currencies():
    for currency in ("USD", "INR"):
        for symbol, count, bps in (("A", 10, 10), ("B", 10, 12), ("C", 10, 11)):
            for _ in range(count):
                make_episode(bps, symbol=symbol, currency=currency)
    analytics.refresh_performance()
    rows = PerformanceStat.objects.filter(strategy_key="SMA_CROSSOVER", instrument__in=["A", "B", "C"], risk_profile="", context="")
    assert {r.currency for r in rows} == {"USD", "INR"} and all(r.shrinkage is not None for r in rows)


# ================================================================================================ edge monitor
def series_for(key, returns, currency="USD", **kw):
    for i, bps in enumerate(returns):
        make_episode(bps, key=key, currency=currency, offset_days=i, **kw)


def test_edge_decay_is_flagged_with_n_baseline_and_an_estimated_start_and_only_once():
    series_for("DECAYING", [20, -10] * 5 + [-60, -55, -65, -58, -62, -57])
    series_for("STEADY", [20, -10] * 12)
    series_for("YOUNG", [20, -10, 5])
    assert changepoint.refresh() >= 3
    decay = ChangePointState.objects.get(strategy_key="DECAYING", metric="RETURN")
    assert decay.status == "EDGE_DECAY" and decay.n == 16 and decay.reference_n == 10 and decay.change_index == 11
    assert decay.baseline_mean == pytest.approx(5.0) and "not proof" in decay.note and decay.change_at is not None
    assert ChangePointState.objects.get(strategy_key="STEADY", metric="RETURN").status == "STABLE"
    young = ChangePointState.objects.get(strategy_key="YOUNG", metric="RETURN")
    assert young.status == "INSUFFICIENT" and "at least 15" in young.note
    assert LearningEvent.objects.filter(type="CHANGE_POINT").count() == 1
    changepoint.refresh()
    assert LearningEvent.objects.filter(type="CHANGE_POINT").count() == 1  # recomputing does not repeat the alarm


def test_improvement_is_reported_as_improvement_and_currencies_are_separate_series():
    series_for("RISING", [20, -10] * 5 + [90, 85, 95, 88, 92, 87])
    series_for("RISING", [20, -10] * 12, currency="INR")
    changepoint.refresh()
    assert ChangePointState.objects.get(strategy_key="RISING", currency="USD", metric="RETURN").status == "EDGE_IMPROVEMENT"
    assert ChangePointState.objects.get(strategy_key="RISING", currency="INR", metric="RETURN").status == "STABLE"


def test_overconfident_probability_of_profit_is_flagged_as_calibration_drift():
    for i in range(20):
        make_episode(-30, key="OPT_STRAT", offset_days=i, extra={"opt.pop": 0.8})  # predicted 80% wins, every trade lost
    for i in range(20):
        make_episode(30 if i % 5 else -30, key="OPT_OK", offset_days=i, extra={"opt.pop": 0.8})  # wins 80%: calibrated
    changepoint.refresh()
    bad = ChangePointState.objects.get(strategy_key="OPT_STRAT", metric="POP_CALIBRATION")
    good = ChangePointState.objects.get(strategy_key="OPT_OK", metric="POP_CALIBRATION")
    assert bad.status == "EDGE_DECAY" and good.status == "STABLE"
    assert ChangePointState.objects.filter(strategy_key="OPT_STRAT", metric="RETURN").exists()


def test_edge_monitor_endpoint_always_carries_n_and_a_warning():
    series_for("DECAYING", [20, -10] * 5 + [-60, -55, -65, -58, -62, -57])
    changepoint.refresh()
    body = Client().get("/api/v1/intelligence/changepoints/?strategy_key=DECAYING").json()
    row = body["data"][0]
    assert row["status"] == "EDGE_DECAY" and row["n"] == 16 and row["change_index"] == 11
    assert "not a verdict" in body["meta"]["note"]


# =============================================================================================== the playbook
def finding(review, rule_id, severity="MEDIUM", defect=False, text="A rule finding."):
    return MistakeFinding.objects.create(review=review, category="REGIME_MISMATCH", severity=severity, description=text,
                                         evidence_refs=["metrics.ctx.event_in_hold"], source=MistakeFinding.Source.RULE, rule_id=rule_id, is_defect=defect)


def trade_with_findings(rules, **kw):
    episode = make_episode(-40, extra={"ctx.available": True, "ctx.event_in_hold": True, "margin.rps": 1.0, "operator.manual_orders_during_hold": 1}, **kw)
    review = review_with(episode, [])
    for rule in rules:
        if isinstance(rule, tuple):
            finding(review, rule[0], rule[1], rule[2])
        else:
            finding(review, rule)
    return episode, review


def test_at_most_three_experiments_ordered_by_severity_then_defects_and_never_advice():
    _, review = trade_with_findings([("ENTRY_MARGIN_TINY", "LOW", False), ("EVENT_IN_HOLD_UNACKNOWLEDGED", "MEDIUM", False),
                                      ("WATCH_BAND_FULL_SIZE", "MEDIUM", False), ("MANUAL_ORDER_DURING_HOLD", "MEDIUM", True),
                                      ("HIGH_COST_DRAG", "HIGH", False)])  # a rule with no measurement plan is simply skipped
    created = playbook.build_for_review(review)
    assert [c.experiment["rule_id"] for c in created] == ["MANUAL_ORDER_DURING_HOLD", "EVENT_IN_HOLD_UNACKNOWLEDGED", "WATCH_BAND_FULL_SIZE"]
    for s in created:
        assert s.kind == "EXPERIMENT" and s.status == "PROPOSED" and s.is_hypothesis and s.experiment["label"] == "HYPOTHESIS"
        assert s.experiment["paper_only"] is True and s.experiment["min_n"] == 8 and "stop" in s.experiment["stop_rule"].lower()
        assert "nothing is changed" in s.risk_of_change and s.parameter_key == "" and s.proposed_value is None
        assert dsl.parse_predicate(s.experiment["predicate"]) == s.experiment["predicate"]  # a valid, canonical lesson-language condition


def test_the_history_check_uses_past_trades_excludes_the_trade_itself_and_says_too_few_when_it_must():
    for _ in range(8):
        make_episode(-60, extra={"ctx.available": True, "ctx.event_in_hold": True})
    for _ in range(14):
        make_episode(25, extra={"ctx.available": True, "ctx.event_in_hold": False})
    _, review = trade_with_findings(["EVENT_IN_HOLD_UNACKNOWLEDGED"])
    (s,) = playbook.build_for_review(review)
    h = s.experiment["history_check"]
    assert h["n_match"] == 8 and h["n_other"] == 14 and h["delta_bps"] == pytest.approx(-85.0) and h["p_value"] < 0.01
    assert h["verdict"].startswith("SUGGESTIVE") and "not enough on its own" in h["verdict"]
    fresh_episode, fresh_review = trade_with_findings(["MANUAL_ORDER_DURING_HOLD"], key="BRAND_NEW")
    (t,) = playbook.build_for_review(fresh_review)
    assert t.experiment["history_check"]["n_match"] == 0 and t.experiment["history_check"]["verdict"].startswith("TOO FEW")
    assert t.experiment["history_check"]["p_value"] is None


def test_one_open_experiment_per_condition_and_strategy_and_building_twice_adds_nothing():
    _, review = trade_with_findings(["EVENT_IN_HOLD_UNACKNOWLEDGED"])
    (first,) = playbook.build_for_review(review)
    assert playbook.build_for_review(review) == []  # idempotent for the same review
    _, other = trade_with_findings(["EVENT_IN_HOLD_UNACKNOWLEDGED"])
    assert playbook.build_for_review(other) == []  # the same condition is already open for this strategy
    Suggestion.objects.filter(pk=first.pk).update(status="REJECTED")
    assert len(playbook.build_for_review(other)) == 1  # once decided, a later trade may propose it again


def test_fallback_reviews_still_get_a_playbook_because_it_comes_from_rules_not_the_model():
    from apps.intelligence.services import reviews as reviews_module

    episode = make_episode(-40, extra={"ctx.available": True, "ctx.event_in_hold": True})
    review = review_with(episode, [], fallback=True)
    finding(review, "EVENT_IN_HOLD_UNACKNOWLEDGED")
    assert len(playbook.build_for_review(review)) == 1 and review.is_fallback
    assert "EXPERIMENT" not in json.dumps(reviews_module.prompts.output_schema())  # the model cannot produce one


def test_accepting_an_experiment_only_acknowledges_it_and_never_applies_anything():
    _, review = trade_with_findings(["EVENT_IN_HOLD_UNACKNOWLEDGED"])
    (s,) = playbook.build_for_review(review)
    res = Client().post(f"/api/v1/intelligence/suggestions/{s.pk}/accept/", data="{}", content_type="application/json", HTTP_IDEMPOTENCY_KEY="ex-1")
    assert res.status_code == 200, res.content
    s.refresh_from_db()
    assert s.status == "ACKNOWLEDGED" and s.applied_version_id is None


def test_forward_progress_counts_only_matching_trades_that_close_after_the_plan_was_made():
    for _ in range(5):
        make_episode(-50, extra={"ctx.event_in_hold": True})  # before the plan: history, not progress
    _, review = trade_with_findings(["EVENT_IN_HOLD_UNACKNOWLEDGED"])
    (s,) = playbook.build_for_review(review)
    assert playbook.forward_progress(s) == (0, 8)
    for i in range(3):
        make_episode(-50, offset_days=2 + i, extra={"ctx.event_in_hold": True})
    make_episode(-50, offset_days=6, extra={"ctx.event_in_hold": False})  # does not match
    assert playbook.forward_progress(s) == (3, 8)


def plan_with_forward_trades(matching_bps, other_bps, n_match=8, rule="EVENT_IN_HOLD_UNACKNOWLEDGED", key="SMA_CROSSOVER", spread=2):
    _, review = trade_with_findings([rule], key=key)
    (s,) = playbook.build_for_review(review)
    for i in range(n_match):
        make_episode(matching_bps + spread * ((i % 5) - 2), key=key, offset_days=2 + i,
                     extra={"ctx.available": True, "ctx.event_in_hold": True, "operator.manual_orders_during_hold": 1})
    for i in range(15):
        make_episode(other_bps + spread * ((i % 5) - 2), key=key, offset_days=1 + i,
                     extra={"ctx.available": True, "ctx.event_in_hold": False, "operator.manual_orders_during_hold": 0})
    return s


def test_a_clear_difference_over_enough_trades_resolves_as_confirmed_with_multiple_comparison_control():
    s = plan_with_forward_trades(-55, 25)
    out = playbook.resolve_experiments()
    assert out == {"resolved": 1, "expired": 0}
    outcome = SuggestionOutcome.objects.get(suggestion=s)
    s.refresh_from_db()
    assert outcome.result == "HURT" and outcome.n_after == 8 and outcome.ci_high < 0
    assert s.experiment["resolution"]["survives_fdr"] is True and s.experiment["resolution"]["q_value"] <= 0.10
    assert "did worse" in s.experiment["resolution"]["reading"]
    event = LearningEvent.objects.get(type="SUGGESTION_RESOLVED", dedupe_key=f"experiment-outcome:{s.pk}")
    assert event.payload["experiment"] and event.payload["result"] == "HURT"
    assert playbook.resolve_experiments() == {"resolved": 0, "expired": 0}  # resolved once, never twice


def test_a_small_noisy_difference_is_inconclusive_even_with_enough_trades():
    s = plan_with_forward_trades(20, 22, spread=40)  # a 2 bps difference inside a 40 bps spread
    playbook.resolve_experiments()
    assert SuggestionOutcome.objects.get(suggestion=s).result == "INCONCLUSIVE"
    s.refresh_from_db()
    assert "no reliable difference" in s.experiment["resolution"]["reading"]


def test_an_experiment_with_too_few_matching_trades_waits_and_then_expires():
    s = plan_with_forward_trades(-55, 25, n_match=3)
    assert playbook.resolve_experiments() == {"resolved": 0, "expired": 0}
    Suggestion.objects.filter(pk=s.pk).update(created_at=timezone.now() - timedelta(days=120))
    assert playbook.resolve_experiments() == {"resolved": 0, "expired": 1}
    s.refresh_from_db()
    assert s.status == "EXPIRED" and "not enough matching trades" in s.decision_reason and not SuggestionOutcome.objects.filter(suggestion=s).exists()


def test_experiments_resolved_together_are_judged_as_one_family():
    strong = plan_with_forward_trades(-55, 25, key="STRAT_A")
    weak = plan_with_forward_trades(18, 22, rule="MANUAL_ORDER_DURING_HOLD", key="STRAT_B", spread=40)
    playbook.resolve_experiments()
    a, b = (Suggestion.objects.get(pk=x.pk).experiment["resolution"] for x in (strong, weak))
    assert a["q_value"] >= a["p_value"] - 1e-12 and b["q_value"] >= b["p_value"] - 1e-12  # adjusted, never better than raw
    assert SuggestionOutcome.objects.get(suggestion=strong).result == "HURT" and SuggestionOutcome.objects.get(suggestion=weak).result == "INCONCLUSIVE"


def test_experiments_endpoint_report_section_and_suggestion_rows():
    from apps.intelligence.services import report as report_service

    episode, review = trade_with_findings(["EVENT_IN_HOLD_UNACKNOWLEDGED", "ENTRY_MARGIN_TINY"])
    created = playbook.build_for_review(review)
    client = Client()
    body = client.get("/api/v1/intelligence/experiments/").json()
    assert len(body["data"]) == 2 and "not advice" in body["meta"]["note"]
    row = next(r for r in body["data"] if r["rule_id"] == "EVENT_IN_HOLD_UNACKNOWLEDGED")
    assert row["condition"] == "ctx.event_in_hold == True" and row["progress"] == {"matching_forward_trades": 0, "needed": 8}
    assert row["history_check"]["verdict"] and row["outcome"] is None and row["paper_only"] is True
    assert client.get("/api/v1/intelligence/experiments/?status=REJECTED").json()["data"] == []
    detail = client.get(f"/api/v1/intelligence/suggestions/?review_id={review.pk}").json()
    assert any(r.get("experiment") for r in detail["data"])
    report = report_service.build_report(TradeEpisode.objects.get(pk=episode.pk))
    section = next(s for s in report["sections"] if s["id"] == "playbook")
    assert "nothing is changed" in section["title"] and len(section["blocks"][1]["rows"]) == len(created)
    table = next(s for s in report["sections"] if s["id"] == "suggestions")["blocks"][0]
    assert table["type"] == "text"  # experiments are not repeated in the generic suggestions table


# =========================================================================================== what-if sweep (FDR)
def option_trades(signal_effect, key):
    from tests.intelligence.test_learning import make_episode as mk

    import random

    rng = random.Random(5)
    for i in range(40):
        score = 55 + (i % 10) * 2
        mean = signal_effect * (score - 65)
        mk(mean + rng.gauss(0, 25), key=key, extra={"asset_family": "OPTION", "opt.entry_score": float(score), "opt.iv_score": float(40 + (i * 7) % 30),
                                                          "opt.tqs": float(45 + (i * 5) % 25)})
    TradeEpisode.objects.filter(strategy_key=key).update(asset_family="OPTION")


def test_what_if_sweep_finds_a_real_filter_but_controls_false_discoveries_on_noise():
    option_trades(8.0, "OPT_REAL")
    real = counterfactuals.options_filter_sweep("OPT_REAL")
    assert real["comparisons"] > 5 and any(r["holds_after_correction"] for r in real["results"])
    assert all(r["q_value"] is None or r["q_value"] >= (r["p_value"] or 0) - 1e-12 for r in real["results"])
    assert any("Benjamini-Hochberg" in w for w in real["warnings"])
    option_trades(0.0, "OPT_NOISE")
    noise = counterfactuals.options_filter_sweep("OPT_NOISE")
    assert noise["comparisons"] > 5 and not any(r["holds_after_correction"] for r in noise["results"])  # nothing survives on pure noise


# ===================================================================================== daily task and digest
def test_daily_learning_runs_every_step_and_the_digest_names_edge_alarms_and_waiting_experiments():
    series_for("DECAYING", [20, -10] * 5 + [-60, -55, -65, -58, -62, -57])
    _, review = trade_with_findings(["EVENT_IN_HOLD_UNACKNOWLEDGED"], key="DECAYING")
    playbook.build_for_review(review)
    result = tasks.daily_learning()
    assert set(result) >= {"retired", "outcomes", "experiments", "change_points", "performance", "calibration"} and "error" not in result
    data = digest.build_digest(days=1)
    assert data["experiments_waiting"] == 1 and any(c["status"] == "EDGE_DECAY" for c in data["change_points"])
    text = digest.to_text(data)
    assert "change point: DECAYING RETURN EDGE_DECAY" in text and "experiments: 1" in text


def test_a_failing_step_never_stops_the_others(monkeypatch):
    monkeypatch.setattr(changepoint, "refresh", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    series_for("S", [20, -10] * 4)
    result = tasks.daily_learning()
    assert result["change_points"] == "error" and result["performance"] >= 1


def test_new_table_is_in_the_flush_guard_and_the_dataset_export():
    from apps.intelligence.guard import intel_models
    from apps.intelligence.management.commands.export_intelligence_dataset import TABLES

    assert ChangePointState in intel_models() and "change_points" in {name for name, _ in TABLES}
