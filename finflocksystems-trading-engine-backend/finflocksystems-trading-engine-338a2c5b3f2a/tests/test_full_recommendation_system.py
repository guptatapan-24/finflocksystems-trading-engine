from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from io import StringIO
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import numpy as np
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.accounts.models import BrokerAccount
from apps.allocation.models import RebalanceRun
from apps.instruments.models import BrokerContract, InstrumentProviderMapping
from apps.oms.models import Order
from apps.portfolio_construction.models import (
    GoalStrategyAssignment,
    PortfolioConstructionPlan,
    PortfolioGoalAllocation,
    StrategyConstructionProfile,
)
from apps.portfolio_construction.rules import MAXIMUM_RISK, resolved_goal_rules
from apps.portfolios.models import TradingPortfolio
from apps.research.configuration import RecommendationSystemConfiguration
from apps.research.enums import StrategyRole
from apps.research.engines.base import ResearchProtocolContext
from apps.research.models import (
    GoalRecommendationRun,
    GICSTaxonomyNode,
    InstrumentEligibilitySnapshot,
    InstrumentFeatureSnapshot,
    RecommendationCacheSnapshot,
    RecommendationBatchRun,
    ResearchFundamentalFact,
    ResearchIntradayBar,
    ResearchDatasetVersion,
    ResearchStrategyImplementation,
    ResearchUniverseMember,
)
from apps.research.services.bundle_import import import_bundle
from apps.research.services.acceptance import (
    effective_strategy_family_cap,
    effective_strategy_family_cap_for_run,
    validate_recommendation_for_construction,
)
from apps.research.services.point_in_time_data import point_in_time_facts, refresh_fundamentals
from apps.research.services.recommendation_batch import (
    _locked_goal_results,
    create_recommendation_batch,
    run_recommendation_batch,
)
from apps.research.services.recommendation_cache import (
    _fallback_copy,
    _gics_exposure,
    _price_fallback_candidates,
    build_cache_snapshot,
    calculate_role_scores,
    target_stock_count,
)
from apps.research.services.research_data import refresh_intraday_history
from apps.research.services.strategy_registry import REGISTRY, validate_registry_for_dataset
from apps.research.services.universe_pipeline import qualify_and_substitute_finalists
from apps.strategies.models import StrategyInstance


pytestmark = pytest.mark.django_db
# The research bundle is vendored into the repository as research_bundle/.
# This used to point at parents[2], a sibling of the repository checkout,
# so it resolved to a path that only existed on the machine the bundle was
# authored on -- and to "/Trading_Engine_Stock_Strategy_Universe_JSON" in a
# container, where parents[2] is the filesystem root.
BUNDLE = Path(__file__).resolve().parents[1] / "research_bundle"


def test_full_configuration_is_typed_and_enforces_one_strategy_per_stock(tmp_path):
    configuration = RecommendationSystemConfiguration.from_environment({}, default_artifact_root=tmp_path)
    assert configuration.universe_key == "US_LARGE_CAP_GICS"
    assert configuration.maximum_stocks == 20
    assert configuration.minimum_stocks == 5
    assert configuration.daily_lookback_years == 10
    assert configuration.intraday_lookback_days == 90
    assert configuration.maximum_strategies_per_stock == 1
    with pytest.raises(RuntimeError, match="exactly one primary strategy"):
        RecommendationSystemConfiguration.from_environment(
            {"RECOMMENDATION_MAX_STRATEGIES_PER_STOCK": "2"}, default_artifact_root=tmp_path,
        )


def test_registry_explicitly_loads_all_97_catalogue_implementations():
    assert len(REGISTRY) == 97
    for research_id, entry in REGISTRY.items():
        assert entry.research_id == research_id
        assert entry.load() is not None
        assert entry.backtest_engine in {"SINGLE_ASSET", "CROSS_SECTIONAL", "ALLOCATOR", "OVERLAY", "EVENT", "PAIR_BASKET"}
        assert entry.supported_direction == ("LONG",)
        assert entry.runtime_mapping is None or entry.role == StrategyRole.EXECUTION


def test_all_97_implementations_emit_deterministic_role_appropriate_bounded_outputs():
    rng = np.random.default_rng(97)
    close = 100 * np.exp(np.cumsum(rng.normal(.0003, .01, 420)))
    bars = [{
        "date": (date(2024, 1, 1) + timedelta(days=index)).isoformat(),
        "open": value * .999, "high": value * 1.01, "low": value * .99,
        "close": value, "volume": 1_000_000 + index, "vwap": value,
    } for index, value in enumerate(close)]
    feature_values = {
        "formation_return": .12, "trailing_return": .12, "absolute_momentum": .12,
        "risk_adjusted_momentum": .6, "proximity": .95, "residual_return": .08,
        "relative_return": .07, "realized_volatility": .2, "peer_residual_zscore": 1.2,
        "earnings_yield": .05, "book_to_market": .4, "sales_to_price": .2, "fcf_yield": .04,
        "roe": .2, "roa": .1, "gross_profitability": .3, "accruals": -.02,
        "operating_margin": .18, "roic": .15, "asset_growth": .04, "capex_growth": .03,
        "idiosyncratic_volatility": .15, "beta": .9, "dividend_growth": .08,
        "payout_sustainability": .8, "dividend_yield": .025, "net_buyback_yield": .01,
        "cash_conversion": 1.1, "interest_coverage": 8, "net_debt_to_ebitda": 1.2,
        "revenue_growth": .1, "earnings_growth": .12, "value": .5, "quality": .7,
        "momentum": .6, "low_volatility": -.2, "within_sector_value": .4,
        "within_sector_quality": .6, "within_sector_momentum": .5,
        "eps_revision_1m": .02, "eps_revision_3m": .04, "estimate_dispersion": .1,
        "payout_ratio": .4, "fcf_coverage": 2.5, "dividend_growth_5y": .07,
        "free_cash_flow_yield": .04, "shares_outstanding_change": -.01, "debt_paydown_yield": .01,
    }
    panel = [{
        "instrument_id": index + 1, "symbol": f"S{index}", "sector": f"SEC{index % 3}",
        "industry": f"IND{index % 3}", "sub_industry": "SUB", "liquidity": 10_000_000 - index,
        "returns": rng.normal(.0002, .01, 260).tolist(),
        "features": {**feature_values, "formation_return": .12 + index * .01},
    } for index in range(8)]
    returns = np.asarray([item["returns"] for item in panel], dtype=float).T
    context = ResearchProtocolContext()
    decision = datetime(2026, 1, 10, tzinfo=dt_timezone.utc)
    role_counts = {}
    for research_id, entry in REGISTRY.items():
        implementation = entry.load()
        role_counts[entry.role] = role_counts.get(entry.role, 0) + 1
        if entry.role == StrategyRole.EXECUTION:
            first = implementation.signals(bars, {}, context)
            second = implementation.signals(bars, {}, context)
            assert len(first.desired_exposure) == len(bars), research_id
            assert np.allclose(first.desired_exposure, second.desired_exposure, equal_nan=True), research_id
            assert np.nanmin(first.desired_exposure) >= 0 and np.nanmax(first.desired_exposure) <= 1, research_id
        elif entry.role in {StrategyRole.SELECTOR, StrategyRole.INCOME}:
            first = implementation.rank(panel, {}, context)
            second = implementation.rank(panel, {}, context)
            assert [(item["symbol"], item.get("model_score")) for item in first] == [
                (item["symbol"], item.get("model_score")) for item in second
            ], research_id
        elif entry.role == StrategyRole.ALLOCATOR:
            weights = implementation.allocate(
                returns, {}, {"per_stock_cap": 1, "sector_vector": [item["sector"] for item in panel]}, {},
            )
            assert len(weights) == len(panel) and min(weights) >= 0 and sum(weights) == pytest.approx(1), research_id
        elif entry.role == StrategyRole.OVERLAY:
            weights = implementation.apply(
                [.1] * len(panel), {"realized_volatility": .2, "drawdown": -.1,
                "market_trend": 1, "average_correlation": .6, "regime": "NORMAL", "liquidity_stress": .1}, {},
            )
            assert len(weights) == len(panel) and all(0 <= value <= .1 for value in weights), research_id
        elif entry.role == StrategyRole.EVENT:
            local_decision = (
                datetime(2026, 1, 31, tzinfo=dt_timezone.utc) if research_id == "EVT_005_MONTH_END"
                else datetime(2026, 1, 2, tzinfo=dt_timezone.utc) if research_id == "EVT_004_TURN_MONTH"
                else decision
            )
            event_type = {
                "EVT_006_EXDIV": "DIVIDEND", "EVT_007_INDEX": "INDEX_CHANGE", "EVT_008_SPLIT": "SPLIT",
            }.get(research_id, "EARNINGS")
            events = [{"event_type": event_type, "available_timestamp": local_decision - timedelta(days=2),
                       "effective_timestamp": local_decision - timedelta(days=1),
                       "payload": {"standardized_surprise": .5}},
                      {"event_type": event_type, "available_timestamp": local_decision + timedelta(days=1),
                       "effective_timestamp": local_decision - timedelta(days=1),
                       "payload": {"standardized_surprise": 9}}]
            result = implementation.signals(events, bars, {"decision_timestamp": local_decision}, context)
            assert len(result) == 1 and all(item["available_at_decision"] for item in result), research_id
        elif entry.role == StrategyRole.PAIR_BASKET:
            result = implementation.targets(panel, {"maximum_neighbors": 3}, context)
            assert all(not item["runtime_eligible"] for item in result), research_id
            assert len(result) <= len(panel) * 3, research_id
        else:
            pytest.fail(f"Unhandled role for {research_id}: {entry.role}")
    assert sum(role_counts.values()) == 97


def test_bundle_import_registers_every_strategy_and_keeps_pair_models_research_only():
    dataset, _ = import_bundle(BUNDLE, activate=True)
    assert validate_registry_for_dataset(dataset) == {"strategies": 97, "implementations": 97, "valid": True}
    implementations = ResearchStrategyImplementation.objects.filter(research_strategy__dataset_version=dataset)
    assert implementations.count() == 97
    assert implementations.filter(role="PAIR_BASKET", executable_strategy_definition__isnull=False).count() == 0
    assert implementations.filter(role="EXECUTION", exact_semantic_match=True, executable_strategy_definition__isnull=False).exists()
    assert not ResearchDatasetVersion.objects.exclude(pk=dataset.pk).filter(status="ACTIVE").exists()
    universe = dataset.universes.get(key="US_LARGE_CAP_GICS")
    members = universe.members.filter(active=True, membership_end__isnull=True)
    assert members.count() == 500
    assert members.filter(issuer__isnull=False, instrument__isnull=False).count() == 500
    assert dataset.classifications.filter(issuer_id__in=members.values("issuer_id")).values("issuer_id").distinct().count() == 500
    represented_sectors = GICSTaxonomyNode.objects.filter(
        dataset_version=dataset, level="SECTOR",
        children__children__children__classifications__issuer_id__in=members.values("issuer_id"),
    ).distinct()
    assert represented_sectors.count() == 11


def test_local_bootstrap_is_idempotent_and_never_fabricates_research_data():
    first=StringIO()
    call_command(
        "bootstrap_recommendation_system",
        "--bundle-path",str(BUNDLE),
        "--skip-external",
        stdout=first,
    )
    second=StringIO()
    call_command(
        "bootstrap_recommendation_system",
        "--bundle-path",str(BUNDLE),
        "--skip-external",
        stdout=second,
    )

    dataset=ResearchDatasetVersion.objects.get(status="ACTIVE")
    runtime_mappings=ResearchStrategyImplementation.objects.filter(
        research_strategy__dataset_version=dataset,
        executable_strategy_definition__isnull=False,
    )
    assert dataset.strategies.filter(active=True).count()==97
    assert dataset.universes.get(key="US_LARGE_CAP_GICS").members.filter(
        active=True,membership_end__isnull=True,instrument__isnull=False,
    ).count()==500
    assert runtime_mappings.exists()
    assert StrategyConstructionProfile.objects.filter(
        strategy_definition_id__in=runtime_mappings.values("executable_strategy_definition_id"),
        construction_enabled=True,
    ).count()==runtime_mappings.count()
    assert dataset.protocols.filter(active=True).count()==1
    assert RecommendationCacheSnapshot.objects.filter(goal_timeframe="NOW",status="COMPLETED").exists()
    assert ResearchFundamentalFact.objects.count()==0
    output=second.getvalue()
    assert "SKIPPED_EXPLICITLY" in output
    assert '"demo_research_data_created": false' in output
    assert "no production research data was fabricated" in output


def test_bootstrap_requires_explicit_session_or_explicit_external_skip():
    with pytest.raises(CommandError,match="broker-session-id is required"):
        call_command("bootstrap_recommendation_system","--bundle-path",str(BUNDLE))
    assert not ResearchDatasetVersion.objects.exists()


def test_all_valid_goal_profiles_have_bounded_counts_and_live_constraints():
    pairs = []
    profile_ranges = {
        "HURRY": (5, 8), "FAST": (6, 10), "BUILD": (8, 12),
        "GROW": (10, 15), "COMPOUND": (12, 20),
    }
    for timeframe, maximum_risk in MAXIMUM_RISK.items():
        for risk_level in range(1, maximum_risk + 1):
            rules = resolved_goal_rules(timeframe, risk_level)
            count = target_stock_count(timeframe, risk_level)
            pairs.append((timeframe, risk_level))
            assert 0 <= rules["minimum_cash_weight"] <= 1
            assert 0 < rules["maximum_stock_weight"] <= 1
            if timeframe == "NOW":
                assert count == 0 and rules["minimum_cash_weight"] == 1
            else:
                low, high = profile_ranges[timeframe]
                assert low <= count <= high
                assert count * rules["maximum_stock_weight"] >= 1 - rules["minimum_cash_weight"]
    assert len(pairs) == 20


def test_deployment_readiness_requires_all_20_warm_cache_profiles(client, settings, monkeypatch):
    settings.BROKER_SESSION_ENCRYPTION_KEY = "readiness-test-key"
    settings.IBKR_GATEWAY_IMAGE = "docker.io/example/trading-engine-ib-gateway@sha256:" + ("a" * 64)
    settings.QCH_APP_ID = "readiness-app"
    settings.QCH_API_HOST = "https://qch.example"
    settings.QCH_SERVICE_TOKEN = "readiness-token"
    monkeypatch.setenv("QCH_APP_ID", settings.QCH_APP_ID)
    monkeypatch.setenv("QCH_API_HOST", settings.QCH_API_HOST)
    monkeypatch.setenv("QCH_SERVICE_TOKEN", settings.QCH_SERVICE_TOKEN)
    dataset, _ = import_bundle(BUNDLE, activate=True)
    not_ready = client.get("/readyz")
    assert not_ready.status_code == 503
    assert len(not_ready.json()["error"]["details"]["missing_cache_profiles"]) == 20
    protocol = dataset.protocols.get(active=True)
    for timeframe, maximum_risk in MAXIMUM_RISK.items():
        for risk_level in range(1, maximum_risk + 1):
            RecommendationCacheSnapshot.objects.create(
                dataset_version=dataset, protocol_version=protocol, goal_timeframe=timeframe,
                risk_level=risk_level, as_of_date=timezone.localdate(),
                input_hash=f"{timeframe}-{risk_level}".ljust(64, "0"), fallback_tier=1,
                status="COMPLETED", expires_at=timezone.now() + timedelta(days=1),
            )
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["data"] == {
        "status": "ready", "universe_members": 500, "strategy_implementations": 97,
        "current_cache_profiles": 20, "required_cache_profiles": 20, "missing_cache_profiles": [],
        "deployment": {"available": True, "ready": True, "missing": [], "invalid": []},
    }


def test_price_and_snapshot_fallbacks_preserve_auditable_provenance():
    dataset, _ = import_bundle(BUNDLE, activate=True)
    universe = dataset.universes.get(key="US_LARGE_CAP_GICS")
    member = universe.members.select_related("instrument", "issuer").first()
    rows = [{
        "instrument_id": member.instrument_id, "universe_member_id": member.pk,
        "symbol": member.source_symbol, "company": member.security_name, "gics": {},
        "feature_date": timezone.localdate().isoformat(),
        "features": {"median_dollar_volume_20d": 500_000_000, "risk_adjusted_momentum": 1.2,
                     "formation_return": .1, "realized_volatility": .2, "maximum_drawdown": .15},
    }]
    price_only = _price_fallback_candidates(dataset, "FAST", 3, rows)
    baseline = _price_fallback_candidates(dataset, "FAST", 3, rows, baseline_only=True)
    assert price_only[0]["research_strategy_id"] == "TR_006_DONCHIAN_20"
    assert baseline[0]["research_strategy_id"] == "BH_001"
    assert price_only[0]["execution_strategy_definition_id"]
    protocol = dataset.protocols.get(active=True)
    source = RecommendationCacheSnapshot.objects.create(
        dataset_version=dataset, protocol_version=protocol, goal_timeframe="FAST", risk_level=3,
        as_of_date=timezone.localdate(), input_hash="f" * 64, candidate_pool=price_only,
        selected_stocks=price_only, expected_metrics={"cash_weight": .8}, gics_exposure={"sector": {"45": .2}},
        fallback_tier=1, data_freshness={"feature_count": 500}, status="COMPLETED",
        expires_at=timezone.now() + timedelta(days=1),
    )
    stale = _fallback_copy(source, 2, timezone.localdate() + timedelta(days=1), "LAST_VALID_FULL_SNAPSHOT")
    outage = _fallback_copy(source, 5, timezone.localdate() + timedelta(days=2), "LATEST_VALIDATED_DEPLOYMENT_SNAPSHOT")
    assert stale.fallback_tier == 2 and outage.fallback_tier == 5
    assert stale.data_freshness["fallback_source_snapshot_id"] == source.pk
    assert outage.gics_exposure == source.gics_exposure
    assert _gics_exposure([{**price_only[0], "weight": .2}])["sector"]["UNKNOWN"] == pytest.approx(.2)


def test_role_scoring_handles_feature_rows_without_completed_pair_experiments():
    dataset, _ = import_bundle(BUNDLE, activate=True)
    universe = dataset.universes.get(key="US_LARGE_CAP_GICS")
    member = universe.members.select_related("instrument").first()
    InstrumentFeatureSnapshot.objects.create(
        instrument=member.instrument, feature_key="common_daily", frequency="1d",
        as_of_date=timezone.localdate(), available_at=timezone.now(),
        data_version="role-score-data", implementation_version="role-score-test",
        value={
            "median_dollar_volume_20d": 500_000_000,
            "realized_volatility": .2,
            "maximum_drawdown": .15,
            "peer_residual_zscore": .5,
        },
    )
    result = calculate_role_scores(dataset)
    assert result == {"stock_scores": 19, "role_scores": 0}


def test_finalist_qualification_substitutes_one_failure_without_blocking_the_goal():
    dataset, _ = import_bundle(BUNDLE, activate=True)
    universe = dataset.universes.get(key="US_LARGE_CAP_GICS")
    members = list(universe.members.select_related("instrument")[:6])
    for index, member in enumerate(members[1:], start=1):
        BrokerContract.objects.create(
            instrument=member.instrument, conid=9_700_000 + index, primary_exchange="NASDAQ",
            local_symbol=member.source_symbol, qualified_at=timezone.now(),
        )

    class MissingFirstGateway:
        def search_contracts(self, symbol):
            return []

    result = qualify_and_substitute_finalists(members, 5, gateway=MissingFirstGateway())
    assert [item.pk for item in result.selected] == [item.pk for item in members[1:]]
    assert result.failures[0]["symbol"] == members[0].source_symbol
    assert result.substitutions == ({"symbol": members[5].source_symbol, "replacement_rank": 6},)


def test_intraday_uses_finnhub_first_versions_revisions_and_falls_back_to_exact_ibkr():
    dataset, _ = import_bundle(BUNDLE, activate=True)
    member = dataset.universes.get(key="US_LARGE_CAP_GICS").members.select_related("instrument").first()
    InstrumentProviderMapping.objects.update_or_create(
        instrument=member.instrument, provider="FINNHUB",
        defaults={"provider_symbol": member.source_symbol, "status": "VERIFIED", "verified_at": timezone.now()},
    )
    stamp = (timezone.now() - timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)

    class FinnhubBars:
        close = 101

        def historical_candles(self, symbol, frequency, start, end):
            return [SimpleNamespace(window_start=stamp, window_end=stamp + timedelta(hours=1),
                                    open=100, high=103, low=99, close=self.close, volume=1_000)]

    finnhub = FinnhubBars()
    first = refresh_intraday_history(member.instrument, frequency="1h", days=90, finnhub=finnhub)
    finnhub.close = 102
    second = refresh_intraday_history(member.instrument, frequency="1h", days=90, finnhub=finnhub)
    versions = list(ResearchIntradayBar.objects.filter(instrument=member.instrument).order_by("data_version"))
    assert first["provider"] == second["provider"] == "FINNHUB"
    assert [item.data_version for item in versions] == [1, 2]
    assert float(versions[-1].close) == 102

    contract = BrokerContract.objects.create(
        instrument=member.instrument, conid=9_600_001, primary_exchange="NASDAQ",
        local_symbol=member.source_symbol, qualified_at=timezone.now(),
    )

    class FailedFinnhub:
        def historical_candles(self, *args):
            raise RuntimeError("provider outage")

    class GatewayBars:
        def historical_bars(self, payload):
            assert payload["conid"] == contract.conid and payload["duration"].endswith(" D")
            return {"conid": contract.conid, "provider": "IBKR_TRADES", "bars": [
                {"date": stamp.isoformat(), "open": 100, "high": 104, "low": 99, "close": 103,
                 "volume": 1_100, "average": 102},
            ]}

        def historical_schedule(self, payload):
            return {"sessions": [{"start": (stamp - timedelta(minutes=1)).isoformat(),
                                   "end": (stamp + timedelta(hours=2)).isoformat()}]}

    fallback = refresh_intraday_history(
        member.instrument, frequency="1h", days=90, finnhub=FailedFinnhub(), gateway=GatewayBars(),
    )
    assert fallback["provider"] == "IBKR_TRADES" and fallback["primary_error"] == "provider outage"
    assert ResearchIntradayBar.objects.filter(instrument=member.instrument, window_start=stamp).count() == 3


def test_fundamental_revisions_are_not_backdated_into_point_in_time_reads():
    dataset, _ = import_bundle(BUNDLE, activate=True)
    member = dataset.universes.get(key="US_LARGE_CAP_GICS").members.select_related("instrument", "issuer").first()
    InstrumentProviderMapping.objects.update_or_create(
        instrument=member.instrument, provider="FINNHUB",
        defaults={"provider_symbol": member.source_symbol, "status": "VERIFIED", "verified_at": timezone.now()},
    )
    filed = datetime(2026, 2, 1, tzinfo=dt_timezone.utc)

    class Financials:
        value = 100

        def reported_financials(self, symbol):
            return [{"endDate": "2025-12-31", "filedDate": filed.isoformat(),
                     "report": {"bs": [{"concept": "Assets", "value": self.value, "unit": "USD"}]}}]

    client = Financials()
    first_retrieval = datetime(2026, 2, 2, tzinfo=dt_timezone.utc)
    second_retrieval = datetime(2026, 3, 2, tzinfo=dt_timezone.utc)
    with patch("apps.research.services.point_in_time_data.timezone.now", return_value=first_retrieval):
        refresh_fundamentals(member, client=client)
    client.value = 110
    with patch("apps.research.services.point_in_time_data.timezone.now", return_value=second_retrieval):
        refresh_fundamentals(member, client=client)
    before_revision = list(point_in_time_facts(member.issuer, datetime(2026, 2, 15, tzinfo=dt_timezone.utc))["fundamentals"])
    after_revision = list(point_in_time_facts(member.issuer, datetime(2026, 3, 3, tzinfo=dt_timezone.utc))["fundamentals"])
    assert [float(item.value) for item in before_revision] == [100]
    assert [item.revision_version for item in after_revision] == [1, 2]
    assert ResearchFundamentalFact.objects.get(revision_version=2).public_availability_timestamp == second_retrieval


def test_plan_batch_attaches_all_goals_once_without_orders_rebalances_or_instances(client):
    dataset, _ = import_bundle(BUNDLE, activate=True)
    universe = dataset.universes.get(key="US_LARGE_CAP_GICS")
    protocol = dataset.protocols.get(active=True)
    implementation_candidates = ResearchStrategyImplementation.objects.filter(
        research_strategy__dataset_version=dataset,
        role=StrategyRole.EXECUTION,
        exact_semantic_match=True,
        executable_strategy_definition__isnull=False,
    ).select_related("research_strategy").order_by("research_strategy__research_id")
    implementations = []
    strategy_family_counts = {}
    for candidate in implementation_candidates:
        family = candidate.research_strategy.family
        if strategy_family_counts.get(family, 0) >= 2:
            continue
        strategy_family_counts[family] = strategy_family_counts.get(family, 0) + 1
        implementations.append(candidate)
        if len(implementations) == 5:
            break
    assert len(implementations) == 5
    members = list(ResearchUniverseMember.objects.filter(universe=universe).select_related("instrument")[:5])
    rows = []
    for index, member in enumerate(members):
        implementation = implementations[index]
        BrokerContract.objects.create(
            instrument=member.instrument, conid=9_800_000 + index, primary_exchange="NASDAQ",
            local_symbol=member.source_symbol, qualified_at=timezone.now(),
        )
        rows.append({
            "instrument_id": member.instrument_id, "universe_member_id": member.pk,
            "symbol": member.source_symbol, "company": member.security_name, "gics": {},
            "research_strategy_id": implementation.research_strategy.research_id,
            "execution_strategy_definition_id": implementation.executable_strategy_definition_id,
            "strategy_name": implementation.research_strategy.name, "execution_timeframe": "1d",
            "parameters": {}, "candidate_score": 80, "final_score": 160 - index,
            "expected_return": .08, "expected_volatility": .20, "expected_drawdown": .18,
            "reason": "Deterministic full-system test candidate.", "weight": .18,
        })
    cache = RecommendationCacheSnapshot.objects.create(
        dataset_version=dataset, protocol_version=protocol, goal_timeframe="FAST", risk_level=3,
        as_of_date=timezone.localdate(), input_hash="c" * 64, candidate_pool=rows, selected_stocks=rows,
        allocator_strategy_id="INV_VOL_001", overlay_strategy_ids=["RISK_001_VOL_TARGET"],
        expected_metrics={"expected_return": .08, "expected_volatility": .20, "expected_drawdown": .18},
        fallback_tier=1, data_freshness={"as_of_date": timezone.localdate().isoformat()}, status="COMPLETED",
        expires_at=timezone.now() + timedelta(days=1),
    )
    account = BrokerAccount.objects.create(account_id="DU-FULL-REC", net_liquidation=100_000, available_cash=100_000)
    portfolio = TradingPortfolio.objects.create(name="Full recommendation batch", account=account)
    plan = PortfolioConstructionPlan.objects.create(portfolio=portfolio)
    goal = PortfolioGoalAllocation.objects.create(
        plan=plan, name="Fast goal", allocation_weight=1, timeframe_bucket="FAST", risk_level=3,
    )
    original_version = plan.version
    batch, created = create_recommendation_batch(plan, "full-recommendation-batch")
    assert created is True and batch.status == "QUEUED"
    locked_results = _locked_goal_results(batch)
    assert locked_results.query.select_for_update is True
    assert locked_results.query.select_for_update_of == ("self",)
    same, second_created = create_recommendation_batch(plan, "full-recommendation-batch")
    assert second_created is False and same.pk == batch.pk
    completed = run_recommendation_batch(batch)
    completed.refresh_from_db(); plan.refresh_from_db(); goal.refresh_from_db()
    assert completed.status == "COMPLETED"
    recommendation_count=GoalRecommendationRun.objects.count()
    replayed=run_recommendation_batch(batch)
    assert replayed.status=="COMPLETED"
    assert GoalRecommendationRun.objects.count()==recommendation_count
    assert completed.metrics["orders_created"] == completed.metrics["rebalances_created"] == 0
    assert completed.metrics["strategy_instances_created"] == 0
    assert plan.version == original_version + 1
    assert goal.accepted_recommendation_run_id is not None
    accepted = GoalRecommendationRun.objects.get(pk=goal.accepted_recommendation_run_id)
    sleeves = list(accepted.sleeves.all())
    rules = resolved_goal_rules("FAST", 3)
    assert len(sleeves) == 5
    assert all(item.strategy_share == 1 and item.stock_weight <= rules["maximum_stock_weight"] for item in sleeves)
    assert Decimal(str(accepted.metrics["cash_weight"])) >= rules["minimum_cash_weight"]
    assert Decimal(accepted.optimizer_snapshot["constraints"]["strategy_family_cap"]) == accepted.policy.strategy_family_cap
    InstrumentEligibilitySnapshot.objects.filter(universe_member__in=members).update(builder_eligible=True)
    validate_recommendation_for_construction(accepted)
    assignments = GoalStrategyAssignment.objects.filter(goal_instrument_selection__goal_allocation=goal, enabled=True)
    assert assignments.count() == len(sleeves)
    assert all(item.strategy_share == 1 and item.create_instance for item in assignments)
    assert StrategyInstance.objects.count() == Order.objects.count() == RebalanceRun.objects.count() == 0
    assert cache.fallback_tier == 1
    previous_run_id = goal.accepted_recommendation_run_id
    changed = client.patch(
        f"/api/v1/portfolio-construction/goals/{goal.pk}/",
        data=json.dumps({"name": "Fast goal"}), content_type="application/json",
    )
    assert changed.status_code == 200
    goal.refresh_from_db(); plan.refresh_from_db()
    assert goal.construction_source == "MANUAL_OPTIMIZER" and goal.accepted_recommendation_run_id is None
    assert plan.version == original_version + 2
    regenerated, was_created = create_recommendation_batch(plan, "full-recommendation-regenerate")
    assert was_created is True
    run_recommendation_batch(regenerated)
    goal.refresh_from_db(); plan.refresh_from_db()
    assert goal.accepted_recommendation_run_id != previous_run_id
    assert plan.version == original_version + 3
    assert GoalStrategyAssignment.objects.filter(
        goal_instrument_selection__goal_allocation=goal, enabled=True,
    ).count() == len(sleeves)
    assert StrategyInstance.objects.count() == Order.objects.count() == RebalanceRun.objects.count() == 0
    latest_run_id = goal.accepted_recommendation_run_id
    deleted = client.delete(f"/api/v1/portfolio-construction/goals/{goal.pk}/")
    assert deleted.status_code == 200 and not PortfolioGoalAllocation.objects.filter(pk=goal.pk).exists()
    latest_run = GoalRecommendationRun.objects.get(pk=latest_run_id)
    assert latest_run.goal_allocation_id is None and latest_run.acceptance.goal_id is None
    historical = client.get(f"/api/v1/portfolio-construction/recommendation-batches/{regenerated.pk}/")
    assert historical.status_code == 200 and historical.json()["data"]["goals"][0]["goal_name"] == "Fast goal"


def test_family_cap_is_enforced_for_full_models_and_relaxed_for_audited_fallbacks():
    policy = SimpleNamespace(strategy_family_cap=Decimal("0.40"))
    assert effective_strategy_family_cap(policy, 1) == Decimal("0.40")
    assert effective_strategy_family_cap(policy, 2) == Decimal("0.40")
    assert effective_strategy_family_cap(policy, 3) == Decimal("1")
    assert effective_strategy_family_cap(policy, 5) == Decimal("1")

    old_fallback_run = SimpleNamespace(
        policy=policy,
        optimizer_snapshot={},
        metrics={"fallback_tier": 4},
    )
    assert effective_strategy_family_cap_for_run(old_fallback_run) == Decimal("1")

    audited_run = SimpleNamespace(
        policy=policy,
        optimizer_snapshot={"constraints": {"strategy_family_cap": "0.55"}},
        metrics={"fallback_tier": 4},
    )
    assert effective_strategy_family_cap_for_run(audited_run) == Decimal("0.55")


def test_plan_recommendation_unexpected_failure_is_a_readable_json_envelope(client):
    account = BrokerAccount.objects.create(account_id="DU-REC-ERROR", net_liquidation=10_000, available_cash=10_000)
    portfolio = TradingPortfolio.objects.create(name="Recommendation error envelope", account=account)
    plan = PortfolioConstructionPlan.objects.create(portfolio=portfolio)
    with (
        patch(
            "apps.portfolio_construction.views.portfolio_builder_readiness",
            return_value={"ready":True,"blockers":[],"details":{}},
        ),
        patch(
            "apps.portfolio_construction.views.create_recommendation_batch",
            side_effect=RuntimeError("private database failure"),
        ),
    ):
        result = client.post(
            f"/api/v1/portfolio-construction/plans/{plan.pk}/recommendations/",
            data="{}", content_type="application/json", HTTP_IDEMPOTENCY_KEY="readable-error-envelope",
        )
    assert result.status_code == 500
    assert result["Content-Type"].startswith("application/json")
    assert result.json()["error"] == {
        "code": "RECOMMENDATION_BATCH_INTERNAL_ERROR",
        "message": "Recommendation generation failed unexpectedly. Retry or inspect the Backend logs.",
        "details": {},
    }
    assert "private database failure" not in result.content.decode()


def test_plan_recommendation_post_only_queues_and_dispatches_celery(client,settings):
    settings.RESEARCH_BUNDLE_PATH=str(BUNDLE)
    import_bundle(BUNDLE,activate=True)
    build_cache_snapshot("NOW",1)
    account=BrokerAccount.objects.create(
        account_id="DU-ASYNC-REC",net_liquidation=10_000,available_cash=10_000,
    )
    portfolio=TradingPortfolio.objects.create(name="Async recommendation",account=account)
    plan=PortfolioConstructionPlan.objects.create(portfolio=portfolio)
    PortfolioGoalAllocation.objects.create(
        plan=plan,name="Immediate reserve",allocation_weight=1,timeframe_bucket="NOW",risk_level=1,
    )
    with patch("apps.portfolio_construction.views.generate_recommendation_batch.delay") as dispatch:
        result=client.post(
            f"/api/v1/portfolio-construction/plans/{plan.pk}/recommendations/",
            data="{}",content_type="application/json",HTTP_IDEMPOTENCY_KEY="async-batch",
        )
    body=result.json()["data"]
    assert result.status_code==202
    assert body["status"]=="QUEUED"
    dispatch.assert_called_once_with(body["id"])
    assert RecommendationBatchRun.objects.get(pk=body["id"]).status=="QUEUED"


def test_portfolio_builder_preflight_reports_exact_missing_components(client,settings):
    settings.RESEARCH_BUNDLE_PATH=str(BUNDLE)
    account=BrokerAccount.objects.create(account_id="DU-PREFLIGHT")
    portfolio=TradingPortfolio.objects.create(name="Blocked builder",account=account)
    plan=PortfolioConstructionPlan.objects.create(portfolio=portfolio)
    PortfolioGoalAllocation.objects.create(
        plan=plan,name="Long-term goal",allocation_weight=1,timeframe_bucket="BUILD",risk_level=3,
    )
    result=client.get(
        f"/api/v1/portfolio-construction/readiness/?portfolio={portfolio.pk}&plan={plan.pk}"
    )
    body=result.json()["data"]
    codes={item["code"] for item in body["blockers"]}
    assert result.status_code==200
    assert body["ready"] is False
    assert {
        "ACTIVE_RESEARCH_DATASET_MISSING",
        "RECOMMENDATION_CACHES_MISSING",
        "BROKER_GATEWAY_NOT_CONNECTED",
    } <= codes
    blocked=client.post(
        f"/api/v1/portfolio-construction/plans/{plan.pk}/recommendations/",
        data="{}",content_type="application/json",HTTP_IDEMPOTENCY_KEY="blocked-preflight",
    )
    assert blocked.status_code==503
    assert blocked.json()["error"]["code"]=="PORTFOLIO_BUILDER_NOT_READY"
    assert not RecommendationBatchRun.objects.exists()


def test_portfolio_builder_preflight_audits_registry_data_features_profiles_and_caches(client,settings):
    settings.RESEARCH_BUNDLE_PATH=str(BUNDLE)
    dataset,_=import_bundle(BUNDLE,activate=True)
    mapped=ResearchStrategyImplementation.objects.filter(
        research_strategy__dataset_version=dataset,
        executable_strategy_definition__isnull=False,
    ).first()
    assert mapped is not None
    mapped.executable_strategy_definition=None
    mapped.save(update_fields=["executable_strategy_definition"])
    account=BrokerAccount.objects.create(account_id="DU-DETAILED-PREFLIGHT")
    portfolio=TradingPortfolio.objects.create(name="Detailed blocked builder",account=account)
    plan=PortfolioConstructionPlan.objects.create(portfolio=portfolio)
    PortfolioGoalAllocation.objects.create(
        plan=plan,name="Long-term goal",allocation_weight=1,timeframe_bucket="GROW",risk_level=4,
    )

    result=client.get(
        f"/api/v1/portfolio-construction/readiness/?portfolio={portfolio.pk}&plan={plan.pk}"
    )
    body=result.json()["data"]
    codes={item["code"] for item in body["blockers"]}
    assert body["ready"] is False
    assert {
        "STRATEGY_RUNTIME_MAPPINGS_INCOMPLETE",
        "CONSTRUCTION_PROFILES_INCOMPLETE",
        "RESEARCH_DATA_MISSING",
        "RESEARCH_FEATURES_MISSING",
        "RECOMMENDATION_CACHES_MISSING",
        "BROKER_GATEWAY_NOT_CONNECTED",
    } <= codes


def test_non_now_empty_cache_fails_instead_of_becoming_intentional_cash():
    dataset,_=import_bundle(BUNDLE,activate=True)
    protocol=dataset.protocols.get(active=True)
    RecommendationCacheSnapshot.objects.create(
        dataset_version=dataset,protocol_version=protocol,goal_timeframe="FAST",risk_level=3,
        as_of_date=timezone.localdate(),input_hash="empty-cache".ljust(64,"0"),
        candidate_pool=[],selected_stocks=[],expected_metrics={"cash_weight":1},
        fallback_tier=1,status="COMPLETED",expires_at=timezone.now()+timedelta(days=1),
    )
    account=BrokerAccount.objects.create(
        account_id="DU-EMPTY-REC",net_liquidation=10_000,available_cash=10_000,
    )
    portfolio=TradingPortfolio.objects.create(name="Empty recommendation",account=account)
    plan=PortfolioConstructionPlan.objects.create(portfolio=portfolio)
    PortfolioGoalAllocation.objects.create(
        plan=plan,name="Growth goal",allocation_weight=1,timeframe_bucket="FAST",risk_level=3,
    )
    batch,_=create_recommendation_batch(plan,"empty-non-now")

    with pytest.raises(ValueError,match="cannot be treated as intentional cash-only"):
        run_recommendation_batch(batch,gateway=SimpleNamespace())

    batch.refresh_from_db()
    assert batch.status=="FAILED"
    assert "no positive stock/strategy sleeves" in batch.error


def test_now_goal_batch_is_cash_only_and_never_qualifies_or_blocks():
    dataset, _ = import_bundle(BUNDLE, activate=True)
    account = BrokerAccount.objects.create(account_id="DU-NOW-REC", net_liquidation=50_000, available_cash=50_000)
    portfolio = TradingPortfolio.objects.create(name="Cash-only recommendation", account=account)
    plan = PortfolioConstructionPlan.objects.create(portfolio=portfolio)
    goal = PortfolioGoalAllocation.objects.create(
        plan=plan, name="Immediate reserve", allocation_weight=1, timeframe_bucket="NOW", risk_level=1,
    )
    batch, _ = create_recommendation_batch(plan, "cash-only-batch")
    completed = run_recommendation_batch(batch)
    goal.refresh_from_db()
    run = GoalRecommendationRun.objects.get(pk=goal.accepted_recommendation_run_id)
    assert completed.status == "COMPLETED" and run.status == "COMPLETED"
    assert run.sleeves.count() == 0 and run.metrics["cash_weight"] == 1
    assert "BLOCKED" not in str(completed.goal_results.get().summary)
    assert StrategyInstance.objects.count() == Order.objects.count() == RebalanceRun.objects.count() == 0
