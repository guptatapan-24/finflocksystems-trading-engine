from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import timedelta

import numpy as np
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ..models import (
    CrossSectionalFeatureSnapshot,
    EventFeatureSnapshot,
    InstrumentFeatureSnapshot,
    MarketRegimeSnapshot,
    ResearchAnalystFact,
    ResearchCorporateAction,
    ResearchDailyBar,
    ResearchEvent,
    ResearchFundamentalFact,
)
from .artifacts import FilesystemArtifactStore


FEATURE_VERSION = "common-daily-v1"


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _latest_bars(instrument_id, as_of_date, limit=300):
    seen, rows = set(), []
    query = ResearchDailyBar.objects.filter(
        instrument_id=instrument_id, trading_date__lte=as_of_date, quality_status="VALID",
    ).order_by("-trading_date", "-data_version")
    for row in query:
        if row.trading_date in seen:
            continue
        seen.add(row.trading_date); rows.append(row)
        if len(rows) >= limit:
            break
    return list(reversed(rows))


def _instrument_features(rows):
    close = np.asarray([float(row.adjusted_close) for row in rows], dtype=float)
    volume = np.asarray([float(row.volume) for row in rows], dtype=float)
    if len(close) < 2:
        return {}
    returns = np.diff(np.log(close))
    result = {
        "adjusted_close": float(close[-1]), "return_1d": float(close[-1] / close[-2] - 1),
        "median_dollar_volume_20d": float(np.median((close * volume)[-20:])),
        "realized_volatility": float(np.std(returns[-60:], ddof=1) * np.sqrt(252)) if len(returns) > 1 else 0,
        "proximity": float(close[-1] / np.max(close[-min(252, len(close)):])),
    }
    for horizon in (5, 20, 21, 63, 126, 252):
        if len(close) > horizon:
            result[f"return_{horizon}d"] = float(close[-1] / close[-horizon - 1] - 1)
    result["formation_return"] = result.get("return_252d", result.get("return_63d", result["return_1d"]))
    result["risk_adjusted_momentum"] = result["formation_return"] / max(result["realized_volatility"], 1e-9)
    peak = np.maximum.accumulate(close)
    result["maximum_drawdown"] = abs(float(np.min(close / peak - 1)))
    return result


def _normalise_metric(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _safe_ratio(numerator, denominator):
    if numerator is None or denominator in (None, 0):
        return None
    return float(numerator) / abs(float(denominator))


def _point_in_time_fundamental_features(issuer_id, instrument_id, as_of_date, decision_timestamp, price):
    rows = ResearchFundamentalFact.objects.filter(
        issuer_id=issuer_id, period_end__lte=as_of_date,
        public_availability_timestamp__lte=decision_timestamp, revision_timestamp__lte=decision_timestamp,
    ).order_by("metric", "-period_end", "-revision_version", "-data_version")
    by_metric = defaultdict(list); seen = set(); provenance = []
    for row in rows:
        key = _normalise_metric(row.metric)
        identity = (key, row.period_end)
        if identity in seen:
            continue
        seen.add(identity); by_metric[key].append(float(row.value))
        provenance.append((row.pk, row.data_version, row.revision_version))

    def pick(patterns, position=0):
        for pattern in patterns:
            for key, values in by_metric.items():
                if pattern in key and len(values) > position:
                    return values[position]
        return None

    revenue = pick(("revenues", "revenue", "salesrevenue")); previous_revenue = pick(("revenues", "revenue", "salesrevenue"), 1)
    income = pick(("netincomeloss", "netincome")); previous_income = pick(("netincomeloss", "netincome"), 1)
    assets = pick(("assets",)); previous_assets = pick(("assets",), 1)
    equity = pick(("stockholdersequity", "shareholdersequity", "equity"))
    gross_profit = pick(("grossprofit",)); operating_income = pick(("operatingincomeloss", "operatingincome"))
    operating_cash = pick(("netcashprovidedbyusedinoperatingactivities", "operatingcashflow"))
    capex = pick(("paymentstoacquirepropertyplantandequipment", "capitalexpenditure"))
    dividends = pick(("paymentsofdividends", "dividendspaid")); debt = pick(("longtermdebt", "debtcurrent"))
    cash = pick(("cashandcashequivalents", "cashcashequivalents")); interest = pick(("interestexpense",))
    shares = pick(("weightedaveragenumberofshares", "commonstocksharesoutstanding", "sharesoutstanding"))
    previous_shares = pick(("weightedaveragenumberofshares", "commonstocksharesoutstanding", "sharesoutstanding"), 1)
    market_cap = float(price) * shares if shares and price else None
    free_cash_flow = operating_cash - abs(capex or 0) if operating_cash is not None else None
    total_dividends = abs(dividends) if dividends is not None else None
    invested_capital = (equity or 0) + (debt or 0) - (cash or 0) if equity is not None else None
    result = {}
    candidates = {
        "earnings_yield": _safe_ratio(income, market_cap), "book_to_market": _safe_ratio(equity, market_cap),
        "sales_to_price": _safe_ratio(revenue, market_cap), "fcf_yield": _safe_ratio(free_cash_flow, market_cap),
        "roe": _safe_ratio(income, equity), "roa": _safe_ratio(income, assets),
        "gross_profitability": _safe_ratio(gross_profit, assets), "operating_margin": _safe_ratio(operating_income, revenue),
        "roic": _safe_ratio(operating_income, invested_capital),
        "asset_growth": _safe_ratio((assets - previous_assets) if assets is not None and previous_assets is not None else None, previous_assets),
        "capex_growth": _safe_ratio(capex, revenue),
        "dividend_yield": _safe_ratio(total_dividends, market_cap),
        "payout_sustainability": 1 - min(1.0, _safe_ratio(total_dividends, income) or 0) if income and income > 0 else None,
        "payout_ratio": _safe_ratio(total_dividends, income), "fcf_coverage": _safe_ratio(free_cash_flow, total_dividends),
        "cash_conversion": _safe_ratio(operating_cash, income), "interest_coverage": _safe_ratio(operating_income, interest),
        "net_debt_to_ebitda": _safe_ratio((debt or 0) - (cash or 0), operating_income),
        "revenue_growth": _safe_ratio((revenue - previous_revenue) if revenue is not None and previous_revenue is not None else None, previous_revenue),
        "earnings_growth": _safe_ratio((income - previous_income) if income is not None and previous_income is not None else None, previous_income),
        "shares_outstanding_change": _safe_ratio((shares - previous_shares) if shares is not None and previous_shares is not None else None, previous_shares),
        "free_cash_flow_yield": _safe_ratio(free_cash_flow, market_cap),
        "net_buyback_yield": -_safe_ratio((shares - previous_shares) if shares is not None and previous_shares is not None else None, previous_shares) if shares is not None and previous_shares else None,
        "debt_paydown_yield": _safe_ratio(cash, market_cap),
    }
    result.update({key: value for key, value in candidates.items() if value is not None and np.isfinite(value)})
    dividend_actions = ResearchCorporateAction.objects.filter(
        instrument_id=instrument_id, action_type="DIVIDEND", effective_at__date__lte=as_of_date,
        effective_at__date__gte=as_of_date - timedelta(days=5 * 366), quality_status="VALID",
        revision_timestamp__lte=decision_timestamp,
    ).filter(Q(announced_at__isnull=True) | Q(announced_at__lte=decision_timestamp)).order_by("effective_at")
    annual = defaultdict(float)
    for action in dividend_actions:
        raw = (action.payload or {}).get("amount", (action.payload or {}).get("cash_amount"))
        try:
            annual[action.effective_at.year] += float(raw)
        except (TypeError, ValueError):
            continue
    if len(annual) >= 2:
        years = sorted(annual)
        first, last = annual[years[0]], annual[years[-1]]
        if first > 0 and last >= 0:
            growth = (last / first) ** (1 / max(1, years[-1] - years[0])) - 1
            result["dividend_growth"] = growth; result["dividend_growth_5y"] = growth
    analyst = ResearchAnalystFact.objects.filter(
        issuer_id=issuer_id, public_availability_timestamp__lte=decision_timestamp,
        revision_timestamp__lte=decision_timestamp,
    ).order_by("metric", "-event_timestamp", "-data_version")
    analyst_by_metric = defaultdict(list); analyst_provenance = []
    for fact in analyst:
        if fact.value is not None:
            analyst_by_metric[_normalise_metric(fact.metric)].append(float(fact.value))
            analyst_provenance.append((fact.pk, fact.data_version))
    revisions = [values[0] - values[1] for values in analyst_by_metric.values() if len(values) >= 2]
    estimates = [values[0] for values in analyst_by_metric.values() if values]
    if revisions:
        result["eps_revision_1m"] = float(np.mean(revisions)); result["eps_revision_3m"] = float(np.mean(revisions))
    if len(estimates) >= 2:
        result["estimate_dispersion"] = float(np.std(estimates, ddof=1))
    return result, provenance + analyst_provenance


def _cross_sectional_enrich(panel, return_series):
    if not panel:
        return
    count = min((len(values) for values in return_series.values()), default=0)
    market = None
    if count >= 20:
        matrix = np.asarray([values[-count:] for values in return_series.values()], dtype=float)
        market = np.mean(matrix, axis=0)
    sector_momentum = defaultdict(list)
    for row in panel:
        sector_momentum[row["sector"]].append(row.get("formation_return", 0))
    sector_means = {key: float(np.mean(values)) for key, values in sector_momentum.items()}
    median_volatility = float(np.median([row.get("realized_volatility", 0) for row in panel])) or 1.0
    for row in panel:
        row["trailing_return"] = row.get("formation_return", 0)
        row["absolute_momentum"] = row.get("formation_return", 0)
        row["relative_return"] = row.get("formation_return", 0) - sector_means.get(row["sector"], 0)
        row["residual_return"] = row["relative_return"]
        row["peer_residual_zscore"] = row["relative_return"] / max(row.get("realized_volatility", 0), 1e-9)
        row["idiosyncratic_volatility"] = row.get("realized_volatility", 0)
        row["low_volatility"] = -row.get("realized_volatility", 0)
        if market is not None:
            values = return_series[row["instrument_id"]][-count:]
            variance = float(np.var(market, ddof=1))
            row["beta"] = float(np.cov(values, market, ddof=1)[0, 1] / variance) if variance > 0 else 1.0
        value_fields = [row.get(key) for key in ("earnings_yield", "book_to_market", "fcf_yield") if row.get(key) is not None]
        quality_fields = [row.get(key) for key in ("roe", "roa", "gross_profitability") if row.get(key) is not None]
        if value_fields: row["value"] = float(np.mean(value_fields))
        if quality_fields: row["quality"] = float(np.mean(quality_fields))
        row["momentum"] = row.get("risk_adjusted_momentum", 0)
        row["within_sector_value"] = row.get("value", 0)
        row["within_sector_quality"] = row.get("quality", 0)
        row["within_sector_momentum"] = row.get("risk_adjusted_momentum", 0)


@transaction.atomic
def precompute_common_features(universe, *, as_of_date=None):
    as_of_date = as_of_date or timezone.localdate()
    store = FilesystemArtifactStore(settings.RESEARCH_ARTIFACT_ROOT)
    panel, written = [], 0
    data_versions, return_series = {}, {}
    decision_timestamp = timezone.now()
    members = universe.members.filter(active=True, instrument__isnull=False).select_related(
        "instrument", "issuer"
    ).prefetch_related("issuer__classifications__sub_industry_node__parent__parent__parent")
    for member in members:
        bars = _latest_bars(member.instrument_id, as_of_date)
        values = _instrument_features(bars)
        if not values:
            continue
        fundamentals, fundamental_versions = _point_in_time_fundamental_features(
            member.issuer_id, member.instrument_id, as_of_date, decision_timestamp, values["adjusted_close"],
        )
        values.update(fundamentals)
        classification = member.issuer.classifications.filter(taxonomy_version=universe.dataset_version).first()
        node = classification.sub_industry_node if classification else None
        industry = node.parent if node else None; group = industry.parent if industry else None; sector = group.parent if group else None
        data_version = _hash({"bars": [(row.trading_date, row.data_version, row.provider) for row in bars], "facts": fundamental_versions})
        row = {
            "instrument_id": member.instrument_id, "symbol": member.source_symbol,
            "sector": sector.code if sector else "", "industry": industry.code if industry else "",
            "sub_industry": node.code if node else "", **values,
        }
        panel.append(row)
        data_versions[member.instrument_id] = data_version
        close = np.asarray([float(item.adjusted_close) for item in bars], dtype=float)
        return_series[member.instrument_id] = np.diff(np.log(close))
    _cross_sectional_enrich(panel, return_series)
    panel_context = _hash([(row["instrument_id"], row.get("adjusted_close"), row.get("formation_return")) for row in panel])
    data_versions = {instrument_id: _hash({"instrument": version, "panel": panel_context}) for instrument_id, version in data_versions.items()}
    for row in panel:
        values = {key: value for key, value in row.items() if key not in {"instrument_id", "symbol", "sector", "industry", "sub_industry"}}
        snapshot, created = InstrumentFeatureSnapshot.objects.update_or_create(
            instrument_id=row["instrument_id"], feature_key="common_daily", frequency="1d", as_of_date=as_of_date,
            data_version=data_versions[row["instrument_id"]], implementation_version=FEATURE_VERSION,
            defaults={"available_at": timezone.now(), "value": values, "artifact_uri": ""},
        )
        written += int(created)
    artifact_uri = store.write_table(f"features/common_daily/date={as_of_date.isoformat()}/panel", panel)
    for row in panel:
        InstrumentFeatureSnapshot.objects.filter(
            instrument_id=row["instrument_id"], feature_key="common_daily", frequency="1d", as_of_date=as_of_date,
            implementation_version=FEATURE_VERSION,
        ).update(artifact_uri=artifact_uri)
    panel_version = panel_context
    CrossSectionalFeatureSnapshot.objects.update_or_create(
        universe=universe, feature_key="common_daily_panel", frequency="1d", as_of_date=as_of_date,
        data_version=panel_version, implementation_version=FEATURE_VERSION,
        defaults={"available_at": timezone.now(), "summary": {"instrument_count": len(panel)}, "artifact_uri": artifact_uri},
    )
    if panel:
        daily = np.asarray([row["return_1d"] for row in panel])
        volatility = float(np.std(daily, ddof=1) * np.sqrt(252)) if len(daily) > 1 else 0
        breadth = float(np.mean(daily > 0))
        regime = "CRISIS" if volatility > .40 else "STRESSED" if volatility > .25 else "CALM" if volatility < .12 else "NORMAL"
        MarketRegimeSnapshot.objects.update_or_create(
            universe=universe, as_of_date=as_of_date, data_version=panel_version, implementation_version=FEATURE_VERSION,
            defaults={"available_at": timezone.now(), "regime": regime, "features": {"cross_sectional_volatility": volatility, "breadth": breadth}},
        )
    event_written = 0
    for event in ResearchEvent.objects.filter(available_timestamp__date__lte=as_of_date, quality_status="VALID"):
        if event.available_timestamp > timezone.now():
            continue
        _, created = EventFeatureSnapshot.objects.update_or_create(
            event=event, instrument=event.instrument, feature_key="event_availability", as_of_date=as_of_date,
            data_version=str(event.data_version), implementation_version=FEATURE_VERSION,
            defaults={"available_at": event.available_timestamp, "value": {"event_type": event.event_type, "days_since_event": (as_of_date - event.effective_timestamp.date()).days}},
        )
        event_written += int(created)
    return {"instrument_features": written, "panel_instruments": len(panel), "event_features": event_written, "artifact_uri": artifact_uri}


def feature_rows_as_of(universe, decision_timestamp):
    """Point-in-time lookup; snapshots published later than the decision are excluded."""
    snapshots = InstrumentFeatureSnapshot.objects.filter(
        instrument__research_memberships__universe=universe,
        as_of_date__lte=decision_timestamp.date(), available_at__lte=decision_timestamp,
        feature_key="common_daily", implementation_version=FEATURE_VERSION,
    ).order_by("instrument_id", "-as_of_date")
    result = {}
    for snapshot in snapshots:
        result.setdefault(snapshot.instrument_id, snapshot.value)
    return result
