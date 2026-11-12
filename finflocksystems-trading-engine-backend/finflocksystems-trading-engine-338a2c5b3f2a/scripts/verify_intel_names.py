"""Verify that every engine name the intelligence layer depends on still exists.

Single source of truth for docs/intelligence/NAME_MAP.md. It loads each mapped
model through the app registry and asserts that every listed field exists, so a
renamed or removed engine field fails here instead of at runtime.

Run standalone:  python scripts/verify_intel_names.py
Run in CI:       tests/intelligence/test_name_map.py imports `verify()`.
"""
import importlib
import os
import sys
from pathlib import Path

# (documented name, app_label, model name, fields the intelligence layer reads)
MODEL_MAP = [
    ("StrategyDefinition", "strategies", "StrategyDefinition",
     ["key", "name", "plugin_path", "parameter_schema", "supported_asset_types",
      "supported_directions", "supported_timeframes", "version", "enabled"]),
    ("StrategyInstance", "strategies", "StrategyInstance",
     ["name", "definition", "portfolio", "instrument", "timeframe", "parameters",
      "target_configuration", "risk_policy", "order_policy", "execution_mode", "state",
      "enabled", "allocated_capital", "kill_switch", "version", "state_data",
      "effective_from", "effective_to", "block_reason", "workflow_trace_id"]),
    ("StrategyVersion", "strategies", "StrategyVersion",
     ["strategy_instance", "version", "configuration_snapshot", "parameter_hash",
      "created_at", "activated_at", "retired_at"]),
    ("StrategyRun", "strategies", "StrategyRun",
     ["strategy_instance", "strategy_version", "input_hash", "idempotency_key",
      "triggering_event_id", "configuration_snapshot", "context_snapshot", "status",
      "started_at", "completed_at"]),
    ("StrategySignal", "strategies", "StrategySignal",
     ["run", "strategy_instance", "strategy_version", "signal_type", "signal_time",
      "reason", "details"]),
    ("StrategyTarget", "strategies", "StrategyTarget",
     ["run", "strategy_instance", "strategy_version", "instrument", "target_type",
      "target_quantity", "direction", "signal_type", "metadata", "execution_mode"]),
    ("OptionSelectionSnapshot", "strategies", "OptionSelectionSnapshot",
     ["strategy_instance", "strategy_version", "market_bar_id", "market_bar_version",
      "observed_at", "provider", "model_version", "atr_value", "spot", "entry_score",
      "selected_strategy", "expiration", "candidate_rankings", "selected_legs",
      "source_snapshot"]),
    ("StrategyAttributedPosition", "strategies", "StrategyAttributedPosition",
     ["strategy_instance", "instrument", "portfolio", "quantity", "average_cost"]),
    ("StrategyAction", "strategies", "StrategyAction",
     ["strategy_instance", "action", "idempotency_key", "request_hash", "status", "result"]),
    ("OrderPolicy", "strategies", "OrderPolicy", ["name", "order_type", "allow_market_order"]),
    ("StrategyRiskPolicy", "strategies", "StrategyRiskPolicy",
     ["name", "maximum_weight", "maximum_notional", "maximum_quantity", "configuration"]),
    ("Instrument", "instruments", "Instrument",
     ["symbol", "asset_class", "exchange", "primary_exchange", "currency", "multiplier",
      "lot_size", "min_tick", "trading_calendar"]),
    ("BrokerContract", "instruments", "BrokerContract", ["instrument", "conid"]),
    ("OptionContract", "instruments", "OptionContract",
     ["instrument", "underlying", "expiration", "strike", "right", "multiplier",
      "trading_class", "settlement"]),
    ("TradingPortfolio", "portfolios", "TradingPortfolio",
     ["name", "account", "gateway_session", "kill_switch"]),
    ("PortfolioPosition", "portfolios", "PortfolioPosition",
     ["portfolio", "instrument", "quantity", "average_cost", "realized_pnl", "market_price"]),
    ("CashLedgerEntry", "portfolios", "CashLedgerEntry",
     ["portfolio", "amount", "currency", "kind", "reference", "idempotency_key"]),
    ("PositionLedgerEntry", "portfolios", "PositionLedgerEntry",
     ["portfolio", "instrument", "quantity_delta", "price", "realized_pnl", "kind",
      "reference", "idempotency_key"]),
    ("BrokerAccount", "accounts", "BrokerAccount", ["account_id", "base_currency"]),
    ("BrokerGatewaySession", "broker_gateway", "BrokerGatewaySession",
     ["id", "mode", "status", "market_data_type"]),
    ("OrderIntent", "oms", "OrderIntent",
     ["portfolio", "strategy_instance", "strategy_version", "instrument", "side",
      "quantity", "order_type", "limit_price", "reference_price", "origin", "mode",
      "source", "idempotency_key", "created_at", "rebalance", "strategy_snapshot"]),
    ("Order", "oms", "Order",
     ["intent", "internal_id", "status", "quantity", "filled_quantity",
      "average_fill_price", "created_at"]),
    ("OrderStatusHistory", "oms", "OrderStatusHistory",
     ["order", "from_status", "to_status", "source", "reason_code", "occurred_at", "event_key"]),
    ("Fill", "execution", "Fill",
     ["order", "execution_id", "quantity", "price", "commission", "currency", "executed_at"]),
    ("OrderIntentAttribution", "allocation", "OrderIntentAttribution",
     ["order_intent", "strategy_instance", "strategy_version", "strategy_snapshot",
      "target_delta", "allocated_quantity", "allocated_value", "allocated_cost"]),
    ("PositionSizingDecision", "position_sizing", "PositionSizingDecision",
     ["order_intent", "target_quantity", "approved_quantity", "binding_constraint",
      "limits", "entry_price", "stop_price"]),
    ("RiskCheckResult", "risk", "RiskCheckResult",
     ["order_intent", "check_name", "decision", "reason", "requested_quantity",
      "approved_quantity", "details"]),
    ("KillSwitch", "risk", "KillSwitch", ["scope", "scope_id", "enabled"]),
    ("MarketBar", "market_streams", "MarketBar",
     ["instrument", "bar_id", "interval", "window_start", "window_end", "open", "high",
      "low", "close", "volume", "version", "is_final", "provider", "market_data_type",
      "processing_mode", "received_at"]),
    ("IndicatorValue", "market_streams", "IndicatorValue",
     ["instrument", "bar", "indicator", "indicator_role", "value", "previous_value",
      "timeframe", "source_bar_id", "source_bar_version", "processing_mode", "event_time"]),
    ("InstrumentMarketState", "market_streams", "InstrumentMarketState",
     ["instrument", "status", "reference_price", "latest_event_at", "stale_after_seconds",
      "reference_price_source", "market_data_type"]),
    ("StrategyEvaluationJob", "market_streams", "StrategyEvaluationJob",
     ["strategy_instance", "strategy_version", "bar", "strategy_run", "status", "error_code"]),
    ("MarketDataConsumerLease", "market_streams", "MarketDataConsumerLease",
     ["gateway_session", "instrument", "timeframe", "consumer_type", "lease_key", "expires_at"]),
    ("AuditEvent", "audit", "AuditEvent",
     ["event_type", "actor", "aggregate_type", "aggregate_id", "data", "idempotency_key",
      "created_at"]),
    ("OutboxEvent", "audit", "OutboxEvent",
     ["topic", "event_type", "aggregate_type", "aggregate_id", "payload", "idempotency_key",
      "status"]),
    ("BrokerPositionSnapshot", "broker_gateway", "BrokerPositionSnapshot",
     ["broker_account", "snapshot_key", "complete", "status", "positions", "received_at"]),
    ("ReconciliationRun", "reconciliation", "ReconciliationRun",
     ["gateway_session", "broker_account", "trigger", "status", "started_at"]),
    ("ReconciliationBreak", "reconciliation", "ReconciliationBreak",
     ["run", "category", "severity", "material", "resolved", "created_at"]),
]

# Documented names that do NOT exist under that name (see NAME_MAP.md MISMATCHES).
# model name -> real equivalent as "app_label.Model"
MISSING_DOCUMENTED_MODELS = {
    "PortfolioTargetSnapshot": "allocation.PortfolioTargetSnapshot (re-exported by rebalancing)",
    "RebalanceRun": "allocation.RebalanceRun (re-exported by rebalancing)",
}

# (dotted module, attribute) pairs the layer calls or patches. Verified by import.
FUNCTION_MAP = [
    ("apps.oms.services", "apply_execution"),
    ("apps.oms.services", "_apply_weighted_average_fill"),
    ("apps.oms.services", "_apply_late_commission"),
    ("apps.strategies.reporting", "attributed_fills"),
    ("apps.strategies.reporting", "strategy_pnl"),
    ("apps.strategies.reporting", "trade_points"),
    ("apps.strategies.framework", "create_instance"),
    ("apps.strategies.framework", "update_instance"),
    ("apps.strategies.framework", "enable_instance"),
    ("apps.strategies.framework", "activate_instance"),
    ("apps.strategies.framework", "_validate_activation"),
    ("apps.strategies.framework", "_create_version"),
    ("apps.strategies.framework", "configuration_snapshot"),
    ("apps.strategies.framework", "current_version"),
    ("apps.strategies.framework", "evaluate_instance"),
    ("apps.strategies.tasks", "activate_strategy_instance"),
    ("apps.strategies.deletion", "delete_strategy_instance"),
    ("apps.strategies.plugins", "get_plugin"),
    ("apps.strategies.plugins.base", "StrategyPlugin"),
    ("apps.core.views", "response"),
    ("apps.core.throttling", "throttle_response"),
    ("apps.core.idempotency", "canonical_request_hash"),
    ("apps.core.idempotency", "require_matching_request"),
    ("apps.core.idempotency", "IdempotencyConflict"),
    ("apps.event_bus.tasks", "compact_operational_records"),
    ("apps.execution.readiness", "REQUIRED_WORKER_ROLES"),
    ("apps.execution.readiness", "record_worker_heartbeat"),
    ("apps.execution.modes", "ExecutionMode"),
    ("apps.strategies.input_identity", "indicator_output_name"),
    ("apps.strategies.deletion", "ACTIVE_ORDER_STATES"),
    ("apps.strategies.framework", "retire_version"),
    ("streaming.flink.jobs.processing", "compute_indicator"),
]

# (dotted settings name) the layer reads or extends.
SETTINGS_MAP = [
    "CELERY_TASK_ROUTES", "CELERY_BEAT_SCHEDULE", "INSTALLED_APPS",
    "OUTBOX_RETENTION_DAYS", "OPTION_PRETRADE_RISK_ENABLED", "KAFKA_ENABLED",
    "EXECUTION_REQUIRED_KAFKA_TOPICS", "MARKET_PRICE_STALE_SECONDS",
]


def _django_setup():
    base = Path(__file__).resolve().parent.parent
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()


def verify():
    """Return a list of human-readable problems; empty means the map is intact."""
    from django.apps import apps
    from django.conf import settings

    problems = []
    for documented, app_label, model_name, fields in MODEL_MAP:
        try:
            model = apps.get_model(app_label, model_name)
        except LookupError:
            problems.append(f"{documented}: {app_label}.{model_name} is not a registered model")
            continue
        real = {field.name for field in model._meta.get_fields()}
        for field in fields:
            if field not in real:
                problems.append(f"{documented}: {app_label}.{model_name} has no field '{field}'")
    for documented in MISSING_DOCUMENTED_MODELS:
        if any(model.__name__ == documented and model._meta.app_label == "rebalancing"
               for model in apps.get_models()):
            problems.append(f"{documented} now lives in app 'rebalancing'; update NAME_MAP.md")
    for module_name, attribute in FUNCTION_MAP:
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # pragma: no cover - reported, not raised
            problems.append(f"{module_name}: import failed ({exc})")
            continue
        if not hasattr(module, attribute):
            problems.append(f"{module_name}.{attribute} does not exist")
    for name in SETTINGS_MAP:
        if not hasattr(settings, name):
            problems.append(f"settings.{name} does not exist")
    return problems


def main():
    _django_setup()
    problems = verify()
    for problem in problems:
        print(f"MISMATCH: {problem}")
    print(f"{len(MODEL_MAP)} models, {len(FUNCTION_MAP)} functions, {len(SETTINGS_MAP)} settings checked; "
          f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
