from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.instruments.models import Issuer

from ..enums import DatasetStatus
from ..models import (
    BacktestProtocolVersion,
    CompatibilityRuleSet,
    GICSTaxonomyNode,
    InstrumentClassification,
    ResearchDatasetVersion,
    ResearchFeatureDefinition,
    ResearchStrategyDefinition,
    ResearchStrategyFeatureRequirement,
    ResearchUniverse,
    ResearchUniverseMember,
)
from .bundle_validation import FREQUENCY_MAP, canonical_hash, flatten_gics, strategy_role, validate_bundle
from .universe_mapping import map_research_universe
from .strategy_registry import synchronize_strategy_registry


class BundleImportError(ValueError):
    pass


def _feature_category_map(registry):
    return {str(feature): str(category) for category, features in registry.items() for feature in features}


@transaction.atomic
def import_bundle(bundle_path, *, activate=False, map_instruments=True):
    validated = validate_bundle(bundle_path)
    version = str(validated.manifest["generated_on"])
    bundle_name = str(validated.manifest["bundle_name"])
    existing = ResearchDatasetVersion.objects.select_for_update().filter(
        bundle_name=bundle_name, version=version
    ).first()
    if existing:
        if existing.manifest_hash != validated.manifest_hash:
            raise BundleImportError("Different bundle content cannot reuse an existing bundle name/version")
        if activate and existing.status != DatasetStatus.ACTIVE:
            _activate(existing)
        return existing, False
    snapshot_date = validated.documents["stock_universe.json"]["metadata"]["snapshot_date"]
    dataset = ResearchDatasetVersion.objects.create(
        bundle_name=bundle_name,
        version=version,
        snapshot_date=snapshot_date,
        source_path=str(validated.root),
        status=DatasetStatus.VALIDATED,
        manifest_hash=validated.manifest_hash,
        file_hashes=validated.file_hashes,
        source_metadata={
            "manifest": validated.manifest,
            "gics": validated.documents["gics_taxonomy.json"].get("metadata", {}),
            "stocks": validated.documents["stock_universe.json"].get("metadata", {}),
            "strategies": validated.documents["strategy_universe.json"].get("metadata", {}),
        },
        validation_report=validated.report,
        imported_at=timezone.now(),
    )
    nodes = {}
    for level, source, parent_code, path in flatten_gics(validated.documents["gics_taxonomy.json"]):
        node = GICSTaxonomyNode.objects.create(
            dataset_version=dataset,
            level=level,
            code=source["code"],
            name=source["name"],
            parent=nodes.get(parent_code),
            path=path,
            active=True,
        )
        nodes[node.code] = node
    stock_document = validated.documents["stock_universe.json"]
    universe = ResearchUniverse.objects.create(
        key="US_LARGE_CAP_GICS",
        name=stock_document["metadata"]["name"],
        description=stock_document.get("universe_rules", {}).get("purpose", ""),
        dataset_version=dataset,
        membership_type="CURRENT_SNAPSHOT",
        active=True,
    )
    for stock in stock_document["stocks"]:
        issuer_data = stock["issuer_metadata"]
        issuer, _ = Issuer.objects.update_or_create(
            cik=str(issuer_data["cik"]),
            defaults={
                "legal_name": stock["security"],
                "display_name": stock["security"],
                "headquarters": issuer_data.get("headquarters") or "",
                "founded": str(issuer_data.get("founded") or ""),
            },
        )
        ResearchUniverseMember.objects.create(
            universe=universe,
            issuer=issuer,
            source_symbol=str(stock["symbol"]).upper(),
            security_name=stock["security"],
            currency=stock["currency"],
            exchange_hint=stock.get("broker_exchange_hint", ""),
            membership_start=None,
            membership_end=None,
            membership_status="CURRENT_SNAPSHOT",
            research_eligibility_configuration=stock.get("research_eligibility", {}),
            risk_timeframe_profile=stock.get("risk_timeframe_profile", {}),
            mapping_notes="Current snapshot only; no historical membership claim",
            active=True,
        )
        InstrumentClassification.objects.create(
            instrument=None,
            issuer=issuer,
            taxonomy_version=dataset,
            sub_industry_node=nodes[stock["gics"]["sub_industry_code"]],
            effective_from=dataset.snapshot_date,
            effective_to=None,
            is_current=True,
            source_dataset_version=dataset,
        )
    strategy_document = validated.documents["strategy_universe.json"]
    feature_categories = _feature_category_map(strategy_document.get("feature_registry", {}))
    for feature_key, category in feature_categories.items():
        ResearchFeatureDefinition.objects.update_or_create(
            key=feature_key,
            defaults={
                "category": category,
                "description": f"Declared by research bundle category {category}",
                "supported_frequencies": ["1d", "1h", "15m", "5m", "1m"],
                "status": "DECLARED",
            },
        )
    for source in strategy_document["strategies"]:
        configuration = dict(source)
        strategy = ResearchStrategyDefinition.objects.create(
            research_id=source["id"],
            dataset_version=dataset,
            name=source["name"],
            family=source["family"],
            scope=source["scope"],
            role=strategy_role(source),
            description=source.get("description", ""),
            research_hypothesis=source.get("research_hypothesis", ""),
            production_status=source.get("production_status", "RESEARCH_CANDIDATE"),
            engine_compatibility=source.get("engine_compatibility", {}),
            supported_directions=source.get("supported_directions", []),
            supported_frequencies=[FREQUENCY_MAP[item] for item in source.get("supported_bar_frequencies", [])],
            typical_holding_period=source.get("typical_holding_period", ""),
            required_data=source.get("required_data", []),
            features=source.get("features", []),
            signal_logic=source.get("signal_logic", {}),
            parameter_grid=source.get("parameter_grid", {}),
            eligibility_filters=source.get("eligibility_filters", []),
            portfolio_construction=source.get("portfolio_construction", {}),
            risk_controls=source.get("risk_controls", []),
            recommended_risk_levels=source.get("recommended_risk_levels", []),
            recommended_goal_timeframes=source.get("recommended_goal_timeframes", []),
            required_metrics=source.get("required_metrics", []),
            known_failure_modes=source.get("known_failure_modes", []),
            configuration_hash=canonical_hash(configuration),
            active=True,
        )
        for feature_source in source.get("features", []):
            key = str(feature_source.get("name", "")).strip()
            if not key:
                raise BundleImportError(f"Strategy {strategy.research_id} has a feature without a name")
            feature, _ = ResearchFeatureDefinition.objects.get_or_create(
                key=key,
                defaults={
                    "category": feature_categories.get(key, "strategy_specific"),
                    "description": f"Declared by {strategy.research_id}",
                    "formula": str(feature_source.get("formula", "")),
                    "supported_frequencies": strategy.supported_frequencies,
                    "required_datasets": strategy.required_data,
                    "status": "DECLARED",
                },
            )
            if not feature.formula and feature_source.get("formula"):
                feature.formula = str(feature_source["formula"])
                feature.save(update_fields=["formula", "updated_at"])
            ResearchStrategyFeatureRequirement.objects.create(research_strategy=strategy, feature=feature)
    compatibility = validated.documents["compatibility_rules.json"]
    CompatibilityRuleSet.objects.create(
        dataset_version=dataset,
        configuration=compatibility,
        configuration_hash=canonical_hash(compatibility),
        active=activate,
    )
    protocol = validated.documents["backtest_spec.json"]
    BacktestProtocolVersion.objects.create(
        protocol_id=protocol["metadata"]["id"],
        dataset_version=dataset,
        configuration=protocol,
        configuration_hash=canonical_hash(protocol),
        active=activate,
    )
    synchronize_strategy_registry(dataset)
    if map_instruments:
        map_research_universe(universe, create_unqualified=True)
    if activate:
        _activate(dataset)
    AuditEvent.objects.create(
        event_type="research.bundle.imported",
        actor="trusted-operator",
        aggregate_type="research_dataset",
        aggregate_id=str(dataset.pk),
        data={"bundle_name": bundle_name, "version": version, "activated": activate, **validated.report["counts"]},
        idempotency_key=f"research-bundle-import:{validated.manifest_hash}",
    )
    return dataset, True


def _activate(dataset):
    now = timezone.now()
    ResearchDatasetVersion.objects.select_for_update().filter(
        bundle_name=dataset.bundle_name, status=DatasetStatus.ACTIVE
    ).exclude(pk=dataset.pk).update(status=DatasetStatus.RETIRED, retired_at=now)
    ResearchUniverse.objects.filter(dataset_version__bundle_name=dataset.bundle_name).exclude(
        dataset_version=dataset
    ).update(active=False)
    BacktestProtocolVersion.objects.filter(dataset_version__bundle_name=dataset.bundle_name).exclude(
        dataset_version=dataset
    ).update(active=False)
    CompatibilityRuleSet.objects.filter(dataset_version__bundle_name=dataset.bundle_name).exclude(
        dataset_version=dataset
    ).update(active=False)
    dataset.status = DatasetStatus.ACTIVE
    dataset.activated_at = now
    dataset.retired_at = None
    dataset.save(update_fields=["status", "activated_at", "retired_at"])
    ResearchUniverse.objects.filter(dataset_version=dataset).update(active=True)
    BacktestProtocolVersion.objects.filter(dataset_version=dataset).update(active=True)
    CompatibilityRuleSet.objects.filter(dataset_version=dataset).update(active=True)
