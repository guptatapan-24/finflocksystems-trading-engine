from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from apps.core.views import method_guard, response


def _portfolio(request):
    from apps.portfolios.models import TradingPortfolio

    query = TradingPortfolio.objects.select_related("account", "gateway_session")
    portfolio_id = request.GET.get("portfolio")
    return query.filter(pk=portfolio_id).first() if portfolio_id else query.order_by("pk").first()


def _account_row(account):
    if not account:
        return None
    return {
        "id": account.pk,
        "account_id": account.account_id,
        "alias": account.alias,
        "base_currency": account.base_currency,
        "net_liquidation": account.net_liquidation,
        "available_cash": account.available_cash,
        "buying_power": account.buying_power,
        "daily_pnl": account.daily_pnl,
        "is_reconciled": account.is_reconciled,
        "kill_switch": account.kill_switch,
        "updated_at": account.updated_at,
    }


def _portfolio_row(portfolio):
    if not portfolio:
        return None
    return {
        "id": portfolio.pk,
        "name": portfolio.name,
        "account_id": portfolio.account_id,
        "account": portfolio.account.account_id,
        "cash_buffer_pct": portfolio.cash_buffer_pct,
        "margin_buffer_pct": portfolio.margin_buffer_pct,
        "minimum_notional": portfolio.minimum_notional,
        "minimum_quantity": portfolio.minimum_quantity,
        "minimum_drift": portfolio.minimum_drift,
        "kill_switch": portfolio.kill_switch,
    }


def dashboard_summary(request):
    invalid=method_guard(request,"GET")
    if invalid:return invalid
    from apps.audit.models import AuditEvent
    from apps.broker_gateway.client import GatewayClient, GatewayError
    from apps.market_streams.models import InstrumentMarketState
    from apps.oms.models import Order
    from apps.portfolios.models import PortfolioPosition
    from apps.reconciliation.models import ReconciliationBreak
    from apps.risk.models import KillSwitch, RiskCheckResult
    from apps.strategies.models import StrategyInstance

    portfolio = _portfolio(request)
    account = portfolio.account if portfolio else None
    positions = (PortfolioPosition.objects.filter(portfolio=portfolio).select_related("instrument__market_state")
                 if portfolio else PortfolioPosition.objects.none())
    from apps.market_data.pricing import effective_position_price
    position_values=[
        Decimal(item.quantity) * effective_position_price(item)[0]
        * Decimal(item.instrument.multiplier or 1).normalize()
        for item in positions
    ]
    gross_exposure = sum((abs(value) for value in position_values), Decimal(0))
    net_exposure = sum(position_values, Decimal(0))
    order_query = Order.objects.all()
    strategy_query = StrategyInstance.objects.all()
    if portfolio:
        order_query = order_query.filter(intent__portfolio=portfolio)
        strategy_query = strategy_query.filter(portfolio=portfolio)
    open_statuses = ["CREATED", "RISK_APPROVED", "QUEUED", "BROKER_BLOCKED", "SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED", "CANCEL_PENDING", "UNKNOWN"]
    material_breaks = ReconciliationBreak.objects.filter(material=True, resolved=False)

    gateway = None
    gateway_error = None
    try:
        gateway = GatewayClient.for_portfolio(portfolio).health() if portfolio else None
    except GatewayError as exc:
        gateway_error = str(exc)

    attention = []
    if not gateway or not gateway.get("connected"):
        attention.append({"id": "gateway", "severity": "CRITICAL", "title": "IBKR Gateway is disconnected", "detail": gateway_error or "Reconnect the selected session and verify broker callbacks."})
    if account and (not account.is_reconciled or material_breaks.exists()):
        attention.append({"id": "reconciliation", "severity": "CRITICAL" if material_breaks.exists() else "WARNING", "title": "Broker state is not reconciled", "detail": f"{material_breaks.count()} unresolved material break(s)."})
    if KillSwitch.objects.filter(enabled=True).exists() or (portfolio and (portfolio.kill_switch or portfolio.account.kill_switch)):
        attention.append({"id": "kill-switch", "severity": "CRITICAL", "title": "A kill switch is active", "detail": "New executable actions remain ineligible until an authorized operator releases the switch."})
    stale_count = InstrumentMarketState.objects.exclude(status="FRESH").count()
    if stale_count:
        attention.append({"id": "stale-market", "severity": "WARNING", "title": "Market data requires attention", "detail": f"{stale_count} instrument(s) are stale or unavailable."})
    blocked_count = strategy_query.filter(state__in=["BLOCKED", "ERROR"]).count()
    if blocked_count:
        attention.append({"id": "blocked-strategies", "severity": "WARNING", "title": "Strategies are not ready", "detail": f"{blocked_count} strategy instance(s) are blocked or in error."})
    held_count = RiskCheckResult.objects.filter(decision__in=["HELD", "REJECTED"]).count()
    if held_count:
        attention.append({"id": "risk-decisions", "severity": "WARNING", "title": "Recent risk decisions need review", "detail": f"{held_count} persisted risk check(s) are held or rejected."})

    activity = [{
        "id": item.pk,
        "event_type": item.event_type,
        "actor": item.actor,
        "aggregate_type": item.aggregate_type,
        "aggregate_id": item.aggregate_id,
        "data": item.data,
        "created_at": item.created_at,
    } for item in AuditEvent.objects.order_by("-created_at")[:12]]
    reconciled = bool(account and account.is_reconciled and not material_breaks.exists())
    return response({
        "mode":portfolio.gateway_session.mode.upper() if portfolio and portfolio.gateway_session else "PAPER",
        "account": _account_row(account),
        "portfolio": _portfolio_row(portfolio),
        "gateway": gateway,
        "gateway_error": gateway_error,
        "reconciliation_status": "RECONCILED" if reconciled else "BLOCKED",
        "nav": account.net_liquidation if account else 0,
        "cash": account.available_cash if account else 0,
        "buying_power": account.buying_power if account else 0,
        "daily_pnl": account.daily_pnl if account else 0,
        "gross_exposure": gross_exposure,
        "net_exposure": net_exposure,
        "active_strategies": strategy_query.filter(enabled=True).count(),
        "open_orders": order_query.filter(status__in=open_statuses).count(),
        "positions": positions.count(),
        "recent_activity": activity,
        "attention": attention,
        "updated_at": timezone.now(),
    })


def portfolio_series(request):
    invalid=method_guard(request,"GET")
    if invalid:return invalid
    from apps.market_streams.models import MarketBar
    from apps.portfolios.models import PortfolioPosition

    portfolio = _portfolio(request)
    if not portfolio:
        return response(status=404, error={"code": "NOT_FOUND", "message": "Portfolio not found", "details": {}})
    positions = list(PortfolioPosition.objects.filter(portfolio=portfolio).select_related("instrument__market_state"))
    from apps.market_data.pricing import effective_position_price
    quantities = {item.instrument_id: Decimal(item.quantity) for item in positions}
    current_prices = {item.instrument_id: effective_position_price(item) for item in positions}
    multipliers = {
        item.instrument_id: Decimal(item.instrument.multiplier or 1).normalize()
        for item in positions
    }
    current_values = {
        item.instrument_id: Decimal(item.quantity) * current_prices[item.instrument_id][0]
        * multipliers[item.instrument_id]
        for item in positions
    }
    gross_current = sum((abs(value) for value in current_values.values()), Decimal(0))
    allocation = [{
        "instrument_id": item.instrument_id,
        "symbol": item.instrument.symbol,
        "value": float(current_values[item.instrument_id]),
        "price_provider":current_prices[item.instrument_id][1],"price_source":current_prices[item.instrument_id][2],
        "weight": float(abs(current_values[item.instrument_id]) / gross_current) if gross_current else 0,
    } for item in positions]

    raw_bars = list(MarketBar.objects.filter(instrument_id__in=quantities, is_final=True).order_by("-window_end", "-version")[:1200])
    latest_versions = {}
    for bar in raw_bars:
        key = (bar.instrument_id, bar.bar_id)
        if key not in latest_versions:
            latest_versions[key] = bar
    bars = sorted(latest_versions.values(), key=lambda item: item.window_end)
    latest_prices = {}
    points = []
    for bar in bars:
        latest_prices[bar.instrument_id] = Decimal(bar.close)
        if quantities and not all(instrument_id in latest_prices for instrument_id in quantities):
            continue
        values = [
            quantities[instrument_id] * latest_prices[instrument_id] * multipliers[instrument_id]
            for instrument_id in quantities
        ]
        net = sum(values, Decimal(0))
        gross = sum((abs(value) for value in values), Decimal(0))
        points.append({"time": bar.window_end, "estimated_nav": Decimal(portfolio.account.available_cash) + net, "gross": gross, "net": net})
    points = points[-240:]
    source = "POSTGRES_MARKET_BARS_WITH_CURRENT_HOLDINGS"
    if not points:
        now = portfolio.account.updated_at or timezone.now()
        points = [{"time": now, "estimated_nav": Decimal(portfolio.account.net_liquidation), "gross": gross_current, "net": sum(current_values.values(), Decimal(0))}]
        source = "CURRENT_BROKER_SNAPSHOT"
    else:
        offset = Decimal(portfolio.account.net_liquidation) - points[-1]["estimated_nav"]
        for point in points:
            point["estimated_nav"] += offset
    first_nav = points[0]["estimated_nav"]
    return response({
        "portfolio_id": portfolio.pk,
        "source": source,
        "nav": [{"time": point["time"], "value": float(point["estimated_nav"])} for point in points],
        "pnl": [{"time": point["time"], "value": float(point["estimated_nav"] - first_nav)} for point in points],
        "exposure": [{"time": point["time"], "gross": float(point["gross"]), "net": float(point["net"])} for point in points],
        "allocation_by_instrument": allocation,
    })
