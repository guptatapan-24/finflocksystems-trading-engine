from django.db.models import Count

from apps.core.views import response

from .models import (
    InstrumentEligibilitySnapshot,
    ResearchCandidateScore,
    ResearchDatasetVersion,
    ResearchExperiment,
    ResearchStrategyDefinition,
    ResearchStrategyReadiness,
    ResearchUniverse,
)


def _page(request, query, serializer):
    try:
        page = max(1, int(request.GET.get("page", 1)))
        page_size = max(1, min(100, int(request.GET.get("page_size", 50))))
    except ValueError as exc:
        raise ValueError("page and page_size must be integers") from exc
    count = query.count()
    start = (page - 1) * page_size
    items = [serializer(item) for item in query[start:start + page_size]]
    return response(items, meta={
        "count": count,
        "page": page,
        "page_size": page_size,
        "next_page": page + 1 if start + page_size < count else None,
        "previous_page": page - 1 if page > 1 else None,
    })

def dataset_versions(request):
    if request.method != "GET":
        return response(status=405, error={"code": "METHOD_NOT_ALLOWED", "message": "GET required", "details": {}})
    query = ResearchDatasetVersion.objects.order_by("-snapshot_date", "-pk")
    if request.GET.get("status"):
        query = query.filter(status=request.GET["status"].upper())
    return _page(request, query, lambda item: {
        "id": item.pk, "bundle_name": item.bundle_name, "version": item.version,
        "snapshot_date": item.snapshot_date, "status": item.status,
        "manifest_hash": item.manifest_hash, "validation_report": item.validation_report,
        "imported_at": item.imported_at, "activated_at": item.activated_at,
    })


def universes(request, universe_id=None, members=False):
    if request.method != "GET":
        return response(status=405, error={"code": "METHOD_NOT_ALLOWED", "message": "GET required", "details": {}})
    if members:
        try:
            universe = ResearchUniverse.objects.get(pk=universe_id)
        except ResearchUniverse.DoesNotExist:
            return response(status=404, error={"code": "NOT_FOUND", "message": "Universe not found", "details": {}})
        query = universe.members.select_related("issuer", "instrument").order_by("source_symbol")
        if request.GET.get("mapping_status"):
            query = query.filter(mapping_status=request.GET["mapping_status"].upper())
        return _page(request, query, lambda item: {
            "id": item.pk, "symbol": item.source_symbol, "security_name": item.security_name,
            "issuer_id": item.issuer_id, "cik": item.issuer.cik, "instrument_id": item.instrument_id,
            "currency": item.currency, "exchange_hint": item.exchange_hint,
            "membership_type": universe.membership_type, "membership_start": item.membership_start,
            "membership_end": item.membership_end, "mapping_status": item.mapping_status,
            "mapping_notes": item.mapping_notes, "active": item.active,
        })
    query = ResearchUniverse.objects.select_related("dataset_version").annotate(member_count=Count("members")).order_by("-dataset_version__snapshot_date")
    if request.GET.get("active") in {"true", "false"}:
        query = query.filter(active=request.GET["active"] == "true")
    return _page(request, query, lambda item: {
        "id": item.pk, "key": item.key, "name": item.name, "description": item.description,
        "dataset_version_id": item.dataset_version_id, "dataset_version": item.dataset_version.version,
        "membership_type": item.membership_type, "active": item.active,
        "member_count": item.member_count,
    })


def strategies(request, research_id=None):
    if request.method != "GET":
        return response(status=405, error={"code": "METHOD_NOT_ALLOWED", "message": "GET required", "details": {}})
    query = ResearchStrategyDefinition.objects.select_related("dataset_version").prefetch_related("implementations").order_by("research_id")
    if research_id:
        item = query.filter(research_id=research_id, dataset_version__status="ACTIVE").first()
        if not item:
            return response(status=404, error={"code": "NOT_FOUND", "message": "Research strategy not found", "details": {}})
        return response(_strategy_row(item, detail=True))
    for field in ("family", "scope", "role"):
        if request.GET.get(field):
            query = query.filter(**{field: request.GET[field] if field != "role" else request.GET[field].upper()})
    if request.GET.get("active") in {"true", "false"}:
        query = query.filter(active=request.GET["active"] == "true")
    return _page(request, query, _strategy_row)


def _strategy_row(item, detail=False):
    row = {
        "id": item.pk, "research_id": item.research_id, "name": item.name,
        "family": item.family, "scope": item.scope, "role": item.role,
        "production_status": item.production_status, "supported_directions": item.supported_directions,
        "supported_frequencies": item.supported_frequencies, "active": item.active,
        "dataset_version_id": item.dataset_version_id,
        "implementation_statuses": list(item.implementations.values_list("status", flat=True)),
    }
    if detail:
        row.update({
            "description": item.description, "research_hypothesis": item.research_hypothesis,
            "engine_compatibility": item.engine_compatibility, "required_data": item.required_data,
            "features": item.features, "signal_logic": item.signal_logic, "parameter_grid": item.parameter_grid,
            "eligibility_filters": item.eligibility_filters, "portfolio_construction": item.portfolio_construction,
            "risk_controls": item.risk_controls, "recommended_risk_levels": item.recommended_risk_levels,
            "recommended_goal_timeframes": item.recommended_goal_timeframes,
            "required_metrics": item.required_metrics, "known_failure_modes": item.known_failure_modes,
        })
    return row


def readiness(request):
    if request.method != "GET":
        return response(status=405, error={"code": "METHOD_NOT_ALLOWED", "message": "GET required", "details": {}})
    query = ResearchStrategyReadiness.objects.select_related("research_strategy").order_by("-as_of_date", "research_strategy__research_id")
    if request.GET.get("builder_ready") in {"true", "false"}:
        query = query.filter(builder_ready=request.GET["builder_ready"] == "true")
    return _page(request, query, lambda item: {
        "id": item.pk, "research_id": item.research_strategy.research_id, "as_of_date": item.as_of_date,
        "data_ready": item.data_ready, "features_ready": item.features_ready,
        "implementation_ready": item.implementation_ready, "backtest_ready": item.backtest_ready,
        "approved": item.approved, "builder_ready": item.builder_ready,
        "blocking_reasons": item.blocking_reasons,
    })


def candidate_scores(request):
    if request.method != "GET":
        return response(status=405, error={"code": "METHOD_NOT_ALLOWED", "message": "GET required", "details": {}})
    query = ResearchCandidateScore.objects.select_related("strategy", "instrument").order_by("-as_of_date", "-score")
    if request.GET.get("timeframe"):
        query = query.filter(goal_timeframe=request.GET["timeframe"].upper())
    if request.GET.get("risk_level"):
        query = query.filter(risk_level=request.GET["risk_level"])
    if request.GET.get("eligible") in {"true", "false"}:
        query = query.filter(eligible=request.GET["eligible"] == "true")
    return _page(request, query, lambda item: {
        "id": item.pk, "research_id": item.strategy.research_id,
        "instrument_id": item.instrument_id, "symbol": item.instrument.symbol if item.instrument else None,
        "goal_timeframe": item.goal_timeframe, "risk_level": item.risk_level,
        "as_of_date": item.as_of_date, "score": item.score, "eligible": item.eligible,
        "hard_rejection_reasons": item.hard_rejection_reasons, "expires_at": item.expires_at,
    })


def experiments(request, experiment_id):
    if request.method != "GET":
        return response(status=405, error={"code": "METHOD_NOT_ALLOWED", "message": "GET required", "details": {}})
    try:
        item = ResearchExperiment.objects.select_related("strategy", "universe", "protocol").get(pk=experiment_id)
    except ResearchExperiment.DoesNotExist:
        return response(status=404, error={"code": "NOT_FOUND", "message": "Experiment not found", "details": {}})
    return response({
        "id": item.pk, "research_id": item.strategy.research_id, "universe_id": item.universe_id,
        "protocol_id": item.protocol.protocol_id, "experiment_type": item.experiment_type,
        "parameter_budget": item.parameter_budget, "status": item.status, "error": item.error,
        "trial_count": item.trials.count(), "started_at": item.started_at, "completed_at": item.completed_at,
    })
