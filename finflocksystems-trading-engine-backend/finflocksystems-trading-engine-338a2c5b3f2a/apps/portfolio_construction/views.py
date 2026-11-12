import json
import logging
import uuid
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction

from apps.audit.models import AuditEvent
from apps.core.idempotency import canonical_request_hash
from apps.core.throttling import throttle_response
from apps.core.validation import decimal_field, require_fields
from apps.core.views import response
from apps.instruments.models import Instrument
from apps.portfolios.models import TradingPortfolio
from apps.strategies.models import OrderPolicy, StrategyDefinition, StrategyRiskPolicy
from apps.research.models import BacktestProtocolVersion, RecommendationBatchRun, ResearchDatasetVersion
from apps.research.services.builder_readiness import portfolio_builder_readiness
from apps.execution.modes import require_portfolio_execution_ready
from apps.research.services.recommendation_batch import create_recommendation_batch
from apps.research.tasks import generate_recommendation_batch

from .models import (
    GoalInstrumentSelection,
    GoalStrategyAssignment,
    PortfolioConstructionPlan,
    PortfolioConstructionRun,
    PortfolioGoalAllocation,
)
from .rules import RISK_OPTIONS, TIMEFRAME_OPTIONS, resolved_goal_rules
from .services import (
    ConstructionAlreadyApplied,
    ConstructionError,
    bump_plan_version,
    create_construction_run,
    eligible_strategies,
    plan_validation,
    validate_assignment,
    validate_instrument_selection,
)


logger = logging.getLogger(__name__)


def _actor(request):
    user = getattr(request, "user", None)
    return user.get_username() if user and user.is_authenticated else "operator/system"


def _audit(request, event_type, portfolio_id, data, idempotency_key=None):
    AuditEvent.objects.get_or_create(
        idempotency_key=idempotency_key or f"audit:{event_type}:{uuid.uuid4()}",
        defaults={
            "event_type": event_type,
            "actor": _actor(request),
            "aggregate_type": "portfolio",
            "aggregate_id": str(portfolio_id),
            "data": data,
        },
    )


def _payload(request):
    value = json.loads(request.body or b"{}")
    if not isinstance(value, dict):
        raise ValueError("Request body must be a JSON object")
    return value


def _goal_row(goal):
    rules = resolved_goal_rules(goal.timeframe_bucket, goal.risk_level)
    return {
        "id": goal.pk,
        "plan_id": goal.plan_id,
        "name": goal.name,
        "allocation_weight": goal.allocation_weight,
        "allocation_percentage": goal.allocation_weight * Decimal(100),
        "timeframe_bucket": goal.timeframe_bucket,
        "risk_level": goal.risk_level,
        "enabled": goal.enabled,
        "display_order": goal.display_order,
        "construction_source": goal.construction_source,
        "accepted_recommendation_run_id": goal.accepted_recommendation_run_id,
        "resolved_rules": rules,
        "instrument_count": goal.instrument_selections.filter(enabled=True).count(),
        "created_at": goal.created_at,
        "updated_at": goal.updated_at,
    }


def _plan_row(plan):
    validation = plan_validation(plan)
    return {
        "id": plan.pk,
        "portfolio_id": plan.portfolio_id,
        "name": plan.name,
        "status": plan.status,
        "version": plan.version,
        **validation,
        "timeframe_options": [{"code": code, "label": label} for code, label in TIMEFRAME_OPTIONS],
        "risk_options": [
            {"level": level, "code": code, "label": label} for level, code, label in RISK_OPTIONS
        ],
        "goals": [_goal_row(goal) for goal in plan.goals.all().order_by("display_order", "pk")],
        "created_at": plan.created_at,
        "updated_at": plan.updated_at,
    }


def _instrument_row(selection):
    return {
        "id": selection.pk,
        "goal_id": selection.goal_allocation_id,
        "instrument_id": selection.instrument_id,
        "symbol": selection.instrument.symbol,
        "asset_class": selection.instrument.asset_class,
        "exchange": selection.instrument.exchange,
        "currency": selection.instrument.currency,
        "minimum_weight": selection.minimum_weight,
        "maximum_weight": selection.maximum_weight,
        "display_order": selection.display_order,
        "enabled": selection.enabled,
        "assignment_count": selection.assignments.filter(enabled=True).count(),
        "created_at": selection.created_at,
        "updated_at": selection.updated_at,
    }


def _assignment_row(assignment):
    return {
        "id": assignment.pk,
        "goal_instrument_id": assignment.goal_instrument_selection_id,
        "goal_id": assignment.goal_instrument_selection.goal_allocation_id,
        "strategy_definition_id": assignment.strategy_definition_id,
        "strategy_key": assignment.strategy_definition.key,
        "strategy_name": assignment.strategy_definition.name,
        "instrument_id": assignment.goal_instrument_selection.instrument_id,
        "symbol": assignment.goal_instrument_selection.instrument.symbol,
        "execution_timeframe": assignment.execution_timeframe,
        "parameter_overrides": assignment.parameter_overrides,
        "parameter_hash": assignment.parameter_hash,
        "strategy_share": assignment.strategy_share,
        "risk_policy_id": assignment.risk_policy_id,
        "order_policy_id": assignment.order_policy_id,
        "create_instance": assignment.create_instance,
        "enabled": assignment.enabled,
        "created_strategy_instance_id": assignment.created_strategy_instance_id,
        "created_at": assignment.created_at,
        "updated_at": assignment.updated_at,
    }


def _rebalance_row(rebalance):
    if not rebalance:
        return None
    return {
        "id": rebalance.pk,
        "mode": rebalance.mode,
        "run_type": rebalance.run_type,
        "status": rebalance.status,
        "phase": rebalance.phase,
        "planned_turnover": rebalance.planned_turnover,
    }


def _run_row(run, detail=False):
    row = {
        "id": run.pk,
        "plan_id": run.plan_id,
        "portfolio_id": run.plan.portfolio_id,
        "status": run.status,
        "application_status": run.application_status,
        "retryable": run.retryable,
        "last_error": run.last_error,
        "attempt_count": run.attempt_count,
        "nav": run.nav,
        "final_target_weights": run.final_target_weights,
        "metrics": run.metrics,
        "warnings": run.warnings,
        "applied_rebalance": _rebalance_row(run.applied_rebalance) if run.applied_rebalance_id else None,
        "applied_at": run.applied_at,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
    }
    if detail:
        row.update({
            "plan_snapshot": run.plan_snapshot,
            "instrument_snapshot": run.instrument_snapshot,
            "assignment_snapshot": run.assignment_snapshot,
            "goals": run.goal_results,
            "policy_snapshot": run.policy_snapshot,
            "targets": [
                {
                    "id": target.pk,
                    "instrument_id": target.instrument_id,
                    "symbol": target.instrument.symbol,
                    "current_weight": target.current_weight,
                    "target_weight": target.target_weight,
                    "weight_change": target.target_weight - target.current_weight,
                    "target_value": target.target_value,
                    "expected_return_contribution": target.expected_return_contribution,
                    "risk_contribution": target.risk_contribution,
                    "goal_contributions": target.goal_contributions,
                    "shared_across_goals": len(target.goal_contributions) > 1,
                    "rank": target.rank,
                }
                for target in run.targets.select_related("instrument").order_by("rank")
            ],
            "strategy_instances": run.metrics.get("strategy_instances", []),
        })
        previews = run.rebalances.all()
        if run.applied_rebalance_id:
            previews = previews.exclude(pk=run.applied_rebalance_id)
        preview = previews.order_by("-created_at").first()
        row["rebalance"] = _rebalance_row(preview)
        row["planned_trades"] = []
        if preview:
            row["planned_trades"] = [
                {
                    "instrument_id": target.instrument_id,
                    "symbol": target.instrument.symbol,
                    "current_weight": target.current_weight,
                    "target_weight": target.target_weight,
                    "side": "BUY" if target.trade_quantity > 0 else "SELL" if target.trade_quantity < 0 else "NONE",
                    "quantity": abs(target.trade_quantity),
                    "reference_price": target.reference_price,
                    "estimated_cost": target.estimated_cost,
                    "suppressed": target.suppressed,
                    "suppression_reason": target.suppression_reason,
                }
                for target in preview.targets.select_related("instrument").order_by("rank")
            ]
    return row


def _recommendation_batch_row(batch, detail=True):
    row = {
        "id": batch.pk, "plan_id": batch.plan_id, "requested_plan_version": batch.requested_plan_version,
        "status": batch.status, "dataset_version_id": batch.dataset_id, "protocol_version_id": batch.protocol_id,
        "metrics": batch.metrics, "error": batch.error, "created_at": batch.created_at,
        "started_at": batch.started_at, "completed_at": batch.completed_at,
    }
    if detail:
        snapshots = {int(item["goal_id"]): item for item in (batch.input_snapshot or [])}
        row["goals"] = [{
            "goal_id": item.goal_id or (item.summary or {}).get("goal_id"),
            "goal_name": item.goal.name if item.goal else (
                (item.summary or {}).get("goal_name")
                or snapshots.get(int((item.summary or {}).get("goal_id") or 0), {}).get("name", "Deleted goal")
            ),
            "status": item.status,
            "recommendation_run_id": item.recommendation_run_id, "fallback_tier": item.fallback_tier,
            **(item.summary or {}),
        } for item in batch.goal_results.select_related("goal").order_by("goal__display_order", "goal_id")]
    return row


def plan_recommendations(request, plan_id):
    if request.method != "POST":
        return response(status=405, error={"code": "METHOD_NOT_ALLOWED", "message": "POST required", "details": {}})
    key = request.headers.get("Idempotency-Key")
    if not key:
        return response(status=400, error={"code": "IDEMPOTENCY_KEY_REQUIRED", "message": "Idempotency-Key header is required", "details": {}})
    throttled = throttle_response(request, "plan_recommendations", limit=settings.OPTIMIZATION_THROTTLE_LIMIT,
                                  window_seconds=settings.EXPENSIVE_OPERATION_THROTTLE_WINDOW_SECONDS)
    if throttled:
        return throttled
    try:
        payload = _payload(request)
        if payload:
            raise ValueError("The request body must be empty; enabled goals are read from the locked plan")
        plan = PortfolioConstructionPlan.objects.select_related("portfolio__gateway_session").get(pk=plan_id)
        readiness = portfolio_builder_readiness(portfolio=plan.portfolio, plan=plan)
        if not readiness["ready"]:
            return response(status=503, error={
                "code": "PORTFOLIO_BUILDER_NOT_READY",
                "message": "Portfolio Builder prerequisites are not ready",
                "details": readiness,
            })
        batch, created = create_recommendation_batch(plan_id, key)
        if batch.status == "QUEUED":
            try:
                generate_recommendation_batch.delay(batch.pk)
            except Exception as exc:
                logger.exception("Could not dispatch recommendation batch %s", batch.pk)
                return response(status=503, error={
                    "code": "RECOMMENDATION_QUEUE_UNAVAILABLE",
                    "message": "Recommendation batch was created but the worker queue is unavailable",
                    "details": {
                        "batch_id": batch.pk,
                        "batch_status": batch.status,
                        "retryable": True,
                        "error": str(exc)[:500],
                    },
                })
        status = 202 if batch.status in {"QUEUED", "RUNNING"} else 200
        return response(_recommendation_batch_row(batch), status=status)
    except PortfolioConstructionPlan.DoesNotExist:
        return response(status=404, error={"code": "NOT_FOUND", "message": "Construction plan not found", "details": {}})
    except (ResearchDatasetVersion.DoesNotExist, BacktestProtocolVersion.DoesNotExist) as exc:
        return response(status=503, error={
            "code": "RECOMMENDATION_SYSTEM_NOT_READY",
            "message": "Portfolio Builder research dataset or protocol is not active",
            "details": {"retryable": False, "blocker": exc.__class__.__name__},
        })
    except (ValueError, json.JSONDecodeError) as exc:
        return response(status=409, error={"code": "RECOMMENDATION_BATCH_FAILED", "message": str(exc), "details": {}})
    except Exception:
        logger.exception("Unexpected recommendation batch failure for plan %s", plan_id)
        return response(status=500, error={
            "code": "RECOMMENDATION_BATCH_INTERNAL_ERROR",
            "message": "Recommendation generation failed unexpectedly. Retry or inspect the Backend logs.",
            "details": {},
        })


def recommendation_batches(request, batch_id):
    if request.method != "GET":
        return response(status=405, error={"code": "METHOD_NOT_ALLOWED", "message": "GET required", "details": {}})
    try:
        batch = RecommendationBatchRun.objects.select_related("plan").get(pk=batch_id)
        return response(_recommendation_batch_row(batch))
    except RecommendationBatchRun.DoesNotExist:
        return response(status=404, error={"code": "NOT_FOUND", "message": "Recommendation batch not found", "details": {}})


def builder_readiness(request):
    if request.method != "GET":
        return response(status=405, error={"code": "METHOD_NOT_ALLOWED", "message": "GET required", "details": {}})
    try:
        portfolio_id = request.GET.get("portfolio")
        if not portfolio_id:
            raise ValueError("portfolio query parameter is required")
        portfolio = TradingPortfolio.objects.select_related("gateway_session").get(pk=portfolio_id)
        plan = None
        if request.GET.get("plan"):
            plan = PortfolioConstructionPlan.objects.get(pk=request.GET["plan"], portfolio=portfolio)
        return response(portfolio_builder_readiness(portfolio=portfolio, plan=plan))
    except (ValueError, TradingPortfolio.DoesNotExist, PortfolioConstructionPlan.DoesNotExist) as exc:
        status = 404 if isinstance(exc, (TradingPortfolio.DoesNotExist, PortfolioConstructionPlan.DoesNotExist)) else 400
        return response(status=status, error={"code": "INVALID_BUILDER_PREFLIGHT", "message": str(exc), "details": {}})


def plans(request, plan_id=None):
    if request.method == "GET":
        if plan_id:
            try:
                return response(_plan_row(PortfolioConstructionPlan.objects.get(pk=plan_id)))
            except PortfolioConstructionPlan.DoesNotExist:
                return response(status=404, error={"code": "NOT_FOUND", "message": "Construction plan not found", "details": {}})
        query = PortfolioConstructionPlan.objects.all().order_by("portfolio_id")
        if request.GET.get("portfolio"):
            query = query.filter(portfolio_id=request.GET["portfolio"])
        return response([_plan_row(item) for item in query])
    if request.method not in {"POST", "PATCH"}:
        return response(status=405, error={"code": "METHOD_NOT_ALLOWED", "message": "GET, POST, or PATCH required", "details": {}})
    try:
        payload = _payload(request)
        if request.method == "POST":
            unknown = set(payload) - {"portfolio_id", "name", "status"}
            if unknown:
                raise ValueError(f"Unsupported plan fields: {', '.join(sorted(unknown))}")
            require_fields(payload, "portfolio_id")
            portfolio = TradingPortfolio.objects.get(pk=payload["portfolio_id"])
            requested_status = str(payload.get("status") or "DRAFT").upper()
            if requested_status not in {item[0] for item in PortfolioConstructionPlan.STATUSES}:
                raise ValueError("Plan status must be DRAFT, ACTIVE, or PAUSED")
            with transaction.atomic():
                plan, created = PortfolioConstructionPlan.objects.get_or_create(
                    portfolio=portfolio,
                    defaults={
                        "name": str(payload.get("name") or "Portfolio Builder")[:128],
                        "status": requested_status,
                    },
                )
                if not created:
                    for field in ("name", "status"):
                        if field in payload:
                            setattr(plan, field, str(payload[field]).upper() if field == "status" else str(payload[field])[:128])
                    if payload.keys() - {"portfolio_id"}:
                        plan.version += 1
                        plan.save()
            event = "portfolio.construction_plan.created" if created else "portfolio.construction_plan.changed"
        else:
            unknown = set(payload) - {"name", "status"}
            if unknown:
                raise ValueError(f"Unsupported plan fields: {', '.join(sorted(unknown))}")
            with transaction.atomic():
                plan = PortfolioConstructionPlan.objects.select_for_update().get(pk=plan_id)
                if "name" in payload:
                    plan.name = str(payload["name"])[:128]
                if "status" in payload:
                    plan.status = str(payload["status"]).upper()
                plan.version += 1
                plan.full_clean()
                plan.save()
            created = False
            event = "portfolio.construction_plan.changed"
        _audit(request, event, plan.portfolio_id, {"plan_id": plan.pk, "version": plan.version})
        return response(_plan_row(plan), status=201 if created else 200)
    except (KeyError, ValueError, TypeError, json.JSONDecodeError, TradingPortfolio.DoesNotExist,
            PortfolioConstructionPlan.DoesNotExist, DjangoValidationError) as exc:
        return response(status=400, error={"code": "INVALID_CONSTRUCTION_PLAN", "message": str(exc), "details": {}})


def _goal_values(payload, goal=None):
    allowed = {"name", "allocation_weight", "allocation_percentage", "timeframe_bucket", "risk_level", "enabled", "display_order"}
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(f"Unsupported goal fields: {', '.join(sorted(unknown))}")
    if "allocation_weight" in payload and "allocation_percentage" in payload:
        raise ValueError("Use allocation_weight or allocation_percentage, not both")
    values = {}
    if "name" in payload:
        values["name"] = str(payload["name"]).strip()[:128]
        if not values["name"]:
            raise ValueError("Goal name is required")
    if "allocation_weight" in payload:
        values["allocation_weight"] = decimal_field(payload, "allocation_weight", required=True, positive=True)
    elif "allocation_percentage" in payload:
        values["allocation_weight"] = decimal_field(payload, "allocation_percentage", required=True, positive=True) / Decimal(100)
    if "timeframe_bucket" in payload:
        values["timeframe_bucket"] = str(payload["timeframe_bucket"]).upper()
    if "risk_level" in payload:
        values["risk_level"] = int(payload["risk_level"])
    if "enabled" in payload:
        if not isinstance(payload["enabled"], bool):
            raise ValueError("enabled must be a boolean")
        values["enabled"] = payload["enabled"]
    if "display_order" in payload:
        values["display_order"] = int(payload["display_order"])
    if goal is None:
        for field in ("name", "allocation_weight", "timeframe_bucket", "risk_level"):
            if field not in values:
                raise ValueError(f"{field} is required")
    return values


def plan_goals(request, plan_id):
    if request.method != "POST":
        return response(status=405, error={"code": "METHOD_NOT_ALLOWED", "message": "POST required", "details": {}})
    try:
        payload = _payload(request)
        values = _goal_values(payload)
        with transaction.atomic():
            plan = PortfolioConstructionPlan.objects.select_for_update().get(pk=plan_id)
            if values.get("enabled", True) and plan.goals.filter(enabled=True).count() >= 10:
                raise ValueError("At most ten goals may be enabled")
            goal = PortfolioGoalAllocation(plan=plan, **values)
            goal.full_clean()
            goal.save()
            bump_plan_version(plan)
        _audit(request, "portfolio.construction_goal.created", plan.portfolio_id, {"plan_id": plan.pk, "goal_id": goal.pk})
        return response(_goal_row(goal), status=201)
    except (ValueError, TypeError, InvalidOperation, json.JSONDecodeError, DjangoValidationError,
            PortfolioConstructionPlan.DoesNotExist) as exc:
        return response(status=400, error={"code": "INVALID_CONSTRUCTION_GOAL", "message": str(exc), "details": {}})


def goal_detail(request, goal_id):
    if request.method not in {"PATCH", "DELETE"}:
        return response(status=405, error={"code": "METHOD_NOT_ALLOWED", "message": "PATCH or DELETE required", "details": {}})
    try:
        with transaction.atomic():
            goal = PortfolioGoalAllocation.objects.select_for_update().select_related("plan").get(pk=goal_id)
            plan = PortfolioConstructionPlan.objects.select_for_update().get(pk=goal.plan_id)
            if request.method == "DELETE":
                result = {"id": goal.pk, "name": goal.name, "plan_id": plan.pk}
                goal.delete()
                bump_plan_version(plan)
                event = "portfolio.construction_goal.deleted"
            else:
                values = _goal_values(_payload(request), goal)
                if goal.construction_source == "ACCEPTED_RECOMMENDATION":
                    goal.construction_source = "MANUAL_OPTIMIZER"
                    goal.accepted_recommendation_run = None
                enabling = values.get("enabled", goal.enabled)
                if enabling and not goal.enabled and plan.goals.filter(enabled=True).exclude(pk=goal.pk).count() >= 10:
                    raise ValueError("At most ten goals may be enabled")
                for field, value in values.items():
                    setattr(goal, field, value)
                goal.full_clean()
                goal.save()
                bump_plan_version(plan)
                result = _goal_row(goal)
                event = "portfolio.construction_goal.changed"
        _audit(request, event, plan.portfolio_id, {"plan_id": plan.pk, "goal_id": goal_id})
        return response(result)
    except (ValueError, TypeError, InvalidOperation, json.JSONDecodeError, DjangoValidationError,
            PortfolioGoalAllocation.DoesNotExist) as exc:
        return response(status=400, error={"code": "INVALID_CONSTRUCTION_GOAL", "message": str(exc), "details": {}})


def goal_eligible_strategies(request, goal_id):
    if request.method != "GET":
        return response(status=405, error={"code": "METHOD_NOT_ALLOWED", "message": "GET required", "details": {}})
    try:
        return response(eligible_strategies(PortfolioGoalAllocation.objects.get(pk=goal_id)))
    except PortfolioGoalAllocation.DoesNotExist:
        return response(status=404, error={"code": "NOT_FOUND", "message": "Goal not found", "details": {}})


def goal_instruments(request, goal_id):
    try:
        goal = PortfolioGoalAllocation.objects.select_related("plan").get(pk=goal_id)
    except PortfolioGoalAllocation.DoesNotExist:
        return response(status=404, error={"code": "NOT_FOUND", "message": "Goal not found", "details": {}})
    if request.method == "GET":
        return response([
            _instrument_row(item)
            for item in goal.instrument_selections.select_related("instrument").order_by("display_order", "pk")
        ])
    if request.method != "POST":
        return response(status=405, error={"code": "METHOD_NOT_ALLOWED", "message": "GET or POST required", "details": {}})
    try:
        from apps.research.services.acceptance import require_manual_edit_allowed

        require_manual_edit_allowed(goal)
        payload = _payload(request)
        allowed = {"instrument_id", "enabled", "minimum_weight", "maximum_weight", "display_order"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(f"Unsupported instrument fields: {', '.join(sorted(unknown))}")
        require_fields(payload, "instrument_id")
        instrument = Instrument.objects.get(pk=payload["instrument_id"])
        minimum, maximum = validate_instrument_selection(
            instrument=instrument,
            minimum_weight=payload.get("minimum_weight"),
            maximum_weight=payload.get("maximum_weight"),
        )
        enabled = payload.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        with transaction.atomic():
            selection = GoalInstrumentSelection(
                goal_allocation=goal,
                instrument=instrument,
                enabled=enabled,
                minimum_weight=minimum,
                maximum_weight=maximum,
                display_order=int(payload.get("display_order", goal.instrument_selections.count())),
            )
            selection.full_clean()
            selection.save()
            bump_plan_version(goal.plan)
        _audit(request, "portfolio.construction_instrument.created", goal.plan.portfolio_id, {
            "plan_id": goal.plan_id, "goal_id": goal.pk, "goal_instrument_id": selection.pk,
        })
        return response(_instrument_row(selection), status=201)
    except (ValueError, TypeError, json.JSONDecodeError, IntegrityError, ConstructionError,
            DjangoValidationError, Instrument.DoesNotExist) as exc:
        return response(status=400, error={"code": "INVALID_CONSTRUCTION_INSTRUMENT", "message": str(exc), "details": {}})


def instrument_detail(request, goal_instrument_id):
    if request.method not in {"PATCH", "DELETE"}:
        return response(status=405, error={"code": "METHOD_NOT_ALLOWED", "message": "PATCH or DELETE required", "details": {}})
    try:
        with transaction.atomic():
            selection = GoalInstrumentSelection.objects.select_related(
                "goal_allocation__plan", "instrument",
            ).get(pk=goal_instrument_id)
            from apps.research.services.acceptance import require_manual_edit_allowed

            require_manual_edit_allowed(selection.goal_allocation)
            plan = PortfolioConstructionPlan.objects.select_for_update().get(pk=selection.goal_allocation.plan_id)
            if request.method == "DELETE":
                result = {"id": selection.pk, "goal_id": selection.goal_allocation_id}
                selection.delete()
                event = "portfolio.construction_instrument.deleted"
            else:
                payload = _payload(request)
                allowed = {"enabled", "minimum_weight", "maximum_weight", "display_order"}
                unknown = set(payload) - allowed
                if unknown:
                    raise ValueError(f"Unsupported instrument fields: {', '.join(sorted(unknown))}")
                if "enabled" in payload:
                    if not isinstance(payload["enabled"], bool):
                        raise ValueError("enabled must be a boolean")
                    selection.enabled = payload["enabled"]
                if "display_order" in payload:
                    selection.display_order = int(payload["display_order"])
                minimum, maximum = validate_instrument_selection(
                    instrument=selection.instrument,
                    minimum_weight=payload.get("minimum_weight", selection.minimum_weight),
                    maximum_weight=payload.get("maximum_weight", selection.maximum_weight),
                )
                if "minimum_weight" in payload:
                    selection.minimum_weight = minimum
                if "maximum_weight" in payload:
                    selection.maximum_weight = maximum
                selection.full_clean()
                selection.save()
                result = _instrument_row(selection)
                event = "portfolio.construction_instrument.changed"
            bump_plan_version(plan)
        _audit(request, event, plan.portfolio_id, {
            "plan_id": plan.pk, "goal_instrument_id": goal_instrument_id,
        })
        return response(result)
    except GoalInstrumentSelection.DoesNotExist:
        return response(status=404, error={"code": "NOT_FOUND", "message": "Selection not found", "details": {}})
    except (ValueError, TypeError, json.JSONDecodeError, ConstructionError, DjangoValidationError) as exc:
        return response(status=400, error={"code": "INVALID_CONSTRUCTION_INSTRUMENT", "message": str(exc), "details": {}})


def _assignment_values(payload, selection, assignment=None):
    allowed = {
        "strategy_definition_id", "strategy_key", "execution_timeframe", "parameter_overrides",
        "strategy_share", "risk_policy_id", "order_policy_id", "create_instance", "enabled",
    }
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(f"Unsupported assignment fields: {', '.join(sorted(unknown))}")
    if assignment is None and bool(payload.get("strategy_definition_id")) == bool(payload.get("strategy_key")):
        raise ValueError("Provide exactly one of strategy_definition_id or strategy_key")
    definition = assignment.strategy_definition if assignment else (
        StrategyDefinition.objects.get(pk=payload["strategy_definition_id"])
        if payload.get("strategy_definition_id") else
        StrategyDefinition.objects.get(key=str(payload["strategy_key"]).upper())
    )
    if "strategy_definition_id" in payload or "strategy_key" in payload:
        if bool(payload.get("strategy_definition_id")) == bool(payload.get("strategy_key")):
            raise ValueError("Provide exactly one of strategy_definition_id or strategy_key")
        definition = (
            StrategyDefinition.objects.get(pk=payload["strategy_definition_id"])
            if payload.get("strategy_definition_id") else
            StrategyDefinition.objects.get(key=str(payload["strategy_key"]).upper())
        )
    timeframe = str(payload.get("execution_timeframe", assignment.execution_timeframe if assignment else ""))
    if not timeframe:
        raise ValueError("execution_timeframe is required")
    parameters = payload.get("parameter_overrides", assignment.parameter_overrides if assignment else {})
    existing_owners = selection.assignments.filter(enabled=True, create_instance=True)
    if assignment:
        existing_owners = existing_owners.exclude(pk=assignment.pk)
    if "strategy_share" in payload:
        share = decimal_field(payload, "strategy_share", required=True, positive=True)
    elif assignment:
        share = assignment.strategy_share
    elif not existing_owners.exists():
        share = Decimal(1)
    else:
        raise ValueError("strategy_share is required when a stock has multiple assignments")
    risk_policy = assignment.risk_policy if assignment else None
    if "risk_policy_id" in payload:
        risk_policy = StrategyRiskPolicy.objects.get(pk=payload["risk_policy_id"]) if payload["risk_policy_id"] else None
    order_policy = assignment.order_policy if assignment else None
    if "order_policy_id" in payload:
        order_policy = OrderPolicy.objects.get(pk=payload["order_policy_id"]) if payload["order_policy_id"] else None
    create_instance = payload.get("create_instance", assignment.create_instance if assignment else True)
    enabled = payload.get("enabled", assignment.enabled if assignment else True)
    if not isinstance(create_instance, bool) or not isinstance(enabled, bool):
        raise ValueError("create_instance and enabled must be booleans")
    parameters = validate_assignment(
        goal_instrument_selection=selection,
        definition=definition,
        execution_timeframe=timeframe,
        parameter_overrides=parameters,
        strategy_share=share,
        risk_policy=risk_policy,
        order_policy=order_policy,
    )
    return {
        "strategy_definition": definition,
        "execution_timeframe": timeframe,
        "parameter_overrides": parameters,
        "parameter_hash": canonical_request_hash("parameters", parameters),
        "strategy_share": share,
        "risk_policy": risk_policy,
        "order_policy": order_policy,
        "create_instance": create_instance,
        "enabled": enabled,
    }


def instrument_assignments(request, goal_instrument_id):
    try:
        selection = GoalInstrumentSelection.objects.select_related(
            "goal_allocation__plan", "instrument",
        ).get(pk=goal_instrument_id)
    except GoalInstrumentSelection.DoesNotExist:
        return response(status=404, error={"code": "NOT_FOUND", "message": "Goal instrument not found", "details": {}})
    if request.method == "GET":
        return response([
            _assignment_row(item) for item in selection.assignments.select_related(
                "strategy_definition", "goal_instrument_selection__goal_allocation",
                "goal_instrument_selection__instrument",
            ).order_by("pk")
        ])
    if request.method != "POST":
        return response(status=405, error={"code": "METHOD_NOT_ALLOWED", "message": "GET or POST required", "details": {}})
    try:
        from apps.research.services.acceptance import require_manual_edit_allowed

        require_manual_edit_allowed(selection.goal_allocation)
        values = _assignment_values(_payload(request), selection)
        with transaction.atomic():
            assignment = GoalStrategyAssignment.objects.create(
                goal_instrument_selection=selection, **values,
            )
            bump_plan_version(selection.goal_allocation.plan)
        _audit(request, "portfolio.construction_assignment.created", selection.goal_allocation.plan.portfolio_id, {
            "plan_id": selection.goal_allocation.plan_id,
            "goal_instrument_id": selection.pk,
            "assignment_id": assignment.pk,
        })
        return response(_assignment_row(assignment), status=201)
    except (ValueError, TypeError, json.JSONDecodeError, IntegrityError, ConstructionError,
            StrategyDefinition.DoesNotExist, StrategyRiskPolicy.DoesNotExist, OrderPolicy.DoesNotExist) as exc:
        return response(status=400, error={"code": "INVALID_CONSTRUCTION_ASSIGNMENT", "message": str(exc), "details": {}})


def assignment_detail(request, assignment_id):
    if request.method not in {"PATCH", "DELETE"}:
        return response(status=405, error={"code": "METHOD_NOT_ALLOWED", "message": "PATCH or DELETE required", "details": {}})
    try:
        with transaction.atomic():
            assignment = GoalStrategyAssignment.objects.select_related(
                "goal_instrument_selection__goal_allocation__plan",
                "goal_instrument_selection__instrument", "strategy_definition", "risk_policy", "order_policy",
            ).get(pk=assignment_id)
            selection = assignment.goal_instrument_selection
            from apps.research.services.acceptance import require_manual_edit_allowed

            require_manual_edit_allowed(selection.goal_allocation)
            plan = PortfolioConstructionPlan.objects.select_for_update().get(pk=selection.goal_allocation.plan_id)
            if request.method == "DELETE":
                result = {"id": assignment.pk, "goal_instrument_id": selection.pk}
                assignment.delete()
                event = "portfolio.construction_assignment.deleted"
            else:
                values = _assignment_values(_payload(request), selection, assignment)
                for field, value in values.items():
                    setattr(assignment, field, value)
                assignment.full_clean()
                assignment.save()
                result = _assignment_row(assignment)
                event = "portfolio.construction_assignment.changed"
            bump_plan_version(plan)
        _audit(request, event, plan.portfolio_id, {"plan_id": plan.pk, "assignment_id": assignment_id})
        return response(result)
    except GoalStrategyAssignment.DoesNotExist:
        return response(status=404, error={"code": "NOT_FOUND", "message": "Assignment not found", "details": {}})
    except (ValueError, TypeError, json.JSONDecodeError, IntegrityError, ConstructionError,
            DjangoValidationError, StrategyDefinition.DoesNotExist, StrategyRiskPolicy.DoesNotExist,
            OrderPolicy.DoesNotExist) as exc:
        return response(status=400, error={"code": "INVALID_CONSTRUCTION_ASSIGNMENT", "message": str(exc), "details": {}})


def preview(request):
    if request.method != "POST":
        return response(status=405, error={"code": "METHOD_NOT_ALLOWED", "message": "POST required", "details": {}})
    key = request.headers.get("Idempotency-Key")
    if not key:
        return response(status=400, error={"code": "IDEMPOTENCY_KEY_REQUIRED", "message": "Idempotency-Key header is required", "details": {}})
    throttled = throttle_response(
        request,
        "portfolio_construction",
        limit=settings.OPTIMIZATION_THROTTLE_LIMIT,
        window_seconds=settings.EXPENSIVE_OPERATION_THROTTLE_WINDOW_SECONDS,
    )
    if throttled:
        return throttled
    try:
        payload = _payload(request)
        unknown = set(payload) - {"plan_id", "nav", "refresh_history"}
        if unknown:
            raise ValueError(f"Unsupported preview fields: {', '.join(sorted(unknown))}")
        require_fields(payload, "plan_id")
        if "refresh_history" in payload and not isinstance(payload["refresh_history"], bool):
            raise ValueError("refresh_history must be a boolean")
        nav = decimal_field(payload, "nav", positive=True, allow_zero=False)
        plan = PortfolioConstructionPlan.objects.select_related("portfolio__account").get(pk=payload["plan_id"])
        refresh_history = bool(payload.get("refresh_history", True))
        run = create_construction_run(
            plan,
            key,
            nav=nav,
            refresh_history=refresh_history,
            retry_failed=request.headers.get("Idempotency-Retry", "").strip().lower() in {"1", "true", "yes"},
            defer=True,
        )
        from .tasks import execute_construction_run

        transaction.on_commit(lambda: execute_construction_run.delay(run.pk, refresh_history, True))
        _audit(request, "portfolio.construction.requested", plan.portfolio_id, {
            "plan_id": plan.pk, "construction_run_id": run.pk, "plan_version": plan.version,
        }, f"audit:construction-preview:{key}")
        return response(_run_row(run, True), status=202)
    except (ValueError, InvalidOperation, json.JSONDecodeError, ConstructionError,
            PortfolioConstructionPlan.DoesNotExist) as exc:
        return response(status=400, error={"code": "CONSTRUCTION_PREVIEW_FAILED", "message": str(exc), "details": {}})


def apply(request, run_id):
    if request.method != "POST":
        return response(status=405, error={"code": "METHOD_NOT_ALLOWED", "message": "POST required", "details": {}})
    key = request.headers.get("Idempotency-Key")
    if not key:
        return response(status=400, error={"code": "IDEMPOTENCY_KEY_REQUIRED", "message": "Idempotency-Key header is required", "details": {}})
    try:
        payload = _payload(request)
        unknown = set(payload) - {"plan_id", "portfolio_id"}
        if unknown:
            raise ValueError(f"Unsupported apply fields: {', '.join(sorted(unknown))}")
        run = PortfolioConstructionRun.objects.select_related("plan__portfolio", "applied_rebalance").get(pk=run_id)
        if "plan_id" in payload and int(payload["plan_id"]) != run.plan_id:
            raise ConstructionError("Construction run and plan do not belong together")
        if "portfolio_id" in payload and int(payload["portfolio_id"]) != run.plan.portfolio_id:
            raise ConstructionError("Construction run and portfolio do not belong together")
        if run.status != "COMPLETED":
            raise ConstructionError("Only a completed construction run can be applied")
        if run.applied_rebalance_id and run.application_idempotency_key != key:
            raise ConstructionAlreadyApplied(run)
        if run.application_status in {"QUEUED", "APPLYING"} and run.application_idempotency_key != key:
            return response(status=409, error={
                "code": "IDEMPOTENCY_CONFLICT",
                "message": "This construction application is already in progress under another Idempotency-Key",
                "details": {},
            })
        if run.application_status == "FAILED":
            retry = request.headers.get("Idempotency-Retry", "").strip().lower() in {"1", "true", "yes"}
            if run.application_idempotency_key != key or not retry or not run.retryable:
                return response(status=409, error={
                    "code": "RETRY_NOT_ALLOWED",
                    "message": run.last_error or "Failed construction application requires an explicit retry",
                    "details": {"retryable": run.retryable},
                })
        mode = require_portfolio_execution_ready(run.plan.portfolio)
        if not run.applied_rebalance_id and run.application_status not in {"QUEUED", "APPLYING"}:
            with transaction.atomic():
                locked = PortfolioConstructionRun.objects.select_for_update().get(pk=run.pk)
                locked.application_status = "QUEUED"
                locked.application_idempotency_key = key
                locked.retryable = False
                locked.last_error = ""
                locked.save(update_fields=["application_status", "application_idempotency_key", "retryable", "last_error"])
                run = locked
            from .tasks import apply_construction_run_task

            transaction.on_commit(lambda: apply_construction_run_task.delay(run.pk, key, mode))
        _audit(request, "portfolio.construction.application_requested", run.plan.portfolio_id, {
            "construction_run_id": run.pk, "mode": mode,
        }, f"audit:construction-apply-request:{key}")
        run.refresh_from_db()
        return response(_run_row(run, True), status=200 if run.applied_rebalance_id else 202)
    except ConstructionAlreadyApplied as exc:
        return response(status=409, error={
            "code": exc.code,
            "message": str(exc),
            "details": {"applied_rebalance_id": exc.construction_run.applied_rebalance_id},
        })
    except (ValueError, json.JSONDecodeError, ConstructionError, PortfolioConstructionRun.DoesNotExist) as exc:
        return response(status=400, error={"code": "CONSTRUCTION_APPLY_FAILED", "message": str(exc), "details": {}})


def runs(request, run_id=None):
    if request.method != "GET":
        return response(status=405, error={"code": "METHOD_NOT_ALLOWED", "message": "GET required", "details": {}})
    if run_id:
        try:
            return response(_run_row(
                PortfolioConstructionRun.objects.select_related("plan__portfolio", "applied_rebalance").get(pk=run_id),
                True,
            ))
        except PortfolioConstructionRun.DoesNotExist:
            return response(status=404, error={"code": "NOT_FOUND", "message": "Construction run not found", "details": {}})
    query = PortfolioConstructionRun.objects.select_related("plan__portfolio", "applied_rebalance").order_by("-created_at")
    if request.GET.get("portfolio"):
        query = query.filter(plan__portfolio_id=request.GET["portfolio"])
    if request.GET.get("plan"):
        query = query.filter(plan_id=request.GET["plan"])
    return response([_run_row(item) for item in query[:100]])
