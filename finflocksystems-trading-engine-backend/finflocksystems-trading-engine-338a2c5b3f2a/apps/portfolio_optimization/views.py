import json
import uuid
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction

from apps.audit.models import AuditEvent
from apps.core.throttling import throttle_response
from apps.core.validation import decimal_field, require_fields
from apps.core.views import response
from apps.execution.modes import (
    execution_mode_for_portfolio,
    require_portfolio_execution_ready,
)
from apps.instruments.models import Instrument
from apps.portfolios.models import TradingPortfolio

from .models import (
    PortfolioOptimizationPolicy,
    PortfolioOptimizationRun,
    PortfolioUniverse,
    PortfolioUniverseInstrument,
)
from .services import (
    OptimizationAlreadyApplied,
    OptimizationError,
    UniverseSizeError,
    run_optimization,
)


POLICY_FIELDS = [
    "name", "method", "lookback_days", "return_estimation", "covariance_estimation", "risk_free_rate",
    "target_cash_weight", "minimum_weight", "maximum_weight", "maximum_turnover",
    "transaction_cost_penalty", "long_only", "enabled",
]


def _actor(request):
    user = getattr(request, "user", None)
    return user.get_username() if user and user.is_authenticated else "operator/system"


def _audit(request, event_type, aggregate_id, data, idempotency_key=None):
    AuditEvent.objects.get_or_create(
        idempotency_key=idempotency_key or f"audit:{event_type}:{uuid.uuid4()}",
        defaults={
            "event_type": event_type,
            "actor": _actor(request),
            "aggregate_type": "portfolio",
            "aggregate_id": str(aggregate_id),
            "data": data,
        },
    )


def _universe_row(universe):
    memberships = universe.memberships.select_related("instrument").order_by("instrument__symbol")
    return {
        "id": universe.pk,
        "portfolio_id": universe.portfolio_id,
        "name": universe.name,
        "include_strategy_instruments": universe.include_strategy_instruments,
        "minimum_history_observations": universe.minimum_history_observations,
        "maximum_instruments": universe.maximum_instruments,
        "selected_count": memberships.filter(enabled=True).count(),
        "enabled": universe.enabled,
        "instruments": [
            {"instrument_id": item.instrument_id, "symbol": item.instrument.symbol, "enabled": item.enabled}
            for item in memberships
        ],
        "updated_at": universe.updated_at,
    }


def _policy_row(policy):
    return {"id": policy.pk, "portfolio_id": policy.portfolio_id, **{field: getattr(policy, field) for field in POLICY_FIELDS},
            "execution_mode": policy.execution_mode, "version": policy.version, "updated_at": policy.updated_at}


def _target_row(target):
    return {
        "id": target.pk,
        "instrument_id": target.instrument_id,
        "symbol": target.instrument.symbol,
        "current_weight": target.current_weight,
        "optimized_weight": target.optimized_weight,
        "weight_change": target.weight_change,
        "target_value": target.target_value,
        "expected_return_contribution": target.expected_return_contribution,
        "risk_contribution": target.risk_contribution,
        "constraint_status": target.constraint_status,
        "rank": target.rank,
    }


def _run_row(run, detail=False):
    row = {
        "id": run.pk,
        "portfolio_id": run.portfolio_id,
        "policy_id": run.policy_id,
        "universe_id": run.universe_id,
        "trigger": run.trigger,
        "status": run.status,
        "input_start_date": run.input_start_date,
        "input_end_date": run.input_end_date,
        "nav": run.nav,
        "objective_value": run.objective_value,
        "expected_return": run.expected_return,
        "expected_volatility": run.expected_volatility,
        "sharpe_ratio": run.sharpe_ratio,
        "turnover": run.turnover,
        "cash_weight": run.cash_weight,
        "solver_status": run.solver_status,
        "warnings": run.warnings,
        "error_details": run.error_details,
        "flow_reference": run.flow_reference,
        "application_status": run.application_status,
        "applied_at": run.applied_at,
        "created_at": run.created_at,
        "completed_at": run.completed_at,
    }
    row["applied_rebalance"] = None
    if run.applied_rebalance_id:
        applied = run.applied_rebalance
        row["applied_rebalance"] = {
                "id": applied.pk,
                "mode": applied.mode,
                "run_type": applied.run_type,
            "status": applied.status,
            "phase": applied.phase,
            "planned_turnover": applied.planned_turnover,
        }
    if detail:
        row["policy_snapshot"] = run.policy_snapshot
        row["constraints"] = run.constraints_snapshot
        row["current_weights"] = run.current_weights
        row["targets"] = [_target_row(item) for item in run.targets.select_related("instrument").order_by("rank")]
        preview_query = run.rebalances.all()
        if run.applied_rebalance_id:
            preview_query = preview_query.exclude(pk=run.applied_rebalance_id)
        rebalance = preview_query.order_by("-created_at").first()
        row["rebalance"] = None
        row["planned_trades"] = []
        if rebalance:
            row["rebalance"] = {
                "id": rebalance.pk,
                "mode": rebalance.mode,
                "run_type": rebalance.run_type,
                "status": rebalance.status,
                "phase": rebalance.phase,
                "planned_turnover": rebalance.planned_turnover,
            }
            row["planned_trades"] = [
                {
                    "instrument_id": target.instrument_id,
                    "symbol": target.instrument.symbol,
                    "side": "BUY" if target.trade_quantity > 0 else "SELL" if target.trade_quantity < 0 else "NONE",
                    "quantity": abs(target.trade_quantity),
                    "reference_price": target.reference_price,
                    "estimated_cost": target.estimated_cost,
                    "suppressed": target.suppressed,
                    "suppression_reason": target.suppression_reason,
                }
                for target in rebalance.targets.select_related("instrument").order_by("rank")
            ]
    return row


def universes(request):
    if request.method == "GET":
        query = PortfolioUniverse.objects.all().select_related("portfolio")
        if request.GET.get("portfolio"):
            query = query.filter(portfolio_id=request.GET["portfolio"])
        return response([_universe_row(item) for item in query])
    if request.method != "POST":
        return response(status=405, error={"code": "METHOD_NOT_ALLOWED", "message": "GET or POST required", "details": {}})
    try:
        payload = json.loads(request.body or b"{}")
        if not isinstance(payload,dict):raise ValueError("Request body must be a JSON object")
        allowed={"portfolio_id","instrument_ids","maximum_instruments","minimum_history_observations","name",
            "include_strategy_instruments","enabled"}
        unknown=set(payload)-allowed
        if unknown:raise ValueError(f"Unsupported universe fields: {', '.join(sorted(unknown))}")
        require_fields(payload,"portfolio_id","instrument_ids")
        if not isinstance(payload["instrument_ids"],list):raise ValueError("instrument_ids must be a list")
        for field in ("include_strategy_instruments","enabled"):
            if field in payload and not isinstance(payload[field],bool):raise ValueError(f"{field} must be a boolean")
        portfolio = TradingPortfolio.objects.get(pk=payload["portfolio_id"])
        instrument_ids = list(dict.fromkeys(int(item) for item in payload.get("instrument_ids", [])))
        maximum_instruments = int(payload.get("maximum_instruments", 50))
        if len(instrument_ids) < 2:
            raise ValueError("Select at least two instruments")
        if len(instrument_ids) > maximum_instruments:
            raise UniverseSizeError(len(instrument_ids), maximum_instruments)
        instruments = list(Instrument.objects.filter(pk__in=instrument_ids, active=True, tradable=True, asset_class="STK"))
        if len(instruments) != len(instrument_ids):
            raise ValueError("Universe instruments must be active, tradable stocks")
        with transaction.atomic():
            universe, _ = PortfolioUniverse.objects.update_or_create(
                portfolio=portfolio,
                defaults={
                    "name": str(payload.get("name") or "Default universe")[:128],
                    "include_strategy_instruments": bool(payload.get("include_strategy_instruments", False)),
                    "minimum_history_observations": int(payload.get("minimum_history_observations", 60)),
                    "maximum_instruments": maximum_instruments,
                    "enabled": bool(payload.get("enabled", True)),
                },
            )
            if universe.minimum_history_observations < 20 or universe.maximum_instruments < 2:
                raise ValueError("Minimum history must be at least 20 observations and maximum instruments at least two")
            universe.memberships.exclude(instrument_id__in=instrument_ids).delete()
            for instrument in instruments:
                PortfolioUniverseInstrument.objects.update_or_create(
                    universe=universe, instrument=instrument, defaults={"enabled": True}
                )
        _audit(
            request,
            "portfolio.universe.changed",
            portfolio.pk,
            {
                "universe_id": universe.pk,
                "selected_count": len(instrument_ids),
                "maximum_instruments": universe.maximum_instruments,
            },
        )
        return response(_universe_row(universe), status=201)
    except UniverseSizeError as exc:
        return response(
            status=400,
            error={
                "code": exc.code,
                "message": str(exc),
                "details": {
                    "selected_count": exc.selected_count,
                    "maximum_instruments": exc.maximum_instruments,
                },
            },
        )
    except (KeyError, ValueError, TypeError, json.JSONDecodeError, TradingPortfolio.DoesNotExist) as exc:
        return response(status=400, error={"code": "INVALID_PORTFOLIO_UNIVERSE", "message": str(exc), "details": {}})


def policies(request):
    if request.method == "GET":
        query = PortfolioOptimizationPolicy.objects.all().select_related("portfolio")
        if request.GET.get("portfolio"):
            query = query.filter(portfolio_id=request.GET["portfolio"])
        return response([_policy_row(item) for item in query])
    if request.method != "POST":
        return response(status=405, error={"code": "METHOD_NOT_ALLOWED", "message": "GET or POST required", "details": {}})
    try:
        payload = json.loads(request.body or b"{}")
        if not isinstance(payload,dict):raise ValueError("Request body must be a JSON object")
        unknown=set(payload)-({"portfolio_id"}|set(POLICY_FIELDS))
        if unknown:raise ValueError(f"Unsupported optimization policy fields: {', '.join(sorted(unknown))}")
        require_fields(payload,"portfolio_id")
        for field in ("long_only","enabled"):
            if field in payload and not isinstance(payload[field],bool):raise ValueError(f"{field} must be a boolean")
        portfolio = TradingPortfolio.objects.get(pk=payload["portfolio_id"])
        values = {field: payload[field] for field in POLICY_FIELDS if field in payload}
        values["execution_mode"] = execution_mode_for_portfolio(
            portfolio,
        )
        if values.get("method", "MINIMUM_VARIANCE") not in {value for value, _ in PortfolioOptimizationPolicy.METHODS}:
            raise ValueError("Method must be MINIMUM_VARIANCE or MAXIMUM_SHARPE")
        policy = PortfolioOptimizationPolicy.objects.filter(portfolio=portfolio).first()
        defaults={"minimum_weight":policy.minimum_weight if policy else "0","maximum_weight":policy.maximum_weight if policy else "1",
            "target_cash_weight":policy.target_cash_weight if policy else "0.02","maximum_turnover":policy.maximum_turnover if policy else "1",
            "transaction_cost_penalty":policy.transaction_cost_penalty if policy else "0","risk_free_rate":policy.risk_free_rate if policy else "0"}
        precision={"minimum_weight":10,"maximum_weight":10,"target_cash_weight":10,"maximum_turnover":10,
            "transaction_cost_penalty":16,"risk_free_rate":12}
        checked={field:decimal_field({field:values.get(field,default)},field,required=True,
            max_digits=precision[field],decimal_places=8) for field,default in defaults.items()}
        for field,value in checked.items():
            if field in values:values[field]=value
        minimum=checked["minimum_weight"];maximum=checked["maximum_weight"];cash=checked["target_cash_weight"]
        turnover=checked["maximum_turnover"];transaction_penalty=checked["transaction_cost_penalty"]
        lookback_days = int(values.get("lookback_days", policy.lookback_days if policy else 252))
        long_only = bool(values.get("long_only", policy.long_only if policy else True))
        if minimum < 0 or maximum <= 0 or minimum > maximum:
            raise ValueError("Long-only minimum and maximum weights are invalid")
        if cash < 0 or cash >= 1:
            raise ValueError("Target cash weight must be at least zero and less than one")
        if turnover < 0:
            raise ValueError("Maximum turnover cannot be negative")
        if transaction_penalty < 0:
            raise ValueError("Transaction-cost penalty cannot be negative")
        if lookback_days < 30:
            raise ValueError("Lookback must be at least 30 days")
        if values.get("return_estimation",policy.return_estimation if policy else "HISTORICAL_MEAN")!="HISTORICAL_MEAN":
            raise ValueError("return_estimation must be HISTORICAL_MEAN")
        if values.get("covariance_estimation",policy.covariance_estimation if policy else "SAMPLE")!="SAMPLE":
            raise ValueError("covariance_estimation must be SAMPLE")
        if not long_only:
            raise ValueError("This release supports long-only optimization only")
        with transaction.atomic():
            if policy:
                for field, value in values.items():
                    setattr(policy, field, value)
                policy.version += 1
                policy.save()
            else:
                policy = PortfolioOptimizationPolicy.objects.create(portfolio=portfolio, **values)
        _audit(
            request,
            "portfolio.optimization_policy.changed",
            portfolio.pk,
            {"policy_id": policy.pk, "version": policy.version, "execution_mode": policy.execution_mode},
        )
        return response(_policy_row(policy), status=201)
    except (KeyError, ValueError, InvalidOperation, TypeError, json.JSONDecodeError, TradingPortfolio.DoesNotExist) as exc:
        return response(status=400, error={"code": "INVALID_OPTIMIZATION_POLICY", "message": str(exc), "details": {}})


def execute(request, preview=False):
    if request.method != "POST":
        return response(status=405, error={"code": "METHOD_NOT_ALLOWED", "message": "POST required", "details": {}})
    key = request.headers.get("Idempotency-Key")
    if not key:
        return response(status=400, error={"code": "IDEMPOTENCY_KEY_REQUIRED", "message": "Idempotency-Key header is required", "details": {}})
    throttled = throttle_response(
        request,
        "portfolio_optimization",
        limit=settings.OPTIMIZATION_THROTTLE_LIMIT,
        window_seconds=settings.EXPENSIVE_OPERATION_THROTTLE_WINDOW_SECONDS,
    )
    if throttled:
        return throttled
    try:
        payload = json.loads(request.body or b"{}")
        if not isinstance(payload,dict):raise ValueError("Request body must be a JSON object")
        allowed={"portfolio_id","policy_id","universe_id","trigger","nav","refresh_history"} if preview else {
            "optimization_run_id","portfolio_id","policy_id","universe_id"}
        unknown=set(payload)-allowed
        if unknown:raise ValueError(f"Unsupported optimization fields: {', '.join(sorted(unknown))}")
        require_fields(payload,"portfolio_id" if preview else "optimization_run_id")
        if "refresh_history" in payload and not isinstance(payload["refresh_history"],bool):raise ValueError("refresh_history must be a boolean")
        nav=decimal_field(payload,"nav",positive=True,allow_zero=False) if preview else None
        if preview:
            portfolio = TradingPortfolio.objects.select_related("account").get(pk=payload["portfolio_id"])
            run = run_optimization(
                portfolio,
                key,
                trigger=str(payload.get("trigger") or "PREVIEW").upper(),
                nav=nav,
                refresh_history=bool(payload.get("refresh_history", True)),
                retry_failed=request.headers.get("Idempotency-Retry","").strip().lower() in {"1","true","yes"},
                defer=True,
            )
            for field, actual in {"policy_id": run.policy_id, "universe_id": run.universe_id}.items():
                if field in payload and int(payload[field]) != actual:
                    raise OptimizationError(
                        "Selected portfolio, universe, policy, and optimization run do not belong together"
                    )
            from .tasks import execute_optimization_run
            refresh_history=bool(payload.get("refresh_history",True))
            transaction.on_commit(lambda:execute_optimization_run.delay(run.pk,refresh_history,None,True))
            _audit(
                request,
                "portfolio.optimization.requested",
                run.portfolio_id,
                {"optimization_run_id": run.pk, "universe_id": run.universe_id, "policy_id": run.policy_id},
                f"audit:optimization-preview:{key}",
            )
        else:
            run = PortfolioOptimizationRun.objects.select_related("portfolio__account","policy","universe","applied_rebalance").get(
                pk=payload["optimization_run_id"])
            for field, actual in {
                "portfolio_id": run.portfolio_id,
                "policy_id": run.policy_id,
                "universe_id": run.universe_id,
            }.items():
                if field in payload and int(payload[field]) != actual:
                    raise OptimizationError(
                        "Selected portfolio, universe, policy, and optimization run do not belong together"
                    )
            if run.status!="COMPLETED":raise OptimizationError("Only a completed optimization run can be applied")
            if run.applied_rebalance_id and run.application_idempotency_key!=key:raise OptimizationAlreadyApplied(run)
            if run.application_status in {"QUEUED","APPLYING"} and run.application_idempotency_key!=key:
                return response(status=409,error={"code":"IDEMPOTENCY_CONFLICT",
                    "message":"This optimization application is already in progress under another Idempotency-Key","details":{}})
            if run.application_status=="FAILED":
                if run.application_idempotency_key!=key:
                    return response(status=409,error={"code":"IDEMPOTENCY_CONFLICT",
                        "message":"Retry the failed optimization application with its original Idempotency-Key","details":{}})
                retry=request.headers.get("Idempotency-Retry","").strip().lower() in {"1","true","yes"}
                if not retry or not run.retryable:
                    return response(status=409,error={"code":"RETRY_NOT_ALLOWED",
                        "message":run.last_error or "Failed optimization application requires an explicit retry","details":{"retryable":run.retryable}})
            mode=require_portfolio_execution_ready(run.portfolio)
            if not run.applied_rebalance_id and run.application_status not in {"QUEUED","APPLYING"}:
                with transaction.atomic():
                    locked=PortfolioOptimizationRun.objects.select_for_update().get(pk=run.pk)
                    locked.application_status="QUEUED";locked.application_idempotency_key=key
                    locked.retryable=False;locked.last_error=""
                    locked.save(update_fields=["application_status","application_idempotency_key","retryable","last_error"]);run=locked
                from .tasks import apply_optimization_run_task
                transaction.on_commit(lambda:apply_optimization_run_task.delay(run.pk,key,mode))
            _audit(
                request,
                "portfolio.optimization.application_requested",
                run.portfolio_id,
                {"optimization_run_id": run.pk, "mode": mode},
                f"audit:optimization-apply:{key}",
            )
        run.refresh_from_db()
        return response(_run_row(run, True), status=200 if run.applied_rebalance_id else 202)
    except OptimizationAlreadyApplied as exc:
        return response(
            status=409,
            error={
                "code": exc.code,
                "message": str(exc),
                "details": {"applied_rebalance_id": exc.optimization_run.applied_rebalance_id},
            },
        )
    except UniverseSizeError as exc:
        return response(
            status=400,
            error={
                "code": exc.code,
                "message": str(exc),
                "details": {
                    "selected_count": exc.selected_count,
                    "maximum_instruments": exc.maximum_instruments,
                },
            },
        )
    except (KeyError, ValueError, InvalidOperation, OptimizationError, json.JSONDecodeError,
            TradingPortfolio.DoesNotExist, PortfolioOptimizationRun.DoesNotExist) as exc:
        return response(status=400, error={"code": "OPTIMIZATION_FAILED", "message": str(exc), "details": {}})


def runs(request, run_id=None):
    if request.method != "GET":
        return response(status=405,error={"code":"METHOD_NOT_ALLOWED","message":"GET required","details":{}})
    if run_id:
        try:
            return response(_run_row(PortfolioOptimizationRun.objects.get(pk=run_id), True))
        except PortfolioOptimizationRun.DoesNotExist:
            return response(status=404, error={"code": "NOT_FOUND", "message": "Optimization run not found", "details": {}})
    query = PortfolioOptimizationRun.objects.all().order_by("-created_at")
    if request.GET.get("portfolio"):
        query = query.filter(portfolio_id=request.GET["portfolio"])
    return response([_run_row(item) for item in query[:100]])
