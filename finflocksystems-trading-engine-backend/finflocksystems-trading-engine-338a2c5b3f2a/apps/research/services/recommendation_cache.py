from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Max
from django.utils import timezone

from apps.core.lazy_imports import lazy_module
from apps.portfolio_construction.rules import MAXIMUM_RISK, resolved_goal_rules

from ..engines.base import ResearchProtocolContext
from ..enums import ImplementationStatus, StrategyRole
from ..models import (
    BacktestProtocolVersion,
    InstrumentFeatureSnapshot,
    MarketRegimeSnapshot,
    RecommendationCacheSnapshot,
    ResearchCandidateScore,
    ResearchDailyBar,
    ResearchDataCoverageSummary,
    ResearchDatasetVersion,
    ResearchEvent,
    ResearchRoleScore,
    ResearchStrategyImplementation,
)
from .classification import hierarchy
from .strategy_registry import registry_entry
from .universe_pipeline import active_recommendation_universe


# Deferred: reached from config.urls via portfolio_construction.views ->
# research.services.recommendation_batch. See apps/core/lazy_imports.py.
np = lazy_module("numpy")


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _strategy_parameters(strategy):
    values = {}
    for name, choices in (strategy.parameter_grid or {}).items():
        values[name] = choices[0] if isinstance(choices, list) and choices else choices
    return values


def target_stock_count(timeframe, risk_level):
    ranges = {"HURRY": (5, 8), "FAST": (6, 10), "BUILD": (8, 12), "GROW": (10, 15), "COMPOUND": (12, 20)}
    if timeframe == "NOW":
        return 0
    low, high = ranges[timeframe]
    value = high - round((risk_level - 1) / 4 * (high - low))
    return max(settings.RECOMMENDATION_MIN_STOCKS, min(settings.RECOMMENDATION_MAX_STOCKS, value))


def _latest_features(universe, as_of_date):
    query = InstrumentFeatureSnapshot.objects.filter(
        instrument__research_memberships__universe=universe, feature_key="common_daily",
        as_of_date__lte=as_of_date, available_at__lte=timezone.now(),
    ).select_related("instrument__issuer").order_by("instrument_id", "-as_of_date")
    rows = {}
    for snapshot in query:
        if snapshot.instrument_id in rows:
            continue
        membership = snapshot.instrument.research_memberships.filter(universe=universe, active=True).first()
        if not membership:
            continue
        classification = snapshot.instrument.classifications.filter(taxonomy_version=universe.dataset_version).select_related(
            "sub_industry_node__parent__parent__parent"
        ).first()
        gics = hierarchy(classification)
        rows[snapshot.instrument_id] = {
            "instrument_id": snapshot.instrument_id, "universe_member_id": membership.pk,
            "symbol": snapshot.instrument.symbol, "company": snapshot.instrument.issuer.display_name if snapshot.instrument.issuer else membership.security_name,
            "features": snapshot.value, "gics": gics, "feature_date": snapshot.as_of_date.isoformat(),
        }
    return list(rows.values())


def _selection_contributions(dataset, rows, decision_timestamp):
    panel = [{**row, "features": row["features"]} for row in rows]
    values = defaultdict(list); contributors = defaultdict(list)
    completed_strategy_ids = set(dataset.experiments.filter(status="COMPLETED").values_list("strategy_id", flat=True))
    definitions = dataset.strategies.filter(
        active=True, pk__in=completed_strategy_ids, role__in=[StrategyRole.SELECTOR, StrategyRole.INCOME],
    ).order_by("research_id")
    for definition in definitions:
        strategy = registry_entry(definition.research_id).load()
        try:
            ranked = strategy.rank(panel, {}, ResearchProtocolContext())
        except (KeyError, TypeError, ValueError):
            continue
        denominator = max(1, len(ranked) - 1)
        for rank, item in enumerate(ranked):
            instrument_id = item["instrument_id"]
            values[instrument_id].append(100 * (1 - rank / denominator))
            contributors[instrument_id].append(definition.research_id)
    event_adjustment = defaultdict(float)
    pair_ids = list(dataset.strategies.filter(
        active=True, pk__in=completed_strategy_ids, role=StrategyRole.PAIR_BASKET,
    ).values_list("research_id", flat=True))
    event_ids = {
        research_id for research_id in dataset.strategies.filter(
            active=True, pk__in=completed_strategy_ids, role=StrategyRole.EVENT,
        ).values_list("research_id", flat=True)
    }
    event_strategy_map = {
        "EARNINGS": {"EVT_001_PEAD", "EVT_002_EARN_GAP", "EVT_003_PRE_EARN_AVOID"},
        "DIVIDEND": {"EVT_006_EXDIV"}, "EX_DIVIDEND": {"EVT_006_EXDIV"},
        "INDEX_CHANGE": {"EVT_007_INDEX"}, "SPLIT": {"EVT_008_SPLIT"},
    }
    recent_events = ResearchEvent.objects.filter(
        instrument_id__in=[row["instrument_id"] for row in rows],
        available_timestamp__lte=decision_timestamp,
        effective_timestamp__gte=decision_timestamp - timedelta(days=30),
        effective_timestamp__lte=decision_timestamp + timedelta(days=30),
        quality_status="VALID",
    )
    for event in recent_events:
        if event.effective_timestamp > decision_timestamp:
            contributors[event.instrument_id].extend(sorted(event_ids & {"EVT_003_PRE_EARN_AVOID"}))
            continue
        surprise = float((event.payload or {}).get("standardized_surprise", (event.payload or {}).get("abnormal_return", 0)) or 0)
        event_adjustment[event.instrument_id] += max(-25.0, min(25.0, surprise * 10))
        contributors[event.instrument_id].extend(sorted(event_ids & event_strategy_map.get(event.event_type, set())))
    calendar_ids = set()
    if decision_timestamp.day <= 3:
        calendar_ids.add("EVT_004_TURN_MONTH")
    if (decision_timestamp + timedelta(days=3)).month != decision_timestamp.month:
        calendar_ids.add("EVT_005_MONTH_END")
    if calendar_ids:
        for row in rows:
            contributors[row["instrument_id"]].extend(sorted(event_ids & calendar_ids))
    if pair_ids:
        for row in rows:
            if row["features"].get("peer_residual_zscore") is not None:
                contributors[row["instrument_id"]].extend(pair_ids)
    return {
        row["instrument_id"]: {
            "selector_fundamental": float(np.mean(values[row["instrument_id"]])) if values[row["instrument_id"]] else 50.0,
            "event": max(0.0, min(100.0, 50.0 + event_adjustment[row["instrument_id"]])),
            "pair_relative_value": min(100.0, 50.0 + 10.0 * abs(float(row["features"].get("peer_residual_zscore", 0)))) if pair_ids else 50.0,
            "contributors": list(dict.fromkeys(contributors[row["instrument_id"]])),
        }
        for row in rows
    }


def calculate_role_scores(dataset, *, as_of_date=None):
    as_of_date = as_of_date or timezone.localdate()
    universe = active_recommendation_universe()
    features = _latest_features(universe, as_of_date)
    if not features:
        return {"stock_scores": 0, "role_scores": 0}
    liquidity = np.asarray([float(row["features"].get("median_dollar_volume_20d", 0)) for row in features])
    order = np.argsort(np.argsort(liquidity))
    liquidity_percentiles = order / max(1, len(features) - 1)
    contributions = _selection_contributions(dataset, features, timezone.now())
    sector_counts = defaultdict(int)
    for row in features:
        sector_counts[(row.get("gics") or {}).get("sector", {}).get("code", "UNKNOWN")] += 1
    updated = 0
    expiry = timezone.now() + timedelta(days=settings.RESEARCH_SCORE_MAX_AGE_DAYS)
    for row, percentile in zip(features, liquidity_percentiles):
        values = row["features"]
        volatility = float(values.get("realized_volatility", .30))
        drawdown = float(values.get("maximum_drawdown", .50))
        extra = contributions[row["instrument_id"]]
        sector = (row.get("gics") or {}).get("sector", {}).get("code", "UNKNOWN")
        components = {
            "liquidity": float(percentile * 100), "data_quality": 100.0,
            "gics_diversification": 100.0 * (1 - sector_counts[sector] / max(1, len(features))),
            "selector_fundamental": extra["selector_fundamental"], "event": extra["event"],
            "pair_relative_value": extra["pair_relative_value"],
            "volatility_fit": max(0.0, 100 * (1 - volatility / .60)),
            "drawdown_fit": max(0.0, 100 * (1 - drawdown / .80)),
            "capacity": min(100.0, float(values.get("median_dollar_volume_20d", 0)) / 1_000_000),
        }
        score = np.mean(list(components.values()))
        for timeframe, maximum_risk in MAXIMUM_RISK.items():
            if timeframe == "NOW":
                continue
            for risk_level in range(1, maximum_risk + 1):
                ResearchRoleScore.objects.update_or_create(
                    score_type="STOCK", dataset_version=dataset, strategy=None, instrument_id=row["instrument_id"],
                    goal_timeframe=timeframe, risk_level=risk_level, as_of_date=as_of_date,
                    defaults={"score": Decimal(str(score)), "components": components, "contributing_strategy_ids": extra["contributors"], "expires_at": expiry},
                )
                updated += 1
    role_updated = 0
    trials = universe.experiments.filter(status="COMPLETED", instrument__isnull=True).select_related("strategy").prefetch_related("trials")
    type_map = {"ALLOCATOR": "ALLOCATOR", "OVERLAY": "OVERLAY", "EVENT": "EVENT", "PAIR_BASKET": "PAIR_BASKET"}
    for experiment in trials:
        score_type = type_map.get(experiment.role)
        if not score_type:
            continue
        trial = experiment.trials.filter(status="COMPLETED").order_by("-summary_metrics__sharpe").first()
        if not trial:
            continue
        metrics = trial.summary_metrics or {}
        score = max(0, min(100, 50 + 15 * float(metrics.get("sharpe", 0)) - 50 * float(metrics.get("max_drawdown", 0))))
        for timeframe, maximum_risk in MAXIMUM_RISK.items():
            if timeframe == "NOW": continue
            for risk_level in range(1, maximum_risk + 1):
                ResearchRoleScore.objects.update_or_create(
                    score_type=score_type, dataset_version=dataset, strategy=experiment.strategy, instrument=None,
                    goal_timeframe=timeframe, risk_level=risk_level, as_of_date=as_of_date,
                    defaults={"score": Decimal(str(score)), "components": metrics, "contributing_strategy_ids": [experiment.strategy.research_id], "expires_at": expiry},
                ); role_updated += 1
    return {"stock_scores": updated, "role_scores": role_updated}


def _execution_mapping(dataset, research_strategy_id, *, allow_validated=False):
    statuses = [ImplementationStatus.BUILDER_READY, ImplementationStatus.APPROVED, ImplementationStatus.PAPER_VALIDATED,
                ImplementationStatus.APPROVED_FOR_RECOMMENDATION]
    if allow_validated:
        statuses += [ImplementationStatus.VALIDATED, ImplementationStatus.BACKTESTED, ImplementationStatus.SCORED]
    return ResearchStrategyImplementation.objects.filter(
        research_strategy__dataset_version=dataset, research_strategy__research_id=research_strategy_id,
        exact_semantic_match=True, executable_strategy_definition__enabled=True, status__in=statuses,
    ).select_related("research_strategy", "executable_strategy_definition").first()


def _full_model_candidates(dataset, timeframe, risk_level, rows):
    now = timezone.now(); row_by_instrument = {row["instrument_id"]: row for row in rows}
    stock_scores = {}
    for item in ResearchRoleScore.objects.filter(
        score_type="STOCK", dataset_version=dataset, goal_timeframe=timeframe, risk_level=risk_level,
        expires_at__gt=now, instrument_id__in=row_by_instrument,
    ).order_by("instrument_id", "-as_of_date"):
        stock_scores.setdefault(item.instrument_id, item)
    protocol = BacktestProtocolVersion.objects.get(dataset_version=dataset, active=True)
    execution_query = ResearchCandidateScore.objects.filter(
        dataset_version=dataset, goal_timeframe=timeframe, risk_level=risk_level, eligible=True,
        protocol_version=protocol, expires_at__gt=now, instrument_id__in=row_by_instrument,
        strategy__role="EXECUTION",
    ).select_related("strategy").order_by("instrument_id", "strategy_id", "-as_of_date")
    latest_execution = {}
    for candidate in execution_query:
        latest_execution.setdefault((candidate.instrument_id, candidate.strategy_id), candidate)
    execution = sorted(latest_execution.values(), key=lambda item: (-float(item.score), item.strategy.research_id))
    result = {}
    for candidate in execution:
        if candidate.instrument_id in result or candidate.instrument_id not in stock_scores:
            continue
        implementation = _execution_mapping(dataset, candidate.strategy.research_id)
        if not implementation:
            continue
        row = row_by_instrument[candidate.instrument_id]; features = row["features"]
        final_score = float(stock_scores[candidate.instrument_id].score) + float(candidate.score)
        result[candidate.instrument_id] = {
            **{key: value for key, value in row.items() if key != "features"},
            "research_strategy_id": candidate.strategy.research_id,
            "execution_strategy_definition_id": implementation.executable_strategy_definition_id,
            "strategy_name": candidate.strategy.name, "execution_timeframe": implementation.supported_frequency,
            "parameters": candidate.best_parameters or implementation.default_parameters, "candidate_score": float(candidate.score),
            "capacity_weight": float((candidate.capacity_metrics or {}).get("maximum_weight", 1)),
            "final_score": final_score, "expected_return": float((candidate.metrics or {}).get("expected_return", features.get("formation_return", 0))),
            "expected_volatility": float((candidate.metrics or {}).get("expected_volatility", features.get("realized_volatility", .30))),
            "expected_drawdown": float((candidate.metrics or {}).get("expected_drawdown", features.get("maximum_drawdown", .50))),
            "contributing_strategy_ids": stock_scores[candidate.instrument_id].contributing_strategy_ids + [candidate.strategy.research_id],
            "reason": f"Current full-model stock and execution scores are {float(stock_scores[candidate.instrument_id].score):.1f} and {float(candidate.score):.1f}.",
        }
    return sorted(result.values(), key=lambda row: (-row["final_score"], row["symbol"]))


def _price_fallback_candidates(dataset, timeframe, risk_level, rows, *, baseline_only=False):
    strategy_by_timeframe = {"HURRY": "MR_002_RSI14", "FAST": "TR_006_DONCHIAN_20", "BUILD": "MOM_002_TS_63", "GROW": "TR_001_SMA_020_100", "COMPOUND": "BH_001"}
    research_id = "BH_001" if baseline_only else strategy_by_timeframe[timeframe]
    implementation = _execution_mapping(dataset, research_id, allow_validated=True) or _execution_mapping(dataset, "BH_001", allow_validated=True)
    if not implementation:
        raise ValueError("No validated baseline long-only runtime mapping is available")
    ranked = sorted(rows, key=lambda row: (
        -float(row["features"].get("median_dollar_volume_20d", 0)),
        -float(row["features"].get("risk_adjusted_momentum", 0)), row["symbol"],
    ))
    result = []
    for row in ranked:
        values = row["features"]
        result.append({
            **{key: value for key, value in row.items() if key != "features"},
            "research_strategy_id": implementation.research_strategy.research_id,
            "execution_strategy_definition_id": implementation.executable_strategy_definition_id,
            "strategy_name": implementation.research_strategy.name, "execution_timeframe": implementation.supported_frequency,
            "parameters": implementation.default_parameters, "candidate_score": None,
            "capacity_weight": min(1.0, float(values.get("median_dollar_volume_20d", 0)) / 250_000_000),
            "final_score": float(values.get("median_dollar_volume_20d", 0)),
            "expected_return": float(values.get("formation_return", 0)),
            "expected_volatility": float(values.get("realized_volatility", .30)),
            "expected_drawdown": float(values.get("maximum_drawdown", .50)),
            "contributing_strategy_ids": [implementation.research_strategy.research_id],
            "reason": "Diversified liquid baseline fallback." if baseline_only else "Current price-only model fallback.",
        })
    return result


def _diversified_select(candidates, count):
    selected=[];sector_counts=defaultdict(int)
    for row in candidates:
        sector=(row.get("gics") or {}).get("sector",{}).get("code", "UNKNOWN")
        if sector_counts[sector] and len({(item.get("gics") or {}).get("sector",{}).get("code") for item in selected}) < min(11,count):
            continue
        selected.append(row);sector_counts[sector]+=1
        if len(selected)>=count:break
    if len(selected)<count:
        for row in candidates:
            if row not in selected:selected.append(row)
            if len(selected)>=count:break
    return selected


def _return_matrix(selected, as_of_date, limit=260):
    instrument_ids = [row["instrument_id"] for row in selected]
    prices = defaultdict(dict); seen = set()
    query = ResearchDailyBar.objects.filter(
        instrument_id__in=instrument_ids, trading_date__lte=as_of_date, quality_status="VALID",
    ).only("instrument_id", "trading_date", "adjusted_close", "data_version").order_by(
        "instrument_id", "-trading_date", "-data_version",
    )
    counts = defaultdict(int)
    for bar in query:
        identity = (bar.instrument_id, bar.trading_date)
        if identity in seen or counts[bar.instrument_id] >= limit:
            continue
        seen.add(identity); counts[bar.instrument_id] += 1
        prices[bar.instrument_id][bar.trading_date] = float(bar.adjusted_close)
    if not instrument_ids or any(len(prices[item]) < 60 for item in instrument_ids):
        return None
    common_dates = sorted(set.intersection(*(set(prices[item]) for item in instrument_ids)))
    if len(common_dates) < 60:
        return None
    levels = np.asarray([[prices[item][value] for item in instrument_ids] for value in common_dates], dtype=float)
    return np.diff(np.log(levels), axis=0)


def _weights(selected, rules, *, allocator=None, as_of_date=None):
    investable=float(1-Decimal(rules["minimum_cash_weight"]));cap=float(rules["maximum_stock_weight"])
    if not selected:return [],1.0
    raw = None
    if allocator is not None and as_of_date is not None:
        matrix = _return_matrix(selected, as_of_date)
        if matrix is not None:
            try:
                implementation = registry_entry(allocator.strategy.research_id).load()
                raw = np.asarray(implementation.allocate(
                    matrix, {}, {
                        "per_stock_cap": cap,
                        "sector_vector": [(row.get("gics") or {}).get("sector", {}).get("code", "UNKNOWN") for row in selected],
                    }, {**_strategy_parameters(allocator.strategy), "max_weight": cap},
                ), dtype=float)
            except (KeyError, TypeError, ValueError):
                raw = None
    if raw is None:
        raw=np.asarray([1/max(float(row["expected_volatility"]),.05) for row in selected])
    raw=raw/np.sum(raw)*investable
    capacity=np.asarray([min(cap,float(row.get("capacity_weight",cap))) for row in selected])
    raw=np.minimum(raw,capacity)
    remainder=investable-float(np.sum(raw))
    for _ in range(100):
        eligible=np.where(raw<capacity-1e-12)[0]
        if remainder<=1e-10 or not len(eligible):break
        addition=min(remainder/len(eligible),min(capacity[index]-raw[index] for index in eligible));raw[eligible]+=addition;remainder=investable-float(np.sum(raw))
    return raw.tolist(),1-float(np.sum(raw))


def _apply_overlays(selected, weights, overlays, universe, as_of_date):
    if not selected or not overlays:
        return list(weights), []
    regime = MarketRegimeSnapshot.objects.filter(
        universe=universe, as_of_date__lte=as_of_date, available_at__lte=timezone.now(),
    ).order_by("-as_of_date").first()
    total = max(float(np.sum(weights)), 1e-12)
    risk_state = {
        "realized_volatility": sum(float(row.get("expected_volatility", .2)) * weight for row, weight in zip(selected, weights)) / total,
        "drawdown": -sum(float(row.get("expected_drawdown", 0)) * weight for row, weight in zip(selected, weights)) / total,
        "market_trend": sum(float(row.get("expected_return", 0)) * weight for row, weight in zip(selected, weights)) / total,
        "average_correlation": .2, "regime": regime.regime if regime else "NORMAL", "liquidity_stress": 0,
    }
    current = list(weights); applied = []
    for score in overlays:
        try:
            implementation = registry_entry(score.strategy.research_id).load()
            current = implementation.apply(current, risk_state, _strategy_parameters(score.strategy))
            applied.append(score)
        except (KeyError, TypeError, ValueError):
            continue
    return current, applied


def _gics_exposure(selected):
    exposure = {"sector": defaultdict(float), "industry": defaultdict(float), "sub_industry": defaultdict(float)}
    for row in selected:
        gics = row.get("gics") or {}
        weight = float(row.get("weight") or 0)
        for level in exposure:
            code = (gics.get(level) or {}).get("code") or "UNKNOWN"
            exposure[level][code] += weight
    return {level: dict(sorted(values.items())) for level, values in exposure.items()}


def _fallback_copy(source, tier, as_of_date, reason):
    input_hash = _hash({"fallback_source": source.pk, "tier": tier, "as_of_date": as_of_date})
    snapshot, _ = RecommendationCacheSnapshot.objects.update_or_create(
        dataset_version=source.dataset_version, protocol_version=source.protocol_version,
        goal_timeframe=source.goal_timeframe, risk_level=source.risk_level,
        as_of_date=as_of_date, input_hash=input_hash,
        defaults={
            "candidate_pool": source.candidate_pool, "selected_stocks": source.selected_stocks,
            "allocator_strategy_id": source.allocator_strategy_id,
            "overlay_strategy_ids": source.overlay_strategy_ids,
            "expected_metrics": source.expected_metrics, "gics_exposure": source.gics_exposure,
            "fallback_tier": tier,
            "data_freshness": {**(source.data_freshness or {}), "fallback_source_snapshot_id": source.pk, "fallback_reason": reason},
            "status": "COMPLETED",
            "expires_at": timezone.now() + timedelta(hours=settings.RECOMMENDATION_SNAPSHOT_MAX_AGE_HOURS),
        },
    )
    return snapshot


def build_cache_snapshot(timeframe, risk_level, *, as_of_date=None):
    as_of_date=as_of_date or timezone.localdate();universe=active_recommendation_universe();dataset=universe.dataset_version
    protocol=BacktestProtocolVersion.objects.get(dataset_version=dataset,active=True);rules=resolved_goal_rules(timeframe,risk_level)
    if timeframe=="NOW":candidates=[];selected=[];tier=1;weights=[];cash=1.0
    else:
        rows=_latest_features(universe,as_of_date)
        full=_full_model_candidates(dataset,timeframe,risk_level,rows)
        desired=target_stock_count(timeframe,risk_level)
        if len(full)>=desired:
            candidates=full;tier=1
        else:
            previous=RecommendationCacheSnapshot.objects.filter(
                dataset_version=dataset,goal_timeframe=timeframe,risk_level=risk_level,status="COMPLETED",
                created_at__gte=timezone.now()-timedelta(days=settings.RESEARCH_STALE_SCORE_FALLBACK_DAYS),fallback_tier=1,
            ).order_by("-created_at").first()
            if previous:return _fallback_copy(previous,2,as_of_date,"LAST_VALID_FULL_SNAPSHOT")
            try:
                candidates=_price_fallback_candidates(dataset,timeframe,risk_level,rows);tier=3
            except ValueError:
                candidates=[]
            if len(candidates)<desired:
                try:
                    candidates=_price_fallback_candidates(dataset,timeframe,risk_level,rows,baseline_only=True);tier=4
                except ValueError:
                    candidates=[]
            if not candidates:
                existing=RecommendationCacheSnapshot.objects.filter(
                    dataset_version=dataset,protocol_version=protocol,goal_timeframe=timeframe,
                    risk_level=risk_level,status="COMPLETED",
                ).order_by("-created_at").first()
                if existing:return _fallback_copy(existing,5,as_of_date,"LATEST_VALIDATED_DEPLOYMENT_SNAPSHOT")
                raise ValueError("Operational deployment failure: no provider data or validated recommendation snapshot exists")
        selected=_diversified_select(candidates,desired)
        allocator=ResearchRoleScore.objects.filter(score_type="ALLOCATOR",dataset_version=dataset,goal_timeframe=timeframe,risk_level=risk_level,expires_at__gt=timezone.now()).select_related("strategy").order_by("-score").first()
        weights,cash=_weights(selected,rules,allocator=allocator,as_of_date=as_of_date)
        for row,weight in zip(selected,weights):row["weight"]=weight
    allocator=ResearchRoleScore.objects.filter(score_type="ALLOCATOR",dataset_version=dataset,goal_timeframe=timeframe,risk_level=risk_level,expires_at__gt=timezone.now()).select_related("strategy").order_by("-score").first() if timeframe!="NOW" else None
    overlays=list(ResearchRoleScore.objects.filter(score_type="OVERLAY",dataset_version=dataset,goal_timeframe=timeframe,risk_level=risk_level,expires_at__gt=timezone.now()).select_related("strategy").order_by("-score")[:2]) if timeframe!="NOW" else []
    if timeframe!="NOW":
        weights,overlays=_apply_overlays(selected,weights,overlays,universe,as_of_date)
        for row,weight in zip(selected,weights):row["weight"]=weight
        cash=1-float(np.sum(weights))
    input_hash=_hash({"date":as_of_date,"timeframe":timeframe,"risk":risk_level,"selected":[(row["instrument_id"],row["research_strategy_id"],row.get("weight")) for row in selected]})
    snapshot,_=RecommendationCacheSnapshot.objects.update_or_create(
        dataset_version=dataset,protocol_version=protocol,goal_timeframe=timeframe,risk_level=risk_level,
        as_of_date=as_of_date,input_hash=input_hash,
        defaults={"candidate_pool":candidates[:settings.RECOMMENDATION_CANDIDATE_POOL_SIZE],"selected_stocks":selected,
                  "allocator_strategy_id":allocator.strategy.research_id if allocator else "INV_VOL_001",
                  "overlay_strategy_ids":[item.strategy.research_id for item in overlays],
                  "expected_metrics":{"expected_return":sum(row.get("expected_return",0)*row.get("weight",0) for row in selected),
                                      "expected_volatility":sum(row.get("expected_volatility",0)*row.get("weight",0) for row in selected),
                                      "expected_drawdown":sum(row.get("expected_drawdown",0)*row.get("weight",0) for row in selected),"cash_weight":cash},
                  "gics_exposure":_gics_exposure(selected),"fallback_tier":tier,"data_freshness":{"as_of_date":as_of_date.isoformat(),"feature_count":len(rows) if timeframe!="NOW" else 0},
                  "status":"COMPLETED","expires_at":timezone.now()+timedelta(hours=settings.RECOMMENDATION_SNAPSHOT_MAX_AGE_HOURS)},
    )
    return snapshot


def warm_all_recommendation_caches(*, as_of_date=None):
    dataset=ResearchDatasetVersion.objects.filter(status="ACTIVE").first()
    if not dataset:raise ValueError("No active research dataset")
    calculate_role_scores(dataset,as_of_date=as_of_date)
    rows=[]
    for timeframe,maximum_risk in MAXIMUM_RISK.items():
        for risk_level in range(1,maximum_risk+1):
            snapshot=build_cache_snapshot(timeframe,risk_level,as_of_date=as_of_date)
            rows.append({"timeframe":timeframe,"risk_level":risk_level,"snapshot_id":snapshot.pk,"fallback_tier":snapshot.fallback_tier})
    return {"snapshots":rows,"count":len(rows)}


def prepare_recommendation_caches(*, as_of_date=None):
    """Build every cache profile independently and report real data blockers."""
    dataset=ResearchDatasetVersion.objects.filter(status="ACTIVE").first()
    if not dataset:raise ValueError("No active research dataset")
    calculate_role_scores(dataset,as_of_date=as_of_date)
    rows=[];failures=[]
    for timeframe,maximum_risk in MAXIMUM_RISK.items():
        for risk_level in range(1,maximum_risk+1):
            try:
                snapshot=build_cache_snapshot(timeframe,risk_level,as_of_date=as_of_date)
                rows.append({"timeframe":timeframe,"risk_level":risk_level,"snapshot_id":snapshot.pk,
                             "fallback_tier":snapshot.fallback_tier})
            except ValueError as exc:
                failures.append({"timeframe":timeframe,"risk_level":risk_level,"error":str(exc)})
    return {"snapshots":rows,"count":len(rows),"failures":failures,"ready":not failures}


def best_cached_recommendation(timeframe,risk_level):
    now=timezone.now();universe=active_recommendation_universe();dataset=universe.dataset_version
    protocol=BacktestProtocolVersion.objects.get(dataset_version=dataset,active=True)
    current=RecommendationCacheSnapshot.objects.filter(
        dataset_version=dataset,protocol_version=protocol,goal_timeframe=timeframe,
        risk_level=risk_level,status="COMPLETED",expires_at__gt=now,
    ).order_by("fallback_tier","-created_at").first()
    if current:return current
    return build_cache_snapshot(timeframe,risk_level)
