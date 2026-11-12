from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from apps.portfolio_construction.models import PortfolioConstructionPlan, StrategyConstructionProfile

from ..models import (
    BacktestProtocolVersion,
    InstrumentFeatureSnapshot,
    RecommendationCacheSnapshot,
    ResearchDataCoverageSummary,
    ResearchDatasetVersion,
    ResearchStrategyImplementation,
    ResearchUniverse,
)
from .bundle_validation import validate_bundle
from .strategy_registry import REGISTRY


def _blocker(code, message, **details):
    return {"code": code, "message": message, "details": details}


@lru_cache(maxsize=4)
def _validate_configured_bundle(path, manifest_mtime_ns):
    del manifest_mtime_ns
    return validate_bundle(path)


def _bundle_report():
    root = Path(settings.RESEARCH_BUNDLE_PATH)
    manifest = root / "manifest.json"
    if not root.is_dir():
        return None, _blocker(
            "RESEARCH_BUNDLE_MISSING",
            f"Research bundle directory is missing at {root}",
            path=str(root),
        )
    try:
        stamp = manifest.stat().st_mtime_ns
        validated = _validate_configured_bundle(str(root), stamp)
        return validated, None
    except Exception as exc:
        return None, _blocker(
            "RESEARCH_BUNDLE_INVALID",
            f"Research bundle validation failed: {exc}",
            path=str(root),
        )


def _required_cache_profiles(plan):
    if plan is not None:
        profiles = {
            (goal.timeframe_bucket, goal.risk_level)
            for goal in plan.goals.filter(enabled=True)
        }
        if profiles:
            return profiles
    from apps.portfolio_construction.rules import MAXIMUM_RISK

    return {
        (timeframe, risk_level)
        for timeframe, maximum_risk in MAXIMUM_RISK.items()
        for risk_level in range(1, maximum_risk + 1)
    }


def portfolio_builder_readiness(*, portfolio=None, plan=None):
    blockers = []
    details = {
        "bundle_path": str(settings.RESEARCH_BUNDLE_PATH),
        "expected_strategy_count": len(REGISTRY),
    }
    _, bundle_error = _bundle_report()
    if bundle_error:
        blockers.append(bundle_error)

    dataset = ResearchDatasetVersion.objects.filter(status="ACTIVE").order_by("-snapshot_date").first()
    details["active_dataset_id"] = dataset.pk if dataset else None
    if not dataset:
        blockers.append(_blocker(
            "ACTIVE_RESEARCH_DATASET_MISSING",
            "No active research dataset is installed; run bootstrap_recommendation_system",
        ))

    protocol = None
    universe = None
    if dataset:
        protocol = BacktestProtocolVersion.objects.filter(dataset_version=dataset, active=True).first()
        if not protocol:
            blockers.append(_blocker(
                "ACTIVE_BACKTEST_PROTOCOL_MISSING",
                "The active research dataset has no active backtest protocol",
                dataset_id=dataset.pk,
            ))
        universe = ResearchUniverse.objects.filter(
            dataset_version=dataset,
            key=settings.RECOMMENDATION_UNIVERSE_KEY,
            active=True,
        ).first()
        if not universe:
            blockers.append(_blocker(
                "RECOMMENDATION_UNIVERSE_MISSING",
                f"Active universe {settings.RECOMMENDATION_UNIVERSE_KEY} is missing",
                dataset_id=dataset.pk,
            ))

    profiles = _required_cache_profiles(plan)
    requires_equities = any(timeframe != "NOW" for timeframe, _ in profiles)
    details["required_cache_profiles"] = [f"{timeframe}:{risk}" for timeframe, risk in sorted(profiles)]

    if dataset:
        strategy_count = dataset.strategies.filter(active=True).count()
        implementation_count = ResearchStrategyImplementation.objects.filter(
            research_strategy__dataset_version=dataset,
            research_strategy__active=True,
        ).count()
        expected_runtime = sum(1 for entry in REGISTRY.values() if entry.runtime_mapping)
        runtime_mappings = ResearchStrategyImplementation.objects.filter(
            research_strategy__dataset_version=dataset,
            research_strategy__active=True,
            executable_strategy_definition__isnull=False,
        )
        runtime_count = runtime_mappings.count()
        profile_count = StrategyConstructionProfile.objects.filter(
            strategy_definition_id__in=runtime_mappings.values("executable_strategy_definition_id"),
            construction_enabled=True,
        ).count()
        details.update({
            "research_strategies": strategy_count,
            "strategy_implementations": implementation_count,
            "expected_runtime_mappings": expected_runtime,
            "runtime_mappings": runtime_count,
            "construction_profiles": profile_count,
        })
        if strategy_count != len(REGISTRY) or implementation_count != len(REGISTRY):
            blockers.append(_blocker(
                "STRATEGY_REGISTRY_INCOMPLETE",
                f"Strategy registry is incomplete ({strategy_count} definitions, {implementation_count} implementations; expected {len(REGISTRY)} each)",
                definitions=strategy_count,
                implementations=implementation_count,
                expected=len(REGISTRY),
            ))
        if requires_equities and runtime_count != expected_runtime:
            blockers.append(_blocker(
                "STRATEGY_RUNTIME_MAPPINGS_INCOMPLETE",
                f"Executable runtime mappings are incomplete ({runtime_count}/{expected_runtime})",
                current=runtime_count,
                expected=expected_runtime,
            ))
        if requires_equities and profile_count != expected_runtime:
            blockers.append(_blocker(
                "CONSTRUCTION_PROFILES_INCOMPLETE",
                f"Recommendation construction profiles are incomplete ({profile_count}/{expected_runtime})",
                current=profile_count,
                expected=expected_runtime,
            ))

    if universe:
        members = universe.members.filter(active=True, membership_end__isnull=True)
        member_count = members.count()
        mapped_count = members.filter(instrument__isnull=False).count()
        details.update({"universe_members": member_count, "mapped_members": mapped_count})
        if member_count != 500:
            blockers.append(_blocker(
                "RECOMMENDATION_UNIVERSE_INCOMPLETE",
                f"Recommendation universe has {member_count}/500 active members",
                current=member_count,
                expected=500,
            ))
        if requires_equities and mapped_count != member_count:
            blockers.append(_blocker(
                "INSTRUMENT_MAPPINGS_INCOMPLETE",
                f"Canonical instrument mappings are incomplete ({mapped_count}/{member_count})",
                current=mapped_count,
                expected=member_count,
            ))
        if requires_equities:
            eligible_data = ResearchDataCoverageSummary.objects.filter(
                universe_member__universe=universe,
                universe_member__active=True,
                recommendation_eligible=True,
            ).count()
            feature_instruments = InstrumentFeatureSnapshot.objects.filter(
                instrument__research_memberships__universe=universe,
                feature_key="common_daily",
                available_at__lte=timezone.now(),
            ).values("instrument_id").distinct().count()
            details.update({
                "data_ready_members": eligible_data,
                "feature_ready_instruments": feature_instruments,
                "minimum_candidate_count": settings.RECOMMENDATION_MIN_STOCKS,
            })
            if eligible_data < settings.RECOMMENDATION_MIN_STOCKS:
                blockers.append(_blocker(
                    "RESEARCH_DATA_MISSING",
                    f"Research history is ready for {eligible_data} members; at least {settings.RECOMMENDATION_MIN_STOCKS} are required",
                    current=eligible_data,
                    minimum=settings.RECOMMENDATION_MIN_STOCKS,
                ))
            if feature_instruments < settings.RECOMMENDATION_MIN_STOCKS:
                blockers.append(_blocker(
                    "RESEARCH_FEATURES_MISSING",
                    f"Current feature data exists for {feature_instruments} instruments; at least {settings.RECOMMENDATION_MIN_STOCKS} are required",
                    current=feature_instruments,
                    minimum=settings.RECOMMENDATION_MIN_STOCKS,
                ))

    current_profiles = set()
    if dataset and protocol:
        current_profiles = set(RecommendationCacheSnapshot.objects.filter(
            dataset_version=dataset,
            protocol_version=protocol,
            status="COMPLETED",
            expires_at__gt=timezone.now(),
            goal_timeframe__in={timeframe for timeframe, _ in profiles},
        ).values_list("goal_timeframe", "risk_level"))
    missing_profiles = sorted(profiles - current_profiles)
    details["current_cache_profiles"] = [f"{timeframe}:{risk}" for timeframe, risk in sorted(current_profiles & profiles)]
    details["missing_cache_profiles"] = [f"{timeframe}:{risk}" for timeframe, risk in missing_profiles]
    if missing_profiles:
        blockers.append(_blocker(
            "RECOMMENDATION_CACHES_MISSING",
            f"{len(missing_profiles)} required recommendation cache profile(s) are missing or stale",
            profiles=details["missing_cache_profiles"],
        ))

    if requires_equities:
        session = getattr(portfolio, "gateway_session", None) if portfolio else None
        connected = bool(
            session
            and session.status == session.Status.CONNECTED
            and session.commands_enabled
            and (session.last_gateway_state or {}).get("connected")
        )
        details["gateway_session_id"] = str(session.pk) if session else None
        details["gateway_connected"] = connected
        if not connected:
            blockers.append(_blocker(
                "BROKER_GATEWAY_NOT_CONNECTED",
                "The selected portfolio needs a connected, command-ready IBKR broker session",
                session_id=str(session.pk) if session else None,
                status=session.status if session else "NOT_SELECTED",
            ))

    return {"ready": not blockers, "blockers": blockers, "details": details}
