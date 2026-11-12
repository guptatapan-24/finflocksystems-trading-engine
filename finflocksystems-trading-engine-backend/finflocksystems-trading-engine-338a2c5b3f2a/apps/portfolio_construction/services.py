from decimal import Decimal, ROUND_DOWN

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.audit.models import AuditEvent, OperationAttempt, OutboxEvent
from apps.core.idempotency import canonical_request_hash, require_matching_request
from apps.execution.modes import (
    RunType,
    execution_mode_for_portfolio,
    require_portfolio_execution_ready,
)
from apps.instruments.models import Instrument
from apps.market_data.models import InstrumentPriceHistory
from apps.portfolio_optimization.services import (
    OptimizationError,
    calculate_weighted_metrics,
    optimize_explicit_universe,
)
from apps.portfolios.models import PortfolioPosition
from apps.strategies.models import StrategyDefinition, StrategyInstance
from apps.strategies.plugins import get_plugin

from .models import (
    GoalInstrumentSelection,
    GoalStrategyAssignment,
    PortfolioConstructionPlan,
    PortfolioConstructionRun,
    PortfolioConstructionTarget,
    StrategyConstructionProfile,
)
from .rules import resolved_goal_rules, validate_timeframe_risk


D = Decimal
WEIGHT_QUANTUM = D("0.00000001")


class ConstructionError(ValueError):
    pass


class ConstructionAlreadyApplied(ConstructionError):
    code = "CONSTRUCTION_ALREADY_APPLIED"

    def __init__(self, construction_run):
        self.construction_run = construction_run
        super().__init__(
            f"Construction run {construction_run.pk} was already applied by rebalance "
            f"{construction_run.applied_rebalance_id}"
        )


def decimal_string(value):
    return format(D(str(value)), "f")


def bump_plan_version(plan):
    PortfolioConstructionPlan.objects.filter(pk=plan.pk).update(version=F("version") + 1, updated_at=timezone.now())
    plan.refresh_from_db(fields=["version", "updated_at"])
    return plan


def plan_validation(plan):
    goals = list(plan.goals.filter(enabled=True).order_by("display_order", "pk"))
    total = sum((D(goal.allocation_weight) for goal in goals), D(0))
    errors = []
    if not goals:
        errors.append({"code": "NO_ENABLED_GOALS", "message": "At least one goal must be enabled"})
    if len(goals) > 10:
        errors.append({"code": "TOO_MANY_GOALS", "message": "At most ten goals may be enabled"})
    if total != D(1):
        errors.append({
            "code": "ALLOCATION_TOTAL",
            "message": "Enabled goal allocations must total exactly 100%",
            "allocated_weight": decimal_string(total),
        })
    for goal in goals:
        try:
            validate_timeframe_risk(goal.timeframe_bucket, goal.risk_level)
        except ValueError as exc:
            errors.append({"code": "INVALID_GOAL_RULES", "goal_id": goal.pk, "message": str(exc)})
    return {
        "allocated_weight": total,
        "allocated_percentage": total * D(100),
        "enabled_goal_count": len(goals),
        "ready_to_preview": not errors,
        "validation_errors": errors,
    }


def require_plan_ready(plan):
    validation = plan_validation(plan)
    if validation["validation_errors"]:
        raise ConstructionError("; ".join(item["message"] for item in validation["validation_errors"]))
    return validation


def strategy_eligibility(definition, goal, *, system_generated=False):
    reasons = []
    profile = StrategyConstructionProfile.objects.filter(strategy_definition=definition).first()
    plugin = None
    try:
        plugin = get_plugin(definition)
    except Exception:
        reasons.append("Strategy implementation is unavailable")
    if not definition.enabled:
        reasons.append("Strategy is disabled")
    if not profile:
        reasons.append("No construction profile is configured")
    else:
        if not profile.construction_enabled:
            reasons.append("Strategy is disabled for portfolio construction")
        if not profile.user_selectable and not system_generated:
            reasons.append("Strategy is not user-selectable")
        if goal.timeframe_bucket not in profile.supported_goal_timeframes:
            reasons.append(f"Strategy does not support {goal.timeframe_bucket} goals")
        if not profile.minimum_risk <= goal.risk_level <= profile.maximum_risk:
            reasons.append(f"Strategy supports risk levels {profile.minimum_risk}-{profile.maximum_risk}")
    if goal.timeframe_bucket == "NOW":
        reasons.append("NOW goals are intentionally cash-only")
    if "STK" not in definition.supported_asset_types:
        reasons.append("Strategy does not support stocks")
    if "LONG" not in definition.supported_directions:
        reasons.append("Strategy cannot operate long-only")
    if not definition.supported_timeframes:
        reasons.append("Strategy has no supported market-data timeframe")
    return {
        "strategy_definition_id": definition.pk,
        "key": definition.key,
        "name": definition.name,
        "summary": profile.summary if profile else definition.description,
        "limitations": profile.limitations if profile else "",
        "execution_timeframes": definition.supported_timeframes,
        "default_parameters": plugin.default_parameters if plugin else {},
        "parameter_schema": definition.parameter_schema,
        "eligible": not reasons,
        "reason": "; ".join(reasons),
    }


def eligible_strategies(goal):
    rows = [strategy_eligibility(item, goal) for item in StrategyDefinition.objects.all().order_by("name")]
    return {
        "goal_id": goal.pk,
        "eligible": [item for item in rows if item["eligible"]],
        "rejected": [item for item in rows if not item["eligible"]],
    }


def validate_instrument_selection(*, instrument, minimum_weight=None, maximum_weight=None):
    if not instrument.active or not instrument.tradable or instrument.asset_class != "STK":
        raise ConstructionError("Instrument must be an active, tradable stock")
    minimum = None if minimum_weight is None else D(str(minimum_weight))
    maximum = None if maximum_weight is None else D(str(maximum_weight))
    if minimum is not None and not D(0) <= minimum <= D(1):
        raise ConstructionError("minimum_weight must be between 0 and 1")
    if maximum is not None and not D(0) <= maximum <= D(1):
        raise ConstructionError("maximum_weight must be between 0 and 1")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ConstructionError("minimum_weight must not exceed maximum_weight")
    return minimum, maximum


def validate_assignment(
    *, goal_instrument_selection, definition, execution_timeframe, parameter_overrides,
    strategy_share=1, risk_policy=None, order_policy=None, system_generated=False,
):
    goal = goal_instrument_selection.goal_allocation
    validate_instrument_selection(
        instrument=goal_instrument_selection.instrument,
        minimum_weight=goal_instrument_selection.minimum_weight,
        maximum_weight=goal_instrument_selection.maximum_weight,
    )
    eligibility = strategy_eligibility(definition, goal, system_generated=system_generated)
    if not eligibility["eligible"]:
        raise ConstructionError(eligibility["reason"])
    if execution_timeframe not in definition.supported_timeframes:
        raise ConstructionError(f"Unsupported execution timeframe {execution_timeframe}")
    if not isinstance(parameter_overrides, dict):
        raise ConstructionError("parameter_overrides must be an object")
    try:
        parameters = get_plugin(definition).validate_configuration(parameter_overrides, {})
    except ValueError as exc:
        raise ConstructionError(str(exc)) from exc
    if parameters.get("direction", "LONG") != "LONG":
        raise ConstructionError("Portfolio Builder assignments must be long-only")
    share = D(str(strategy_share))
    if not D(0) <= share <= D(1):
        raise ConstructionError("strategy_share must be between 0 and 1")
    if risk_policy and (not risk_policy.enabled or risk_policy.allow_short):
        raise ConstructionError("Risk policy must be enabled and long-only")
    if order_policy and not order_policy.enabled:
        raise ConstructionError("Order policy must be enabled")
    return parameters


def _instrument_snapshot(selection):
    return {
        "id": selection.pk,
        "goal_id": selection.goal_allocation_id,
        "instrument_id": selection.instrument_id,
        "symbol": selection.instrument.symbol,
        "minimum_weight": decimal_string(selection.minimum_weight) if selection.minimum_weight is not None else None,
        "maximum_weight": decimal_string(selection.maximum_weight) if selection.maximum_weight is not None else None,
        "display_order": selection.display_order,
        "enabled": selection.enabled,
    }


def _assignment_snapshot(assignment):
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
        "strategy_share": decimal_string(assignment.strategy_share),
        "risk_policy_id": assignment.risk_policy_id,
        "order_policy_id": assignment.order_policy_id,
        "create_instance": assignment.create_instance,
        "enabled": assignment.enabled,
    }


def assignment_share_validation(instrument_selection, assignments=None):
    assignments = list(assignments if assignments is not None else instrument_selection.assignments.all())
    owning = [item for item in assignments if item.enabled and item.create_instance]
    total = sum((D(str(item.strategy_share)) for item in owning), D(0))
    errors = []
    if not owning:
        errors.append("At least one enabled assignment that creates an instance is required")
    elif total != D(1):
        errors.append(f"Enabled strategy shares must total exactly 100% (currently {decimal_string(total * 100)}%)")
    return {"total": total, "valid": not errors, "errors": errors}


def snapshot_plan(plan):
    require_plan_ready(plan)
    goals = list(plan.goals.filter(enabled=True).order_by("display_order", "pk"))
    goal_rows = []
    instrument_rows = []
    assignment_rows = []
    policy_rows = {}
    for goal in goals:
        rules = resolved_goal_rules(goal.timeframe_bucket, goal.risk_level)
        goal_row = {
            "id": goal.pk,
            "name": goal.name,
            "allocation_weight": decimal_string(goal.allocation_weight),
            "timeframe_bucket": goal.timeframe_bucket,
            "risk_level": goal.risk_level,
            "display_order": goal.display_order,
            "construction_source": goal.construction_source,
            "accepted_recommendation_run_id": goal.accepted_recommendation_run_id,
        }
        if goal.construction_source == "ACCEPTED_RECOMMENDATION":
            if not goal.accepted_recommendation_run_id:
                raise ConstructionError("Recommendation construction mode requires an accepted recommendation")
            from apps.research.services.acceptance import validate_recommendation_for_construction

            try:
                accepted = validate_recommendation_for_construction(goal.accepted_recommendation_run)
            except ValueError as exc:
                raise ConstructionError(str(exc)) from exc
            goal_row["accepted_recommendation"] = {
                "run_id": goal.accepted_recommendation_run_id,
                "expires_at": goal.accepted_recommendation_run.expires_at.isoformat(),
                "dataset_version_id": goal.accepted_recommendation_run.dataset_version_id,
                "protocol_version_id": goal.accepted_recommendation_run.protocol_version_id,
                "cash_weight": decimal_string(accepted["cash_weight"]),
                "stock_weights": {
                    str(instrument_id): decimal_string(weight)
                    for instrument_id, weight in accepted["stock_weights"].items()
                },
                "group_weights": accepted["group_weights"],
            }
        goal_rows.append(goal_row)
        policy_rows[str(goal.pk)] = {
            key: decimal_string(value) if isinstance(value, D) else value for key, value in rules.items()
        }
        selections = goal.instrument_selections.filter(enabled=True).select_related("instrument")
        for selection in selections:
            validate_instrument_selection(
                instrument=selection.instrument,
                minimum_weight=selection.minimum_weight,
                maximum_weight=selection.maximum_weight,
            )
            instrument_rows.append(_instrument_snapshot(selection))
            assignments = list(selection.assignments.select_related(
                "strategy_definition", "risk_policy", "order_policy",
            ))
            for assignment in assignments:
                if not assignment.enabled:
                    continue
                parameters = validate_assignment(
                    goal_instrument_selection=selection,
                    definition=assignment.strategy_definition,
                    execution_timeframe=assignment.execution_timeframe,
                    parameter_overrides=assignment.parameter_overrides,
                    strategy_share=assignment.strategy_share,
                    risk_policy=assignment.risk_policy,
                    order_policy=assignment.order_policy,
                )
                if parameters != assignment.parameter_overrides:
                    raise ConstructionError("Stored assignment parameters are not canonical; save the assignment again")
                if assignment.parameter_hash != canonical_request_hash("parameters", parameters):
                    raise ConstructionError("Stored assignment parameter identity is invalid; save the assignment again")
                assignment_rows.append(_assignment_snapshot(assignment))
    return {
        "plan": {
            "id": plan.pk,
            "portfolio_id": plan.portfolio_id,
            "name": plan.name,
            "status": plan.status,
            "version": plan.version,
        },
        "goals": goal_rows,
        "instruments": instrument_rows,
        "assignments": assignment_rows,
        "policies": policy_rows,
    }


def create_construction_run(
    plan,
    idempotency_key,
    *,
    nav=None,
    refresh_history=True,
    retry_failed=False,
    defer=False,
):
    snapshot = snapshot_plan(plan)
    nav = D(str(nav if nav is not None else plan.portfolio.account.net_liquidation))
    if nav <= 0:
        raise ConstructionError("Portfolio NAV must be positive")
    request_hash = canonical_request_hash("portfolio_construction", {
        "plan_id": plan.pk,
        "plan_version": plan.version,
        "nav": nav,
        "refresh_history": refresh_history,
    })
    existing = PortfolioConstructionRun.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        try:
            require_matching_request(existing.request_hash, request_hash)
        except ValueError as exc:
            raise ConstructionError(str(exc)) from exc
        if existing.status != "FAILED" or not retry_failed:
            return existing
        if not existing.retryable:
            raise ConstructionError("Failed construction preview is not retryable")
        with transaction.atomic():
            run = PortfolioConstructionRun.objects.select_for_update().get(pk=existing.pk)
            run.targets.all().delete()
            run.status = "QUEUED" if defer else "CALCULATING"
            run.retryable = False
            run.last_error = ""
            run.goal_results = []
            run.final_target_weights = {}
            run.metrics = {}
            run.warnings = []
            run.started_at = None
            run.completed_at = None
            run.attempt_count += 1
            run.save()
            OperationAttempt.objects.create(
                operation_type="PORTFOLIO_CONSTRUCTION",
                operation_id=str(run.pk),
                attempt_number=run.attempt_count,
                request_hash=run.request_hash,
                status="QUEUED" if defer else "PROCESSING",
            )
        return run
    run = PortfolioConstructionRun.objects.create(
        plan=plan,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        status="QUEUED" if defer else "CALCULATING",
        nav=nav,
        plan_snapshot=snapshot["plan"],
        goal_snapshot=snapshot["goals"],
        instrument_snapshot=snapshot["instruments"],
        assignment_snapshot=snapshot["assignments"],
        policy_snapshot=snapshot["policies"],
    )
    OperationAttempt.objects.create(
        operation_type="PORTFOLIO_CONSTRUCTION",
        operation_id=str(run.pk),
        attempt_number=run.attempt_count,
        request_hash=run.request_hash,
        status="QUEUED" if defer else "PROCESSING",
    )
    return run


def _quantize_weight(value):
    return D(str(value)).quantize(WEIGHT_QUANTUM, rounding=ROUND_DOWN)


def _metric_strings(metrics):
    return {
        "expected_return": decimal_string(metrics.get("expected_return", 0)),
        "expected_volatility": decimal_string(metrics.get("expected_volatility", 0)),
        "sharpe_ratio": decimal_string(metrics.get("sharpe_ratio", 0)),
    }


def _single_stock_metrics(instruments, weights, refresh_history):
    try:
        return calculate_weighted_metrics(
            instruments,
            weights,
            lookback_days=252,
            minimum_history_observations=60,
            refresh_history=refresh_history,
        )
    except OptimizationError as exc:
        return {
            "expected_return": 0,
            "expected_volatility": 0,
            "sharpe_ratio": 0,
            "expected_return_contributions": {},
            "risk_contributions": {},
            "warnings": [{"code": "METRICS_UNAVAILABLE", "message": str(exc)}],
        }


def run_construction(construction_run, *, refresh_history=True):
    run_id = construction_run.pk if isinstance(construction_run, PortfolioConstructionRun) else construction_run
    run = PortfolioConstructionRun.objects.select_related("plan__portfolio__account").get(pk=run_id)
    if run.status == "COMPLETED":
        return run
    if run.status not in {"QUEUED", "DISPATCHED", "CALCULATING"}:
        return run
    run.status = "CALCULATING"
    run.started_at = timezone.now()
    run.save(update_fields=["status", "started_at"])
    OperationAttempt.objects.filter(
        operation_type="PORTFOLIO_CONSTRUCTION",
        operation_id=str(run.pk),
        attempt_number=run.attempt_count,
        status="QUEUED",
    ).update(status="PROCESSING")
    try:
        instruments_by_goal = {}
        for selection in run.instrument_snapshot:
            if selection.get("enabled", True):
                instruments_by_goal.setdefault(int(selection["goal_id"]), []).append(selection)
        assignments_by_instrument = {}
        for assignment in run.assignment_snapshot:
            if assignment.get("enabled", True):
                assignments_by_instrument.setdefault(int(assignment["goal_instrument_id"]), []).append(assignment)
        goal_results = []
        combined = {}
        combined_contributions = {}
        strategy_aggregates = {}
        all_warnings = []
        instruments_by_id = {
            item.pk: item
            for item in Instrument.objects.filter(
                pk__in={int(item["instrument_id"]) for item in run.instrument_snapshot},
                active=True,
                tradable=True,
                asset_class="STK",
            )
        }
        for goal in run.goal_snapshot:
            goal_id = int(goal["id"])
            allocation = D(goal["allocation_weight"])
            rules = run.policy_snapshot[str(goal_id)]
            minimum_cash = D(rules["minimum_cash_weight"])
            maximum_stock = D(rules["maximum_stock_weight"])
            selections = instruments_by_goal.get(goal_id, [])
            instrument_ids = sorted({int(item["instrument_id"]) for item in selections})
            instruments = [instruments_by_id[item_id] for item_id in instrument_ids if item_id in instruments_by_id]
            selections_by_instrument_id = {int(item["instrument_id"]): item for item in selections}
            warnings = []
            apply_blocked = False
            local_weights = {}
            metrics = {
                "expected_return": 0,
                "expected_volatility": 0,
                "sharpe_ratio": 0,
                "expected_return_contributions": {},
                "risk_contributions": {},
                "warnings": [],
            }
            construction_source = goal.get("construction_source", "MANUAL_OPTIMIZER")
            if construction_source == "ACCEPTED_RECOMMENDATION":
                accepted = goal.get("accepted_recommendation") or {}
                local_weights = {
                    int(instrument_id): D(weight)
                    for instrument_id, weight in accepted.get("stock_weights", {}).items()
                }
                if set(local_weights) != set(instrument_ids):
                    raise ConstructionError(
                        f"Accepted recommendation selections changed for goal {goal['name']}; detach or regenerate"
                    )
                if any(weight > maximum_stock for weight in local_weights.values()):
                    raise ConstructionError(f"Accepted recommendation exceeds the live stock cap for goal {goal['name']}")
                cash_weight = D(accepted.get("cash_weight", 1))
                if cash_weight < minimum_cash or sum(local_weights.values(), D(0)) + cash_weight != D(1):
                    raise ConstructionError(f"Accepted recommendation violates the live cash floor for goal {goal['name']}")
                metrics = _single_stock_metrics(instruments, local_weights, refresh_history)
            elif goal["timeframe_bucket"] == "NOW":
                cash_weight = D(1)
            elif not instruments:
                cash_weight = D(1)
                apply_blocked = True
                warnings.append({
                    "code": "NO_STOCKS_SELECTED",
                    "message": "No stocks are selected; preview is cash-only and apply is blocked for this goal",
                })
            elif len(instruments) == 1:
                selection = selections_by_instrument_id[instruments[0].pk]
                local_minimum = D(selection["minimum_weight"] or 0)
                local_maximum = min(maximum_stock, D(selection["maximum_weight"] or 1))
                stock_weight = min(D(1) - minimum_cash, local_maximum)
                if local_minimum > stock_weight:
                    raise ConstructionError(
                        f"Weight bounds are infeasible for {selection['symbol']} in goal {goal['name']}"
                    )
                local_weights[instruments[0].pk] = stock_weight
                cash_weight = D(1) - stock_weight
                if stock_weight < D(1) - minimum_cash:
                    warnings.append({
                        "code": "SINGLE_STOCK_LIMIT",
                        "message": "The single selected stock was capped and the unused allocation remains cash",
                    })
                metrics = _single_stock_metrics(instruments, local_weights, refresh_history)
            else:
                minimum_weights = []
                maximum_weights = []
                for instrument in instruments:
                    selection = selections_by_instrument_id[instrument.pk]
                    minimum_weights.append(D(selection["minimum_weight"] or 0))
                    maximum_weights.append(min(maximum_stock, D(selection["maximum_weight"] or 1)))
                stock_total = min(D(1) - minimum_cash, sum(maximum_weights, D(0)))
                if sum(minimum_weights, D(0)) > stock_total:
                    raise ConstructionError(f"Weight bounds are infeasible for goal {goal['name']}")
                cash_weight = D(1) - stock_total
                solved = optimize_explicit_universe(
                    instruments,
                    method=rules["optimizer_method"],
                    cash_weight=cash_weight,
                    minimum_weight=minimum_weights,
                    maximum_weight=maximum_weights,
                    lookback_days=int(rules["lookback_days"]),
                    minimum_history_observations=int(rules["minimum_history_observations"]),
                    maximum_turnover=10,
                    transaction_cost_penalty=0,
                    risk_free_rate=0,
                    long_only=True,
                    refresh_history=refresh_history,
                )
                local_weights = {
                    instrument_id: _quantize_weight(solved["weights"][index])
                    for index, instrument_id in enumerate(solved["instrument_ids"])
                }
                cash_weight = D(1) - sum(local_weights.values(), D(0))
                metrics = solved
            warnings.extend(metrics.get("warnings", []))
            stocks = []
            for instrument_id, local_weight in sorted(local_weights.items(), key=lambda item: (-item[1], item[0])):
                contribution = allocation * local_weight
                combined[instrument_id] = combined.get(instrument_id, D(0)) + contribution
                contribution_row = {
                    "goal_id": goal_id,
                    "goal_name": goal["name"],
                    "goal_allocation_weight": decimal_string(allocation),
                    "local_weight": decimal_string(local_weight),
                    "portfolio_contribution": decimal_string(contribution),
                }
                combined_contributions.setdefault(instrument_id, []).append(contribution_row)
                selection = selections_by_instrument_id[instrument_id]
                assignments = [
                    item for item in assignments_by_instrument.get(int(selection["id"]), [])
                    if item.get("create_instance", True)
                ]
                share_total = sum((D(item["strategy_share"]) for item in assignments), D(0))
                share_valid = bool(assignments) and share_total == D(1)
                if not share_valid:
                    apply_blocked = True
                    message = (
                        "At least one enabled assignment that creates an instance is required"
                        if not assignments else
                        f"Enabled strategy shares must total exactly 100% (currently {decimal_string(share_total * 100)}%)"
                    )
                    warnings.append({
                        "code": "INVALID_STRATEGY_SHARES",
                        "instrument_id": instrument_id,
                        "symbol": selection["symbol"],
                        "message": message,
                    })
                strategy_rows = []
                for assignment in assignments:
                    controlled_weight = contribution * D(assignment["strategy_share"])
                    identity = (
                        int(assignment["strategy_definition_id"]),
                        instrument_id,
                        assignment["execution_timeframe"],
                        assignment["parameter_hash"],
                        assignment.get("risk_policy_id"),
                        assignment.get("order_policy_id"),
                    )
                    aggregate = strategy_aggregates.setdefault(identity, {
                        "strategy_definition_id": identity[0],
                        "strategy_key": assignment["strategy_key"],
                        "strategy_name": assignment["strategy_name"],
                        "instrument_id": instrument_id,
                        "symbol": assignment["symbol"],
                        "execution_timeframe": assignment["execution_timeframe"],
                        "parameter_hash": assignment["parameter_hash"],
                        "parameter_overrides": assignment["parameter_overrides"],
                        "risk_policy_id": assignment.get("risk_policy_id"),
                        "order_policy_id": assignment.get("order_policy_id"),
                        "target_weight": D(0),
                        "assignment_ids": [],
                        "contributions": [],
                    })
                    aggregate["target_weight"] += controlled_weight
                    aggregate["assignment_ids"].append(int(assignment["id"]))
                    aggregate["contributions"].append({
                        "assignment_id": int(assignment["id"]),
                        "goal_id": goal_id,
                        "strategy_share": assignment["strategy_share"],
                        "portfolio_weight": decimal_string(controlled_weight),
                    })
                    strategy_rows.append({
                        "assignment_id": int(assignment["id"]),
                        "strategy_definition_id": int(assignment["strategy_definition_id"]),
                        "strategy_name": assignment["strategy_name"],
                        "strategy_share": assignment["strategy_share"],
                        "portfolio_weight": decimal_string(controlled_weight),
                    })
                stocks.append({
                    "instrument_id": instrument_id,
                    "symbol": instruments_by_id[instrument_id].symbol,
                    "goal_instrument_id": int(selection["id"]),
                    "strategy_share_total": decimal_string(share_total),
                    "strategy_share_valid": share_valid,
                    "strategies": strategy_rows,
                    **contribution_row,
                })
            result = {
                "goal_id": goal_id,
                "name": goal["name"],
                "allocation_weight": decimal_string(allocation),
                "goal_nav": decimal_string(run.nav * allocation),
                "timeframe_bucket": goal["timeframe_bucket"],
                "risk_level": goal["risk_level"],
                "optimizer_method": "ACCEPTED_RECOMMENDATION" if construction_source == "ACCEPTED_RECOMMENDATION" else rules["optimizer_method"],
                "construction_source": construction_source,
                "accepted_recommendation_run_id": goal.get("accepted_recommendation_run_id"),
                "cash_weight": decimal_string(cash_weight),
                "maximum_stock_weight": decimal_string(maximum_stock),
                "stocks": stocks,
                "metrics": _metric_strings(metrics),
                "warnings": warnings,
                "intentionally_cash_only": goal["timeframe_bucket"] == "NOW",
                "apply_blocked": apply_blocked,
            }
            goal_results.append(result)
            all_warnings.extend({**warning, "goal_id": goal_id, "goal_name": goal["name"]} for warning in warnings)
        combined = {key: _quantize_weight(value) for key, value in combined.items() if value > 0}
        cash_weight = D(1) - sum(combined.values(), D(0))
        if cash_weight < 0 or sum(combined.values(), D(0)) + cash_weight != D(1):
            raise ConstructionError("Combined stock and cash weights must equal 100%")
        final_instruments = [instruments_by_id[key] for key in combined]
        combined_metrics = _single_stock_metrics(final_instruments, combined, refresh_history)
        all_warnings.extend(combined_metrics.get("warnings", []))
        strategy_targets = []
        for identity, item in strategy_aggregates.items():
            strategy_targets.append({
                **{key: value for key, value in item.items() if key != "target_weight"},
                "identity": canonical_request_hash("builder_strategy_identity", identity),
                "target_weight": decimal_string(_quantize_weight(item["target_weight"])),
            })
        strategy_targets.sort(key=lambda item: (
            item["strategy_name"], item["symbol"], item["execution_timeframe"], item["identity"]
        ))
        positions = {
            item.instrument_id: D(item.quantity) * D(item.market_price)
            * D(item.instrument.multiplier or 1) / D(run.nav)
            for item in PortfolioPosition.objects.filter(
                portfolio=run.plan.portfolio
            ).select_related("instrument")
            if D(item.market_price) > 0
        }
        with transaction.atomic():
            run.targets.all().delete()
            for rank, (instrument_id, target_weight) in enumerate(sorted(combined.items(), key=lambda item: (-item[1], item[0]))):
                PortfolioConstructionTarget.objects.create(
                    construction_run=run,
                    instrument=instruments_by_id[instrument_id],
                    current_weight=positions.get(instrument_id, D(0)),
                    target_weight=target_weight,
                    target_value=target_weight * D(run.nav),
                    expected_return_contribution=D(str(combined_metrics.get("expected_return_contributions", {}).get(instrument_id, 0))),
                    risk_contribution=D(str(combined_metrics.get("risk_contributions", {}).get(instrument_id, 0))),
                    goal_contributions=combined_contributions[instrument_id],
                    rank=rank,
                )
            run.goal_results = goal_results
            run.final_target_weights = {
                "cash": decimal_string(cash_weight),
                "stocks": {str(key): decimal_string(value) for key, value in combined.items()},
            }
            run.metrics = {**_metric_strings(combined_metrics), "strategy_targets": strategy_targets}
            run.warnings = all_warnings
            run.status = "COMPLETED"
            run.retryable = False
            run.last_error = ""
            run.completed_at = timezone.now()
            run.save()
            OperationAttempt.objects.filter(
                operation_type="PORTFOLIO_CONSTRUCTION",
                operation_id=str(run.pk),
                attempt_number=run.attempt_count,
            ).update(status="COMPLETED", result={"construction_run_id": run.pk}, completed_at=run.completed_at)
            OutboxEvent.objects.create(
                topic="portfolio.construction.completed.v1",
                event_type="portfolio.construction.completed",
                aggregate_type="portfolio",
                aggregate_id=str(run.plan.portfolio_id),
                partition_key=str(run.plan.portfolio_id),
                payload={"construction_run_id": run.pk, "plan_id": run.plan_id},
                idempotency_key=f"construction:{run.pk}:attempt:{run.attempt_count}:completed",
            )
            AuditEvent.objects.create(
                event_type="portfolio.construction.completed",
                actor="system",
                aggregate_type="portfolio",
                aggregate_id=str(run.plan.portfolio_id),
                data={"construction_run_id": run.pk, "plan_id": run.plan_id, "plan_version": run.plan_snapshot["version"]},
                idempotency_key=f"audit:construction:{run.pk}:attempt:{run.attempt_count}:completed",
            )
        return run
    except Exception as exc:
        run.status = "FAILED"
        run.last_error = str(exc)[:1000]
        run.retryable = not isinstance(exc, (ConstructionError, OptimizationError, ValueError))
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "last_error", "retryable", "completed_at"])
        OperationAttempt.objects.filter(
            operation_type="PORTFOLIO_CONSTRUCTION",
            operation_id=str(run.pk),
            attempt_number=run.attempt_count,
        ).update(status="FAILED", retryable=run.retryable, error=run.last_error, completed_at=run.completed_at)
        if isinstance(exc, ConstructionError):
            raise
        raise ConstructionError(str(exc)) from exc


def latest_prices(construction_run):
    result = {}
    for target in construction_run.targets.select_related("instrument"):
        price = InstrumentPriceHistory.objects.filter(
            instrument=target.instrument, provider="FINNHUB"
        ).order_by("-trading_date").first()
        if not price:
            raise ConstructionError(f"No historical reference price is available for {target.instrument.symbol}")
        result[target.instrument_id] = price.adjusted_close or price.close
    for position in PortfolioPosition.objects.filter(portfolio=construction_run.plan.portfolio):
        if position.instrument_id not in result and position.market_price > 0:
            result[position.instrument_id] = position.market_price
    return result


def plan_construction_rebalance(
    construction_run,
    idempotency_key,
    *,
    mode=None,
    run_type=RunType.PREVIEW,
    strict_market_state=False,
):
    if construction_run.status != "COMPLETED":
        raise ConstructionError("Only a completed construction run can create rebalance targets")
    from apps.rebalancing.services import plan_rebalance

    return plan_rebalance(
        construction_run.plan.portfolio,
        "GOAL_CONSTRUCTION",
        idempotency_key,
        prices=latest_prices(construction_run),
        nav=construction_run.nav,
        mode=mode,
        run_type=run_type,
        strict_market_state=strict_market_state,
        construction_run=construction_run,
    )


def _instance_name(run, target, suffix=0):
    base = f"Builder {target['strategy_name']} {target['symbol']} {target['execution_timeframe']}"
    if suffix:
        base = f"{base} {suffix}"
    return base[:128]


def create_or_reuse_strategy_instances(run):
    from apps.strategies.framework import create_instance, update_instance
    from apps.strategies.models import OrderPolicy, StrategyRiskPolicy

    linked = []
    execution_mode = execution_mode_for_portfolio(
        run.plan.portfolio,
    )
    for target in run.metrics.get("strategy_targets", []):
        target_configuration = {
            "target_weight": target["target_weight"],
            "capital_share": "1",
            "priority": 100,
            "construction_run_id": str(run.pk),
        }
        candidates = StrategyInstance.objects.filter(
            portfolio=run.plan.portfolio,
            definition_id=int(target["strategy_definition_id"]),
            instrument_id=int(target["instrument_id"]),
            timeframe=target["execution_timeframe"],
            risk_policy_id=target.get("risk_policy_id"),
            order_policy_id=target.get("order_policy_id"),
            execution_mode=execution_mode,
        ).order_by("pk")
        instance = next(
            (
                item for item in candidates
                if item.parameters==target["parameter_overrides"]
                and (
                    not item.enabled
                    or bool(item.target_configuration.get("construction_run_id"))
                )
            ),
            None,
        )
        created=False
        if not instance:
            definition = StrategyDefinition.objects.get(pk=target["strategy_definition_id"], enabled=True)
            risk_policy = (
                StrategyRiskPolicy.objects.get(pk=target["risk_policy_id"], enabled=True)
                if target.get("risk_policy_id") else None
            )
            order_policy = (
                OrderPolicy.objects.get(pk=target["order_policy_id"], enabled=True)
                if target.get("order_policy_id") else None
            )
            name = _instance_name(run, target)
            suffix = 1
            while StrategyInstance.objects.filter(portfolio=run.plan.portfolio, name=name).exists():
                suffix += 1
                name = _instance_name(run, target, suffix)
            instance, _ = create_instance(
                name=name,
                definition_key=definition.key,
                portfolio=run.plan.portfolio,
                instrument_id=int(target["instrument_id"]),
                timeframe=target["execution_timeframe"],
                parameters=target["parameter_overrides"],
                target_configuration=target_configuration,
                risk_policy=risk_policy,
                order_policy=order_policy,
                execution_mode=execution_mode,
                qualify=False,
            )
            created=True
        elif instance.target_configuration != target_configuration:
            instance = update_instance(instance, {"target_configuration": target_configuration})
        if instance.execution_mode != execution_mode:
            raise ConstructionError(
                f"Construction-created strategy instances must use {execution_mode} mode"
            )
        for assignment_id in target["assignment_ids"]:
            linked.append({
                "assignment_id": assignment_id,
                "strategy_instance_id": instance.pk,
                "target_weight": target["target_weight"],
                "strategy_creation": "CREATED" if created else "REUSED",
                "activation_status": "QUEUED",
                "market_subscription": "PENDING",
                "block_reason": "",
            })
            GoalStrategyAssignment.objects.filter(pk=assignment_id).update(created_strategy_instance=instance)
    return linked


def _application_progress(rows):
    activation_states={str(row.get("activation_status") or "") for row in rows}
    subscription_states={str(row.get("market_subscription") or "") for row in rows}
    failures=activation_states & {"BLOCKED","ERROR"} or subscription_states & {"ERROR"}
    if failures:
        return "PARTIALLY_APPLIED","FAILED","FAILED"
    if rows and all(row.get("execution_active") for row in rows):
        return "APPLIED","EXECUTION_ACTIVE","ACTIVE"
    if rows and all(row.get("first_evaluation_complete") for row in rows):
        return "ACTIVATING","FIRST_EVALUATION_COMPLETE","ACTIVE"
    if rows and all(row.get("waiting_for_live_bar") for row in rows):
        return "ACTIVATING","WAITING_FOR_LIVE_BAR","ACTIVE"
    if rows and all(row.get("warmup_complete") for row in rows):
        return "ACTIVATING","WARMUP_COMPLETE","ACTIVE"
    if rows and all(row.get("subscription_ready") for row in rows):
        return "ACTIVATING","SUBSCRIPTION_READY","ACTIVE"
    return "ACTIVATING","PENDING","PENDING"


@transaction.atomic
def record_strategy_activation_result(construction_run_id,strategy_instance_id):
    from apps.market_streams.models import MarketDataSubscription
    try:
        run_id=int(construction_run_id)
    except (TypeError,ValueError):
        return None
    run=PortfolioConstructionRun.objects.select_for_update().select_related(
        "plan__portfolio__gateway_session").filter(pk=run_id).first()
    if run is None:return None
    rows=[dict(row) for row in run.metrics.get("strategy_instances",[])]
    instance=StrategyInstance.objects.filter(pk=strategy_instance_id).first()
    if instance is None:return run
    subscription=MarketDataSubscription.objects.filter(
        gateway_session=instance.portfolio.gateway_session,
        instrument=instance.instrument,
        timeframe=instance.timeframe,
    ).first()
    for row in rows:
        if int(row["strategy_instance_id"])!=int(strategy_instance_id):continue
        row["activation_status"]=instance.state
        row["market_subscription"]=subscription.state if subscription else "MISSING"
        row["enabled"]=instance.enabled
        row["block_reason"]=instance.block_reason
        row["active_provider"]=subscription.active_provider if subscription else "NONE"
        row["warmup_progress"]=instance.warmup_progress
        from apps.market_streams.services import current_warmup_required
        row["warmup_required"]=current_warmup_required(instance)
        row["subscription_ready"]=instance.subscription_ready_at is not None
        row["warmup_complete"]=instance.warmup_completed_at is not None
        row["waiting_for_live_bar"]=instance.state=="READY_WAITING_FOR_LIVE_BAR"
        row["first_evaluation_complete"]=instance.first_evaluation_completed_at is not None
        row["execution_active"]=instance.execution_active_at is not None
    application_status,activation_status,subscription_status=_application_progress(rows)
    failures=[row["block_reason"] for row in rows if row.get("block_reason")]
    run.metrics={
        **run.metrics,
        "application":{
            "construction_application":"APPLIED",
            "rebalance_creation":"CREATED" if run.applied_rebalance_id else "FAILED",
            "strategy_creation":"COMPLETED",
            "strategy_activation":activation_status,
            "market_subscription":subscription_status,
        },
        "strategy_instances":rows,
    }
    run.application_status=application_status
    run.last_error="; ".join(dict.fromkeys(failures))[:1000]
    run.retryable=application_status=="PARTIALLY_APPLIED"
    run.save(update_fields=["metrics","application_status","last_error","retryable"])
    return run


def queue_builder_strategy_activations(construction_run_id):
    from apps.core.idempotency import canonical_request_hash
    from apps.strategies.framework import enable_instance
    from apps.strategies.models import StrategyAction
    run=PortfolioConstructionRun.objects.get(pk=construction_run_id)
    instance_ids=sorted({
        int(row["strategy_instance_id"])
        for row in run.metrics.get("strategy_instances",[])
    })
    for instance_id in instance_ids:
        key=f"builder-activation:{run.pk}:{instance_id}"[:128]
        request_hash=canonical_request_hash("strategy_action",{
            "strategy_instance_id":instance_id,
            "action":"enable",
            "construction_run_id":run.pk,
        })
        action,_=StrategyAction.objects.get_or_create(
            idempotency_key=key,
            defaults={
                "strategy_instance_id":instance_id,
                "action":"enable",
                "request_hash":request_hash,
            },
        )
        if action.status=="COMPLETED":
            record_strategy_activation_result(run.pk,instance_id)
            continue
        try:
            instance=StrategyInstance.objects.get(pk=instance_id)
            enable_instance(
                instance,action=action,construction_run_id=run.pk)
            record_strategy_activation_result(run.pk,instance_id)
        except Exception:
            record_strategy_activation_result(run.pk,instance_id)
    return len(instance_ids)


@transaction.atomic
def apply_construction_run(construction_run, idempotency_key, *, mode=None):
    run_id = construction_run.pk if isinstance(construction_run, PortfolioConstructionRun) else construction_run
    run = (
        PortfolioConstructionRun.objects.select_for_update(of=("self",))
        .select_related("plan__portfolio__account", "applied_rebalance")
        .get(pk=run_id)
    )
    if run.status != "COMPLETED":
        raise ConstructionError("Only a completed construction run can be applied")
    require_plan_ready(run.plan)
    mode = require_portfolio_execution_ready(run.plan.portfolio)
    recommendation_ids = [
        item.get("accepted_recommendation_run_id")
        for item in run.goal_snapshot
        if item.get("construction_source") == "ACCEPTED_RECOMMENDATION"
    ]
    if recommendation_ids:
        from apps.research.models import GoalRecommendationRun

        current = {
            item.pk: item for item in GoalRecommendationRun.objects.filter(pk__in=recommendation_ids)
        }
        for recommendation_id in recommendation_ids:
            recommendation = current.get(recommendation_id)
            if not recommendation or recommendation.expires_at <= timezone.now():
                raise ConstructionError("Accepted recommendation expired after preview; regenerate before apply")
    if any(item.get("apply_blocked") for item in run.goal_results):
        raise ConstructionError(
            "Every non-cash-only goal must include at least one stock, and enabled strategy shares must total exactly 100%"
        )
    if run.applied_rebalance_id:
        if run.application_idempotency_key == idempotency_key:
            return run, run.applied_rebalance, False
        raise ConstructionAlreadyApplied(run)
    run.application_status = "APPLYING"
    run.application_idempotency_key = idempotency_key
    run.save(update_fields=["application_status", "application_idempotency_key"])
    linked_instances = create_or_reuse_strategy_instances(run)
    rebalance = plan_construction_rebalance(
        run,
        f"{idempotency_key}:rebalance",
        mode=mode,
        run_type=RunType.PREVIEW,
        strict_market_state=False,
    )
    if rebalance.construction_run_id != run.pk:
        raise ConstructionError("Idempotency-Key was already used for a different construction application")
    run.applied_rebalance = rebalance
    run.applied_at = timezone.now()
    run.application_status = "ACTIVATING" if linked_instances else "APPLIED"
    run.metrics = {
        **run.metrics,
        "application":{
            "construction_application":"APPLIED",
            "initial_allocation_preview":"CREATED",
            "strategy_execution_rebalance":"WAITING_FOR_FIRST_LIVE_TARGET",
            "strategy_creation":"COMPLETED",
            "strategy_activation":"QUEUED" if linked_instances else "NOT_REQUIRED",
            "market_subscription":"PENDING" if linked_instances else "NOT_REQUIRED",
        },
        "strategy_instances": linked_instances,
    }
    run.save(update_fields=["applied_rebalance", "applied_at", "application_status", "metrics"])
    AuditEvent.objects.create(
        event_type="portfolio.construction.applied",
        actor="system",
        aggregate_type="portfolio",
        aggregate_id=str(run.plan.portfolio_id),
        data={
            "construction_run_id": run.pk,
            "rebalance_run_id": rebalance.pk,
            "strategy_instances": linked_instances,
            "mode": mode,
        },
        idempotency_key=f"audit:construction-apply:{idempotency_key}",
    )
    if linked_instances:
        transaction.on_commit(lambda: queue_builder_strategy_activations(run.pk))
    return run, rebalance, True
