from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditEvent, OperationAttempt, OutboxEvent
from apps.core.idempotency import canonical_request_hash, require_matching_request
from apps.core.lazy_imports import lazy_module
from apps.execution.modes import (
    RunType,
    execution_mode_for_portfolio,
    require_portfolio_execution_ready,
)
from apps.market_data.models import InstrumentPriceHistory
from apps.market_data.services import fetch_daily_history
from apps.portfolios.models import PortfolioPosition
from apps.strategies.models import StrategyAllocation

from .models import (
    OptimizedPortfolioTarget,
    PortfolioOptimizationPolicy,
    PortfolioOptimizationRun,
    PortfolioUniverse,
)


# Deferred: every Celery worker imports this module transitively via
# portfolio_construction.tasks, but only the optimizer actually runs the math.
# See apps/core/lazy_imports.py for the measured cost.
np = lazy_module("numpy")
_scipy_optimize = lazy_module("scipy.optimize")

D = Decimal
TRADING_DAYS = 252


class OptimizationError(ValueError):
    pass


class OptimizationAlreadyApplied(OptimizationError):
    code = "OPTIMIZATION_ALREADY_APPLIED"

    def __init__(self, optimization_run):
        self.optimization_run = optimization_run
        super().__init__(
            f"Optimization run {optimization_run.pk} was already applied by rebalance "
            f"{optimization_run.applied_rebalance_id}"
        )


class UniverseSizeError(OptimizationError):
    code = "UNIVERSE_SIZE_EXCEEDED"

    def __init__(self, selected_count, maximum_instruments):
        self.selected_count = selected_count
        self.maximum_instruments = maximum_instruments
        super().__init__(
            f"Selected instrument count {selected_count} exceeds maximum_instruments {maximum_instruments}"
        )


def _json_matrix(matrix):
    return [[str(float(value)) for value in row] for row in matrix]


def _feasible_start(current, lower, upper, total):
    weights = np.clip(np.asarray(current, dtype=float), lower, upper)
    for _ in range(100):
        difference = total - float(weights.sum())
        if abs(difference) <= 1e-10:
            break
        available = np.where(weights < upper - 1e-12)[0] if difference > 0 else np.where(weights > lower + 1e-12)[0]
        if not len(available):
            break
        step = difference / len(available)
        weights[available] = np.clip(weights[available] + step, lower[available], upper[available])
    if abs(float(weights.sum()) - total) > 1e-7:
        raise OptimizationError("Weight bounds cannot satisfy the target cash weight")
    return weights


def solve_markowitz(*, expected_returns, covariance, current_weights, method, cash_weight,
                    current_cash_weight=0, minimum_weight=0, maximum_weight=1,
                    maximum_turnover=1, transaction_cost_penalty=0, risk_free_rate=0,
                    long_only=True, external_current_weight=0):
    expected_returns = np.asarray(expected_returns, dtype=float)
    covariance = np.asarray(covariance, dtype=float)
    current = np.asarray(current_weights, dtype=float)
    count = len(expected_returns)
    if count < 2:
        raise OptimizationError("At least two instruments with sufficient history are required")
    if covariance.shape != (count, count) or current.shape != (count,):
        raise OptimizationError("Optimization inputs have inconsistent dimensions")
    cash_weight = float(cash_weight)
    stock_total = 1.0 - cash_weight
    lower = np.asarray(minimum_weight, dtype=float)
    upper = np.asarray(maximum_weight, dtype=float)
    if lower.ndim == 0:
        lower = np.full(count, max(float(lower), 0.0) if long_only else float(lower))
    elif long_only:
        lower = np.maximum(lower, 0.0)
    if upper.ndim == 0:
        upper = np.full(count, float(upper))
    if lower.shape != (count,) or upper.shape != (count,):
        raise OptimizationError("Weight bounds have inconsistent dimensions")
    if not 0 <= cash_weight < 1:
        raise OptimizationError("Target cash weight must be between zero and one")
    if np.any(upper <= 0) or np.any(lower > upper):
        raise OptimizationError("Minimum and maximum weights are invalid")
    if float(lower.sum()) > stock_total + 1e-10 or float(upper.sum()) < stock_total - 1e-10:
        raise OptimizationError("Weight bounds are infeasible for the selected universe and cash target")
    if not np.all(np.isfinite(expected_returns)) or not np.all(np.isfinite(covariance)):
        raise OptimizationError("Expected returns and covariance must be finite")
    covariance = (covariance + covariance.T) / 2
    eigen_min = float(np.linalg.eigvalsh(covariance).min())
    if eigen_min < 1e-8:
        covariance = covariance + np.eye(count) * (1e-8 - eigen_min)
    start = _feasible_start(current, lower, upper, stock_total)
    max_turnover = float(maximum_turnover)
    # Match the rebalance planner's gross traded-notional convention: stock
    # weight changes count once; cash is the residual funding leg.
    fixed_turnover = abs(float(external_current_weight))

    def turnover(weights):
        return float(np.abs(weights - current).sum()) + fixed_turnover

    minimum_reachable = turnover(start)
    if max_turnover + 1e-8 < minimum_reachable:
        # Find the nearest feasible allocation before declaring the policy infeasible.
        nearest = _scipy_optimize.minimize(
            lambda weights: float(np.abs(weights - current).sum()),
            start,
            method="SLSQP",
            bounds=list(zip(lower, upper)),
            constraints=[{"type": "eq", "fun": lambda weights: float(weights.sum()) - stock_total}],
            options={"maxiter": 500, "ftol": 1e-12},
        )
        minimum_reachable = turnover(nearest.x if nearest.success else start)
        if max_turnover + 1e-7 < minimum_reachable:
            raise OptimizationError(f"Maximum turnover is infeasible; at least {minimum_reachable:.6f} is required")
        start = nearest.x

    penalty = float(transaction_cost_penalty)
    risk_free = float(risk_free_rate)

    def portfolio_variance(weights):
        return max(float(weights @ covariance @ weights), 1e-16)

    if method == "MINIMUM_VARIANCE":
        objective = lambda weights: portfolio_variance(weights) + penalty * float(np.abs(weights - current).sum())
    elif method == "MAXIMUM_SHARPE":
        objective = lambda weights: -((float(expected_returns @ weights) - risk_free) / np.sqrt(portfolio_variance(weights))) + penalty * float(np.abs(weights - current).sum())
    else:
        raise OptimizationError("Unsupported optimization method")

    constraints = [{"type": "eq", "fun": lambda weights: float(weights.sum()) - stock_total}]
    if max_turnover < 10:
        constraints.append({"type": "ineq", "fun": lambda weights: max_turnover - turnover(weights)})
    result = _scipy_optimize.minimize(
        objective,
        start,
        method="SLSQP",
        bounds=list(zip(lower, upper)),
        constraints=constraints,
        options={"maxiter": 1000, "ftol": 1e-12},
    )
    if not result.success:
        raise OptimizationError(f"Optimizer failed: {result.message}")
    weights = np.asarray(result.x, dtype=float)
    weights[np.abs(weights) < 1e-12] = 0
    if abs(float(weights.sum()) - stock_total) > 1e-6 or turnover(weights) > max_turnover + 1e-6:
        raise OptimizationError("Optimizer returned weights that violate configured constraints")
    variance = portfolio_variance(weights)
    expected_return = float(expected_returns @ weights)
    volatility = float(np.sqrt(variance))
    sharpe = (expected_return - risk_free) / volatility if volatility else 0.0
    marginal = covariance @ weights
    risk_contributions = weights * marginal / volatility if volatility else np.zeros(count)
    return {
        "weights": weights,
        "covariance": covariance,
        "objective_value": float(result.fun),
        "expected_return": expected_return,
        "expected_volatility": volatility,
        "sharpe_ratio": sharpe,
        "turnover": turnover(weights),
        "risk_contributions": risk_contributions,
        "solver_status": str(result.message),
    }


def universe_instruments(universe):
    instrument_map = {
        item.instrument_id: item.instrument
        for item in universe.memberships.filter(enabled=True, instrument__active=True, instrument__tradable=True, instrument__asset_class="STK").select_related("instrument")
    }
    if universe.include_strategy_instruments:
        allocations = StrategyAllocation.objects.filter(
            portfolio=universe.portfolio, strategy_instance__enabled=True, strategy_instance__kill_switch=False
        ).select_related("strategy_instance__instrument")
        for allocation in allocations:
            instance = allocation.strategy_instance
            if instance.instrument.active and instance.instrument.tradable and instance.instrument.asset_class == "STK":
                instrument_map[instance.instrument_id] = instance.instrument
    if len(instrument_map) > universe.maximum_instruments:
        raise UniverseSizeError(len(instrument_map), universe.maximum_instruments)
    return [instrument_map[key] for key in sorted(instrument_map)]


def _load_prices(instruments, *, lookback_days, minimum_observations, refresh_history, minimum_instruments=2):
    end_date = timezone.now().date()
    calendar_days = max(lookback_days * 2, minimum_observations * 2)
    start_date = end_date - timedelta(days=calendar_days)
    warnings = []
    if refresh_history:
        for instrument in instruments:
            rows = InstrumentPriceHistory.objects.filter(
                instrument=instrument, provider="FINNHUB", trading_date__gte=start_date
            )
            latest = rows.order_by("-trading_date").first()
            if rows.count() < minimum_observations + 1 or not latest or latest.trading_date < end_date - timedelta(days=5):
                fetch_daily_history(instrument, start_date, end_date, purpose="OPTIMIZATION")
    series = {}
    excluded = []
    for instrument in instruments:
        prices = list(
            InstrumentPriceHistory.objects.filter(instrument=instrument, provider="FINNHUB")
            .order_by("-trading_date")[: lookback_days + 1]
            .values_list("trading_date", "adjusted_close", "close")
        )
        mapping = {date: float(adjusted if adjusted is not None else close) for date, adjusted, close in prices if (adjusted or close) and float(adjusted if adjusted is not None else close) > 0}
        if len(mapping) < minimum_observations + 1:
            excluded.append({"instrument_id": instrument.pk, "symbol": instrument.symbol, "reason": "INSUFFICIENT_HISTORY", "observations": len(mapping)})
        else:
            series[instrument.pk] = mapping
    if excluded:
        warnings.append({"code": "INSTRUMENTS_EXCLUDED", "instruments": excluded})
    if len(series) < minimum_instruments:
        noun = "instrument" if minimum_instruments == 1 else "instruments"
        raise OptimizationError(f"At least {minimum_instruments} universe {noun} need sufficient aligned price history")
    common_dates = sorted(set.intersection(*(set(values) for values in series.values())))
    if len(common_dates) < minimum_observations + 1:
        raise OptimizationError(f"Only {max(len(common_dates) - 1, 0)} aligned returns are available; {minimum_observations} are required")
    common_dates = common_dates[-(lookback_days + 1):]
    instrument_ids = sorted(series)
    matrix = np.asarray([[series[instrument_id][date] for instrument_id in instrument_ids] for date in common_dates], dtype=float)
    returns = matrix[1:] / matrix[:-1] - 1.0
    if not np.all(np.isfinite(returns)):
        raise OptimizationError("Historical price returns contain invalid values")
    return instrument_ids, common_dates, matrix, returns, warnings


def _current_weights(portfolio, nav, instrument_ids, available_cash=None):
    from apps.market_data.pricing import effective_position_price
    positions = PortfolioPosition.objects.filter(portfolio=portfolio).select_related("instrument__market_state")
    result = {}
    outside = D(0)
    for position in positions:
        price,_,_=effective_position_price(position)
        value = D(position.quantity) * price * D(position.instrument.multiplier or 1)
        weight = value / nav if nav else D(0)
        if position.instrument_id in instrument_ids:
            result[position.instrument_id] = weight
        else:
            outside += abs(weight)
    cash = D(str(available_cash if available_cash is not None else portfolio.account.available_cash)) / nav
    return result, cash, outside


def _policy_snapshot(policy):
    return {
        "version": policy.version,
        "method": policy.method,
        "lookback_days": policy.lookback_days,
        "return_estimation": policy.return_estimation,
        "covariance_estimation": policy.covariance_estimation,
        "risk_free_rate": str(policy.risk_free_rate),
        "target_cash_weight": str(policy.target_cash_weight),
        "minimum_weight": str(policy.minimum_weight),
        "maximum_weight": str(policy.maximum_weight),
        "maximum_turnover": str(policy.maximum_turnover),
        "transaction_cost_penalty": str(policy.transaction_cost_penalty),
        "long_only": policy.long_only,
        "execution_mode": policy.execution_mode,
    }


def optimize_explicit_universe(
    instruments,
    *,
    method,
    cash_weight,
    maximum_weight,
    lookback_days=252,
    minimum_history_observations=60,
    minimum_weight=0,
    maximum_turnover=10,
    transaction_cost_penalty=0,
    risk_free_rate=0,
    long_only=True,
    refresh_history=True,
    portfolio=None,
    nav=None,
    available_cash=None,
    current_weights=None,
    current_cash_weight=0,
    external_current_weight=0,
):
    """Reusable Markowitz core for an explicit stock universe and resolved policy values."""
    instruments = list(instruments)
    if len(instruments) < 2:
        raise OptimizationError("At least two instruments are required for Markowitz optimization")
    instrument_ids, dates, prices, returns, warnings = _load_prices(
        instruments,
        lookback_days=lookback_days,
        minimum_observations=minimum_history_observations,
        refresh_history=refresh_history,
    )
    expected_returns = returns.mean(axis=0) * TRADING_DAYS
    covariance = np.cov(returns, rowvar=False, ddof=1) * TRADING_DAYS
    if portfolio is not None:
        if nav is None:
            raise OptimizationError("Portfolio NAV is required when resolving current weights")
        current, current_cash, outside = _current_weights(portfolio, D(str(nav)), instrument_ids, available_cash)
    else:
        current = {int(key): D(str(value)) for key, value in (current_weights or {}).items()}
        current_cash = D(str(current_cash_weight))
        outside = D(str(external_current_weight))
    current_vector = np.asarray([float(current.get(instrument_id, 0)) for instrument_id in instrument_ids])
    solved = solve_markowitz(
        expected_returns=expected_returns,
        covariance=covariance,
        current_weights=current_vector,
        method=method,
        cash_weight=cash_weight,
        current_cash_weight=current_cash,
        minimum_weight=minimum_weight,
        maximum_weight=maximum_weight,
        maximum_turnover=maximum_turnover,
        transaction_cost_penalty=transaction_cost_penalty,
        risk_free_rate=risk_free_rate,
        long_only=long_only,
        external_current_weight=outside,
    )
    return {
        **solved,
        "instrument_ids": instrument_ids,
        "dates": dates,
        "prices": prices,
        "returns": returns,
        "expected_returns": expected_returns,
        "warnings": warnings,
        "current_weights": current,
        "current_cash_weight": current_cash,
        "external_current_weight": outside,
    }


def calculate_weighted_metrics(
    instruments,
    target_weights,
    *,
    lookback_days=252,
    minimum_history_observations=60,
    risk_free_rate=0,
    refresh_history=False,
):
    """Calculate annualized metrics for fixed long-only stock weights without optimizing them."""
    instruments = [item for item in instruments if D(str(target_weights.get(item.pk, 0))) > 0]
    if not instruments:
        return {
            "expected_return": 0.0,
            "expected_volatility": 0.0,
            "sharpe_ratio": 0.0,
            "expected_return_contributions": {},
            "risk_contributions": {},
            "warnings": [],
        }
    instrument_ids, _, _, returns, warnings = _load_prices(
        instruments,
        lookback_days=lookback_days,
        minimum_observations=minimum_history_observations,
        refresh_history=refresh_history,
        minimum_instruments=1,
    )
    expected_returns = returns.mean(axis=0) * TRADING_DAYS
    if len(instrument_ids) == 1:
        covariance = np.asarray([[float(np.var(returns[:, 0], ddof=1) * TRADING_DAYS)]])
    else:
        covariance = np.cov(returns, rowvar=False, ddof=1) * TRADING_DAYS
    covariance = np.atleast_2d(covariance)
    weights = np.asarray([float(target_weights[instrument_id]) for instrument_id in instrument_ids])
    expected_return = float(expected_returns @ weights)
    variance = max(float(weights @ covariance @ weights), 0.0)
    volatility = float(np.sqrt(variance))
    sharpe = (expected_return - float(risk_free_rate)) / volatility if volatility else 0.0
    marginal = covariance @ weights
    risk = weights * marginal / volatility if volatility else np.zeros(len(weights))
    return {
        "expected_return": expected_return,
        "expected_volatility": volatility,
        "sharpe_ratio": sharpe,
        "expected_return_contributions": {
            instrument_id: float(weights[index] * expected_returns[index])
            for index, instrument_id in enumerate(instrument_ids)
        },
        "risk_contributions": {
            instrument_id: float(risk[index]) for index, instrument_id in enumerate(instrument_ids)
        },
        "warnings": warnings,
    }


def run_optimization(portfolio, idempotency_key, *, trigger="MANUAL", nav=None, available_cash=None,
                     refresh_history=True, flow_reference="", retry_failed=False, defer=False,stored_request_hash=None):
    request_hash=stored_request_hash or canonical_request_hash("portfolio_optimization",{
        "portfolio_id":portfolio.pk,"trigger":trigger,"nav":nav,"available_cash":available_cash,
        "refresh_history":refresh_history,"flow_reference":flow_reference})
    existing = PortfolioOptimizationRun.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        try:require_matching_request(existing.request_hash,request_hash)
        except ValueError as exc:raise OptimizationError(str(exc)) from exc
        if not existing.request_hash:
            existing.request_hash=request_hash;existing.save(update_fields=["request_hash"])
        if existing.status in {"QUEUED","DISPATCHED"}:
            run=existing
        elif existing.status!="FAILED":
            return existing
        elif not retry_failed:
            return existing
        elif not existing.retryable:
            raise OptimizationError("Failed optimization is not retryable")
        else:
            with transaction.atomic():
                run=PortfolioOptimizationRun.objects.select_for_update().get(pk=existing.pk)
                run.targets.all().delete()
                run.status="QUEUED" if defer else "CALCULATING";run.retryable=False;run.last_error="";run.error_details={}
                run.solver_status="";run.completed_at=None;run.attempt_count+=1
                run.save(update_fields=["status","retryable","last_error","error_details","solver_status","completed_at","attempt_count"])
                OperationAttempt.objects.create(operation_type="PORTFOLIO_OPTIMIZATION",operation_id=str(run.pk),
                    attempt_number=run.attempt_count,request_hash=run.request_hash,status="QUEUED" if defer else "PROCESSING")
    else:
        run=None
    policy = run.policy if run is not None else PortfolioOptimizationPolicy.objects.filter(portfolio=portfolio, enabled=True).first()
    universe = run.universe if run is not None else PortfolioUniverse.objects.filter(portfolio=portfolio, enabled=True).first()
    if not policy or not universe:
        raise OptimizationError("An enabled portfolio universe and optimization policy are required")
    expected_mode = execution_mode_for_portfolio(portfolio)
    if policy.execution_mode != expected_mode:
        raise OptimizationError(
            "Optimization execution mode must match the portfolio Gateway session"
        )
    nav = D(str(nav if nav is not None else portfolio.account.net_liquidation))
    if nav <= 0:
        raise OptimizationError("Portfolio NAV must be positive")
    if run is None:
        run,created = PortfolioOptimizationRun.objects.get_or_create(idempotency_key=idempotency_key,defaults={
            "portfolio":portfolio,"policy":policy,"universe":universe,"request_hash":request_hash,"trigger":trigger,
            "nav":nav,"cash_weight":policy.target_cash_weight,"policy_snapshot":_policy_snapshot(policy),
            "flow_reference":flow_reference,"status":"QUEUED" if defer else "CALCULATING"})
        if not created:
            try:require_matching_request(run.request_hash,request_hash)
            except ValueError as exc:raise OptimizationError(str(exc)) from exc
            return run
        OperationAttempt.objects.create(operation_type="PORTFOLIO_OPTIMIZATION",operation_id=str(run.pk),
            attempt_number=run.attempt_count,request_hash=run.request_hash,status="QUEUED" if defer else "PROCESSING")
    if defer:
        return run
    if run.status in {"QUEUED","DISPATCHED"}:
        run.status="CALCULATING";run.save(update_fields=["status"])
        OperationAttempt.objects.filter(operation_type="PORTFOLIO_OPTIMIZATION",operation_id=str(run.pk),
            attempt_number=run.attempt_count,status="QUEUED").update(status="PROCESSING")
    try:
        instruments = universe_instruments(universe)
        if len(instruments) < 2:
            raise OptimizationError("Select at least two active stock instruments in the portfolio universe")
        solved = optimize_explicit_universe(
            instruments,
            method=policy.method,
            cash_weight=policy.target_cash_weight,
            lookback_days=policy.lookback_days,
            minimum_history_observations=universe.minimum_history_observations,
            minimum_weight=policy.minimum_weight,
            maximum_weight=policy.maximum_weight,
            maximum_turnover=policy.maximum_turnover,
            transaction_cost_penalty=policy.transaction_cost_penalty,
            risk_free_rate=policy.risk_free_rate,
            long_only=policy.long_only,
            refresh_history=refresh_history,
            portfolio=portfolio,
            nav=nav,
            available_cash=available_cash,
        )
        instrument_ids = solved["instrument_ids"]
        dates = solved["dates"]
        returns = solved["returns"]
        expected_returns = solved["expected_returns"]
        warnings = solved["warnings"]
        current = solved["current_weights"]
        current_cash = solved["current_cash_weight"]
        outside = solved["external_current_weight"]
        instrument_map = {instrument.pk: instrument for instrument in instruments}
        with transaction.atomic():
            for rank, (instrument_id, weight) in enumerate(sorted(zip(instrument_ids, solved["weights"]), key=lambda item: -item[1])):
                index = instrument_ids.index(instrument_id)
                current_weight = current.get(instrument_id, D(0))
                constraint = ""
                if abs(float(weight) - float(policy.minimum_weight)) < 1e-6:
                    constraint = "MINIMUM_WEIGHT"
                elif abs(float(weight) - float(policy.maximum_weight)) < 1e-6:
                    constraint = "MAXIMUM_WEIGHT"
                OptimizedPortfolioTarget.objects.create(
                    optimization_run=run,
                    instrument=instrument_map[instrument_id],
                    current_weight=D(str(float(current_weight))),
                    optimized_weight=D(str(float(weight))),
                    weight_change=D(str(float(weight) - float(current_weight))),
                    target_value=D(str(float(weight))) * nav,
                    expected_return_contribution=D(str(float(weight * expected_returns[index]))),
                    risk_contribution=D(str(float(solved["risk_contributions"][index]))),
                    constraint_status=constraint,
                    rank=rank,
                )
            run.input_start_date = dates[0]
            run.input_end_date = dates[-1]
            run.current_weights = {str(key): str(value) for key, value in current.items()} | {"cash": str(current_cash), "outside_universe": str(outside)}
            run.expected_returns = {str(instrument_id): str(float(expected_returns[index])) for index, instrument_id in enumerate(instrument_ids)}
            run.covariance_snapshot = {"instrument_ids": instrument_ids, "matrix": _json_matrix(solved["covariance"]), "observations": len(returns)}
            run.constraints_snapshot = {
                "stock_weight_total": str(D(1) - D(policy.target_cash_weight)),
                "minimum_weight": str(policy.minimum_weight),
                "maximum_weight": str(policy.maximum_weight),
                "maximum_turnover": str(policy.maximum_turnover),
                "long_only": policy.long_only,
            }
            run.solver_status = solved["solver_status"]
            run.objective_value = D(str(solved["objective_value"]))
            run.expected_return = D(str(solved["expected_return"]))
            run.expected_volatility = D(str(solved["expected_volatility"]))
            run.sharpe_ratio = D(str(solved["sharpe_ratio"]))
            run.turnover = D(str(solved["turnover"]))
            run.warnings = warnings
            run.status = "COMPLETED"
            run.completed_at = timezone.now()
            run.save()
            OutboxEvent.objects.create(
                topic="portfolio.optimization.completed.v1",
                event_type="portfolio.optimization.completed",
                aggregate_type="portfolio",
                aggregate_id=str(portfolio.pk),
                partition_key=str(portfolio.pk),
                payload={"optimization_run_id": run.pk, "method": policy.method, "turnover": str(run.turnover)},
                idempotency_key=f"optimization:{run.pk}:completed",
            )
            AuditEvent.objects.create(
                event_type="portfolio.optimization.completed",
                actor="system",
                aggregate_type="portfolio",
                aggregate_id=str(portfolio.pk),
                data={"optimization_run_id": run.pk, "trigger": trigger, "policy_version": policy.version},
                idempotency_key=f"audit:optimization:{run.pk}:completed",
            )
            OperationAttempt.objects.filter(operation_type="PORTFOLIO_OPTIMIZATION",operation_id=str(run.pk),
                attempt_number=run.attempt_count).update(status="COMPLETED",result={"optimization_run_id":run.pk},
                completed_at=timezone.now())
        return run
    except Exception as exc:
        run.status = "FAILED"
        run.solver_status = "FAILED"
        run.error_details = {"message": str(exc), "type": exc.__class__.__name__}
        run.last_error = str(exc)[:1000]
        run.retryable = not isinstance(exc, (OptimizationError, ValueError))
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "solver_status", "error_details", "last_error", "retryable", "completed_at"])
        OperationAttempt.objects.filter(operation_type="PORTFOLIO_OPTIMIZATION",operation_id=str(run.pk),
            attempt_number=run.attempt_count).update(status="FAILED",retryable=run.retryable,
            error=run.last_error,completed_at=run.completed_at)
        if isinstance(exc, OptimizationError):
            raise
        raise OptimizationError(str(exc)) from exc


def latest_prices(optimization_run):
    result = {}
    for target in optimization_run.targets.select_related("instrument"):
        price = InstrumentPriceHistory.objects.filter(
            instrument=target.instrument, provider="FINNHUB"
        ).order_by("-trading_date").first()
        if not price:
            raise OptimizationError(f"No historical reference price is available for {target.instrument.symbol}")
        result[target.instrument_id] = price.adjusted_close or price.close
    from apps.market_data.pricing import effective_position_price
    for position in PortfolioPosition.objects.filter(portfolio=optimization_run.portfolio).select_related("instrument__market_state"):
        price,_,_=effective_position_price(position)
        if position.instrument_id not in result and price > 0:
            result[position.instrument_id] = price
    return result


def plan_optimized_rebalance(
    optimization_run,
    idempotency_key,
    *,
    mode=None,
    run_type=RunType.PREVIEW,
    strict_market_state=False,
    available_cash=None,
    prices=None,
):
    if optimization_run.status != "COMPLETED":
        raise OptimizationError("Only a completed optimization run can create rebalance targets")
    from apps.rebalancing.services import plan_rebalance

    return plan_rebalance(
        optimization_run.portfolio,
        optimization_run.trigger,
        idempotency_key,
        prices=prices or latest_prices(optimization_run),
        nav=optimization_run.nav,
        available_cash=available_cash,
        mode=mode,
        run_type=run_type,
        strict_market_state=strict_market_state,
        optimization_run=optimization_run,
    )


@transaction.atomic
def apply_optimization_run(
    optimization_run,
    idempotency_key,
    *,
    mode=None,
    strict_market_state=True,
):
    run_id = optimization_run.pk if isinstance(optimization_run, PortfolioOptimizationRun) else optimization_run
    run = (
        PortfolioOptimizationRun.objects.select_for_update(of=("self",))
        .select_related("portfolio__account", "policy", "universe", "applied_rebalance")
        .get(pk=run_id)
    )
    if run.status != "COMPLETED":
        raise OptimizationError("Only a completed optimization run can be applied")
    if run.policy.portfolio_id != run.portfolio_id or run.universe.portfolio_id != run.portfolio_id:
        raise OptimizationError("Optimization run, policy, universe, and portfolio do not belong together")
    if not run.policy.enabled or not run.universe.enabled:
        raise OptimizationError(
            "Optimization policy and universe must still be enabled at apply time"
        )
    mode = require_portfolio_execution_ready(run.portfolio)
    if run.policy.execution_mode != mode:
        raise OptimizationError(
            "Optimization policy mode no longer matches the portfolio Gateway session"
        )
    if run.applied_rebalance_id:
        if run.application_idempotency_key == idempotency_key:
            return run, run.applied_rebalance, False
        raise OptimizationAlreadyApplied(run)

    run.application_status = "APPLYING"
    run.application_idempotency_key = idempotency_key
    run.save(update_fields=["application_status", "application_idempotency_key"])
    rebalance = plan_optimized_rebalance(
        run,
        f"{idempotency_key}:rebalance",
        mode=mode,
        run_type=RunType.EXECUTION,
        strict_market_state=strict_market_state,
    )
    if rebalance.optimization_run_id != run.pk:
        raise OptimizationError("Idempotency-Key was already used for a different optimization application")
    run.applied_rebalance = rebalance
    run.applied_at = timezone.now()
    run.application_status = "APPLIED"
    run.save(update_fields=["applied_rebalance", "applied_at", "application_status"])
    return run, rebalance, True
