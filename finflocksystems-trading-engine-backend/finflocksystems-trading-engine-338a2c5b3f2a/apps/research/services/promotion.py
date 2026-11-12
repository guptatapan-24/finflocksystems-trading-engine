import hashlib
import importlib
import inspect

from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.portfolio_construction.models import StrategyConstructionProfile
from apps.strategies.models import StrategyDefinition

from ..models import ResearchCandidateScore, ResearchStrategyImplementation, ResearchStrategyReadiness


def implementation_hash(path):
    module_name, attribute = path.rsplit(".", 1)
    implementation = getattr(importlib.import_module(module_name), attribute)
    target = implementation if inspect.isclass(implementation) or inspect.isfunction(implementation) else implementation.__class__
    source = inspect.getsource(target)
    state = repr(sorted(getattr(implementation, "__dict__", {}).items()))
    return hashlib.sha256(f"{source}\n{state}".encode("utf-8")).hexdigest()


@transaction.atomic
def promote_strategy(
    research_strategy,
    *,
    implementation_path,
    implementation_version,
    executable_strategy_key,
    approval_actor,
    approval_evidence,
    as_of_date=None,
):
    as_of_date = as_of_date or timezone.localdate()
    if research_strategy.role != "EXECUTION":
        raise ValueError("Only single-asset execution strategies can be promoted to the current runtime")
    if "LONG" not in research_strategy.supported_directions:
        raise ValueError("Current Portfolio Builder promotion requires a long-only implementation")
    candidate = ResearchCandidateScore.objects.filter(
        strategy=research_strategy, eligible=True, score__gte=65, expires_at__gt=timezone.now()
    ).order_by("-score").first()
    if not candidate:
        raise ValueError("No current eligible candidate score of at least 65")
    readiness = ResearchStrategyReadiness.objects.filter(
        research_strategy=research_strategy, as_of_date=as_of_date
    ).first()
    if not readiness or not all((readiness.data_ready, readiness.features_ready, readiness.implementation_ready, readiness.backtest_ready)):
        raise ValueError("Data, feature, implementation, and backtest readiness must all pass")
    definition = StrategyDefinition.objects.get(key=executable_strategy_key)
    profile = StrategyConstructionProfile.objects.filter(
        strategy_definition=definition, construction_enabled=True
    ).first()
    if not profile:
        raise ValueError("Executable strategy requires an enabled construction profile")
    required_evidence = {"golden_vector_passed", "high_cost_passed", "multiple_testing_passed"}
    if not required_evidence.issubset({key for key, value in approval_evidence.items() if value is True}):
        raise ValueError("Approval evidence is incomplete")
    row, _ = ResearchStrategyImplementation.objects.update_or_create(
        research_strategy=research_strategy,
        implementation_path=implementation_path,
        implementation_version=implementation_version,
        defaults={
            "implementation_hash": implementation_hash(implementation_path),
            "role": research_strategy.role,
            "exact_semantic_match": True,
            "supported_frequency": "1d",
            "supported_direction": "LONG",
            "status": "BUILDER_READY" if approval_evidence.get("paper_validated") else "APPROVED_FOR_RECOMMENDATION",
            "executable_strategy_definition": definition,
            "default_parameters": candidate.best_parameters,
            "approval_record": {"actor": approval_actor, "at": timezone.now().isoformat(), **approval_evidence},
        },
    )
    readiness.approved = True
    readiness.builder_ready = bool(approval_evidence.get("paper_validated"))
    readiness.blocking_reasons = [] if readiness.builder_ready else ["PAPER_VALIDATION_REQUIRED"]
    readiness.save(update_fields=["approved", "builder_ready", "blocking_reasons"])
    AuditEvent.objects.create(
        event_type="research.strategy.promoted",
        actor=approval_actor,
        aggregate_type="research_strategy",
        aggregate_id=str(research_strategy.pk),
        data={"implementation_id": row.pk, "strategy_definition_id": definition.pk, "evidence": approval_evidence},
        idempotency_key=f"research-strategy-promotion:{row.pk}:{row.implementation_hash}",
    )
    return row
