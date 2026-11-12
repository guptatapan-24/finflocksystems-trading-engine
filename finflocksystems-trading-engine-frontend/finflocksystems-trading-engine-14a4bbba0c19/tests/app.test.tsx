import {render, screen, waitFor, within} from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import App, {appBasename, normalizeBasename} from '../src/App'
import {queryClient} from '../src/app/queryClient'
import {refreshAfterStrategyDeletion} from '../src/features/strategies/strategyActions'
import {usePreferencesStore} from '../src/stores/preferences'
import {useWorkspacePreferences} from '../src/stores/workspacePreferences'

const definition = {
  id: 44, key: 'CUSTOM_BREAKOUT', name: 'Backend Breakout', description: 'A backend-provided portable definition.', plugin_path: 'plugins.Custom',
  supported_timeframes: ['15m', '1h'], supported_asset_types: ['STK'], supported_directions: ['LONG'], version: 3, enabled: true,
  default_parameters: {lookback: 21, confirmation: 'CLOSE'}, input_requirements: [{input_type: 'INDICATOR', name: 'channel', parameters: {window: 21}, warmup_bars: 22}],
  parameter_schema: {type: 'object', required: ['lookback', 'confirmation'], properties: {lookback: {type: 'integer', minimum: 2}, confirmation: {enum: ['CLOSE', 'INTRABAR']}}},
}

const strategy = {
  id: 7, name: 'Portable breakout', definition_key: definition.key, definition_name: definition.name,
  portfolio_id: 10, portfolio: 'Primary paper', instrument_id: 5, symbol: 'NVDA', asset_class: 'STK', exchange: 'SMART', currency: 'USD',
  conid: 4815747, primary_exchange: 'NASDAQ', timeframe: '15m', parameters: definition.default_parameters,
  target_configuration: {target_weight: 0.1, capital_share: 1, priority: 100}, risk_policy_id: null, order_policy_id: null,
  execution_mode: 'PAPER', state: 'LONG', enabled: true, version: 2, warmup_progress: 22, warmup_required: 22,
  gateway_session_id: 'session-1', gateway_session_name: 'Primary paper gateway',
  activation_stages: {subscription_ready: true, warmup_complete: true, waiting_for_live_bar: false, first_evaluation_complete: true, execution_active: true},
  execution_workflow: {trace_id: '8d57bc42-0ee4-4314-b2a3-3d693e62a111', status: 'ACTIVE', active: false, terminal: false, current_stage: 'ORDER_INTENT_CREATED', poll_after_ms: 12000, observed_at: '2026-07-13T01:00:05Z'},
  activation_operation: {id: 71, status: 'COMPLETED', retryable: false, message: '', attempt_count: 1, idempotency_key: 'activate-71', created_at: '2026-07-13T00:00:00Z', completed_at: '2026-07-13T01:00:04Z'},
  current_price: {value: 125, provider: 'IBKR', source: 'ibkr_live', data_kind: 'LIVE', timestamp: '2026-07-13T01:00:00Z', age_seconds: 4, stale_after_seconds: 1800, fresh_for_execution: true},
  block_reason: '', effective_from: '2026-07-13T00:00:00Z', effective_to: null, last_final_bar: '2026-07-13T01:00:00Z',
  latest_indicators: {channel: 123}, latest_signal: 'ENTER_LONG', current_target: 0.1, attributed_quantity: 4,
  active_order: 'order-active', last_fill: 'fill-1', cooldown: null, created_at: '2026-07-12T00:00:00Z', updated_at: '2026-07-13T01:00:00Z',
  streaming: {strategy_id: 7, strategy: 'Portable breakout', symbol: 'NVDA', timeframe: '15m', status: 'HEALTHY',
    subscription_state: 'ACTIVE', conid: 4815747, last_raw_event: '2026-07-13T01:00:01Z',
    last_canonical_event: '2026-07-13T01:00:02Z', last_final_bar: '2026-07-13T01:00:00Z', warmup_progress: 22,
    warmup_required: 22, last_indicator: '2026-07-13T01:00:03Z', last_strategy_run: '2026-07-13T01:00:04Z', last_error: '', missing: [], stale_after_seconds: 1800},
  versions: [{id: 2, version: 2, parameter_hash: 'abcdef1234567890', configuration_snapshot: {}, created_at: '2026-07-13T00:00:00Z', activated_at: '2026-07-13T00:05:00Z', retired_at: null}],
  requirements: [{identity_hash: 'input-1', input_type: 'INDICATOR', name: 'channel', role: '', parameters: {window: 21}, implementation_version: 1, warmup_bars: 22, shared_by: 1, active: true}],
}

const constructionPlan = {
  id: 301, portfolio_id: 10, name: 'Primary goals', status: 'DRAFT', version: 4,
  allocated_weight: 1, allocated_percentage: 100, enabled_goal_count: 2, ready_to_preview: true,
  validation_errors: [], created_at: '2026-07-13T01:00:00Z', updated_at: '2026-07-13T01:00:00Z',
  timeframe_options: [
    {code: 'NOW', label: 'Now, up to 30 days'}, {code: 'HURRY', label: 'Hurry, 1-3 months'},
    {code: 'FAST', label: 'Fast, 3-12 months'}, {code: 'BUILD', label: 'Build, 1-3 years'},
    {code: 'GROW', label: 'Grow, 3-7 years'}, {code: 'COMPOUND', label: 'Compound, 7+ years'},
  ],
  risk_options: [
    {level: 1, code: 'PRESERVATION', label: 'Capital Preservation'}, {level: 2, code: 'CONSERVATIVE', label: 'Conservative'},
    {level: 3, code: 'BALANCED', label: 'Balanced'}, {level: 4, code: 'GROWTH', label: 'Growth'},
    {level: 5, code: 'AGGRESSIVE', label: 'Aggressive / High Risk-High Reward'},
  ],
  goals: [
    {id: 401, plan_id: 301, name: 'Near-term reserve', allocation_weight: .5, allocation_percentage: 50, timeframe_bucket: 'HURRY', risk_level: 2, enabled: true, display_order: 0, instrument_count: 1, created_at: '', updated_at: '', resolved_rules: {timeframe_bucket: 'HURRY', timeframe_label: 'Hurry, 1-3 months', risk_level: 2, risk_code: 'CONSERVATIVE', risk_label: 'Conservative', maximum_allowed_risk: 2, minimum_cash_weight: .7, maximum_stock_weight: .1, optimizer_method: 'MINIMUM_VARIANCE', lookback_days: 252, minimum_history_observations: 60, long_only: true}},
    {id: 402, plan_id: 301, name: 'Long-term growth', allocation_weight: .5, allocation_percentage: 50, timeframe_bucket: 'GROW', risk_level: 5, enabled: true, display_order: 1, instrument_count: 2, created_at: '', updated_at: '', resolved_rules: {timeframe_bucket: 'GROW', timeframe_label: 'Grow, 3-7 years', risk_level: 5, risk_code: 'AGGRESSIVE', risk_label: 'Aggressive / High Risk-High Reward', maximum_allowed_risk: 5, minimum_cash_weight: .05, maximum_stock_weight: .25, optimizer_method: 'MAXIMUM_SHARPE', lookback_days: 252, minimum_history_observations: 60, long_only: true}},
  ],
}

const constructionPreview = {
  id: 501, plan_id: 301, portfolio_id: 10, status: 'COMPLETED', application_status: 'NOT_APPLIED', retryable: false,
  last_error: '', attempt_count: 1, nav: 100000, final_target_weights: {cash: .7, stocks: {'5': .175, '6': .125}},
  metrics: {expected_return: .08, expected_volatility: .12, sharpe_ratio: .66, strategy_targets: [{identity: 'aggregate-nvda', strategy_definition_id: 44, strategy_name: definition.name, instrument_id: 5, symbol: 'NVDA', execution_timeframe: '15m', target_weight: .175, assignment_ids: [801, 802]}, {identity: 'aggregate-msft', strategy_definition_id: 44, strategy_name: definition.name, instrument_id: 6, symbol: 'MSFT', execution_timeframe: '15m', target_weight: .125, assignment_ids: [803]}]}, warnings: [], applied_rebalance: null, applied_at: null,
  created_at: '', started_at: '', completed_at: '', rebalance: {id: 601, mode: 'PAPER', run_type: 'PREVIEW', status: 'PLANNED', phase: 'PREVIEW_COMPLETE', planned_turnover: .3},
  goals: [
    {goal_id: 401, name: 'Near-term reserve', allocation_weight: .5, goal_nav: 50000, timeframe_bucket: 'HURRY', risk_level: 2, optimizer_method: 'MINIMUM_VARIANCE', cash_weight: .8, maximum_stock_weight: .1, intentionally_cash_only: false, apply_blocked: false, warnings: [], stocks: [{instrument_id: 5, goal_instrument_id: 701, symbol: 'NVDA', goal_id: 401, goal_name: 'Near-term reserve', goal_allocation_weight: .5, local_weight: .1, portfolio_contribution: .05, strategy_share_total: 1, strategy_share_valid: true, strategies: [{assignment_id: 801, strategy_definition_id: 44, strategy_name: definition.name, strategy_share: 1, portfolio_weight: .05}]}]},
    {goal_id: 402, name: 'Long-term growth', allocation_weight: .5, goal_nav: 50000, timeframe_bucket: 'GROW', risk_level: 5, optimizer_method: 'MAXIMUM_SHARPE', cash_weight: .5, maximum_stock_weight: .25, intentionally_cash_only: false, apply_blocked: false, warnings: [], stocks: [{instrument_id: 5, goal_instrument_id: 702, symbol: 'NVDA', goal_id: 402, goal_name: 'Long-term growth', goal_allocation_weight: .5, local_weight: .25, portfolio_contribution: .125, strategy_share_total: 1, strategy_share_valid: true, strategies: [{assignment_id: 802, strategy_definition_id: 44, strategy_name: definition.name, strategy_share: 1, portfolio_weight: .125}]}, {instrument_id: 6, goal_instrument_id: 703, symbol: 'MSFT', goal_id: 402, goal_name: 'Long-term growth', goal_allocation_weight: .5, local_weight: .25, portfolio_contribution: .125, strategy_share_total: 1, strategy_share_valid: true, strategies: [{assignment_id: 803, strategy_definition_id: 44, strategy_name: definition.name, strategy_share: 1, portfolio_weight: .125}]}]},
  ],
  targets: [
    {id: 1, instrument_id: 5, symbol: 'NVDA', current_weight: .1, target_weight: .175, weight_change: .075, target_value: 17500, expected_return_contribution: .03, risk_contribution: .04, shared_across_goals: true, rank: 0, goal_contributions: [{goal_id: 401, goal_name: 'Near-term reserve', local_weight: .1, portfolio_contribution: .05}, {goal_id: 402, goal_name: 'Long-term growth', local_weight: .25, portfolio_contribution: .125}]},
    {id: 2, instrument_id: 6, symbol: 'MSFT', current_weight: 0, target_weight: .125, weight_change: .125, target_value: 12500, expected_return_contribution: .05, risk_contribution: .08, shared_across_goals: false, rank: 1, goal_contributions: [{goal_id: 402, goal_name: 'Long-term growth', local_weight: .25, portfolio_contribution: .125}]},
  ],
  planned_trades: [{instrument_id: 5, symbol: 'NVDA', current_weight: .1, target_weight: .175, side: 'BUY', quantity: 60, reference_price: 125, estimated_cost: 1, suppressed: false, suppression_reason: ''}, {instrument_id: 6, symbol: 'MSFT', current_weight: 0, target_weight: .125, side: 'BUY', quantity: 31, reference_price: 400, estimated_cost: 1, suppressed: false, suppression_reason: ''}],
}

const recommendationBatch = {
  id: 901, plan_id: 301, requested_plan_version: 4, status: 'COMPLETED', dataset_version_id: 21, protocol_version_id: 7,
  metrics: {goal_count: 2, stock_count: 3, fallback_tiers: {'1': 2}}, error: '', created_at: '2026-07-13T01:00:00Z',
  started_at: '2026-07-13T01:00:00Z', completed_at: '2026-07-13T01:00:03Z',
  goals: [
    {goal_id: 401, goal_name: 'Near-term reserve', status: 'COMPLETED', recommendation_run_id: 911, fallback_tier: 1,
      cash_weight: .8, timeframe: 'HURRY', risk_level: 2, metrics: {expected_return: .05}, freshness: {feature_age_seconds: 30},
      stocks: [{instrument_id: 5, universe_member_id: 1005, symbol: 'NVDA', company: 'NVIDIA Corporation',
        gics: {sector: {code: '45', name: 'Information Technology'}}, research_strategy_id: 'BH_001',
        execution_strategy_definition_id: 44, strategy_name: 'Buy and Hold', execution_timeframe: '1d', weight: .1,
        candidate_score: .82, expected_return: .08, expected_volatility: .19, expected_drawdown: -.21,
        reason: 'Strong quality and momentum with conservative position sizing.'}],
    },
    {goal_id: 402, goal_name: 'Long-term growth', status: 'COMPLETED', recommendation_run_id: 912, fallback_tier: 1,
      cash_weight: .5, timeframe: 'GROW', risk_level: 5, metrics: {expected_return: .12}, freshness: {feature_age_seconds: 30},
      stocks: [
        {instrument_id: 5, universe_member_id: 1005, symbol: 'NVDA', company: 'NVIDIA Corporation',
          gics: {sector: {code: '45', name: 'Information Technology'}}, research_strategy_id: 'MOM_002',
          execution_strategy_definition_id: 44, strategy_name: 'Residual Momentum', execution_timeframe: '1d', weight: .25,
          candidate_score: .91, expected_return: .16, expected_volatility: .28, expected_drawdown: -.31,
          reason: 'Top-ranked residual momentum candidate with positive analyst revisions.'},
        {instrument_id: 6, universe_member_id: 1006, symbol: 'MSFT', company: 'Microsoft Corporation',
          gics: {sector: {code: '45', name: 'Information Technology'}}, research_strategy_id: 'QUAL_003',
          execution_strategy_definition_id: 44, strategy_name: 'Quality Composite', execution_timeframe: '1d', weight: .25,
          candidate_score: .88, expected_return: .13, expected_volatility: .2, expected_drawdown: -.23,
          reason: 'Durable profitability and balance-sheet quality.'},
      ],
    },
  ],
}

const data: Record<string, unknown> = {
  system: {mode: 'MULTI_SESSION', execution_modes: ['PAPER', 'LIVE'], execution_mode_source: 'PORTFOLIO_GATEWAY_SESSION', allow_live_trading: true, is_admin: true, global_kill_switch: false, material_breaks: 0, time: '2026-07-13T01:00:00Z'},
  gateway: {connected: true, reconciled: true, mode: 'paper', last_callback: '2026-07-13T01:00:00Z', worker: 'paper-worker'},
  'broker-sessions': [
    {id: 'session-1', display_name: 'Primary paper gateway', username_hint: 'du••ry', mode: 'paper',
      status: 'CONNECTED', connected: true, commands_enabled: true, last_error: '', accounts: [],
      container_status: 'running', account_count: 2, last_gateway_state: {connected: true, reconciled: true, mode: 'paper'},
      provisioned_at: null, connected_at: '2026-07-13T00:01:00Z', last_checked_at: '2026-07-13T01:00:00Z',
      deleted_at: null, needs_novnc: false, novnc_url: null,
      created_at: '2026-07-13T00:00:00Z', updated_at: '2026-07-13T01:00:00Z'},
  ],
  accounts: [
    {id: 1, account_id: 'DU-PRIMARY', alias: 'Primary', base_currency: 'USD', net_liquidation: 100000, available_cash: 40000, buying_power: 200000, daily_pnl: 250, is_reconciled: true, kill_switch: false, updated_at: '2026-07-13T01:00:00Z'},
    {id: 2, account_id: 'DU-SECONDARY', alias: 'Secondary', base_currency: 'USD', net_liquidation: 50000, available_cash: 20000, buying_power: 100000, daily_pnl: -25, is_reconciled: true, kill_switch: false, updated_at: '2026-07-13T01:00:00Z'},
  ],
  'broker-sessions/session-1/accounts': [
    {id: 1, account_id: 'DU-PRIMARY', alias: 'Primary', base_currency: 'USD', net_liquidation: 100000, available_cash: 40000, buying_power: 200000, daily_pnl: 250, is_reconciled: true, kill_switch: false, available: true, last_seen_at: '2026-07-13T01:00:00Z', default_portfolio_id: 10, updated_at: '2026-07-13T01:00:00Z'},
    {id: 2, account_id: 'DU-SECONDARY', alias: 'Secondary', base_currency: 'USD', net_liquidation: 50000, available_cash: 20000, buying_power: 100000, daily_pnl: -25, is_reconciled: true, kill_switch: false, available: true, last_seen_at: '2026-07-13T01:00:00Z', default_portfolio_id: 20, updated_at: '2026-07-13T01:00:00Z'},
  ],
  portfolios: [
    {id: 10, name: 'Primary paper', account_id: 1, account: 'DU-PRIMARY', gateway_session_id: 'session-1', cash_buffer_pct: .02, margin_buffer_pct: .1, minimum_notional: 10, minimum_quantity: 1, minimum_drift: .001, kill_switch: false},
    {id: 20, name: 'Secondary paper', account_id: 2, account: 'DU-SECONDARY', gateway_session_id: 'session-1', cash_buffer_pct: .02, margin_buffer_pct: .1, minimum_notional: 10, minimum_quantity: 1, minimum_drift: .001, kill_switch: false},
  ],
  instruments: [{id: 5, symbol: 'NVDA', asset_class: 'STK', exchange: 'SMART', currency: 'USD', sector: 'Technology', multiplier: 1, lot_size: 1, min_tick: .01, fractional_support: false, trading_calendar: 'XNYS', active: true, tradable: true}, {id: 6, symbol: 'MSFT', asset_class: 'STK', exchange: 'SMART', currency: 'USD', sector: 'Technology', multiplier: 1, lot_size: 1, min_tick: .01, fractional_support: false, trading_calendar: 'XNYS', active: true, tradable: true}],
  positions: [{id: 1, portfolio_id: 10, portfolio: 'Primary paper', account_id: 'DU-PRIMARY', instrument_id: 5, symbol: 'NVDA', asset_class: 'STK', currency: 'USD', quantity: 4, average_cost: 100, market_price: 125, market_value: 500, updated_at: '2026-07-13T01:00:00Z'}],
  'dashboard/summary': {mode: 'PAPER', account: null, portfolio: null, gateway: {connected: true, reconciled: true, mode: 'paper'}, reconciliation_status: 'RECONCILED', nav: 100000, cash: 40000, buying_power: 200000, daily_pnl: 250, gross_exposure: 500, net_exposure: 500, active_strategies: 1, open_orders: 1, positions: 1, recent_activity: [], attention: [], updated_at: '2026-07-13T01:00:00Z'},
  'portfolios/series': {portfolio_id: 10, source: 'POSTGRES_MARKET_BARS_WITH_CURRENT_HOLDINGS', nav: [{time: '2026-07-12T00:00:00Z', value: 99000}, {time: '2026-07-13T00:00:00Z', value: 100000}], pnl: [{time: '2026-07-12T00:00:00Z', value: 0}, {time: '2026-07-13T00:00:00Z', value: 1000}], exposure: [{time: '2026-07-12T00:00:00Z', gross: 480, net: 480}, {time: '2026-07-13T00:00:00Z', gross: 500, net: 500}], allocation_by_instrument: [{instrument_id: 5, symbol: 'NVDA', value: 500, weight: 1}]},
  'strategy-definitions': [definition],
  'strategy-policies': {risk_policies: [{id: 1, name: 'Long only', allow_short: false}], order_policies: [{id: 1, name: 'Patient limit'}]},
  'strategy-instances': [strategy],
  'strategy-instances/7': strategy,
  'strategy-instances/7/execution-timeline': [
    ['CREATED', 'Created', 'COMPLETED'], ['ACTIVATION_QUEUED', 'Activation queued', 'COMPLETED'],
    ['CONTRACT_QUALIFIED', 'Contract qualified', 'COMPLETED'], ['SUBSCRIPTION_PENDING', 'Subscription pending', 'COMPLETED'],
    ['SUBSCRIPTION_ACTIVE', 'Subscription active', 'COMPLETED'], ['WARMUP_IN_PROGRESS', 'Warm-up in progress', 'COMPLETED'],
    ['WARMUP_COMPLETE', 'Warm-up complete', 'COMPLETED'], ['WAITING_FOR_LIVE_BAR', 'Waiting for live bar', 'COMPLETED'],
    ['FIRST_EVALUATION_COMPLETED', 'First evaluation completed', 'COMPLETED'], ['TARGET_GENERATED', 'Target generated', 'COMPLETED'],
    ['REBALANCE_CREATED', 'Rebalance created', 'COMPLETED'], ['ORDER_INTENT_CREATED', 'Order intent created', 'COMPLETED'],
    ['RISK_DECISION', 'Risk approved or blocked', 'COMPLETED'], ['BROKER_COMMAND_SENT', 'Broker command sent', 'COMPLETED'],
    ['ORDER_ACKNOWLEDGED', 'Order acknowledged', 'COMPLETED'], ['FILL_PROGRESS', 'Partially filled or filled', 'PENDING'],
  ].map(([stage, label, status], index) => ({stage, label, status, entity_id: String(index + 1), occurred_at: status === 'COMPLETED' ? `2026-07-13T01:00:${String(index).padStart(2, '0')}Z` : null, trace_id: '8d57bc42-0ee4-4314-b2a3-3d693e62a111', workflow_active: false, workflow_terminal: false})),
  'strategy-instances/7/chart': {source: 'POSTGRES_MARKET_AND_EXECUTION_FACTS', bars: [{time: '2026-07-13T01:00:00Z', open: 121, high: 126, low: 120, close: 125, volume: 1000, version: 1}], indicators: [{time: '2026-07-13T01:00:00Z', name: 'channel', value: 123}], markers: [{time: '2026-07-13T01:00:00Z', type: 'SIGNAL', label: 'Signal ENTER_LONG'}],
    trade_markers: [{time: '2026-07-13T00:58:00Z', type: 'ENTRY', status: 'FILLED', label: 'Entry · 1 fill'}],
    atr_bands: [{time: '2026-07-13T01:00:00Z', support: '118', resistance: '130'}]},
  'strategy-instances/7/ledger': [
    {id: 'run:2', run_id: 2, time: '2026-07-13T01:00:00Z', evaluated_at: '2026-07-13T01:00:04Z', bar_close: '125', spot: null, decision: 'TARGET_SET', signal_type: 'SET_TARGET', status: 'SET_TARGET', gate: null,
      rps: null, rps_threshold: null, rps_passed: null, iv_score: null, iv_score_threshold: null, iv_passed: null, iv_rank: null, iv_percentile: null, tqs: null, tqs_threshold: null, tqs_passed: null,
      atr_support: null, atr_resistance: null, oi_support: null, oi_resistance: null, selected_strategy: null, candidates: [], orders: [], target_count: 1, reason: 'Channel breakout above 124', reason_details: []},
  ],
  'strategy-instances/7/pnl': {currency: 'USD', running_pnl: '42.5', realized_pnl: '0', gross_realized_pnl: '0', unrealized_pnl: '42.5', commissions: '1', open_position: true, marks_complete: true, marks_fresh: true, fill_count: 1,
    legs: [{instrument_id: 5, symbol: 'NVDA', quantity: '5', average_price: '116.5', mark_price: '125', mark_source: 'ibkr_live', mark_fresh: true, multiplier: '1', realized_pnl: '-1', unrealized_pnl: '42.5', open: true}],
    series: [{time: '2026-07-13T00:58:00Z', realized: '-1', unrealized: '0', total: '-1', mark_basis: 'LAST_FILL'}, {time: '2026-07-13T01:00:05Z', realized: '-1', unrealized: '42.5', total: '41.5', mark_basis: 'CURRENT_MARK'}],
    as_of: '2026-07-13T01:00:05Z', scope: 'STRATEGY_ATTRIBUTED_FILLS'},
  'instruments/search': [{symbol: 'NVDA', local_symbol: 'NVDA', conid: 4815747, asset_class: 'STK', exchange: 'SMART', primary_exchange: 'NASDAQ', currency: 'USD', description: 'NVIDIA Corporation', instrument_id: null}],
  orders: [
    {id: 1, internal_id: 'active-order-123', account_id: 'DU-PRIMARY', portfolio_id: 10, symbol: 'NVDA', side: 'BUY', order_type: 'LMT', time_in_force: 'DAY', broker_order_id: '991', broker_permanent_id: '', status: 'ACKNOWLEDGED', quantity: 10, filled_quantity: 4, average_fill_price: 123, created_at: '2026-07-13T00:00:00Z', updated_at: '2026-07-13T01:00:00Z'},
    {id: 2, internal_id: 'filled-order-456', account_id: 'DU-PRIMARY', portfolio_id: 10, symbol: 'NVDA', side: 'BUY', order_type: 'MKT', time_in_force: 'DAY', broker_order_id: '992', broker_permanent_id: '', status: 'FILLED', quantity: 2, filled_quantity: 2, average_fill_price: 124, created_at: '2026-07-12T00:00:00Z', updated_at: '2026-07-12T01:00:00Z'},
  ],
  'orders/active-order-123/detail': {
    order: {},
    status_history: [
      {id: 101, from_status: 'SUBMITTED', to_status: 'REJECTED', broker_status: 'Inactive', reason_code: '201',
        reason: 'Order rejected - insufficient available equity', source: 'ibkr', details: {why_held: ''},
        occurred_at: '2026-07-13T01:00:00Z', operator_requested: false},
      {id: 102, from_status: 'REJECTED', to_status: 'REJECTED', broker_status: 'Inactive', reason_code: '',
        reason: '', source: 'ibkr', details: {}, occurred_at: '2026-07-13T01:00:01Z', operator_requested: false},
    ],
    broker_diagnostics: [], risk_decisions: [], fills: [], strategy_attribution: [],
  },
  executions: [{id: 1, order_id: 'active-order-123', account_id: 'DU-PRIMARY', symbol: 'NVDA', execution_id: 'fill-1', quantity: 4, price: 123, commission: 1, currency: 'USD', executed_at: '2026-07-13T00:30:00Z'}],
  audit: [],
  risk: {kill_switches: [], decisions: []},
  reconciliation: {runs: [], breaks: []},
  'streaming/health': {kafka_enabled: true, data_path_status: 'HEALTHY', data_path_reasons: [],
    gateway: {status: 'HEALTHY', value: {connected: true, reconciled: true}, observed_at: '2026-07-13T01:00:00Z'},
    consumer: {status: 'HEALTHY', last_heartbeat: '2026-07-13T01:00:00Z', value: {}}, metrics: [],
    flink: {status: 'HEALTHY', jobs: []}, strategies: [strategy.streaming], outbox_pending: 0, outbox_failed: 0,
    dead_letter_count: 0, stale_instrument_count: 0},
  'allocations/policies': [{id: 1, portfolio_id: 10, portfolio: 'Primary paper', strategy_id: 1, strategy: 'Portable breakout', target_share: 1, minimum_share: 0, maximum_share: 1, capacity: null, minimum_allocation: 0, priority: 100, enabled: true}],
  'allocations/runs': [], 'rebalancing/policies': [], 'rebalancing/runs': [],
  'data-providers/finnhub': {provider: 'FINNHUB', configured: true, enabled: true, effective_source: 'ENVIRONMENT', environment_configured: true, database_configured: false, database_override_requested: false, database_override_allowed: false, database_override_active: false, masked_api_key: '••••CRET', last_success_at: '2026-07-13T01:00:00Z', last_tested_at: null, last_test_success_at: null, last_error: '', rate_limit_state: {remaining: '59', limit: '60'}, updated_at: null},
  'portfolio-universe': [{id: 1, portfolio_id: 10, name: 'Default universe', include_strategy_instruments: false, minimum_history_observations: 60, maximum_instruments: 50, selected_count: 2, enabled: true, instruments: [{instrument_id: 5, symbol: 'NVDA', enabled: true}, {instrument_id: 6, symbol: 'MSFT', enabled: true}], updated_at: '2026-07-13T01:00:00Z'}],
  'portfolio-optimization/policies': [{id: 1, portfolio_id: 10, name: 'Default Markowitz policy', method: 'MINIMUM_VARIANCE', lookback_days: 252, return_estimation: 'HISTORICAL_MEAN', covariance_estimation: 'SAMPLE', risk_free_rate: 0, target_cash_weight: .05, minimum_weight: 0, maximum_weight: .8, maximum_turnover: .5, transaction_cost_penalty: .01, long_only: true, enabled: true, execution_mode: 'PAPER', version: 1, updated_at: '2026-07-13T01:00:00Z'}],
  'portfolio-optimization/runs': [],
  'portfolio-construction/plans': [constructionPlan],
  'portfolio-construction/runs': [],
  'portfolio-construction/goals/401/eligible-strategies': {goal_id: 401, eligible: [{strategy_definition_id: 44, key: definition.key, name: definition.name, summary: definition.description, limitations: 'Long only', execution_timeframes: definition.supported_timeframes, default_parameters: {...definition.default_parameters, direction: 'LONG'}, parameter_schema: definition.parameter_schema, eligible: true, reason: ''}], rejected: [{strategy_definition_id: 45, key: 'BREAKOUT', name: 'Breakout', summary: '', limitations: '', execution_timeframes: ['1d'], default_parameters: {}, parameter_schema: {}, eligible: false, reason: 'Strategy supports risk levels 3-5'}]},
  'portfolio-construction/goals/402/eligible-strategies': {goal_id: 402, eligible: [{strategy_definition_id: 44, key: definition.key, name: definition.name, summary: definition.description, limitations: 'Long only', execution_timeframes: definition.supported_timeframes, default_parameters: {...definition.default_parameters, direction: 'LONG'}, parameter_schema: definition.parameter_schema, eligible: true, reason: ''}], rejected: []},
  'portfolio-construction/goals/401/instruments': [{id: 701, goal_id: 401, instrument_id: 5, symbol: 'NVDA', asset_class: 'STK', exchange: 'SMART', currency: 'USD', minimum_weight: null, maximum_weight: null, display_order: 0, enabled: true, assignment_count: 2, created_at: '', updated_at: ''}],
  'portfolio-construction/goals/402/instruments': [{id: 702, goal_id: 402, instrument_id: 5, symbol: 'NVDA', asset_class: 'STK', exchange: 'SMART', currency: 'USD', minimum_weight: null, maximum_weight: null, display_order: 0, enabled: true, assignment_count: 1, created_at: '', updated_at: ''}, {id: 703, goal_id: 402, instrument_id: 6, symbol: 'MSFT', asset_class: 'STK', exchange: 'SMART', currency: 'USD', minimum_weight: null, maximum_weight: null, display_order: 1, enabled: true, assignment_count: 1, created_at: '', updated_at: ''}],
  'portfolio-construction/instruments/701/assignments': [{id: 801, goal_instrument_id: 701, goal_id: 401, strategy_definition_id: 44, strategy_key: definition.key, strategy_name: definition.name, instrument_id: 5, symbol: 'NVDA', execution_timeframe: '15m', parameter_overrides: {...definition.default_parameters, direction: 'LONG'}, parameter_hash: 'p1', strategy_share: .5, risk_policy_id: null, order_policy_id: null, create_instance: true, enabled: true, created_strategy_instance_id: null, created_at: '', updated_at: ''}, {id: 804, goal_instrument_id: 701, goal_id: 401, strategy_definition_id: 44, strategy_key: definition.key, strategy_name: definition.name, instrument_id: 5, symbol: 'NVDA', execution_timeframe: '1h', parameter_overrides: {...definition.default_parameters, direction: 'LONG'}, parameter_hash: 'p1', strategy_share: .5, risk_policy_id: 1, order_policy_id: 1, create_instance: true, enabled: true, created_strategy_instance_id: 804, created_at: '', updated_at: ''}],
  'portfolio-construction/instruments/702/assignments': [{id: 802, goal_instrument_id: 702, goal_id: 402, strategy_definition_id: 44, strategy_key: definition.key, strategy_name: definition.name, instrument_id: 5, symbol: 'NVDA', execution_timeframe: '15m', parameter_overrides: {...definition.default_parameters, direction: 'LONG'}, parameter_hash: 'p1', strategy_share: 1, risk_policy_id: null, order_policy_id: null, create_instance: true, enabled: true, created_strategy_instance_id: null, created_at: '', updated_at: ''}],
  'portfolio-construction/instruments/703/assignments': [{id: 803, goal_instrument_id: 703, goal_id: 402, strategy_definition_id: 44, strategy_key: definition.key, strategy_name: definition.name, instrument_id: 6, symbol: 'MSFT', execution_timeframe: '15m', parameter_overrides: {...definition.default_parameters, direction: 'LONG'}, parameter_hash: 'p1', strategy_share: 1, risk_policy_id: null, order_policy_id: null, create_instance: true, enabled: true, created_strategy_instance_id: null, created_at: '', updated_at: ''}],
}

const optimizationPreview = {
  id: 91, portfolio_id: 10, policy_id: 1, universe_id: 1, trigger: 'PREVIEW', status: 'COMPLETED', application_status: 'NOT_APPLIED', applied_at: null, applied_rebalance: null, input_start_date: '2025-07-01', input_end_date: '2026-07-01', nav: 100000,
  objective_value: .02, expected_return: .12, expected_volatility: .18, sharpe_ratio: .66, turnover: .24, cash_weight: .05, solver_status: 'Optimization terminated successfully', warnings: [], error_details: {}, flow_reference: '', created_at: '2026-07-13T01:00:00Z', completed_at: '2026-07-13T01:00:01Z',
  targets: [{id: 1, instrument_id: 5, symbol: 'NVDA', current_weight: .1, optimized_weight: .45, weight_change: .35, target_value: 45000, expected_return_contribution: .05, risk_contribution: .08, constraint_status: '', rank: 0}, {id: 2, instrument_id: 6, symbol: 'MSFT', current_weight: 0, optimized_weight: .5, weight_change: .5, target_value: 50000, expected_return_contribution: .07, risk_contribution: .1, constraint_status: '', rank: 1}],
  planned_trades: [{instrument_id: 5, symbol: 'NVDA', side: 'BUY', quantity: 10, reference_price: 125, estimated_cost: 1, suppressed: false, suppression_reason: ''}, {instrument_id: 6, symbol: 'MSFT', side: 'BUY', quantity: 15, reference_price: 400, estimated_cost: 2, suppressed: false, suppression_reason: ''}],
  rebalance: {id: 81, mode: 'PAPER', run_type: 'PREVIEW', status: 'PLANNED', phase: 'PREVIEW_COMPLETE', planned_turnover: .24},
}

let failDashboard = false
let failStrategyDelete = false
let failConstructionPreview = false
let builderReadinessReady = true
let strategyDefinitionsAvailable = true

function apiPath(input: string) {
  const url = new URL(input, 'http://localhost')
  return (url.pathname.split('/api/v1/')[1] || '').replace(/\/$/, '')
}

beforeEach(() => {
  window.history.replaceState({}, '', '/')
  queryClient.clear()
  usePreferencesStore.setState({selectedSessionId: null, selectedAccountId: null, selectedPortfolioId: null})
  useWorkspacePreferences.getState().resetWorkspace()
  failDashboard = false
  failStrategyDelete = false
  failConstructionPreview = false
  builderReadinessReady = true
  strategyDefinitionsAvailable = true
  const brokerSession = (data['broker-sessions'] as Array<Record<string, unknown>>)[0]
  brokerSession.mode = 'paper'
  brokerSession.last_gateway_state = {connected: true, reconciled: true, mode: 'paper'}
  const firstBuilderAssignment = (data['portfolio-construction/instruments/701/assignments'] as Array<Record<string, unknown>>)[0]
  firstBuilderAssignment.strategy_share = .5
  firstBuilderAssignment.parameter_overrides = {...definition.default_parameters, direction: 'LONG'}
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    const path = apiPath(input)
    const method = init?.method || 'GET'
    if (failDashboard && path === 'dashboard/summary') return {ok: false, status: 400, json: async () => ({ok: false, data: null, error: {code: 'DEGRADED', message: 'Summary unavailable', details: {}}, meta: {}})} as Response
    if (method !== 'GET') {
      if (method === 'DELETE' && path === 'strategy-instances/7') {
        if (failStrategyDelete) return {ok: false, status: 409, json: async () => ({ok: false, data: null, error: {code: 'STRATEGY_DELETION_BLOCKED', message: 'Strategy has 1 open order. Cancel it before deleting.', details: {blockers: [{code: 'OPEN_ORDERS'}]}}, meta: {}})} as Response
        return {ok: true, status: 200, json: async () => ({ok: true, data: {id: 7, name: strategy.name}, error: null, meta: {}})} as Response
      }
      if (path === 'strategy-instances') return {ok: true, status: 201, json: async () => ({ok: true, data: strategy, error: null, meta: {}})} as Response
      if (path === 'instruments/resolve') return {ok: true, status: 200, json: async () => ({ok: true, data: {instrument_id: 5, symbol: 'NVDA', asset_class: 'STK', exchange: 'SMART', currency: 'USD', conid: 4815747, primary_exchange: 'NASDAQ', qualification_command: null}, error: null, meta: {}})} as Response
      if (path === 'portfolio-construction/goals/401/instruments' && method === 'POST') return {ok: true, status: 201, json: async () => ({ok: true, data: (data['portfolio-construction/goals/401/instruments'] as unknown[])[0], error: null, meta: {}})} as Response
      if (path === 'portfolio-construction/assignments/801' && method === 'PATCH') {
        const body = JSON.parse(String(init?.body || '{}'))
        const current = data['portfolio-construction/instruments/701/assignments'] as Array<Record<string, unknown>>
        const item = {...current[0], strategy_share: body.strategy_share, parameter_overrides: body.parameter_overrides}
        data['portfolio-construction/instruments/701/assignments'] = [item, ...current.slice(1)]
        return {ok: true, status: 200, json: async () => ({ok: true, data: item, error: null, meta: {}})} as Response
      }
      if (path === 'orders') return {ok: true, status: 201, json: async () => ({ok: true, data: {internal_id: 'created-order', status: 'QUEUED', decision: 'APPROVED'}, error: null, meta: {}})} as Response
      if (path === 'portfolio-optimization/preview') return {ok: true, status: 201, json: async () => ({ok: true, data: optimizationPreview, error: null, meta: {}})} as Response
      if (path === 'portfolio-optimization/run') return {ok: true, status: 201, json: async () => ({ok: true, data: {...optimizationPreview, application_status: 'APPLIED', applied_at: '2026-07-13T01:02:00Z', applied_rebalance: {id: 82, mode: 'PAPER', run_type: 'EXECUTION', status: 'PLANNED', phase: 'SELLS', planned_turnover: .24}}, error: null, meta: {}})} as Response
      if (path === 'portfolio-construction/plans/301/recommendations') return {ok: true, status: 202, json: async () => ({
        ok: true,
        data: {...recommendationBatch, status: 'QUEUED', goals: recommendationBatch.goals.map((goal) => ({...goal, status: 'QUEUED'}))},
        error: null,
        meta: {},
      })} as Response
      if (path === 'portfolio-construction/preview') {
        const preview = failConstructionPreview
          ? {...constructionPreview, status: 'FAILED', retryable: true, last_error: 'Finnhub API key is not configured', final_target_weights: {}, metrics: {}, goals: [], targets: [], planned_trades: [], rebalance: null}
          : constructionPreview
        return {ok: true, status: 202, json: async () => ({ok: true, data: preview, error: null, meta: {}})} as Response
      }
      if (path === 'portfolio-construction/runs/501/apply') return {ok: true, status: 202, json: async () => ({ok: true, data: {...constructionPreview, application_status: 'APPLIED', applied_at: '2026-07-13T01:03:00Z', applied_rebalance: {id: 602, mode: 'PAPER', run_type: 'EXECUTION', status: 'PLANNED', phase: 'SELLS', planned_turnover: .3}, metrics: {...constructionPreview.metrics, strategy_instances: [{assignment_id: 801, strategy_instance_id: 801, target_weight: .175}]}}, error: null, meta: {}})} as Response
      if (path === 'allocations/flows') return {ok: true, status: 201, json: async () => ({ok: true, data: {id: 92, status: 'COMPLETED', allocation_mode: 'PORTFOLIO_OPTIMIZATION'}, error: null, meta: {}})} as Response
      if (path === 'data-providers/finnhub/configure') return {ok: true, status: 200, json: async () => ({ok: true, data: {...data['data-providers/finnhub'] as object, database_configured: true, masked_api_key: '••••CRET'}, error: null, meta: {}})} as Response
      if (path === 'data-providers/finnhub/test') return {ok: true, status: 200, json: async () => ({ok: true, data: {...data['data-providers/finnhub'] as object, connected: true, source: 'TRANSIENT'}, error: null, meta: {}})} as Response
      return {ok: true, status: 200, json: async () => ({ok: true, data: {}, error: null, meta: {}})} as Response
    }
    if (path === 'portfolio-construction/readiness') {
      const result = builderReadinessReady
        ? {ready: true, blockers: [], details: {}}
        : {ready: false, blockers: [
          {code: 'RESEARCH_DATA_MISSING', message: 'Research history is ready for 0 members; at least 5 are required', details: {current: 0, minimum: 5}},
          {code: 'BROKER_GATEWAY_NOT_CONNECTED', message: 'The selected portfolio needs a connected, command-ready IBKR broker session', details: {status: 'DISCONNECTED'}},
        ], details: {}}
      return {ok: true, status: 200, json: async () => ({ok: true, data: result, error: null, meta: {}})} as Response
    }
    if (path === `portfolio-construction/recommendation-batches/${recommendationBatch.id}`) {
      return {ok: true, status: 200, json: async () => ({ok: true, data: recommendationBatch, error: null, meta: {}})} as Response
    }
    const result = path === 'strategy-definitions' && !strategyDefinitionsAvailable ? [] : data[path]
    return {ok: true, status: 200, json: async () => ({ok: true, data: result ?? [], error: null, meta: {}})} as Response
  }))
})

afterEach(() => vi.unstubAllGlobals())

test('renders bookmarkable primary routes and paper status', async () => {
  render(<App />)
  expect(await screen.findByRole('heading', {name: 'Good overview, Primary paper'})).toBeInTheDocument()
  const nav = screen.getByRole('navigation', {name: 'Primary navigation'})
  const links = within(nav).getAllByRole('link')
  expect(links.map((link) => link.textContent)).toEqual(['Dashboard', 'Strategies', 'Portfolio Builder', 'Portfolio', 'Orders & Activity', 'IBKR Sessions', 'Intelligence', 'System'])
  expect(within(nav).getByRole('link', {name: 'Strategies'})).toHaveAttribute('href', '/strategies')
  expect(screen.getAllByText('PAPER').length).toBeGreaterThan(0)
})

test('System reports Paper and Live readiness separately', async () => {
  window.history.replaceState({}, '', '/system')
  render(<App />)
  expect(await screen.findByRole('heading', {name: 'System'})).toBeInTheDocument()
  expect(screen.getByText('Paper readiness')).toBeInTheDocument()
  expect(screen.getByText('Live readiness')).toBeInTheDocument()
  expect(await screen.findByText('1 command-ready Paper Gateway session')).toBeInTheDocument()
  expect(await screen.findByText('No Live Gateway session is configured')).toBeInTheDocument()
  expect(screen.getByText('Local automatic-execution diagnostics')).toBeInTheDocument()
  expect(screen.getByText('Strategy worker')).toBeInTheDocument()
  expect(screen.getAllByText('Reconciliation').length).toBeGreaterThan(0)
})

test('derives strategy execution mode from the selected portfolio Gateway session', async () => {
  const user = userEvent.setup()
  window.history.replaceState({}, '', '/strategies/new')
  render(<App />)
  expect(await screen.findByRole('heading', {name: 'Create a strategy'})).toBeInTheDocument()
  await user.type(screen.getByLabelText('Ticker'), 'nvda')
  await user.click(await screen.findByRole('button', {name: 'Select NVDA NASDAQ USD'}))
  await user.click(screen.getByRole('button', {name: 'Qualify selected contract'}))
  await screen.findByText('QUALIFIED')
  await user.click(screen.getByRole('button', {name: 'Continue'}))
  await user.type(screen.getByLabelText('Instance name'), 'NVDA portable')
  await user.selectOptions(screen.getByLabelText('Strategy definition'), 'CUSTOM_BREAKOUT')
  expect(screen.getByLabelText('Timeframe')).toHaveValue('15m')
  await user.click(screen.getByRole('button', {name: 'Continue'}))
  expect(screen.getByLabelText('lookback')).toHaveValue(21)
  expect(screen.getByLabelText('confirmation')).toHaveValue('CLOSE')
  await user.click(screen.getByRole('button', {name: 'Continue'}))
  const mode = await screen.findByLabelText('Execution mode')
  expect(mode).toHaveTextContent('PAPER')
  expect(mode).toHaveTextContent('Derived from Primary paper gateway')
  expect(within(mode).queryByRole('combobox')).not.toBeInTheDocument()
  expect(screen.getByRole('button', {name: /Advanced policy settings/})).toHaveAttribute('aria-expanded', 'false')
  await user.click(screen.getByRole('button', {name: 'Continue'}))
  expect(screen.getByText('Execution boundary preserved')).toBeInTheDocument()
  expect(screen.getByRole('button', {name: 'Create as disabled'})).toBeEnabled()
  expect(screen.getByRole('button', {name: 'Create and activate'})).toBeEnabled()
  await user.click(screen.getByRole('button', {name: 'Create as disabled'}))
  await waitFor(() => {
    const call = vi.mocked(fetch).mock.calls.find(([input, init]) => apiPath(String(input)) === 'strategy-instances' && init?.method === 'POST')
    expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({portfolio_id: 10, execution_mode: 'PAPER', activate: false})
  })
})

test('reports an empty backend strategy catalog and recovers on retry', async () => {
  const user = userEvent.setup()
  strategyDefinitionsAvailable = false
  window.history.replaceState({}, '', '/strategies/new')
  render(<App />)
  expect(await screen.findByText('No strategy definitions are enabled')).toBeInTheDocument()
  expect(screen.queryByLabelText('Strategy definition')).not.toBeInTheDocument()

  strategyDefinitionsAvailable = true
  await user.click(screen.getByRole('button', {name: 'Retry'}))
  expect(await screen.findByRole('heading', {name: 'Create a strategy'})).toBeInTheDocument()
})

test('creates and durably activates from the strategy wizard', async () => {
  const user = userEvent.setup()
  window.history.replaceState({}, '', '/strategies/new')
  render(<App />)
  await screen.findByRole('heading', {name: 'Create a strategy'})
  await user.type(screen.getByLabelText('Ticker'), 'nvda')
  await user.click(await screen.findByRole('button', {name: 'Select NVDA NASDAQ USD'}))
  await user.click(screen.getByRole('button', {name: 'Qualify selected contract'}))
  await screen.findByText('QUALIFIED')
  await user.click(screen.getByRole('button', {name: 'Continue'})
  )
  await user.type(screen.getByLabelText('Instance name'), 'NVDA activated')
  await user.selectOptions(screen.getByLabelText('Strategy definition'), 'CUSTOM_BREAKOUT')
  await user.click(screen.getByRole('button', {name: 'Continue'}))
  await user.click(screen.getByRole('button', {name: 'Continue'}))
  await user.click(screen.getByRole('button', {name: 'Continue'}))
  await user.click(screen.getByRole('button', {name: 'Create and activate'}))
  await waitFor(() => {
    const calls = vi.mocked(fetch).mock.calls.filter(([input, init]) =>
      apiPath(String(input)) === 'strategy-instances' && init?.method === 'POST',
    )
    expect(JSON.parse(String(calls.at(-1)?.[1]?.body))).toMatchObject({
      portfolio_id: 10,
      execution_mode: 'PAPER',
      activate: true,
    })
  })
})

test('selected account updates the available portfolio context', async () => {
  const user = userEvent.setup()
  render(<App />)
  const account = await screen.findByLabelText('Selected account')
  await waitFor(() => expect(screen.getByLabelText('Selected portfolio')).toHaveValue('10'))
  await user.selectOptions(account, '2')
  await waitFor(() => expect(screen.getByLabelText('Selected portfolio')).toHaveValue('20'))
  expect(screen.getByRole('option', {name: 'Secondary paper'})).toBeInTheDocument()
})

test('strategy controls expose eligible enable pause and confirmed flatten actions', async () => {
  const user = userEvent.setup()
  window.history.replaceState({}, '', '/strategies')
  render(<App />)
  expect(await screen.findByRole('heading', {name: 'Strategies'})).toBeInTheDocument()
  expect(await screen.findByRole('button', {name: 'Enable Portable breakout'})).toBeDisabled()
  expect(screen.getByRole('button', {name: 'Pause Portable breakout'})).toBeEnabled()
  expect(screen.getByRole('button', {name: 'Delete Portable breakout'})).toBeEnabled()
  await user.click(screen.getByRole('button', {name: 'Flatten Portable breakout'}))
  expect(screen.getByRole('dialog', {name: 'Flatten Portable breakout target?'})).toBeInTheDocument()
  expect(screen.getByRole('button', {name: 'Create flat target'})).toBeDisabled()
})

test('strategy deletion requires the exact name and sends a DELETE request', async () => {
  const user = userEvent.setup()
  window.history.replaceState({}, '', '/strategies')
  render(<App />)
  await user.click(await screen.findByRole('button', {name: 'Delete Portable breakout'}))
  const dialog = screen.getByRole('dialog', {name: 'Delete Portable breakout?'})
  const confirmation = within(dialog).getByLabelText('Strategy name confirmation')
  const submit = within(dialog).getByRole('button', {name: 'Delete strategy'})
  expect(submit).toBeDisabled()
  await user.type(confirmation, 'Portable Breakout')
  expect(submit).toBeDisabled()
  await user.clear(confirmation)
  await user.type(confirmation, 'Portable breakout')
  expect(submit).toBeEnabled()
  await user.click(submit)
  await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/strategy-instances/7/'), expect.objectContaining({method: 'DELETE'})))
  const call = vi.mocked(fetch).mock.calls.find(([input, init]) => String(input).includes('/strategy-instances/7/') && init?.method === 'DELETE')
  expect(JSON.parse(String(call?.[1]?.body))).toEqual({strategy_name: 'Portable breakout'})
})

test('strategy deletion displays backend blocker guidance', async () => {
  failStrategyDelete = true
  const user = userEvent.setup()
  window.history.replaceState({}, '', '/strategies')
  render(<App />)
  await user.click(await screen.findByRole('button', {name: 'Delete Portable breakout'}))
  const dialog = screen.getByRole('dialog', {name: 'Delete Portable breakout?'})
  await user.type(within(dialog).getByLabelText('Strategy name confirmation'), 'Portable breakout')
  await user.click(within(dialog).getByRole('button', {name: 'Delete strategy'}))
  expect(await screen.findByText('Strategy deletion blocked')).toBeInTheDocument()
  expect(screen.getByText('Strategy has 1 open order. Cancel it before deleting.')).toBeInTheDocument()
})

test('strategy deletion invalidates affected portfolio queries and removes detail caches', async () => {
  const affected = [
    ['allocation-policies'], ['allocation-runs'], ['rebalance-runs'], ['streaming'],
    ['portfolio-universe', 10], ['optimization-runs', 10], ['positions', 10],
    ['portfolio-series', 10], ['audit', {}], ['dashboard', 10], ['strategy-instances', {}],
  ]
  affected.forEach((key) => queryClient.setQueryData(key, {}))
  queryClient.setQueryData(['strategy-instance', 7], strategy)
  queryClient.setQueryData(['strategy-timeline', 7], [])
  queryClient.setQueryData(['strategy-chart', 7], {})

  await refreshAfterStrategyDeletion(queryClient, 7)

  affected.forEach((key) => expect(queryClient.getQueryState(key)?.isInvalidated).toBe(true))
  expect(queryClient.getQueryData(['strategy-instance', 7])).toBeUndefined()
  expect(queryClient.getQueryData(['strategy-timeline', 7])).toBeUndefined()
  expect(queryClient.getQueryData(['strategy-chart', 7])).toBeUndefined()
})

test('kill switch requires confirmation and an audit reason', async () => {
  const user = userEvent.setup()
  window.history.replaceState({}, '', '/system')
  render(<App />)
  await user.click(await screen.findByRole('button', {name: 'Engage global'}))
  const dialog = screen.getByRole('dialog', {name: 'Confirm global trading halt'})
  const confirm = within(dialog).getByRole('button', {name: 'Engage kill switch'})
  expect(confirm).toBeDisabled()
  await user.type(within(dialog).getByLabelText('Reason'), 'Broker state is inconsistent')
  expect(confirm).toBeEnabled()
  await user.click(confirm)
  await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/risk/'), expect.objectContaining({method: 'POST'})))
  const call = vi.mocked(fetch).mock.calls.find(([input, init]) => String(input).includes('/risk/') && init?.method === 'POST')
  expect(String(call?.[1]?.body)).toContain('Broker state is inconsistent')
  expect(screen.getByText('Active strategy data paths')).toBeInTheDocument()
  expect(screen.getByText('22 / 22')).toBeInTheDocument()
})

test('portfolio builder generates one-click recommendations, previews merged goals, and applies once', async () => {
  const user = userEvent.setup()
  window.history.replaceState({}, '', '/portfolio-builder')
  render(<App />)
  expect(await screen.findByRole('heading', {name: 'Portfolio Builder'})).toBeInTheDocument()
  expect(await screen.findByText('Allocated: 100% of 100%')).toBeInTheDocument()
  const nearTermRisk = screen.getByLabelText('Near-term reserve risk')
  expect(within(nearTermRisk).queryByRole('option', {name: 'Growth'})).not.toBeInTheDocument()
  expect(within(nearTermRisk).getByRole('option', {name: 'Conservative'})).toBeInTheDocument()
  await user.click(screen.getByRole('button', {name: 'Save goals & generate recommendations'}))
  expect(await screen.findByRole('heading', {name: '2. Recommendations'})).toBeInTheDocument()
  expect(await screen.findByRole('heading', {name: 'Near-term reserve'})).toBeInTheDocument()
  expect(vi.mocked(fetch).mock.calls.some(([input, init]) =>
    String(input).includes(`/portfolio-construction/recommendation-batches/${recommendationBatch.id}/`)
    && (!init?.method || init.method === 'GET'),
  )).toBe(true)
  expect(screen.getByRole('heading', {name: 'Long-term growth'})).toBeInTheDocument()
  expect(screen.getByText('Buy and Hold')).toBeInTheDocument()
  expect(screen.getByText('Quality Composite')).toBeInTheDocument()
  expect(screen.queryByLabelText(/IBKR stock search/)).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', {name: 'Preview rebalance'}))
  expect(await screen.findByRole('heading', {name: '3. Preview & Apply'})).toBeInTheDocument()
  expect(screen.getByText('Preview creates no orders. Review the combined target before explicitly applying it.')).toBeInTheDocument()
  expect(screen.getByText('Shared by 2 goals')).toBeInTheDocument()
  expect(screen.getAllByText('17.5%').length).toBeGreaterThan(0)
  await user.click(screen.getByRole('button', {name: 'Apply to Paper'}))
  const dialog = screen.getByRole('dialog', {name: 'Apply to Paper?'})
  await user.click(within(dialog).getByRole('button', {name: 'Apply to Paper'}))
  expect(await screen.findByText(/Initial allocation preview 602 created/)).toBeInTheDocument()
  const applyCalls = vi.mocked(fetch).mock.calls.filter(([input, init]) => String(input).includes('/portfolio-construction/runs/501/apply/') && init?.method === 'POST')
  expect(applyCalls).toHaveLength(1)
})

test('portfolio builder labels a Gateway-derived Live apply explicitly', async () => {
  const brokerSession = (data['broker-sessions'] as Array<Record<string, unknown>>)[0]
  brokerSession.mode = 'live'
  brokerSession.last_gateway_state = {connected: true, reconciled: true, mode: 'live'}
  const user = userEvent.setup()
  window.history.replaceState({}, '', '/portfolio-builder')
  render(<App />)
  await user.click(await screen.findByRole('button', {name: 'Save goals & generate recommendations'}))
  await user.click(await screen.findByRole('button', {name: 'Preview rebalance'}))
  expect(await screen.findByRole('button', {name: 'Apply to Live'})).toBeInTheDocument()
})


test('portfolio builder preflight names blockers and prevents recommendation dispatch', async () => {
  builderReadinessReady = false
  window.history.replaceState({}, '', '/portfolio-builder')
  render(<App />)

  expect(await screen.findByText('RESEARCH_DATA_MISSING')).toBeInTheDocument()
  expect(screen.getByText('BROKER_GATEWAY_NOT_CONNECTED')).toBeInTheDocument()
  expect(screen.getByText(/Research history is ready for 0 members/)).toBeInTheDocument()
  expect(screen.getByRole('button', {name: 'Save goals & generate recommendations'})).toBeDisabled()
  expect(vi.mocked(fetch).mock.calls.some(([input, init]) =>
    String(input).includes('/portfolio-construction/plans/301/recommendations/')
    && init?.method === 'POST',
  )).toBe(false)
})


test('portfolio builder reports a failed preview instead of rendering an empty result', async () => {
  failConstructionPreview = true
  const user = userEvent.setup()
  window.history.replaceState({}, '', '/portfolio-builder')
  render(<App />)
  await user.click(await screen.findByRole('button', {name: 'Save goals & generate recommendations'}))
  await user.click(await screen.findByRole('button', {name: 'Preview rebalance'}))
  expect(await screen.findByText('Recommendation workflow failed')).toBeInTheDocument()
  expect(screen.getByText('Finnhub API key is not configured')).toBeInTheDocument()
  expect(screen.queryByRole('heading', {name: '3. Preview & Apply'})).not.toBeInTheDocument()
  expect(screen.getByRole('button', {name: 'Preview rebalance'})).toBeInTheDocument()
})

test('portfolio builder removes manual construction and keeps research metadata out of recommendation cards', async () => {
  const user = userEvent.setup()
  window.history.replaceState({}, '', '/portfolio-builder')
  render(<App />)
  await user.click(await screen.findByRole('button', {name: 'Save goals & generate recommendations'}))
  expect(await screen.findByText('Residual Momentum')).toBeInTheDocument()
  expect(screen.getByText('Durable profitability and balance-sheet quality.')).toBeInTheDocument()
  expect(screen.queryByText(/dataset/i)).not.toBeInTheDocument()
  expect(screen.queryByText(/protocol/i)).not.toBeInTheDocument()
  expect(screen.queryByRole('button', {name: 'Add stock'})).not.toBeInTheDocument()
  expect(screen.queryByRole('button', {name: 'Save assignment'})).not.toBeInTheDocument()
  expect(screen.queryByLabelText(/strategy share/i)).not.toBeInTheDocument()
  const recommendationCall = vi.mocked(fetch).mock.calls.find(([input, init]) => String(input).includes('/portfolio-construction/plans/301/recommendations/') && init?.method === 'POST')
  expect(JSON.parse(String(recommendationCall?.[1]?.body))).toEqual({})
  const goalPatches = vi.mocked(fetch).mock.calls.filter(([input, init]) => String(input).includes('/portfolio-construction/goals/') && init?.method === 'PATCH')
  expect(goalPatches).toHaveLength(2)
})

test('advanced target optimizer previews without orders and offers a Paper apply route', async () => {
  const user = userEvent.setup()
  window.history.replaceState({}, '', '/portfolio')
  render(<App />)
  expect(await screen.findByRole('heading', {name: 'Advanced target optimizer'})).toBeInTheDocument()
  await user.click(screen.getByRole('button', {name: /Advanced target optimizer/}))
  await waitFor(() => {
    expect(queryClient.getQueryData(['portfolio-universe', 10])).toEqual(data['portfolio-universe'])
    expect(queryClient.getQueryData(['optimization-policies', 10])).toEqual(data['portfolio-optimization/policies'])
  })
  const previewButton = await screen.findByRole('button', {name: 'Preview optimization'})
  await waitFor(() => expect(previewButton).toBeEnabled())
  await user.click(previewButton)
  expect(await screen.findByText('Current versus optimized allocation')).toBeInTheDocument()
  expect(screen.getByText('Planned trades')).toBeInTheDocument()
  expect(screen.getAllByText('PREVIEW').length).toBeGreaterThan(0)
  expect(screen.getAllByText(/Preview creates no orders/).length).toBeGreaterThan(0)
  expect(screen.getByRole('button', {name: 'Apply to Paper'})).toBeInTheDocument()
})

test('advanced optimizer labels a Gateway-derived Live apply explicitly', async () => {
  const brokerSession = (data['broker-sessions'] as Array<Record<string, unknown>>)[0]
  brokerSession.mode = 'live'
  brokerSession.last_gateway_state = {connected: true, reconciled: true, mode: 'live'}
  const user = userEvent.setup()
  window.history.replaceState({}, '', '/portfolio')
  render(<App />)
  await user.click(await screen.findByRole('button', {name: /Advanced target optimizer/}))
  const previewButton = await screen.findByRole('button', {name: 'Preview optimization'})
  await waitFor(() => expect(previewButton).toBeEnabled())
  await user.click(previewButton)
  expect(await screen.findByRole('button', {name: 'Apply to Live'})).toBeInTheDocument()
})

test('applied optimization disables Apply and shows the applied rebalance', async () => {
  const user = userEvent.setup()
  window.history.replaceState({}, '', '/portfolio')
  render(<App />)
  await user.click(await screen.findByRole('button', {name: /Advanced target optimizer/}))
  await waitFor(() => expect(queryClient.getQueryData(['portfolio-universe', 10])).toEqual(data['portfolio-universe']))
  const previewButton = await screen.findByRole('button', {name: 'Preview optimization'})
  await waitFor(() => expect(previewButton).toBeEnabled())
  await user.click(previewButton)
  await user.click(await screen.findByRole('button', {name: 'Apply to Paper'}))
  const appliedButton = await screen.findByRole('button', {name: 'Optimization already applied'})
  expect(appliedButton).toBeDisabled()
  expect(screen.getByText(/Applied rebalance 82/)).toBeInTheDocument()
})

test('universe selection shows the count and disables Save above the maximum', async () => {
  const user = userEvent.setup()
  window.history.replaceState({}, '', '/portfolio')
  render(<App />)
  await user.click(await screen.findByRole('button', {name: /Advanced target optimizer/}))
  await screen.findByText('Selected 2 of 50 maximum')
  const maximum = await screen.findByLabelText('Maximum instruments')
  await user.clear(maximum)
  await user.type(maximum, '1')
  expect(screen.getByText('Selected 2 of 1 maximum')).toBeInTheDocument()
  expect(screen.getByRole('button', {name: 'Save universe & policy'})).toBeDisabled()
})

test('flow result displays the resolved allocation mode', async () => {
  const user = userEvent.setup()
  window.history.replaceState({}, '', '/portfolio')
  render(<App />)
  await user.click(await screen.findByText('Portfolio flow allocation'))
  await user.type(screen.getByLabelText('Amount'), '100')
  await user.click(screen.getByRole('button', {name: 'Calculate post-flow targets'}))
  expect(await screen.findByText('PORTFOLIO_OPTIMIZATION')).toBeInTheDocument()
})

test('Finnhub dialog opens without administrator sign-in and Test key never saves', async () => {
  const user = userEvent.setup()
  window.history.replaceState({}, '', '/system')
  render(<App />)
  expect(screen.queryByLabelText('Administrator username')).not.toBeInTheDocument()
  await user.click(await screen.findByRole('button', {name: 'Configure Finnhub'}))
  const dialog = screen.getByRole('dialog', {name: 'Finnhub API key'})
  const input = within(dialog).getByLabelText('Finnhub API key')
  expect(input).toHaveAttribute('placeholder', '••••CRET')
  await user.type(input, 'transient-secret')
  await user.click(within(dialog).getByRole('button', {name: 'Test key'}))
  await screen.findByText(/Not saved yet/)
  expect(input).toHaveValue('transient-secret')
  const testCall = vi.mocked(fetch).mock.calls.find(([requestInput, init]) => String(requestInput).includes('/data-providers/finnhub/test/') && init?.method === 'POST')
  expect(String(testCall?.[1]?.body)).toContain('transient-secret')
  expect(vi.mocked(fetch).mock.calls.some(([requestInput]) => String(requestInput).includes('/data-providers/finnhub/configure/'))).toBe(false)
  expect(localStorage.getItem('transient-secret')).toBeNull()
  expect(JSON.stringify(queryClient.getQueryData(['finnhub']))).not.toContain('transient-secret')
})

test('Save key clears the full Finnhub key and only refreshes masked status', async () => {
  const user = userEvent.setup()
  window.history.replaceState({}, '', '/system')
  render(<App />)
  await user.click(await screen.findByRole('button', {name: 'Configure Finnhub'}))
  const dialog = screen.getByRole('dialog', {name: 'Finnhub API key'})
  const input = within(dialog).getByLabelText('Finnhub API key')
  await user.type(input, 'replacement-secret')
  await user.click(within(dialog).getByRole('button', {name: 'Save key'}))
  await waitFor(() => expect(input).toHaveValue(''))
  const call = vi.mocked(fetch).mock.calls.find(([requestInput, init]) => String(requestInput).includes('/data-providers/finnhub/configure/') && init?.method === 'POST')
  expect(String(call?.[1]?.body)).toContain('replacement-secret')
  expect(localStorage.getItem('replacement-secret')).toBeNull()
  expect(JSON.stringify(queryClient.getQueryData(['finnhub']))).not.toContain('replacement-secret')
  expect(screen.queryByDisplayValue('replacement-secret')).not.toBeInTheDocument()
})

test('strategy detail maps backend chart data with no placeholder series', async () => {
  const user = userEvent.setup()
  window.history.replaceState({}, '', '/strategies/7')
  render(<App />)
  expect(await screen.findByRole('heading', {name: 'Portable breakout'})).toBeInTheDocument()
  await user.click(screen.getByRole('tab', {name: 'Chart'}))
  expect(await screen.findByText(/POSTGRES_MARKET_AND_EXECUTION_FACTS/)).toBeInTheDocument()
  expect(screen.getByRole('img', {name: /Strategy price chart with ATR boundary, entry and exit points/})).toBeInTheDocument()
  expect(screen.getByText(/Latest band 118 – 130 · close 125 inside the band/)).toBeInTheDocument()
  expect((await screen.findByTestId('plotly-chart')).dataset.traces).toBe('Underlying OHLC|ATR support|ATR resistance|Entry fill')
})

test('strategy detail shows the strategy running P&L and the decision ledger', async () => {
  const user = userEvent.setup()
  window.history.replaceState({}, '', '/strategies/7')
  render(<App />)
  const strip = await screen.findByLabelText('Strategy running P&L')
  expect(strip).toHaveTextContent('+$42.50')
  expect(strip).toHaveTextContent('Strategy-attributed fills only')
  await user.click(screen.getByRole('tab', {name: 'Ledger'}))
  expect(await screen.findByText('Channel breakout above 124')).toBeInTheDocument()
  expect(screen.getByText('Target set')).toBeInTheDocument()
})

test('strategy detail displays the exact durable workflow, trace, and execution-price provenance', async () => {
  window.history.replaceState({}, '', '/strategies/7')
  render(<App />)
  expect(await screen.findByRole('heading', {name: 'Portable breakout'})).toBeInTheDocument()
  expect(screen.getByText('8d57bc42-0ee4-4314-b2a3-3d693e62a111')).toBeInTheDocument()
  expect(screen.getByText('Activation queued')).toBeInTheDocument()
  expect(screen.getByText('Warm-up complete')).toBeInTheDocument()
  expect(screen.getByText('Waiting for live bar')).toBeInTheDocument()
  expect(screen.getByText('Order acknowledged')).toBeInTheDocument()
  expect(screen.getByText('Partially filled or filled')).toBeInTheDocument()
  expect(screen.getAllByText('IBKR').length).toBeGreaterThan(0)
  expect(screen.getByText('ibkr_live')).toBeInTheDocument()
  expect(screen.getByText('Fresh for execution')).toBeInTheDocument()
})

test('shows activation retry only when the backend marks the failed operation retryable', async () => {
  const user = userEvent.setup()
  const original = {
    state: strategy.state,
    enabled: strategy.enabled,
    block_reason: strategy.block_reason,
    activation_operation: strategy.activation_operation,
  }
  strategy.state = 'BLOCKED'
  strategy.enabled = false
  strategy.block_reason = 'Portfolio Gateway connectivity proof is stale'
  strategy.activation_operation = {
    ...strategy.activation_operation,
    status: 'FAILED',
    retryable: true,
    message: strategy.block_reason,
    idempotency_key: 'retry-activation-71',
  }
  window.history.replaceState({}, '', '/strategies/7')
  render(<App />)
  const retry = await screen.findByRole('button', {name: 'Retry activation'})
  expect(screen.getByText('Backend retryable: Yes')).toBeInTheDocument()
  await user.click(retry)
  const call = vi.mocked(fetch).mock.calls.find(([input, init]) =>
    String(input).includes('/strategy-instances/7/enable/') && init?.method === 'POST',
  )
  const headers = new Headers(call?.[1]?.headers)
  expect(headers.get('Idempotency-Key')).toBe('retry-activation-71')
  expect(headers.get('Idempotency-Retry')).toBe('true')
  Object.assign(strategy, original)
})

test('does not offer activation retry for a non-retryable backend blocker', async () => {
  const original = {
    state: strategy.state,
    enabled: strategy.enabled,
    block_reason: strategy.block_reason,
    activation_operation: strategy.activation_operation,
  }
  strategy.state = 'BLOCKED'
  strategy.enabled = false
  strategy.block_reason = 'Strategy definition is disabled'
  strategy.activation_operation = {
    ...strategy.activation_operation,
    status: 'FAILED',
    retryable: false,
    message: strategy.block_reason,
  }
  window.history.replaceState({}, '', '/strategies/7')
  render(<App />)
  await screen.findByRole('heading', {name: 'Portable breakout'})
  expect(screen.queryByRole('button', {name: 'Retry activation'})).not.toBeInTheDocument()
  expect(screen.getByText('Backend retryable: No')).toBeInTheDocument()
  Object.assign(strategy, original)
})

test('order drawer displays the exact broker rejection reason and explicit empty fallback', async () => {
  const user = userEvent.setup()
  window.history.replaceState({}, '', '/activity')
  render(<App />)
  await user.click(await screen.findByRole('button', {name: 'active-order'}))
  expect(await screen.findByText(/Code 201 · Order rejected - insufficient available equity · ibkr · IBKR Inactive/)).toBeInTheDocument()
  expect(screen.getByText(/No broker reason received · ibkr · IBKR Inactive/)).toBeInTheDocument()
})

test('order filtering, modification, and cancellation preserve the guarded OMS workflow', async () => {
  const user = userEvent.setup()
  window.history.replaceState({}, '', '/activity')
  render(<App />)
  const search = await screen.findByLabelText('Search orders')
  await user.type(search, 'filled-order')
  expect(screen.queryByRole('button', {name: 'active-order'})).not.toBeInTheDocument()
  expect(screen.getByRole('button', {name: 'filled-order'})).toBeInTheDocument()
  await user.clear(search)
  await user.click(await screen.findByRole('button', {name: 'active-order'}))
  await user.click(await screen.findByRole('button', {name: 'Submit modification'}))
  await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/orders/active-order-123/'), expect.objectContaining({method: 'PATCH'})))
  await user.click(screen.getByRole('button', {name: 'Cancel order'}))
  const dialog = screen.getByRole('dialog', {name: 'Cancel NVDA order?'})
  await user.type(within(dialog).getByLabelText('Reason'), 'Operator cancelled stale intent')
  await user.click(within(dialog).getByRole('button', {name: 'Request cancellation'}))
  await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/orders/active-order-123/cancel/'), expect.objectContaining({method: 'POST'})))
})

test('desktop order blotter supports keyboard row inspection without changing mobile navigation state', async () => {
  vi.stubGlobal('matchMedia', vi.fn().mockImplementation((query: string) => ({
    matches: query === '(min-width: 1200px)', media: query, onchange: null,
    addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
  })))
  const user = userEvent.setup()
  window.history.replaceState({}, '', '/activity')
  render(<App />)
  const orderButton = await screen.findByRole('button', {name: 'active-order'})
  const row = orderButton.closest('tr') as HTMLTableRowElement
  row.focus()
  await user.keyboard('{Enter}')
  expect(await screen.findByRole('button', {name: 'Close selected order'})).toBeInTheDocument()
  expect(row).toHaveAttribute('aria-selected', 'true')
  expect(screen.queryByRole('dialog', {name: /NVDA BUY/})).not.toBeInTheDocument()
  expect(useWorkspacePreferences.getState().mobileNavigationOpen).toBe(false)
})

test('keeps the shell usable during a route-level partial failure', async () => {
  failDashboard = true
  render(<App />)
  expect(await screen.findByText('Dashboard summary is unavailable')).toBeInTheDocument()
  expect(screen.getByRole('link', {name: 'System'})).toBeInTheDocument()
  expect(screen.getByRole('button', {name: 'Retry'})).toBeInTheDocument()
})

test('uses the API and application base path contracts', async () => {
  render(<App />)
  await waitFor(() => expect(fetch).toHaveBeenCalled())
  expect(String(vi.mocked(fetch).mock.calls[0][0])).toContain('/api/v1/')
  expect(appBasename()).toBe('/')
  expect(normalizeBasename('/qfsplatform/trading_engine_frontend/')).toBe('/qfsplatform/trading_engine_frontend')
})

test('responsive navigation has an accessible mobile toggle', async () => {
  render(<App />)
  expect(await screen.findByRole('button', {name: 'Open navigation'})).toBeInTheDocument()
})

test('desktop sidebar mode persists without opening mobile navigation', async () => {
  const user = userEvent.setup()
  render(<App />)
  const shell = (await screen.findByRole('navigation', {name: 'Primary navigation'})).closest('.app-shell')
  expect(shell).toHaveClass('sidebar-expanded')
  await user.click(screen.getByRole('button', {name: 'Use compact sidebar'}))
  expect(shell).toHaveClass('sidebar-compact')
  expect(shell?.querySelector('.sidebar')).not.toHaveClass('sidebar-open')
  expect(localStorage.getItem('finflock-workspace-v1')).toContain('compact')
  await user.click(screen.getByRole('button', {name: 'Open navigation'}))
  expect(shell?.querySelector('.sidebar')).toHaveClass('sidebar-open')
  expect(useWorkspacePreferences.getState().sidebarMode).toBe('compact')
})
