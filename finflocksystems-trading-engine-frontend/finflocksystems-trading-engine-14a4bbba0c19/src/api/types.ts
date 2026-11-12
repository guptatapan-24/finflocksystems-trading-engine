export type Scalar = string | number | boolean | null
export type JsonRecord = Record<string, unknown>
export type DecimalValue = string | number | null
export type ExecutionMode = 'PAPER' | 'LIVE'
export type ExecutionRunType = 'PREVIEW' | 'EXECUTION'

export interface ApiProblem {
  code: string
  message: string
  details: unknown
}

export interface ApiEnvelope<T> {
  ok: boolean
  data: T | null
  error: ApiProblem | null
  meta: Record<string, unknown>
}

export interface SystemStatus {
  mode: 'MULTI_SESSION'
  execution_modes: ExecutionMode[]
  execution_mode_source: 'PORTFOLIO_GATEWAY_SESSION'
  allow_live_trading: boolean
  is_admin?: boolean
  broker_deployment: {
    available: boolean
    ready: boolean
    missing: string[]
    invalid: string[]
  }
  global_kill_switch: boolean
  material_breaks: number
  time: string
}

export interface GatewayStatus {
  connected: boolean
  reconciled: boolean
  mode: BrokerSessionMode
  last_callback?: string | null
  worker?: string
}

export type BrokerSessionMode = 'paper' | 'live'
/** What the IBKR login is entitled to; the child requests exactly this type. */
export type BrokerMarketDataType = 'LIVE' | 'DELAYED'
export type BrokerSessionStatus = 'CREATING' | 'STARTING' | 'WAITING_FOR_LOGIN' | 'WAITING_FOR_2FA' | 'CONNECTED' | 'DISCONNECTED' | 'LOGIN_FAILED' | 'ERROR' | 'STOPPING' | 'DELETED'

export interface BrokerSessionAccount extends BrokerAccount {
  available: boolean
  last_seen_at: string
  default_portfolio_id: number | null
}

export interface BrokerGatewaySession {
  id: string
  display_name: string
  username_hint: string
  mode: BrokerSessionMode
  status: BrokerSessionStatus
  connected: boolean
  commands_enabled: boolean
  container_status: string
  account_count: number
  last_error: string
  last_gateway_state: JsonRecord
  created_at: string
  updated_at: string
  provisioned_at: string | null
  connected_at: string | null
  last_checked_at: string | null
  deleted_at: string | null
  needs_novnc: boolean
  novnc_url: string | null
  market_data_type: BrokerMarketDataType
  accounts?: BrokerSessionAccount[]
  default_portfolio_id?: number | null
}

export interface BrokerAccount {
  id: number
  account_id: string
  alias: string
  base_currency: string
  net_liquidation: DecimalValue
  available_cash: DecimalValue
  buying_power: DecimalValue
  daily_pnl: DecimalValue
  is_reconciled: boolean
  kill_switch: boolean
  updated_at: string
}

export interface Portfolio {
  id: number
  name: string
  account_id?: number
  account?: string
  gateway_session_id?: string | null
  cash_buffer_pct: DecimalValue
  margin_buffer_pct: DecimalValue
  minimum_notional: DecimalValue
  minimum_quantity: DecimalValue
  minimum_drift: DecimalValue
  kill_switch: boolean
}

export interface Instrument {
  id: number
  symbol: string
  asset_class: string
  exchange: string
  primary_exchange: string
  currency: string
  sector: string
  multiplier: DecimalValue
  lot_size: DecimalValue
  min_tick: DecimalValue
  fractional_support: boolean
  trading_calendar: string
  active: boolean
  tradable: boolean
  expiration?: string | null
  strike?: DecimalValue
  right?: 'C' | 'P' | null
  trading_class?: string
  underlying_conid?: number | null
}

export interface Position {
  id: number
  portfolio_id: number
  portfolio: string
  account_id: string
  instrument_id: number
  symbol: string
  asset_class: string
  currency: string
  quantity: DecimalValue
  average_cost: DecimalValue
  market_price: DecimalValue
  market_value: DecimalValue
  updated_at: string
}

export interface Order {
  id: number
  internal_id: string
  account_id: string
  portfolio_id?: number
  instrument_id?: number
  origin?: string
  symbol: string
  asset_class: string
  exchange: string
  currency: string
  expiration?: string | null
  strike?: DecimalValue
  right?: 'C' | 'P' | null
  multiplier?: DecimalValue
  trading_class?: string
  underlying_conid?: number | null
  side: string
  order_type: string
  time_in_force: string
  broker_order_id: string
  broker_permanent_id: string
  status: string
  quantity: DecimalValue
  filled_quantity: DecimalValue
  average_fill_price: DecimalValue
  created_at: string
  updated_at: string
}

export interface OrderStatusHistory {
  id: number
  from_status: string
  to_status: string
  broker_status: string
  reason_code: string
  reason: string
  source: string
  details: JsonRecord
  occurred_at: string
  operator_requested: boolean
}

export interface OrderDetail {
  order: Order
  status_history: OrderStatusHistory[]
  broker_diagnostics: OrderStatusHistory[]
  risk_decisions: JsonRecord[]
  fills: JsonRecord[]
  strategy_attribution: JsonRecord[]
}

export interface BrokerCommandSummary {
  id: number
  command_type: string
  status: string
  attempt_count: number
  gateway_command_id: string | null
  broker_order_id?: string
  broker_permanent_id?: string
  last_error?: string
  uncertainty_reason?: string
}

export interface ManualOrderIntentStatus {
  intent_id: number
  origin: string
  operation_status: string
  operation_error?: string
  retryable: boolean
  attempt_count?: number
  message: string
  internal_id?: string
  status?: string
  approved_quantity?: DecimalValue
  broker_order_id?: string
  broker_permanent_id?: string
  filled_quantity?: DecimalValue
  average_fill_price?: DecimalValue
  fill_count?: number
  broker_command?: BrokerCommandSummary | null
  confirmation?: {
    required: true
    warning_code: string
    warning_message: string
    broker_order_id: string
    can_confirm: boolean
    override_options?: Array<{
      code: string
      text: string
    }>
  }
}

export interface ManualOrderQuoteStatus {
  lease_key: string
  lease_expires_at: string
  gateway_session_id: string
  instrument_id: number
  timeframe: string
  subscription_state: string
  subscription_provider: string | null
  market_state: string
  execution_usable: boolean
  reference_price: DecimalValue
  provider: string
  source: string
  latest_event_at: string | null
  age_seconds: number | null
  display_status: string
}

export interface Execution {
  id: number
  order_id: string
  account_id: string
  symbol: string
  execution_id: string
  quantity: DecimalValue
  price: DecimalValue
  commission: DecimalValue
  currency: string
  executed_at: string
}

export interface AuditEvent {
  id: number
  event_type: string
  actor: string
  aggregate_type: string
  aggregate_id: string
  data: JsonRecord
  created_at: string
}

export interface KillSwitch {
  id: number
  scope: string
  scope_id: string
  enabled: boolean
  reason: string
  updated_at: string
}

export interface RiskDecision {
  id: number
  order_intent_id: number
  check_name: string
  decision: string
  reason: string
  requested_quantity: DecimalValue
  approved_quantity: DecimalValue
  created_at: string
}

export interface RiskSummary {
  kill_switches: KillSwitch[]
  decisions: RiskDecision[]
}

export interface ReconciliationRun {
  id: number
  trigger: string
  status: string
  started_at: string
  completed_at: string | null
}

export interface ReconciliationBreak {
  id: number
  run_id: number
  category: string
  severity: string
  internal_value: unknown
  broker_value: unknown
  material: boolean
  resolved: boolean
  resolution: string
  created_at: string
}

export interface ReconciliationSummary {
  runs: ReconciliationRun[]
  breaks: ReconciliationBreak[]
}

export interface StreamMetric {
  id?: number
  component: string
  metric: string
  status: string
  value: Scalar | JsonRecord
  observed_at: string
}

export interface FlinkJob {
  id?: string
  name?: string
  state?: string
  [key: string]: unknown
}

export interface StreamingHealth {
  kafka_enabled: boolean
  data_path_status: string
  data_path_reasons: string[]
  gateway: {status: string; value: JsonRecord; observed_at: string | null}
  consumer: {status: string; last_heartbeat: string | null; age_seconds?: number; value: JsonRecord}
  metrics: StreamMetric[]
  flink: {status?: string; jobs?: FlinkJob[]; error?: string}
  strategies: StrategyStreamStatus[]
  outbox_pending: number
  outbox_failed: number
  dead_letter_count: number
  stale_instrument_count: number
}

export interface StrategyStreamStatus {
  strategy_id: number
  strategy: string
  symbol: string
  timeframe: string
  enabled: boolean
  lifecycle_state: string
  activation_status: string
  status: string
  subscription_state: string
  active_provider: string
  fallback_state: string
  fallback_reason: string
  provider_generation: string | null
  conid: number | null
  last_raw_event: string | null
  last_canonical_event: string | null
  last_final_bar: string | null
  warmup_progress: number
  warmup_required: number
  last_indicator: string | null
  last_strategy_run: string | null
  last_error: string
  missing: string[]
  stale_after_seconds: number
}

export interface ParameterProperty {
  type?: 'integer' | 'number' | 'string' | 'boolean'
  title?: string
  description?: string
  enum?: Scalar[]
  minimum?: number
  maximum?: number
  exclusiveMinimum?: number
  exclusiveMaximum?: number
  default?: Scalar
}

export interface ParameterSchema {
  type?: string
  required?: string[]
  properties?: Record<string, ParameterProperty>
  additionalProperties?: boolean
}

export interface InputRequirement {
  identity_hash?: string
  input_type: string
  name: string
  role?: string
  parameters: JsonRecord
  implementation_version?: number
  warmup_bars: number
  shared_by?: number
  active?: boolean
}

/** Read-only risk profiles from `GET strategy-risk-profiles/`. The Backend owns every number. */
export interface RiskProfileCatalogue {
  enabled: boolean
  default_profile: string
  profile_names: string[]
  definition_keys: string[]
  summary_fields: {key: string; label: string; type: string}[]
  profiles: Record<string, {summary: Record<string, Scalar>; parameters: Record<string, Scalar>}>
}

export interface StrategyDefinition {
  id: number
  key: string
  name: string
  description: string
  plugin_path: string
  input_requirements: InputRequirement[]
  parameter_schema: ParameterSchema
  default_parameters: Record<string, Scalar>
  supported_asset_types: string[]
  supported_directions: string[]
  supported_timeframes: string[]
  version: number
  enabled: boolean
  strategy_kind: 'DIRECTIONAL' | 'OPTIONS' | string
  execution_model: 'SINGLE_INSTRUMENT_WEIGHT' | 'MULTI_LEG_QUANTITY' | string
}

export interface OptionExecutionLeg {
  instrument_id: number
  symbol?: string
  conid?: number
  role?: string
  side?: string
  quantity?: DecimalValue
  target_quantity?: DecimalValue
  right: 'C' | 'P'
  strike: DecimalValue
  expiration: string
  multiplier: DecimalValue
  dhan_oi?: number
  dhan_volume?: number
  dhan_price?: DecimalValue
}

/** One candidate structure the selector graded, with the reason it was refused. */
export interface OptionSelectionCandidate {
  strategy_key?: string
  eligible?: boolean
  rejection_reason?: string | null
  pop?: DecimalValue | null
  max_loss?: DecimalValue | null
  expected_pnl?: DecimalValue | null
  tqs?: DecimalValue | null
  /** The range/IV/breach gate this candidate was measured against. */
  entry_signal?: OptionEntrySignal | null
}

export interface OptionSelectionEvidence {
  reason?: string | null
  /** Inputs the decision could not measure; their TQS weight is dropped rather
   * than scored zero, so this is what the score actually rests on. */
  unavailable_inputs?: string[]
  rankings?: OptionSelectionCandidate[]
  implied_volatility?: DecimalValue | null
  realized_volatility?: DecimalValue | null
  model?: string | null
}

export interface OptionEntrySignal {
  entry_allowed?: boolean
  reasons?: string[]
  /** The backend field; `score` is kept for older payloads. */
  iv_score?: DecimalValue | null
  score?: DecimalValue | null
  iv_condition?: string | null
  either_breach_probability?: DecimalValue | null
  breach_boundary_basis?: string | null
  favourable?: boolean
  iv_rank?: DecimalValue | null
  iv_percentile?: DecimalValue | null
  iv_rank_source?: string | null
  current_iv?: DecimalValue | null
  realized_volatility?: DecimalValue | null
  upper_breach_probability?: DecimalValue | null
  lower_breach_probability?: DecimalValue | null
}

/** The entry decision for the most recent evaluated bar, self-contained: nothing
 * in it is carried over from an earlier bar. */
export interface OptionEntryCheck {
  status: string
  reason: string
  evaluated_at?: string | null
  entry_score?: DecimalValue | null
  entry_threshold?: DecimalValue | null
  iv_score_threshold?: DecimalValue | null
  entry_score_components?: Record<string, DecimalValue | null> | null
  spot?: DecimalValue | null
  iv_rank_252?: DecimalValue | null
  iv_percentile_252?: DecimalValue | null
  chain_features_available?: boolean | null
  selection_evidence?: OptionSelectionEvidence | null
}

export interface OptionExecutionState {
  model_version: string | null
  status: string
  readiness_reason?: string | null
  last_entry_check?: OptionEntryCheck | null
  entry_threshold?: DecimalValue | null
  iv_score_threshold?: DecimalValue | null
  selected_strategy: string | null
  entry_score: DecimalValue | null
  entry_score_scale: DecimalValue
  entry_score_weights: Record<string, DecimalValue>
  /** Per-factor RPS scores. A null factor could not be measured and its weight
   * is renormalised away rather than scored zero. */
  entry_score_components?: Record<string, DecimalValue | null> | null
  entry_signal?: OptionEntrySignal | null
  selection_evidence?: OptionSelectionEvidence | null
  exit_evaluation?: JsonRecord | null
  selection_provider?: string | null
  observed_at: string | null
  expiration: string | null
  atr_reference: DecimalValue | null
  atr_value: DecimalValue | null
  atr_multiplier: DecimalValue
  atr_support: DecimalValue | null
  atr_resistance: DecimalValue | null
  oi_support: DecimalValue | null
  oi_resistance: DecimalValue | null
  spot: DecimalValue | null
  candidate_rankings: JsonRecord[]
  legs: OptionExecutionLeg[]
}

export interface StrategyVersion {
  id: number
  version: number
  parameter_hash: string
  configuration_snapshot: JsonRecord
  created_at: string
  activated_at: string | null
  retired_at: string | null
}

export interface StrategyInstance {
  id: number
  name: string
  definition_key: string
  definition_name: string
  portfolio_id: number
  portfolio: string
  gateway_session_id?: string | null
  gateway_session_name?: string | null
  instrument_id: number
  symbol: string
  asset_class: string
  exchange: string
  currency: string
  conid: number | null
  primary_exchange: string | null
  timeframe: string
  parameters: JsonRecord
  target_configuration: JsonRecord
  risk_policy_id: number | null
  order_policy_id: number | null
  execution_mode: ExecutionMode
  state: string
  enabled: boolean
  activation_status: string
  version: number
  warmup_progress: number
  warmup_required: number
  warmup_started_at: string | null
  warmup_last_progress_at: string | null
  subscription_ready_at: string | null
  warmup_completed_at: string | null
  ready_waiting_since: string | null
  first_evaluation_completed_at: string | null
  execution_active_at: string | null
  activation_stages: {
    subscription_ready: boolean
    warmup_complete: boolean
    waiting_for_live_bar: boolean
    first_evaluation_complete: boolean
    execution_active: boolean
  }
  activation_operation?: {
    id: number
    status: 'PROCESSING' | 'COMPLETED' | 'FAILED'
    retryable: boolean
    message: string
    attempt_count: number
    idempotency_key: string
    created_at: string
    completed_at: string | null
  } | null
  execution_workflow?: {
    trace_id: string
    status: string
    active: boolean
    terminal: boolean
    current_stage: string
    poll_after_ms: number
    observed_at: string
  }
  block_reason: string
  effective_from: string | null
  effective_to: string | null
  last_final_bar: string | null
  latest_indicators: Record<string, DecimalValue>
  latest_signal: string | null
  current_target: DecimalValue
  attributed_quantity: DecimalValue
  active_order: string | null
  last_fill: string | null
  cooldown: string | null
  current_price?: {
    value: DecimalValue
    provider: string
    source: string
    data_kind: 'LIVE' | 'WARM_UP'
    timestamp: string | null
    age_seconds: number | null
    stale_after_seconds: number | null
    fresh_for_execution: boolean
  }
  streaming?: StrategyStreamStatus
  created_at: string
  updated_at: string
  versions?: StrategyVersion[]
  requirements?: InputRequirement[]
  qualification_command?: JsonRecord | null
  option_execution?: OptionExecutionState
}

export interface StrategyTimelineItem {
  time?: string | null
  type?: string
  id?: number
  status: string
  version?: number | null
  detail?: string
  stage?: string
  label?: string
  occurred_at?: string | null
  blocker?: string
  retryable?: boolean
  entity_type?: string
  entity_id?: string | null
  trace_id?: string
  workflow_status?: string
  workflow_active?: boolean
  workflow_terminal?: boolean
}

export interface ExecutionReadinessSignal {
  status: string
  healthy?: boolean
  last_heartbeat?: string | null
  age_seconds?: number | null
  [key: string]: unknown
}

export interface ExecutionDiagnostics {
  ready: boolean
  automatic_execution_ready: boolean
  status: string
  observed_at: string | null
  blockers: {code: string; message: string; details: JsonRecord}[]
  signals: {
    kafka?: ExecutionReadinessSignal
    flink?: ExecutionReadinessSignal
    kafka_consumer?: ExecutionReadinessSignal
    workers?: Record<string, ExecutionReadinessSignal>
    gateway?: ExecutionReadinessSignal
    broker_reconciliation?: ExecutionReadinessSignal
    [key: string]: unknown
  }
}

export interface StrategyChartBar {
  time: string
  open: DecimalValue
  high: DecimalValue
  low: DecimalValue
  close: DecimalValue
  volume: DecimalValue
  version: number
}

export interface StrategyChartIndicator {
  time: string
  name: string
  value: DecimalValue
}

export interface StrategyChartMarker {
  time: string
  type: 'SIGNAL' | 'TARGET' | 'ORDER' | 'FILL'
  label: string
  value?: DecimalValue
}

/** An executed entry or exit of the whole strategy (every leg as one point),
 * or the latest decision still waiting for its fills. */
export interface StrategyTradeMarker {
  time: string
  type: 'ENTRY' | 'EXIT'
  status: 'FILLED' | 'PENDING'
  label: string
}

/** The frozen ATR boundary in force for a bar. */
export interface StrategyAtrBand {
  time: string
  support: DecimalValue
  resistance: DecimalValue
}

export interface StrategyChartData {
  bars: StrategyChartBar[]
  indicators: StrategyChartIndicator[]
  markers: StrategyChartMarker[]
  trade_markers?: StrategyTradeMarker[]
  atr_bands?: StrategyAtrBand[]
  source: string
}

export type StrategyLedgerDecision =
  | 'ENTERED' | 'EXITED' | 'HOLDING' | 'ORDERS_WORKING' | 'NO_TRADE' | 'TARGET_SET'
  | 'ERROR' | 'NOT_EVALUATED' | 'AWAITING_QUOTES'

export interface StrategyLedgerCandidate {
  strategy_key?: string | null
  eligible: boolean
  rejection_reason?: string | null
  iv_score?: DecimalValue | null
  tqs?: DecimalValue | null
  pop?: DecimalValue | null
  expected_pnl?: DecimalValue | null
  max_loss?: DecimalValue | null
}

export interface StrategyLedgerOrder {
  id: string
  symbol: string
  side: string
  quantity: DecimalValue
  filled_quantity: DecimalValue
  average_fill_price: DecimalValue
  status: string
}

/** One evaluated bar: what was decided, on what numbers, and why. */
export interface StrategyLedgerRow {
  id: string
  run_id: number | null
  time: string | null
  evaluated_at: string | null
  bar_close: DecimalValue | null
  spot: DecimalValue | null
  decision: StrategyLedgerDecision
  signal_type: string | null
  status: string | null
  gate: string | null
  rps: DecimalValue | null
  rps_threshold: DecimalValue | null
  rps_passed: boolean | null
  iv_score: DecimalValue | null
  iv_score_threshold: DecimalValue | null
  iv_passed: boolean | null
  iv_rank: DecimalValue | null
  iv_percentile: DecimalValue | null
  tqs: DecimalValue | null
  tqs_threshold: DecimalValue | null
  tqs_passed: boolean | null
  atr_support: DecimalValue | null
  atr_resistance: DecimalValue | null
  oi_support: DecimalValue | null
  oi_resistance: DecimalValue | null
  selected_strategy: string | null
  candidates: StrategyLedgerCandidate[]
  orders: StrategyLedgerOrder[]
  target_count: number
  reason: string
  reason_details: string[]
}

export interface StrategyPnlLeg {
  instrument_id: number
  symbol: string
  quantity: DecimalValue
  average_price: DecimalValue
  mark_price: DecimalValue | null
  mark_source: string | null
  mark_fresh: boolean
  multiplier: DecimalValue
  realized_pnl: DecimalValue
  unrealized_pnl: DecimalValue
  open: boolean
}

export interface StrategyPnlPoint {
  time: string
  realized: DecimalValue
  unrealized: DecimalValue
  total: DecimalValue
  mark_basis: 'LAST_FILL' | 'CURRENT_MARK'
}

/** P&L of this strategy's attributed fills only, never the account's. */
export interface StrategyPnl {
  currency: string
  running_pnl: DecimalValue
  realized_pnl: DecimalValue
  gross_realized_pnl: DecimalValue
  unrealized_pnl: DecimalValue
  commissions: DecimalValue
  open_position: boolean
  marks_complete: boolean
  marks_fresh: boolean
  fill_count: number
  legs: StrategyPnlLeg[]
  series: StrategyPnlPoint[]
  as_of: string
  scope: string
}

export interface RiskPolicy {
  id: number
  name: string
  maximum_weight: DecimalValue
  maximum_notional: DecimalValue
  maximum_quantity: DecimalValue
  allow_short: boolean
}

export interface OrderPolicy {
  id: number
  name: string
  order_type: string
  time_in_force: string
  limit_offset_bps: DecimalValue
  price_collar_bps: DecimalValue
  allow_market_order: boolean
  replace_after_seconds: number
  maximum_replacements: number
  cancel_at_session_end: boolean
  outside_regular_hours: boolean
}

export interface StrategyPolicies {
  risk_policies: RiskPolicy[]
  order_policies: OrderPolicy[]
}

export interface InstrumentResolution {
  instrument_id: number
  symbol: string
  asset_class: string
  exchange: string
  currency: string
  conid: number | null
  primary_exchange: string | null
  qualification_command: JsonRecord | null
  expiration?: string | null
  strike?: DecimalValue
  right?: 'C' | 'P' | null
  multiplier?: DecimalValue
  trading_class?: string
  underlying_conid?: number | null
}

export interface InstrumentSearchResult {
  symbol: string
  local_symbol: string
  conid: number
  asset_class: string
  exchange: string
  primary_exchange: string
  currency: string
  description: string
  instrument_id: number | null
  expiration?: string | null
  strike?: DecimalValue
  right?: 'C' | 'P' | null
  multiplier?: DecimalValue
  trading_class?: string
  underlying_conid?: number | null
}

export interface OptionChainDefinition {
  exchange: string
  trading_class: string
  multiplier: DecimalValue
  expirations: string[]
  strikes: string[]
}

export interface OptionChain {
  underlying: InstrumentSearchResult
  chains: OptionChainDefinition[]
}

export interface SeriesPoint {
  time: string
  value: number
}

export interface ExposurePoint {
  time: string
  gross: number
  net: number
}

export interface InstrumentAllocation {
  instrument_id: number
  symbol: string
  value: number
  weight: number
}

export interface PortfolioSeries {
  portfolio_id: number
  source: string
  nav: SeriesPoint[]
  pnl: SeriesPoint[]
  exposure: ExposurePoint[]
  allocation_by_instrument: InstrumentAllocation[]
}

export interface AttentionItem {
  id: string
  severity: 'INFO' | 'WARNING' | 'CRITICAL'
  title: string
  detail: string
}

export interface DashboardSummary {
  mode: string
  account: BrokerAccount | null
  portfolio: Portfolio | null
  gateway: GatewayStatus | null
  gateway_error?: string | null
  reconciliation_status: string
  nav: DecimalValue
  cash: DecimalValue
  buying_power: DecimalValue
  daily_pnl: DecimalValue
  gross_exposure: DecimalValue
  net_exposure: DecimalValue
  active_strategies: number
  open_orders: number
  positions: number
  recent_activity: AuditEvent[]
  attention: AttentionItem[]
  updated_at: string
}

export interface AllocationPolicy {
  id: number
  portfolio_id: number
  portfolio: string
  strategy_id: number
  strategy: string
  target_share: DecimalValue
  minimum_share: DecimalValue
  maximum_share: DecimalValue
  capacity: DecimalValue
  minimum_allocation: DecimalValue
  priority: number
  enabled: boolean
}

export interface AllocationRun {
  id: number
  flow_id: number
  portfolio_id: number
  flow_type: string
  amount: DecimalValue
  approved_amount: DecimalValue
  unallocated_amount: DecimalValue
  liquidation_policy: string
  allocation_mode: string
  optimization_run_id: number | null
  status: string
  created_at: string
}

export interface RebalancePolicy {
  id: number
  portfolio_id: number
  instrument_drift_threshold: DecimalValue
  portfolio_drift_threshold: DecimalValue
  minimum_trade_notional: DecimalValue
  minimum_trade_quantity: DecimalValue
  cash_buffer_percent: DecimalValue
  fee_buffer: DecimalValue
  maximum_turnover: DecimalValue
  sell_before_buy: boolean
  price_staleness_limit: number
  partial_fill_threshold: DecimalValue
  mode: ExecutionMode
  enabled: boolean
  updated_at: string
}

export interface RebalanceTarget {
  id: number
  instrument_id: number
  target_weight: DecimalValue
  current_weight: DecimalValue
  drift: DecimalValue
  current_quantity: DecimalValue
  target_quantity: DecimalValue
  trade_quantity: DecimalValue
  reference_price: DecimalValue
  estimated_cost: DecimalValue
  suppressed: boolean
  suppression_reason: string
  rank: number
}

export interface RebalanceRun {
  id: number
  portfolio_id: number
  trigger: string
  mode: ExecutionMode
  run_type: ExecutionRunType
  status: string
  phase: string
  nav: DecimalValue
  total_drift: DecimalValue
  planned_turnover: DecimalValue
  target_source: string
  optimization_run_id: number | null
  created_at: string
  last_recalculated_at: string | null
  targets?: RebalanceTarget[]
}

export interface PositionSizingDecision {
  id: number
  instrument_id: number
  side: string
  target_quantity: DecimalValue
  risk_quantity: DecimalValue
  weight_quantity: DecimalValue
  liquidity_quantity: DecimalValue
  cash_quantity: DecimalValue
  broker_quantity: DecimalValue
  approved_quantity: DecimalValue
  binding_constraint: string
  rejected_reason: string
}

// Nightly NFO ingestion status, read from the ingestion's own ledger (backend: GET data-providers/nfo-ingestion/).
export type NfoIngestionState = 'UP_TO_DATE' | 'BEHIND' | 'FAILED' | 'EMPTY' | 'UNAVAILABLE'

export interface NfoIngestionSession {
  session: string
  state: string
  rows: number
  completed_at: string | null
  updated_at: string | null
  error_category: string | null
}

export interface NfoIngestionLastRun {
  run_date: string | null
  attempt: number | null
  status: string | null
  exit_code: number | null
  code: string | null
  message: string
  sessions: Array<string | null>
  failed: {session: string; code: string} | null
  at: string
}

export interface NfoIngestionStatus {
  configured: boolean
  available: boolean
  state: NfoIngestionState
  message: string
  expected_session: string
  latest_session: string | null
  latest_completed_at: string | null
  latest_rows: number | null
  latest_source: string | null
  failure: {session: string; state: string; error_category: string | null; updated_at: string | null} | null
  recent: NfoIngestionSession[]
  last_run?: NfoIngestionLastRun | null
  checked_at: string
}

export interface FinnhubProviderStatus {
  provider: 'FINNHUB'
  configured: boolean
  enabled: boolean
  effective_source: 'ENVIRONMENT' | 'DATABASE' | 'NONE'
  environment_configured: boolean
  database_configured: boolean
  database_override_requested: boolean
  database_override_allowed: boolean
  database_override_active: boolean
  masked_api_key: string
  last_success_at: string | null
  last_tested_at: string | null
  last_test_success_at: string | null
  last_error: string
  rate_limit_state: Record<string, string>
  updated_at: string | null
  connected?: boolean
  source?: 'TRANSIENT' | 'ENVIRONMENT' | 'DATABASE'
}

export interface PortfolioUniverse {
  id: number
  portfolio_id: number
  name: string
  include_strategy_instruments: boolean
  minimum_history_observations: number
  maximum_instruments: number
  selected_count: number
  enabled: boolean
  instruments: {instrument_id: number; symbol: string; enabled: boolean}[]
  updated_at: string
}

export interface PortfolioOptimizationPolicy {
  id: number
  portfolio_id: number
  name: string
  method: 'MINIMUM_VARIANCE' | 'MAXIMUM_SHARPE'
  lookback_days: number
  return_estimation: string
  covariance_estimation: string
  risk_free_rate: DecimalValue
  target_cash_weight: DecimalValue
  minimum_weight: DecimalValue
  maximum_weight: DecimalValue
  maximum_turnover: DecimalValue
  transaction_cost_penalty: DecimalValue
  long_only: boolean
  enabled: boolean
  execution_mode: ExecutionMode
  version: number
  updated_at: string
}

export interface OptimizedPortfolioTarget {
  id: number
  instrument_id: number
  symbol: string
  current_weight: DecimalValue
  optimized_weight: DecimalValue
  weight_change: DecimalValue
  target_value: DecimalValue
  expected_return_contribution: DecimalValue
  risk_contribution: DecimalValue
  constraint_status: string
  rank: number
}

export interface PlannedOptimizationTrade {
  instrument_id: number
  symbol: string
  side: 'BUY' | 'SELL' | 'NONE'
  quantity: DecimalValue
  reference_price: DecimalValue
  estimated_cost: DecimalValue
  suppressed: boolean
  suppression_reason: string
}

export interface PortfolioOptimizationRun {
  id: number
  portfolio_id: number
  policy_id: number
  universe_id: number
  trigger: string
  status: string
  input_start_date: string | null
  input_end_date: string | null
  nav: DecimalValue
  objective_value: DecimalValue
  expected_return: DecimalValue
  expected_volatility: DecimalValue
  sharpe_ratio: DecimalValue
  turnover: DecimalValue
  cash_weight: DecimalValue
  solver_status: string
  warnings: unknown[]
  error_details: JsonRecord
  flow_reference: string
  application_status: 'NOT_APPLIED' | 'APPLYING' | 'APPLIED'
  applied_at: string | null
  created_at: string
  completed_at: string | null
  targets?: OptimizedPortfolioTarget[]
  planned_trades?: PlannedOptimizationTrade[]
  rebalance?: {id: number; mode: ExecutionMode; run_type: ExecutionRunType; status: string; phase: string; planned_turnover: DecimalValue} | null
  applied_rebalance?: {id: number; mode: ExecutionMode; run_type: ExecutionRunType; status: string; phase: string; planned_turnover: DecimalValue} | null
}

export type GoalTimeframe = 'NOW' | 'HURRY' | 'FAST' | 'BUILD' | 'GROW' | 'COMPOUND'

export interface GoalResolvedRules {
  timeframe_bucket: GoalTimeframe
  timeframe_label: string
  risk_level: number
  risk_code: string
  risk_label: string
  maximum_allowed_risk: number
  minimum_cash_weight: DecimalValue
  maximum_stock_weight: DecimalValue
  optimizer_method: 'MINIMUM_VARIANCE' | 'MAXIMUM_SHARPE' | null
  lookback_days: number
  minimum_history_observations: number
  long_only: boolean
}

export interface PortfolioGoalAllocation {
  id: number
  plan_id: number
  name: string
  allocation_weight: DecimalValue
  allocation_percentage: DecimalValue
  timeframe_bucket: GoalTimeframe
  risk_level: number
  enabled: boolean
  display_order: number
  construction_source: 'MANUAL_OPTIMIZER' | 'ACCEPTED_RECOMMENDATION'
  accepted_recommendation_run_id: number | null
  resolved_rules: GoalResolvedRules
  instrument_count: number
  created_at: string
  updated_at: string
}

export interface ConstructionValidationError {
  code: string
  message: string
  goal_id?: number
  allocated_weight?: DecimalValue
}

export interface PortfolioConstructionPlan {
  id: number
  portfolio_id: number
  name: string
  status: 'DRAFT' | 'ACTIVE' | 'PAUSED'
  version: number
  allocated_weight: DecimalValue
  allocated_percentage: DecimalValue
  enabled_goal_count: number
  ready_to_preview: boolean
  validation_errors: ConstructionValidationError[]
  timeframe_options: {code: GoalTimeframe; label: string}[]
  risk_options: {level: number; code: string; label: string}[]
  goals: PortfolioGoalAllocation[]
  created_at: string
  updated_at: string
}

export interface PortfolioBuilderReadinessBlocker {
  code: string
  message: string
  details: JsonRecord
}

export interface PortfolioBuilderReadiness {
  ready: boolean
  blockers: PortfolioBuilderReadinessBlocker[]
  details: JsonRecord
}

export interface GoalConstructionStrategy {
  assignment_id: number
  strategy_definition_id: number
  strategy_name: string
  strategy_share: DecimalValue
  portfolio_weight: DecimalValue
}

export interface GoalConstructionStock {
  instrument_id: number
  symbol: string
  goal_instrument_id: number
  goal_id: number
  goal_name: string
  goal_allocation_weight: DecimalValue
  local_weight: DecimalValue
  portfolio_contribution: DecimalValue
  strategy_share_total: DecimalValue
  strategy_share_valid: boolean
  strategies: GoalConstructionStrategy[]
}

export interface GoalConstructionResult {
  goal_id: number
  name: string
  allocation_weight: DecimalValue
  goal_nav: DecimalValue
  timeframe_bucket: GoalTimeframe
  risk_level: number
  optimizer_method: string | null
  construction_source: 'MANUAL_OPTIMIZER' | 'ACCEPTED_RECOMMENDATION'
  accepted_recommendation_run_id: number | null
  cash_weight: DecimalValue
  maximum_stock_weight: DecimalValue
  stocks: GoalConstructionStock[]
  metrics: {expected_return: DecimalValue; expected_volatility: DecimalValue; sharpe_ratio: DecimalValue}
  warnings: {code: string; message?: string}[]
  intentionally_cash_only: boolean
  apply_blocked: boolean
}

export interface RecommendationStock {
  instrument_id: number
  universe_member_id: number
  symbol: string
  company?: string
  gics?: {sector?: {code?: string; name?: string}; industry?: {code?: string; name?: string}}
  research_strategy_id: string
  execution_strategy_definition_id: number
  strategy_name?: string
  execution_timeframe: string
  weight: DecimalValue
  candidate_score?: DecimalValue
  expected_return: DecimalValue
  expected_volatility: DecimalValue
  expected_drawdown: DecimalValue
  reason: string
}

export interface RecommendationBatchGoal {
  goal_id: number
  goal_name: string
  status: 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'FAILED'
  recommendation_run_id: number | null
  fallback_tier: number
  stocks: RecommendationStock[]
  cash_weight: DecimalValue
  timeframe: GoalTimeframe
  risk_level: number
  metrics: JsonRecord
  freshness: JsonRecord
}

export interface RecommendationBatch {
  id: number
  plan_id: number
  requested_plan_version: number
  status: 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'FAILED'
  dataset_version_id: number
  protocol_version_id: number
  metrics: JsonRecord
  error: string
  goals: RecommendationBatchGoal[]
  created_at: string
  started_at: string | null
  completed_at: string | null
}

export interface PortfolioConstructionTarget {
  id: number
  instrument_id: number
  symbol: string
  current_weight: DecimalValue
  target_weight: DecimalValue
  weight_change: DecimalValue
  target_value: DecimalValue
  expected_return_contribution: DecimalValue
  risk_contribution: DecimalValue
  goal_contributions: {goal_id: number; goal_name: string; local_weight: DecimalValue; portfolio_contribution: DecimalValue}[]
  shared_across_goals: boolean
  rank: number
}

export interface PlannedConstructionTrade extends PlannedOptimizationTrade {
  current_weight: DecimalValue
  target_weight: DecimalValue
}

export interface PortfolioConstructionRun {
  id: number
  plan_id: number
  portfolio_id: number
  status: string
  application_status: 'NOT_APPLIED' | 'QUEUED' | 'APPLYING' | 'ACTIVATING' | 'APPLIED' | 'PARTIALLY_APPLIED' | 'FAILED'
  retryable: boolean
  last_error: string
  attempt_count: number
  nav: DecimalValue
  final_target_weights: {cash?: DecimalValue; stocks?: Record<string, DecimalValue>}
  metrics: {
    expected_return?: DecimalValue
    expected_volatility?: DecimalValue
    sharpe_ratio?: DecimalValue
    strategy_targets?: {
      identity: string
      strategy_definition_id: number
      strategy_name: string
      instrument_id: number
      symbol: string
      execution_timeframe: string
      target_weight: DecimalValue
      assignment_ids: number[]
    }[]
    application?: {
      construction_application: string
      rebalance_creation?: string
      initial_allocation_preview?: string
      strategy_execution_rebalance?: string
      strategy_creation: string
      strategy_activation: string
      market_subscription: string
    }
    strategy_instances?: {
      assignment_id: number
      strategy_instance_id: number
      target_weight: DecimalValue
      strategy_creation: string
      activation_status: string
      market_subscription: string
      enabled?: boolean
      active_provider?: string
      warmup_progress?: number
      warmup_required?: number
      subscription_ready?: boolean
      warmup_complete?: boolean
      waiting_for_live_bar?: boolean
      first_evaluation_complete?: boolean
      execution_active?: boolean
      block_reason?: string
    }[]
  }
  warnings: unknown[]
  goals?: GoalConstructionResult[]
  targets?: PortfolioConstructionTarget[]
  planned_trades?: PlannedConstructionTrade[]
  rebalance?: {id: number; mode: ExecutionMode; run_type: ExecutionRunType; status: string; phase: string; planned_turnover: DecimalValue} | null
  applied_rebalance?: {id: number; mode: ExecutionMode; run_type: ExecutionRunType; status: string; phase: string; planned_turnover: DecimalValue} | null
  applied_at: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
}
