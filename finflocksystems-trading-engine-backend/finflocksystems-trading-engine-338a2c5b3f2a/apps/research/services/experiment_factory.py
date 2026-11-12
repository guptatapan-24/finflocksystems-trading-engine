from __future__ import annotations

import hashlib
import json

from django.db import transaction
from django.db.models import Count, Max
from django.utils import timezone

from ..enums import StrategyRole
from ..models import (
    BacktestProtocolVersion,
    CrossSectionalFeatureSnapshot,
    ResearchDataCoverageSummary,
    ResearchEvent,
    ResearchExperiment,
    ResearchTrial,
)
from .experiments import parameter_candidates
from .feature_pipeline import FEATURE_VERSION
from .strategy_registry import registry_entry


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _canonical_parameters(strategy):
    parameters = {}
    for name, choices in (strategy.parameter_grid or {}).items():
        if isinstance(choices, list) and choices:
            parameters[name] = choices[0]
        elif choices is not None:
            parameters[name] = choices
    return parameters


def _experiment_identity(strategy, universe, protocol, entry, instrument_id=None, *, start_date=None, end_date=None, data_version=""):
    return {
        "dataset_version": universe.dataset_version_id, "protocol_version": protocol.configuration_hash,
        "implementation_hash": entry.implementation_hash, "feature_version": FEATURE_VERSION,
        "instrument": instrument_id, "universe": universe.pk, "parameter_hash": _hash(strategy.parameter_grid),
        "start_date": start_date, "end_date": end_date, "provider_data_version": data_version, "role": entry.role,
    }


@transaction.atomic
def _create_experiment(strategy, universe, protocol, entry, *, instrument_id=None, start_date=None, end_date=None, data_version=""):
    identity = _experiment_identity(strategy, universe, protocol, entry, instrument_id, start_date=start_date, end_date=end_date, data_version=data_version)
    request_hash = _hash(identity)
    experiment, created = ResearchExperiment.objects.get_or_create(
        idempotency_key=f"research-experiment:{request_hash}",
        defaults={
            "strategy": strategy, "universe": universe, "protocol": protocol, "dataset_version": universe.dataset_version,
            "instrument_id": instrument_id, "implementation_hash": entry.implementation_hash, "data_version": data_version,
            "provider_data_version": data_version, "feature_version": FEATURE_VERSION,
            "parameter_space_hash": _hash(strategy.parameter_grid), "start_date": start_date, "end_date": end_date,
            "experiment_type": entry.backtest_engine, "role": entry.role, "parameter_budget": entry.parameter_budget,
            "request_hash": request_hash, "status": "QUEUED",
        },
    )
    if created:
        sampled = parameter_candidates(
            strategy.parameter_grid, baseline=_canonical_parameters(strategy), budget=entry.parameter_budget,
            seed=int(strategy.configuration_hash[:8], 16),
        )
        for parameters in sampled["sampled"]:
            parameter_hash = _hash(parameters)
            ResearchTrial.objects.get_or_create(
                experiment=experiment, instrument_id=instrument_id, parameter_hash=parameter_hash,
                defaults={"parameters": parameters, "window_configuration": {"final_holdout": True}, "status": "QUEUED"},
            )
    return experiment, created


def build_role_aware_experiments(universe, *, protocol=None, as_of_date=None, maximum_single_asset_pairs=None):
    protocol = protocol or BacktestProtocolVersion.objects.get(dataset_version=universe.dataset_version, active=True)
    as_of_date = as_of_date or timezone.localdate()
    strategies = universe.dataset_version.strategies.filter(active=True).order_by("research_id")
    created = 0; by_role = {}; scheduled = []
    coverage = list(ResearchDataCoverageSummary.objects.filter(
        universe_member__universe=universe, universe_member__active=True, recommendation_eligible=True,
    ).select_related("universe_member__instrument").annotate(
        provider_revision=Max("universe_member__instrument__research_daily_bars__revision_timestamp"),
        provider_version=Max("universe_member__instrument__research_daily_bars__data_version"),
    ).order_by("universe_member_id"))
    pair_budget = maximum_single_asset_pairs or len(coverage) * 50
    single_pairs = 0
    latest_panel = CrossSectionalFeatureSnapshot.objects.filter(
        universe=universe, as_of_date__lte=as_of_date, available_at__lte=timezone.now(),
    ).order_by("-as_of_date").first()
    event_version = ResearchEvent.objects.filter(available_timestamp__date__lte=as_of_date).aggregate(
        revision=Max("revision_timestamp"), version=Max("data_version"), count=Count("id"),
    )
    universe_version = _hash({
        "coverage": [(item.universe_member_id, item.daily_end_date, item.provider_revision, item.provider_version) for item in coverage],
        "feature": latest_panel.data_version if latest_panel else "", "events": event_version,
    })
    for strategy in strategies:
        entry = registry_entry(strategy.research_id)
        targets = []
        if entry.role == StrategyRole.EXECUTION:
            targets = [(item.universe_member.instrument_id, item.daily_start_date, min(item.daily_end_date, as_of_date),
                        _hash((item.daily_end_date,item.provider_revision,item.provider_version))) for item in coverage]
        elif entry.role == StrategyRole.EVENT:
            event_types = {
                "EVT_001_PEAD": ["EARNINGS"], "EVT_002_EARN_GAP": ["EARNINGS"],
                "EVT_003_PRE_EARN_AVOID": ["EARNINGS"], "EVT_006_EXDIV": ["DIVIDEND", "EX_DIVIDEND"],
                "EVT_007_INDEX": ["INDEX_CHANGE"], "EVT_008_SPLIT": ["SPLIT"],
            }.get(strategy.research_id)
            if event_types is None:
                event_instruments = [item.universe_member.instrument_id for item in coverage]
            else:
                event_instruments = ResearchEvent.objects.filter(
                    event_type__in=event_types, instrument__isnull=False,
                    available_timestamp__date__lte=as_of_date,
                ).values_list("instrument_id", flat=True).distinct()
            targets = [(instrument_id, None, as_of_date, universe_version) for instrument_id in sorted(event_instruments)]
        else:
            targets = [(None, None, as_of_date, universe_version)]
        for instrument_id, start_date, end_date, data_version in targets:
            if entry.role == StrategyRole.EXECUTION:
                if single_pairs >= pair_budget:
                    break
                single_pairs += 1
            experiment, was_created = _create_experiment(
                strategy, universe, protocol, entry, instrument_id=instrument_id,
                start_date=start_date, end_date=end_date, data_version=data_version,
            )
            created += int(was_created)
            if was_created and len(scheduled)<100:scheduled.append(experiment.pk)
            by_role[entry.role] = by_role.get(entry.role, 0) + int(was_created)
    return {"created":created,"scheduled_preview":scheduled,"scheduled_count":created,
            "by_role":by_role,"single_asset_pairs":single_pairs}
