// Intelligence layer API types (advisory-only backend module).
export type IntelligenceCircuitState = 'CLOSED' | 'OPEN'

export interface IntelligenceHealth {
  enabled: boolean
  provider: string
  models: {review_configured: boolean; deep_configured: boolean}
  api_key_configured: boolean | null
  prices_configured: boolean
  budget: {daily_usd: string; spent_today_usd: string}
  circuit_breaker: IntelligenceCircuitState
  limits: {max_input_tokens: number; max_output_tokens: number}
  rows: Record<string, number>
}

export type EpisodeStatus = 'OPEN' | 'CLOSED' | 'EXPIRED'

export interface IntelligenceEpisode {
  id: number
  strategy_key: string
  instance_id: number
  instance_name: string
  strategy_version: number
  instrument: string
  asset_family: 'STOCK' | 'OPTION'
  execution_mode: string
  currency: string
  direction: string
  status: EpisodeStatus
  opened_at: string
  closed_at: string | null
  gross_pnl: string
  commissions: string
  net_pnl: string
  pnl_basis: string
  return_on_risk: string | null
  hold_seconds: number | null
  exit_reason: string
  exit_subtype: string
  data_quality_flags: string[]
  fill_count: number
}

export interface IntelligenceEpisodeLeg {
  symbol: string
  right: string
  strike: string | null
  expiration: string | null
  entry_side: string
  entry_quantity: string
  entry_avg_price: string
  exit_quantity: string
  exit_avg_price: string
  realized_pnl: string
  commission: string
  entry_slippage_bps: string | null
  exit_slippage_bps: string | null
}

export interface IntelligenceEpisodeDetail extends IntelligenceEpisode {
  metrics: Record<string, string | number | boolean | string[]>
  metrics_version: string | null
  evidence_hash: string | null
  legs: IntelligenceEpisodeLeg[]
}

export interface EpisodeFilters {
  status?: string
  currency?: string
  limit?: number
}

export type SuggestionStatus = 'PROPOSED' | 'ACKNOWLEDGED' | 'ACCEPTED' | 'REJECTED' | 'APPLIED' | 'EXPIRED' | 'SUPERSEDED'

export interface IntelligenceSuggestion {
  id: number
  review_id: number
  instance_id: number
  instance_name: string
  kind: 'PARAMETER' | 'SIZING' | 'RISK' | 'PROCESS' | 'DATA' | 'CODE' | 'EXPERIMENT'
  parameter_key: string
  current_value: unknown
  proposed_value: unknown
  rationale: string
  expected_effect: string
  risk_of_change: string
  confidence: string | null
  evidence_needed: string
  evidence_refs: string[]
  is_hypothesis: boolean
  status: SuggestionStatus
  decided_by: string
  decision_reason: string
  applied_version_id: number | null
  experiment?: ExperimentInfo | null
}

export interface IntelligenceMistake {
  id: number
  category: string
  severity: 'LOW' | 'MEDIUM' | 'HIGH'
  description: string
  evidence_refs: string[]
  source: 'RULE' | 'LLM'
  rule_id: string
  is_defect: boolean
}

export interface IntelligenceReview {
  id: number
  episode_id: number
  verdict: string
  process_grade: string
  outcome_class: string
  summary: string
  confidence: string | null
  is_fallback: boolean
  prompt_version: string
  model: string
  sample_n: number
  strengths: {description: string; evidence_refs: string[]}[]
  open_questions: string[]
  rejected_items: unknown[]
  mistakes: IntelligenceMistake[]
  suggestions: IntelligenceSuggestion[]
  needs_rereview: boolean
}

export interface LiveSnapshot {
  id: number
  observed_at: string
  quote_freshness: string
  marks: Record<string, {price: number | null; quality: string}>
  mark_pnl: string | null
  pnl_bps: string | null
  pct_of_max_loss: string | null
  pct_of_max_profit: string | null
  mae_bps_so_far: string | null
  mfe_bps_so_far: string | null
  distances: Record<string, unknown> & {short_strikes?: {symbol: string; strike: number; right: string; distance_pct: number}[]}
  material: boolean
}

export interface LiveAlertRow {
  id: number
  episode_id: number
  rule_id: string
  severity: 'LOW' | 'MEDIUM' | 'HIGH'
  text: string
  occurrences: number
  first_seen: string
  last_seen: string
}

export interface OpenEpisodeRow extends IntelligenceEpisode {
  latest: LiveSnapshot | null
  alerts: {total: number; high: number}
}

export interface EpisodeLive {
  episode: IntelligenceEpisode
  latest: LiveSnapshot | null
  series: {t: string; mark_pnl: string | null}[]
  alerts: LiveAlertRow[]
  notes: string[]
  is_open: boolean
}

export interface LiveCommentary {
  commentary: string
  watch_items: string[]
  label: string
  snapshot_id: number
  model: string
}

export type ReportBlock =
  | {type: 'kv'; rows: {label: string; value: string}[]}
  | {type: 'table'; columns: string[]; rows: (string | number)[][]}
  | {type: 'text'; text: string}
  | {type: 'list'; items: string[]}

export interface TradeReport {
  episode_id: number
  title: string
  sections: {id: string; title: string; blocks: ReportBlock[]}[]
  report_version?: string
  content_hash?: string
  frozen?: boolean
  artifact_id?: number | null
  provenance?: Record<string, unknown>
}

export type LessonStatus = 'HYPOTHESIS' | 'SUPPORTED' | 'ESTABLISHED' | 'REFUTED' | 'RETIRED'

export interface LessonCondition {metric: string; op: string; value: unknown}

export interface IntelligenceLesson {
  id: number
  strategy_key: string
  statement: string
  status: LessonStatus
  predicate: Record<string, LessonCondition[]>
  expected_effect: {outcome_metric: string; direction: 'positive' | 'negative'}
  supporting_trades: number
  contradicting_trades: number
  n: number
  forward_trades: number
  posterior_mean: number
  p_value?: number | null
  q_value?: number | null
  effect_size_bps: number | null
  effect_ci_low: number | null
  effect_ci_high: number | null
  last_matched_at: string | null
  created_at: string
  evidence?: {episode_id: number; verdict: string; outcome_value: number | null; weight: number; forward: boolean; origin: boolean}[]
  note?: string
}

export interface LearningEventRow {
  id: number
  type: string
  episode_id: number | null
  lesson_id: number | null
  payload: Record<string, unknown>
  created_at: string
}

export interface HistoryCheck {
  n_match: number
  n_other: number
  mean_match_bps: number | null
  mean_other_bps: number | null
  delta_bps: number | null
  p_value: number | null
  verdict: string
}

export interface ExperimentInfo {
  title: string
  try: string
  metric: string
  direction: 'positive' | 'negative'
  min_n: number
  paper_only: boolean
  stop_rule: string
  history_check?: HistoryCheck
  predicate?: Record<string, LessonCondition[]>
}

export interface ExperimentRow {
  id: number
  review_id: number
  strategy_key: string
  instance_name: string
  status: SuggestionStatus
  title: string
  try: string
  condition: string
  metric: string
  direction: string
  min_n: number
  paper_only: boolean
  stop_rule: string
  rule_id: string
  expected_effect: string
  history_check: HistoryCheck | null
  progress: {matching_forward_trades: number; needed: number}
  outcome: {result: 'HELPED' | 'HURT' | 'INCONCLUSIVE'; n_condition: number; n_rest: number; delta_bps: number | null; q_value?: number | null; reading?: string} | null
  created_at: string
}

export interface ChangePointRow {
  strategy_key: string
  currency: string
  metric: 'RETURN' | 'POP_CALIBRATION'
  status: 'INSUFFICIENT' | 'STABLE' | 'EDGE_DECAY' | 'EDGE_IMPROVEMENT'
  n: number
  statistic: number | null
  threshold: number | null
  reference_n: number
  baseline_mean: number | null
  baseline_sd: number | null
  change_index: number | null
  change_at: string | null
  note: string
}

export interface PerformanceRow {
  strategy_key: string
  strategy_version: number | null
  instrument: string
  regime: string
  structure: string
  mode: string
  currency: string
  n: number
  wins: number
  mean_return_bps: number | null
  ci_low_bps: number | null
  ci_high_bps: number | null
  sum_net_pnl: string
  risk_profile?: string
  context?: string
  shrunk_mean_bps?: number | null
  shrunk_sd_bps?: number | null
  shrinkage?: number | null
}

export interface CalibrationRow {
  metric: string
  strategy_key: string
  bin: string
  n: number
  predicted_mean: number | null
  observed_rate: number | null
  brier: number | null
}

export interface LearningDigest {
  window_days: number
  closed_trades: {total: number; per_currency: Record<string, {n: number; wins: number; net_pnl: number}>}
  lessons: {created: string[]; promoted: {statement: string; to: string}[]; retired_or_refuted: {statement: string; to: string}[]; reinforced: number; contradicted: number; by_status: Record<string, number>}
  open_suggestions: number
  experiments_waiting?: number
  change_points?: {strategy_key: string; metric: string; status: string; n: number}[]
  notes: string[]
}

export interface AdvisoryLesson {
  id: number
  statement: string
  status: LessonStatus
  applies: 'yes' | 'maybe'
  n: number
}

export interface PreTradeAdvisoryData {
  id?: number
  trigger: string
  severity: 'INFO' | 'WARNING' | 'HIGH'
  summary: string
  lines: string[]
  lessons: AdvisoryLesson[]
  requires_ack: boolean
  note: string
}

export interface WeeklyReviewRow {
  id: number
  strategy_key: string
  verdict: string
  summary: string
  confidence: string | null
  is_fallback: boolean
  sample_n: number
  model: string
  created_at: string
  strengths?: {description: string; evidence_refs: string[]}[]
  lesson_proposals?: {statement: string; test_plan: string; evidence_refs: string[]}[]
}

export interface CounterfactualResult {
  filter: string
  parameter: string
  threshold: number | string
  n_kept: number
  n_removed: number
  mean_kept_bps: number | null
  mean_removed_bps: number | null
  delta_bps: number | null
  holds_after_correction: boolean
  too_few: boolean
}

export interface CounterfactualSweep {
  strategy_key: string
  n_trades: number
  comparisons: number
  warnings: string[]
  results: CounterfactualResult[]
}

export interface DailyReport {
  scope: 'daily'
  date: string
  title: string
  report_version: string
  content_hash: string
  frozen: boolean
  artifact_id: number | null
  sections: {id: string; title: string; blocks: ReportBlock[]}[]
}

export interface SessionRow {
  zone: string
  session_date: string
  status: 'OPEN' | 'FINAL'
  currency: string
  headline: {closed_trades: number | null; net_pnl: string | null; currency: string; decisions_total: number | null; open_at_asof: number | null}
  narrative_status: string | null
}
