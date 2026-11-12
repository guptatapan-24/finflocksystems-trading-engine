from __future__ import annotations

import hashlib
import importlib
import inspect
import json
from dataclasses import dataclass

from django.db import transaction

from apps.portfolio_construction.models import StrategyConstructionProfile
from apps.strategies.models import StrategyDefinition

from ..enums import ImplementationStatus, StrategyRole
from ..models import ResearchStrategyDefinition, ResearchStrategyImplementation


ALL_TIMEFRAMES = ("HURRY", "FAST", "BUILD", "GROW", "COMPOUND")
ALL_RISKS = (1, 2, 3, 4, 5)
IMPLEMENTATION_VERSION = "full-universe-v1"


@dataclass(frozen=True)
class StrategyRegistration:
    research_id: str
    implementation_path: str
    role: str
    frequency: tuple[str, ...]
    supported_direction: tuple[str, ...]
    data_requirements: tuple[str, ...]
    feature_requirements: tuple[str, ...]
    parameter_names: tuple[str, ...]
    parameter_budget: int
    compatible_timeframes: tuple[str, ...]
    compatible_risks: tuple[int, ...]
    runtime_mapping: str | None
    backtest_engine: str
    fallback_behavior: str
    implementation_version: str = IMPLEMENTATION_VERSION

    @property
    def parameter_schema(self):
        integer_names = {
            "max_names", "lookback_days", "fast_window", "slow_window", "entry_lookback", "exit_lookback",
            "window", "atr_window", "ema_window", "window_months", "signal", "fast", "slow", "vol_window",
            "formation_days", "skip_days", "names_per_sector", "holding_days", "max_holding_days",
            "holding_minutes", "max_holding_minutes", "regression_days", "event_horizon_days",
            "minimum_history_years", "smoothing_days", "max_half_life_days",
        }
        properties = {
            name: {"type": "integer" if name in integer_names else "number"}
            for name in self.parameter_names
        }
        for name in {"rebalance", "covariance", "linkage", "blend_weights", "sector_budget"}.intersection(properties):
            properties[name] = {}
        if "sector_neutral" in properties:
            properties["sector_neutral"] = {"type": "boolean"}
        return {"type": "object", "properties": properties, "additionalProperties": False}

    @property
    def implementation_hash(self):
        implementation = self.load()
        target = implementation if inspect.isclass(implementation) or inspect.isfunction(implementation) else implementation.__class__
        payload = json.dumps({
            "research_id": self.research_id,
            "path": self.implementation_path,
            "role": self.role,
            "parameters": self.parameter_names,
            "version": self.implementation_version,
            "source": inspect.getsource(target),
            "state": sorted(getattr(implementation, "__dict__", {}).items()),
        }, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    def load(self):
        module_name, attribute = self.implementation_path.rsplit(".", 1)
        return getattr(importlib.import_module(module_name), attribute)


BASELINE = ("BH_001", "EW_001", "SEC_EW_001")
ALLOCATION = ("INV_VOL_001", "RP_001", "MINVAR_001", "MAXDIV_001", "HRP_001", "CVaR_001", "BLEND_001")
TREND = (
    "TR_001_SMA_020_100", "TR_002_SMA_050_200", "TR_003_EMA_012_026", "TR_004_EMA_050_150",
    "TR_005_TRIPLE_MA", "TR_006_DONCHIAN_20", "TR_007_DONCHIAN_55", "TR_008_DONCHIAN_100",
    "TR_009_DONCHIAN_252", "TR_010_PRICE_200DMA", "TR_011_MACD", "TR_012_ADX", "TR_013_SUPERTREND",
    "TR_014_KELTNER_BREAK", "TR_015_MONTHLY_TREND",
)
MOMENTUM_EXECUTION = ("MOM_001_TS_21", "MOM_002_TS_63", "MOM_003_TS_126", "MOM_004_TS_189", "MOM_005_TS_252")
MOMENTUM_SELECTORS = (
    "MOM_006_XS_63_5", "MOM_007_XS_126_21", "MOM_008_XS_252_21", "MOM_009_XS_252_63",
    "MOM_010_52W_HIGH", "MOM_011_RISK_ADJ", "MOM_012_RESIDUAL", "MOM_013_SECTOR_REL", "MOM_014_DUAL",
)
MEAN_REVERSION_EXECUTION = (
    "MR_001_RSI2", "MR_002_RSI14", "MR_003_BOLL", "MR_004_MA_DIST", "MR_005_STOCH", "MR_006_WILLIAMS",
    "MR_007_CONNORS", "MR_011_GAP", "MR_012_VWAP",
)
MEAN_REVERSION_SELECTORS = ("MR_008_REV_1D", "MR_009_REV_5D", "MR_010_REV_20D", "MR_013_PEER_Z")
FACTORS = tuple(f"FAC_{index:03d}_{suffix}" for index, suffix in enumerate((
    "VALUE", "QUALITY", "PROFIT", "INVEST", "LOW_VOL", "LOW_BETA", "DIV_GROWTH", "SHAREHOLDER",
    "EARN_QUALITY", "BALANCE", "GROWTH_QUALITY", "VALUE_QUALITY", "QUALITY_MOM", "MULTI", "SECTOR_NEUTRAL",
    "REVISIONS",
), start=1))
STATISTICAL = (
    "STAT_001_PAIR_COIN", "STAT_002_PAIR_BETA", "STAT_003_PCA", "STAT_004_SECTOR", "STAT_005_SUBIND",
    "STAT_006_CLUSTER", "STAT_007_KALMAN", "STAT_008_DISTANCE",
)
OVERLAYS = (
    "RISK_001_VOL_TARGET", "RISK_002_PORT_VOL", "RISK_003_DD", "RISK_004_TREND_VOL", "RISK_005_ATR",
    "RISK_006_CORR", "RISK_007_REGIME", "RISK_008_LIQ",
)
EVENTS = (
    "EVT_001_PEAD", "EVT_002_EARN_GAP", "EVT_003_PRE_EARN_AVOID", "EVT_004_TURN_MONTH",
    "EVT_005_MONTH_END", "EVT_006_EXDIV", "EVT_007_INDEX", "EVT_008_SPLIT",
)
INCOME = ("INC_001_DIV_YIELD", "INC_002_DIV_GROW", "INC_003_REIT_QUALITY", "INC_004_BUYBACK", "INC_005_CASH_RETURN")


PARAMETERS = {
    "BH_001": (), "EW_001": ("rebalance", "max_names"), "SEC_EW_001": ("rebalance", "sector_budget"),
    "INV_VOL_001": ("lookback_days", "rebalance", "vol_floor"),
    "RP_001": ("lookback_days", "covariance", "rebalance"),
    "MINVAR_001": ("lookback_days", "covariance", "max_weight"),
    "MAXDIV_001": ("lookback_days", "max_weight"), "HRP_001": ("lookback_days", "linkage", "rebalance"),
    "CVaR_001": ("lookback_days", "alpha", "max_weight"), "BLEND_001": ("blend_weights", "rebalance"),
}
for key in TREND[:5]: PARAMETERS[key] = ("fast_window", "slow_window", "atr_stop_multiple")
for key in TREND[5:9]: PARAMETERS[key] = ("entry_lookback", "exit_lookback", "atr_stop_multiple")
PARAMETERS.update({
    "TR_010_PRICE_200DMA": ("window", "slope_filter"), "TR_011_MACD": ("fast", "slow", "signal"),
    "TR_012_ADX": ("window", "threshold"), "TR_013_SUPERTREND": ("atr_window", "multiplier"),
    "TR_014_KELTNER_BREAK": ("ema_window", "atr_window", "multiplier"),
    "TR_015_MONTHLY_TREND": ("window_months", "rebalance"),
})
for key in MOMENTUM_EXECUTION: PARAMETERS[key] = ("lookback_days", "threshold", "vol_target")
for key in MOMENTUM_SELECTORS[:4]: PARAMETERS[key] = ("formation_days", "skip_days", "selection_quantile", "retention_quantile")
PARAMETERS.update({
    "MOM_010_52W_HIGH": ("lookback_days", "selection_quantile"),
    "MOM_011_RISK_ADJ": ("lookback_days", "vol_window", "selection_quantile"),
    "MOM_012_RESIDUAL": ("lookback_days", "regression_days"),
    "MOM_013_SECTOR_REL": ("lookback_days", "names_per_sector"),
    "MOM_014_DUAL": ("lookback_days", "selection_quantile"),
})
for key in MEAN_REVERSION_EXECUTION[:7]: PARAMETERS[key] = ("window", "entry_threshold", "max_holding_days", "atr_stop_multiple")
for key, lookback in zip(MEAN_REVERSION_SELECTORS[:3], (1, 5, 20)): PARAMETERS[key] = ("lookback_days", "selection_quantile", "holding_days")
PARAMETERS.update({
    "MR_011_GAP": ("z_threshold", "holding_minutes"), "MR_012_VWAP": ("z_threshold", "max_holding_minutes"),
    "MR_013_PEER_Z": ("regression_days", "z_entry", "z_exit"),
})
for key in FACTORS: PARAMETERS[key] = ("rebalance", "selection_quantile", "winsorization", "sector_neutral")
for key in STATISTICAL: PARAMETERS[key] = ("formation_days", "z_entry", "z_exit", "max_half_life_days")
for key in OVERLAYS: PARAMETERS[key] = ("trigger_or_target", "smoothing_days")
for key in EVENTS: PARAMETERS[key] = ("event_horizon_days", "abnormal_return_threshold", "relative_volume_threshold")
for key in INCOME: PARAMETERS[key] = ("rebalance", "selection_quantile", "minimum_history_years")


def _family_and_role(research_id):
    if research_id == "BH_001": return "baseline", StrategyRole.EXECUTION
    if research_id in BASELINE[1:] + ALLOCATION: return "allocation", StrategyRole.ALLOCATOR
    if research_id in TREND: return "trend", StrategyRole.EXECUTION
    if research_id in MOMENTUM_EXECUTION: return "momentum", StrategyRole.EXECUTION
    if research_id in MOMENTUM_SELECTORS: return "momentum", StrategyRole.SELECTOR
    if research_id in MEAN_REVERSION_EXECUTION: return "mean_reversion", StrategyRole.EXECUTION
    if research_id in MEAN_REVERSION_SELECTORS: return "mean_reversion", StrategyRole.SELECTOR
    if research_id in FACTORS: return "cross_sectional_factor", StrategyRole.SELECTOR
    if research_id in STATISTICAL: return "statistical_arbitrage", StrategyRole.PAIR_BASKET
    if research_id in OVERLAYS: return "volatility_control", StrategyRole.OVERLAY
    if research_id in EVENTS: return "event", StrategyRole.EVENT
    if research_id in INCOME: return "income", StrategyRole.INCOME
    raise KeyError(research_id)


def _registration(research_id):
    family, role = _family_and_role(research_id)
    runtime = f"RESEARCH_{research_id}" if role == StrategyRole.EXECUTION else None
    if research_id == "BH_001": runtime = "FIXED_WEIGHT_REBALANCE"
    engine = {
        StrategyRole.EXECUTION: "SINGLE_ASSET", StrategyRole.SELECTOR: "CROSS_SECTIONAL",
        StrategyRole.ALLOCATOR: "ALLOCATOR", StrategyRole.OVERLAY: "OVERLAY", StrategyRole.EVENT: "EVENT",
        StrategyRole.PAIR_BASKET: "PAIR_BASKET", StrategyRole.INCOME: "CROSS_SECTIONAL",
    }[role]
    data = {
        StrategyRole.EXECUTION: ("adjusted_ohlcv", "corporate_actions", "trading_calendar"),
        StrategyRole.SELECTOR: ("adjusted_ohlcv", "point_in_time_gics"),
        StrategyRole.ALLOCATOR: ("adjusted_ohlcv",), StrategyRole.OVERLAY: ("adjusted_ohlcv",),
        StrategyRole.EVENT: ("point_in_time_events", "adjusted_ohlcv"),
        StrategyRole.PAIR_BASKET: ("adjusted_ohlcv", "point_in_time_gics"),
        StrategyRole.INCOME: ("point_in_time_fundamentals", "corporate_actions", "point_in_time_gics"),
    }[role]
    frequency = ("1h", "1d") if research_id in {"MR_011_GAP", "MR_012_VWAP"} or role in {StrategyRole.EVENT, StrategyRole.PAIR_BASKET, StrategyRole.OVERLAY} else ("1d",)
    return StrategyRegistration(
        research_id=research_id,
        implementation_path=f"apps.research.implementations.{family}.{research_id}",
        role=role, frequency=frequency, supported_direction=("LONG",), data_requirements=data,
        feature_requirements=(family,), parameter_names=PARAMETERS[research_id],
        parameter_budget=1 if research_id == "BH_001" else 16 if role == StrategyRole.EXECUTION else 8,
        compatible_timeframes=ALL_TIMEFRAMES, compatible_risks=ALL_RISKS, runtime_mapping=runtime,
        backtest_engine=engine, fallback_behavior="IGNORE_IF_DATA_UNAVAILABLE" if role != StrategyRole.EXECUTION else "USE_BASELINE_EXECUTION",
    )


REGISTRY = {research_id: _registration(research_id) for research_id in (
    BASELINE + ALLOCATION + TREND + MOMENTUM_EXECUTION + MOMENTUM_SELECTORS +
    MEAN_REVERSION_EXECUTION + MEAN_REVERSION_SELECTORS + FACTORS + STATISTICAL + OVERLAYS + EVENTS + INCOME
)}


def registry_entry(research_id):
    try:
        return REGISTRY[str(research_id).upper()]
    except KeyError as exc:
        raise ValueError(f"No explicit implementation registered for {research_id}") from exc


def validate_registry_for_dataset(dataset):
    definitions = list(ResearchStrategyDefinition.objects.filter(dataset_version=dataset, active=True))
    database_ids = {item.research_id for item in definitions}
    registry_ids = set(REGISTRY)
    if database_ids != registry_ids:
        raise ValueError(f"Strategy registry mismatch; missing={sorted(database_ids-registry_ids)}, extra={sorted(registry_ids-database_ids)}")
    errors = []
    for definition in definitions:
        entry = REGISTRY[definition.research_id]
        entry.load()
        if set(entry.parameter_names) != set(definition.parameter_grid):
            errors.append(f"{definition.research_id}: parameter schema mismatch")
        if entry.role != definition.role:
            errors.append(f"{definition.research_id}: role {definition.role} should be {entry.role}")
    if errors:
        raise ValueError("; ".join(errors))
    return {"strategies": len(definitions), "implementations": len(REGISTRY), "valid": True}


def _runtime_definition(entry, definition):
    if not entry.runtime_mapping:
        return None
    if entry.runtime_mapping == "FIXED_WEIGHT_REBALANCE":
        return StrategyDefinition.objects.get(key=entry.runtime_mapping)
    runtime, _ = StrategyDefinition.objects.update_or_create(
        key=entry.runtime_mapping,
        defaults={
            "name": definition.name, "description": definition.description,
            "plugin_path": "apps.strategies.plugins.catalogue.CatalogueLongOnlyPlugin",
            "input_requirements": [{"type": "BAR", "name": "OHLCV"}],
            "parameter_schema": {**entry.parameter_schema, "additionalProperties": True},
            "supported_asset_types": ["STK"], "supported_directions": ["LONG"],
            "supported_timeframes": list(entry.frequency), "version": 1, "enabled": True,
        },
    )
    StrategyConstructionProfile.objects.update_or_create(
        strategy_definition=runtime,
        defaults={
            "supported_goal_timeframes": list(definition.recommended_goal_timeframes),
            "minimum_risk": min(definition.recommended_risk_levels or [1]),
            "maximum_risk": max(definition.recommended_risk_levels or [5]),
            "construction_enabled": True, "user_selectable": False,
            "summary": definition.description, "limitations": "Created only by the recommendation system; remains disabled after apply.",
        },
    )
    return runtime


@transaction.atomic
def synchronize_strategy_registry(dataset):
    validate_registry_for_dataset(dataset)
    created = 0
    for definition in ResearchStrategyDefinition.objects.select_for_update().filter(dataset_version=dataset, active=True):
        entry = REGISTRY[definition.research_id]
        runtime = _runtime_definition(entry, definition)
        _, was_created = ResearchStrategyImplementation.objects.update_or_create(
            research_strategy=definition,
            implementation_path=entry.implementation_path,
            implementation_version=entry.implementation_version,
            defaults={
                "implementation_hash": entry.implementation_hash, "role": entry.role,
                "exact_semantic_match": True, "supported_frequency": entry.frequency[0],
                "supported_direction": "LONG", "status": ImplementationStatus.VALIDATED,
                "executable_strategy_definition": runtime, "default_parameters": {},
                "approval_record": {"registry_validated": True, "runtime_mapping": entry.runtime_mapping},
            },
        )
        created += int(was_created)
    return {"registered": len(REGISTRY), "created": created}
