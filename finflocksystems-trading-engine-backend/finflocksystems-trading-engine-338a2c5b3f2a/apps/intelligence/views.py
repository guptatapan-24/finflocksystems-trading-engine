from django.conf import settings
from django.utils.dateparse import parse_datetime

from apps.core.views import method_guard, response

from . import conf
from .api_utils import error, staff_required
from .guard import intel_row_counts
from .llm.circuit import breaker_for
from .llm.runner import spent_today_usd
from .models import TradeEpisode

MAX_LIMIT = 200


def _extras():
    try:
        from .services.ops import health_extras

        return health_extras()
    except Exception:
        return None


def _context_health():
    """Outside-context sources at a glance (never raises, never part of readiness)."""
    try:
        from .services.daily_brief import source_states

        return {"enabled": conf.context_enabled(), "sources": source_states()}
    except Exception:
        return {"enabled": False, "sources": []}


@staff_required
def health(request):
    """Intelligence health. Deliberately NOT part of execution readiness (R2)."""
    invalid = method_guard(request, "GET")
    if invalid:
        return invalid
    provider = conf.provider()
    return response({
        "enabled": conf.enabled(),
        "provider": provider,
        "models": {"review_configured": bool(conf.model_for("review")),
                   "deep_configured": bool(conf.model_for("deep"))},
        "api_key_configured": (bool(getattr(settings, "QCH_API_KEY", "")) and bool(getattr(settings, "QCH_BASE_URL", ""))
                               if provider == "qch" else None),
        "prices_configured": bool(str(getattr(settings, "INTELLIGENCE_PRICE_OUTPUT_PER_MTOK_USD", "0") or "0") != "0"),
        "budget": {"daily_usd": str(conf.daily_budget_usd()), "spent_today_usd": str(spent_today_usd())},
        "circuit_breaker": breaker_for(provider).state,
        "limits": {"max_input_tokens": conf.max_input_tokens(), "max_output_tokens": conf.max_output_tokens()},
        "rows": intel_row_counts(),
        "operations": _extras(),
        "context": _context_health(),
    })


def _episode_row(episode, detail=False):
    row = {
        "id": episode.pk, "strategy_key": episode.strategy_key, "instance_id": episode.instance_id,
        "instance_name": episode.instance_name, "strategy_version": episode.strategy_version,
        "instrument": episode.instrument_symbol, "asset_family": episode.asset_family,
        "execution_mode": episode.execution_mode, "currency": episode.currency, "direction": episode.direction,
        "status": episode.status, "opened_at": episode.opened_at, "closed_at": episode.closed_at,
        "gross_pnl": str(episode.gross_pnl), "commissions": str(episode.commissions), "net_pnl": str(episode.net_pnl),
        "pnl_basis": episode.pnl_basis,
        "return_on_risk": None if episode.return_on_risk is None else str(episode.return_on_risk),
        "hold_seconds": episode.hold_seconds, "exit_reason": episode.exit_reason,
        "exit_subtype": episode.exit_subtype, "data_quality_flags": episode.data_quality_flags,
        "fill_count": episode.fill_count,
    }
    if detail:
        metrics = getattr(episode, "metrics_row", None)
        snapshot = getattr(episode, "evidence_snapshot", None)
        row["metrics"] = metrics.metrics if metrics else {}
        row["metrics_version"] = metrics.metrics_version if metrics else None
        row["evidence_hash"] = snapshot.evidence_hash if snapshot else None
        row["legs"] = [{
            "symbol": leg.symbol, "right": leg.right, "strike": None if leg.strike is None else str(leg.strike),
            "expiration": leg.expiration, "entry_side": leg.entry_side, "entry_quantity": str(leg.entry_quantity),
            "entry_avg_price": str(leg.entry_avg_price), "exit_quantity": str(leg.exit_quantity),
            "exit_avg_price": str(leg.exit_avg_price), "realized_pnl": str(leg.realized_pnl),
            "commission": str(leg.commission),
            "entry_slippage_bps": None if leg.entry_slippage_bps is None else str(leg.entry_slippage_bps),
            "exit_slippage_bps": None if leg.exit_slippage_bps is None else str(leg.exit_slippage_bps),
        } for leg in episode.legs.all().order_by("pk")]
    return row


@staff_required
def episodes(request, episode_id=None):
    invalid = method_guard(request, "GET")
    if invalid:
        return invalid
    if episode_id is not None:
        episode = (TradeEpisode.objects.filter(pk=episode_id).select_related("metrics_row", "evidence_snapshot")
                   .prefetch_related("legs").first())
        if episode is None:
            return error(404, "EPISODE_NOT_FOUND", "Trade episode not found")
        return response(_episode_row(episode, detail=True))
    query = TradeEpisode.objects.all()
    for field in ("instance_id", "status", "currency", "asset_family", "execution_mode", "strategy_key"):
        value = request.GET.get(field)
        if value:
            try:
                query = query.filter(**{field: int(value) if field == "instance_id" else value})
            except ValueError:
                return error(400, "INVALID_FILTER", f"Invalid {field}")
    since = request.GET.get("since")
    if since:
        parsed = parse_datetime(since)
        if parsed is None:
            return error(400, "INVALID_FILTER", "since must be an ISO-8601 datetime")
        query = query.filter(opened_at__gte=parsed)
    try:
        limit = min(max(int(request.GET.get("limit", 50)), 1), MAX_LIMIT)
        offset = max(int(request.GET.get("offset", 0)), 0)
    except ValueError:
        return error(400, "INVALID_PAGINATION", "limit and offset must be integers")
    total = query.count()
    rows = [_episode_row(item) for item in query.order_by("-opened_at", "-pk")[offset:offset + limit]]
    return response(rows, meta={"limit": limit, "offset": offset, "total": total})
