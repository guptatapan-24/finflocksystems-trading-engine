import json
import shutil
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest
from django.utils import timezone

from apps.accounts.models import BrokerAccount
from apps.allocation.models import RebalanceRun
from apps.instruments.models import BrokerContract, Issuer
from apps.portfolio_construction.models import PortfolioConstructionPlan, PortfolioGoalAllocation
from apps.portfolio_construction.services import create_construction_run, run_construction
from apps.portfolios.models import TradingPortfolio
from apps.research.engines.base import ResearchProtocolContext
from apps.research.engines.single_asset import SingleAssetBacktestEngine
from apps.research.implementations.baseline import BuyAndHoldResearch
from apps.research.models import (
    GoalRecommendationPolicy,
    GoalRecommendationRun,
    GoalRecommendationSleeve,
    GICSTaxonomyNode,
    InstrumentEligibilitySnapshot,
    ResearchDailyBar,
    ResearchDatasetVersion,
    ResearchStrategyDefinition,
    ResearchStrategyImplementation,
    ResearchUniverseMember,
)
from apps.research.services.acceptance import accept_recommendation
from apps.research.services.bundle_import import import_bundle
from apps.research.services.bundle_validation import BundleValidationError, validate_bundle
from apps.research.services.data_readiness import latest_point_in_time_bars
from apps.research.services.features import rsi, shifted_donchian, sma
from apps.research.services.optimizer import optimize_sleeves
from apps.research.services.scoring import candidate_score
from apps.strategies.models import StrategyDefinition, StrategyInstance


pytestmark = pytest.mark.django_db
D = Decimal
# The research bundle is vendored into the repository as research_bundle/.
# This used to point at parents[2], a sibling of the repository checkout,
# so it resolved to a path that only existed on the machine the bundle was
# authored on -- and to "/Trading_Engine_Stock_Strategy_Universe_JSON" in a
# container, where parents[2] is the filesystem root.
BUNDLE = Path(__file__).resolve().parents[1] / "research_bundle"


def test_bundle_validation_checks_manifest_hashes_schemas_counts_and_taxonomy():
    result = validate_bundle(BUNDLE)
    assert result.report["counts"] == {
        "stocks": 500,
        "strategies": 97,
        "sectors": 11,
        "industry_groups": 25,
        "industries": 74,
        "sub_industries": 163,
    }
    assert result.report["current_snapshot_only"] is True


def test_bundle_validation_rejects_changed_file(tmp_path):
    target = tmp_path / "bundle"
    shutil.copytree(BUNDLE, target)
    path = target / "compatibility_rules.json"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(BundleValidationError, match="Byte count mismatch"):
        validate_bundle(target)


def test_atomic_import_is_idempotent_and_never_creates_fake_broker_contracts():
    dataset, created = import_bundle(BUNDLE, activate=True)
    same, second_created = import_bundle(BUNDLE, activate=True)
    assert created is True and second_created is False and same.pk == dataset.pk
    assert ResearchDatasetVersion.objects.filter(status="ACTIVE").count() == 1
    assert GICSTaxonomyNode.objects.filter(dataset_version=dataset).count() == 273
    assert ResearchUniverseMember.objects.filter(universe__dataset_version=dataset).count() == 500
    assert ResearchStrategyDefinition.objects.filter(dataset_version=dataset).count() == 97
    descriptive_founding = "2020 (1853, United Technologies spinoff)"
    assert Issuer._meta.get_field("founded").max_length >= len(descriptive_founding)
    assert Issuer.objects.get(cik="0001781335").founded == descriptive_founding
    assert BrokerContract.objects.filter(instrument__research_memberships__universe__dataset_version=dataset).count() == 0


def test_features_have_deterministic_warmup_and_shifted_channels():
    values = [1, 2, 3, 4, 5]
    moving = sma(values, 3)
    assert np.isnan(moving[1]) and moving[2:].tolist() == pytest.approx([2.0, 3.0, 4.0])
    upper, lower = shifted_donchian([1, 2, 3, 4], [0, 1, 2, 3], 2)
    assert np.isnan(upper[1]) and upper[2] == 2 and lower[2] == 0
    assert np.isnan(rsi(values, 3)[2])


def test_backtest_executes_signal_on_next_bar_and_applies_cost():
    bars = [
        {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 1_000_000},
        {"open": 110, "high": 111, "low": 109, "close": 110, "volume": 1_000_000},
        {"open": 121, "high": 122, "low": 120, "close": 121, "volume": 1_000_000},
    ]
    result = SingleAssetBacktestEngine().run(
        BuyAndHoldResearch(), bars, {"target_weight": 1},
        ResearchProtocolContext(commission_bps=10),
    )
    assert result.positions == [0.0, 1.0, 1.0]
    assert result.trades[0]["signal_bar_index"] == 0 and result.trades[0]["bar_index"] == 1
    assert result.returns[0] == 0 and result.returns[1] == pytest.approx(0.099)


def test_point_in_time_bar_query_excludes_future_revisions():
    dataset, _ = import_bundle(BUNDLE, activate=True)
    member = ResearchUniverseMember.objects.filter(universe__dataset_version=dataset).first()
    now = timezone.now()
    common = dict(
        instrument=member.instrument,
        trading_date=date(2025, 1, 2),
        raw_open=100, raw_high=101, raw_low=99, raw_close=100,
        adjusted_open=100, adjusted_high=101, adjusted_low=99, adjusted_close=100,
        total_return_close=100, volume=1000, provider="TEST", provider_timestamp=now - timedelta(days=2),
        quality_status="VALID",
    )
    ResearchDailyBar.objects.create(**common, revision_timestamp=now - timedelta(days=1), data_version=1)
    ResearchDailyBar.objects.create(**{**common, "raw_close": 200, "adjusted_close": 200}, revision_timestamp=now + timedelta(days=1), data_version=2)
    rows = latest_point_in_time_bars(member.instrument, as_of_timestamp=now)
    assert len(rows) == 1 and rows[0].data_version == 1 and rows[0].raw_close == D(100)


def test_candidate_scoring_applies_weighted_score_and_hard_rejections():
    components = {key: 80 for key in (
        "out_of_sample_sharpe", "calmar", "drawdown_fit", "regime_consistency",
        "parameter_stability", "cost_resilience", "turnover_efficiency", "capacity",
        "diversification_contribution",
    )}
    passing = {
        "data_quality_pass": True, "timestamps_unambiguous": True, "high_cost_net_return": 0.01,
        "maximum_drawdown": 0.10, "maximum_allowed_drawdown": 0.20, "trade_count": 30,
        "minimum_trades": 20, "parameter_neighborhood_stable": True, "capacity_pass": True,
        "largest_subperiod_contribution": 0.40, "maximum_subperiod_contribution": 0.60,
        "multiple_testing_pass": True, "holdout_untouched": True,
    }
    assert candidate_score(components, passing) == {"score": D("80.000"), "eligible": True, "hard_rejection_reasons": []}
    passing["timestamps_unambiguous"] = False
    assert "LEAKAGE_OR_TIMESTAMP_AMBIGUITY" in candidate_score(components, passing)["hard_rejection_reasons"]


def test_research_optimizer_enforces_gics_stock_family_and_cash_caps():
    candidates = [
        {"identity": str(index), "instrument_id": index, "strategy_family": f"f{index % 2}",
         "sector": f"s{index}", "industry": f"i{index}", "sub_industry": f"u{index}",
         "expected_return": 0.10 + index * 0.01, "expected_volatility": 0.20}
        for index in range(4)
    ]
    result = optimize_sleeves(candidates, constraints={
        "minimum_cash": 0.40, "per_stock_cap": 0.20, "sector_cap": 0.20,
        "industry_cap": 0.20, "sub_industry_cap": 0.20, "strategy_family_cap": 0.40,
    })
    assert sum(result["weights"]) == pytest.approx(0.60)
    assert result["cash_weight"] == pytest.approx(0.40)
    assert max(result["weights"]) <= 0.20 + 1e-8
    assert result["weights"][0] + result["weights"][2] <= 0.40 + 1e-8


def test_acceptance_creates_no_instance_or_rebalance_and_fixed_weight_survives_preview():
    dataset, _ = import_bundle(BUNDLE, activate=True)
    protocol = dataset.protocols.get(active=True)
    member = ResearchUniverseMember.objects.filter(universe__dataset_version=dataset).first()
    BrokerContract.objects.create(
        instrument=member.instrument, conid=990001, local_symbol=member.source_symbol,
        primary_exchange="NYSE", qualified_at=timezone.now(),
    )
    InstrumentEligibilitySnapshot.objects.create(
        universe_member=member, as_of_date=timezone.localdate(), price=100,
        median_dollar_volume_20d=100_000_000, history_days=1000, trading_days_252d=252,
        data_quality_status="VALID", research_eligible=True, builder_eligible=True,
    )
    definition = StrategyDefinition.objects.get(key="FIXED_WEIGHT_REBALANCE")
    research_strategy = ResearchStrategyDefinition.objects.get(dataset_version=dataset, research_id="BH_001")
    implementation = ResearchStrategyImplementation.objects.create(
        research_strategy=research_strategy,
        implementation_path="apps.research.implementations.baseline.BH_001",
        implementation_version="test", implementation_hash="a" * 64, role="EXECUTION",
        exact_semantic_match=True, supported_frequency="1d", supported_direction="LONG",
        status="APPROVED", executable_strategy_definition=definition,
        default_parameters={"direction": "LONG"}, approval_record={"paper_validated": True},
    )
    account = BrokerAccount.objects.create(account_id="DU-RESEARCH", net_liquidation=10000, available_cash=10000)
    portfolio = TradingPortfolio.objects.create(name="Research acceptance", account=account)
    plan = PortfolioConstructionPlan.objects.create(portfolio=portfolio)
    goal = PortfolioGoalAllocation.objects.create(
        plan=plan, name="Fast", allocation_weight=1, timeframe_bucket="FAST", risk_level=3,
    )
    policy = GoalRecommendationPolicy.objects.create(
        name="ACCEPTANCE-TEST", per_stock_cap="0.15", sector_cap=1, industry_cap=1,
        sub_industry_cap=1, strategy_family_cap=1, minimum_cash="0.40",
    )
    recommendation = GoalRecommendationRun.objects.create(
        goal_allocation=goal, requested_plan_version=plan.version, policy=policy,
        dataset_version=dataset, protocol_version=protocol, as_of_date=timezone.localdate(),
        status="COMPLETED", idempotency_key="accept-test", request_hash="b" * 64,
        expires_at=timezone.now() + timedelta(days=1),
    )
    GoalRecommendationSleeve.objects.create(
        recommendation_run=recommendation, instrument=member.instrument, universe_member=member,
        research_strategy=research_strategy, execution_strategy_definition=definition,
        execution_timeframe="1d", parameters={"direction": "LONG"}, sleeve_weight="0.15",
        stock_weight="0.15", strategy_share=1, candidate_score=80,
    )
    acceptance, created = accept_recommendation(recommendation, actor="test")
    assert created is True and acceptance.change_summary["created_strategy_instances"] == 0
    assert StrategyInstance.objects.count() == 0 and RebalanceRun.objects.count() == 0
    goal.refresh_from_db()
    run = create_construction_run(plan, "fixed-preview", refresh_history=False)
    run = run_construction(run, refresh_history=False)
    result = run.goal_results[0]
    assert result["construction_source"] == "ACCEPTED_RECOMMENDATION"
    assert result["stocks"][0]["local_weight"] == "0.15000000"
    assert run.final_target_weights["stocks"][str(member.instrument_id)] == "0.15000000"
    assert StrategyInstance.objects.count() == 0 and RebalanceRun.objects.count() == 0


def test_recommendation_cache_waits_cleanly_for_research_bootstrap(settings):
    from apps.research.tasks import warm_recommendation_cache

    settings.RECOMMENDATION_SYSTEM_ENABLED = True

    assert warm_recommendation_cache.run() == {"status": "WAITING_FOR_ACTIVE_DATASET"}
