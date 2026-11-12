import {queryOptions} from '@tanstack/react-query'
import {request, withQuery} from './client'
import type {
  AllocationPolicy,
  AllocationRun,
  AuditEvent,
  BrokerAccount,
  BrokerGatewaySession,
  BrokerSessionAccount,
  DashboardSummary,
  Execution,
  ExecutionMode,
  GatewayStatus,
  Instrument,
  ManualOrderIntentStatus,
  Order,
  OrderDetail,
  Portfolio,
  PortfolioSeries,
  Position,
  RebalancePolicy,
  RebalanceRun,
  ReconciliationSummary,
  RiskProfileCatalogue,
  RiskSummary,
  StrategyChartData,
  StrategyLedgerRow,
  StrategyPnl,
  StrategyDefinition,
  StrategyInstance,
  StrategyPolicies,
  StrategyTimelineItem,
  StreamingHealth,
  SystemStatus,
  FinnhubProviderStatus,
  NfoIngestionStatus,
  PortfolioUniverse,
  PortfolioOptimizationPolicy,
  PortfolioOptimizationRun,
  PortfolioConstructionPlan,
  PortfolioConstructionRun,
  PortfolioBuilderReadiness,
  RecommendationBatch,
  ExecutionDiagnostics,
} from './types'

const POLL_INTERVAL = 15_000
const IDLE_WORKFLOW_POLL_INTERVAL = 12_000
const PNL_POLL_INTERVAL = 5_000

export const queries = {
  brokerSessions: () => queryOptions({
    queryKey: ['broker-sessions'],
    queryFn: () => request<BrokerGatewaySession[]>('broker-sessions/'),
    refetchInterval: 5_000,
  }),
  brokerSessionAccounts: (sessionId?: string | null) => queryOptions({
    queryKey: ['broker-session-accounts', sessionId ?? 'none'],
    queryFn: () => request<BrokerSessionAccount[]>(`broker-sessions/${sessionId}/accounts/`),
    enabled: Boolean(sessionId),
    refetchInterval: 5_000,
  }),
  system: () => queryOptions({
    queryKey: ['system'],
    queryFn: () => request<SystemStatus>('system/'),
    refetchInterval: POLL_INTERVAL,
  }),
  gateway: () => queryOptions({
    queryKey: ['gateway'],
    queryFn: () => request<GatewayStatus>('gateway/'),
    refetchInterval: POLL_INTERVAL,
  }),
  accounts: () => queryOptions({
    queryKey: ['accounts'],
    queryFn: () => request<BrokerAccount[]>('accounts/'),
    refetchInterval: POLL_INTERVAL,
  }),
  portfolios: () => queryOptions({
    queryKey: ['portfolios'],
    queryFn: () => request<Portfolio[]>('portfolios/'),
    refetchInterval: POLL_INTERVAL,
  }),
  instruments: () => queryOptions({
    queryKey: ['instruments'],
    queryFn: () => request<Instrument[]>('instruments/'),
    staleTime: 60_000,
  }),
  positions: (portfolioId?: number | null) => queryOptions({
    queryKey: ['positions', portfolioId ?? 'all'],
    queryFn: () => request<Position[]>(withQuery('positions/', {portfolio: portfolioId})),
    refetchInterval: POLL_INTERVAL,
  }),
  dashboard: (portfolioId?: number | null) => queryOptions({
    queryKey: ['dashboard', portfolioId ?? 'default'],
    queryFn: () => request<DashboardSummary>(withQuery('dashboard/summary/', {portfolio: portfolioId})),
    refetchInterval: POLL_INTERVAL,
  }),
  portfolioSeries: (portfolioId?: number | null, controls?: {range?: string; interval?: string}) => queryOptions({
    queryKey: controls ? ['portfolio-series', portfolioId ?? 'default', controls] : ['portfolio-series', portfolioId ?? 'default'],
    queryFn: () => request<PortfolioSeries>(withQuery('portfolios/series/', {portfolio: portfolioId, range: controls?.range, interval: controls?.interval})),
    refetchInterval: 30_000,
  }),
  orders: (filters: {portfolioId?: number | null; status?: string; symbol?: string} = {}) => queryOptions({
    queryKey: ['orders', filters],
    queryFn: () => request<Order[]>(withQuery('orders/', {
      portfolio: filters.portfolioId,
      status: filters.status,
      symbol: filters.symbol,
      limit: 250,
    })),
    refetchInterval: POLL_INTERVAL,
  }),
  orderDetail: (internalId: string) => queryOptions({
    queryKey: ['order-detail', internalId],
    queryFn: () => request<OrderDetail>(`orders/${internalId}/detail/`),
    enabled: Boolean(internalId),
    refetchInterval: POLL_INTERVAL,
  }),
  manualOrderIntent: (intentId?: number | null) => queryOptions({
    queryKey: ['manual-order-intent', intentId ?? 'none'],
    queryFn: () => request<ManualOrderIntentStatus>(`orders/intents/${intentId}/status/`),
    enabled: Boolean(intentId),
  }),
  executions: (filters: {portfolioId?: number | null; symbol?: string} = {}) => queryOptions({
    queryKey: ['executions', filters],
    queryFn: () => request<Execution[]>(withQuery('executions/', {
      portfolio: filters.portfolioId,
      symbol: filters.symbol,
      limit: 250,
    })),
    refetchInterval: POLL_INTERVAL,
  }),
  audit: (filters: {eventType?: string; limit?: number} = {}) => queryOptions({
    queryKey: ['audit', filters],
    queryFn: () => request<AuditEvent[]>(withQuery('audit/', {
      event_type: filters.eventType,
      limit: filters.limit ?? 100,
    })),
    refetchInterval: POLL_INTERVAL,
  }),
  risk: () => queryOptions({
    queryKey: ['risk'],
    queryFn: () => request<RiskSummary>('risk/'),
    refetchInterval: POLL_INTERVAL,
  }),
  reconciliation: () => queryOptions({
    queryKey: ['reconciliation'],
    queryFn: () => request<ReconciliationSummary>('reconciliation/'),
    refetchInterval: POLL_INTERVAL,
  }),
  streaming: () => queryOptions({
    queryKey: ['streaming'],
    queryFn: () => request<StreamingHealth>('streaming/health/'),
    refetchInterval: POLL_INTERVAL,
  }),
  executionDiagnostics: () => queryOptions({
    queryKey: ['execution-diagnostics'],
    queryFn: () => request<ExecutionDiagnostics>('execution/diagnostics/'),
    refetchInterval: IDLE_WORKFLOW_POLL_INTERVAL,
  }),
  strategyDefinitions: () => queryOptions({
    queryKey: ['strategy-definitions'],
    queryFn: async () => {
      const definitions = await request<StrategyDefinition[]>('strategy-definitions/')
      if (!Array.isArray(definitions)) throw new Error('Backend returned an invalid strategy definition catalog')
      return definitions
    },
    staleTime: 5 * 60_000,
    refetchOnMount: 'always',
    refetchInterval: (query) => query.state.data?.length === 0 ? 5_000 : false,
  }),
  riskProfiles: () => queryOptions({
    queryKey: ['strategy-risk-profiles'],
    queryFn: () => request<RiskProfileCatalogue>('strategy-risk-profiles/'),
    staleTime: 60_000,
    retry: false,
  }),
  strategyPolicies: () => queryOptions({
    queryKey: ['strategy-policies'],
    queryFn: () => request<StrategyPolicies>('strategy-policies/'),
    staleTime: 60_000,
  }),
  strategies: (filters: {portfolioId?: number | null; state?: string; executionMode?: ExecutionMode} = {}) => queryOptions({
    queryKey: ['strategy-instances', filters],
    queryFn: () => request<StrategyInstance[]>(withQuery('strategy-instances/', {
      portfolio: filters.portfolioId,
      state: filters.state,
      execution_mode: filters.executionMode,
    })),
    refetchInterval: POLL_INTERVAL,
  }),
  strategy: (strategyId: number) => queryOptions({
    queryKey: ['strategy-instance', strategyId],
    queryFn: () => request<StrategyInstance>(`strategy-instances/${strategyId}/`),
    enabled: strategyId > 0,
    refetchInterval: (query) => {
      const workflow = query.state.data?.execution_workflow
      if (workflow?.terminal) return false
      return workflow?.active ? 1_000 : IDLE_WORKFLOW_POLL_INTERVAL
    },
  }),
  strategyTimeline: (strategyId: number) => queryOptions({
    queryKey: ['strategy-timeline', strategyId],
    queryFn: () => request<StrategyTimelineItem[]>(`strategy-instances/${strategyId}/execution-timeline/`),
    enabled: strategyId > 0,
    refetchInterval: (query) => {
      const workflow = query.state.data?.[0]
      if (workflow?.workflow_terminal) return false
      return workflow?.workflow_active ? 1_000 : IDLE_WORKFLOW_POLL_INTERVAL
    },
  }),
  strategyChart: (strategyId: number, controls?: {range?: string; interval?: string}) => queryOptions({
    queryKey: controls ? ['strategy-chart', strategyId, controls] : ['strategy-chart', strategyId],
    queryFn: () => request<StrategyChartData>(withQuery(`strategy-instances/${strategyId}/chart/`, {range: controls?.range, interval: controls?.interval})),
    enabled: strategyId > 0,
    refetchInterval: POLL_INTERVAL,
  }),
  strategyLedger: (strategyId: number) => queryOptions({
    queryKey: ['strategy-ledger', strategyId],
    queryFn: () => request<StrategyLedgerRow[]>(`strategy-instances/${strategyId}/ledger/`),
    enabled: strategyId > 0,
    refetchInterval: POLL_INTERVAL,
  }),
  strategyPnl: (strategyId: number) => queryOptions({
    queryKey: ['strategy-pnl', strategyId],
    queryFn: () => request<StrategyPnl>(`strategy-instances/${strategyId}/pnl/`),
    enabled: strategyId > 0,
    refetchInterval: PNL_POLL_INTERVAL,
  }),
  allocationPolicies: () => queryOptions({
    queryKey: ['allocation-policies'],
    queryFn: () => request<AllocationPolicy[]>('allocations/policies/'),
    staleTime: 30_000,
  }),
  allocationRuns: () => queryOptions({
    queryKey: ['allocation-runs'],
    queryFn: () => request<AllocationRun[]>('allocations/runs/'),
    refetchInterval: POLL_INTERVAL,
  }),
  rebalancePolicies: () => queryOptions({
    queryKey: ['rebalance-policies'],
    queryFn: () => request<RebalancePolicy[]>('rebalancing/policies/'),
    staleTime: 30_000,
  }),
  rebalanceRuns: () => queryOptions({
    queryKey: ['rebalance-runs'],
    queryFn: () => request<RebalanceRun[]>('rebalancing/runs/'),
    refetchInterval: POLL_INTERVAL,
  }),
  nfoIngestion: () => queryOptions({
    queryKey: ['nfo-ingestion'],
    queryFn: () => request<NfoIngestionStatus>('data-providers/nfo-ingestion/'),
    retry: false,
    refetchInterval: 60_000,
  }),
  finnhub: () => queryOptions({
    queryKey: ['finnhub'],
    queryFn: () => request<FinnhubProviderStatus>('data-providers/finnhub/'),
    staleTime: 30_000,
  }),
  portfolioUniverse: (portfolioId?: number | null) => queryOptions({
    queryKey: ['portfolio-universe', portfolioId ?? 'none'],
    queryFn: () => request<PortfolioUniverse[]>(withQuery('portfolio-universe/', {portfolio: portfolioId})),
    enabled: Boolean(portfolioId),
    staleTime: 30_000,
  }),
  optimizationPolicies: (portfolioId?: number | null) => queryOptions({
    queryKey: ['optimization-policies', portfolioId ?? 'none'],
    queryFn: () => request<PortfolioOptimizationPolicy[]>(withQuery('portfolio-optimization/policies/', {portfolio: portfolioId})),
    enabled: Boolean(portfolioId),
    staleTime: 30_000,
  }),
  optimizationRuns: (portfolioId?: number | null) => queryOptions({
    queryKey: ['optimization-runs', portfolioId ?? 'none'],
    queryFn: () => request<PortfolioOptimizationRun[]>(withQuery('portfolio-optimization/runs/', {portfolio: portfolioId})),
    enabled: Boolean(portfolioId),
    refetchInterval: POLL_INTERVAL,
  }),
  constructionPlans: (portfolioId?: number | null) => queryOptions({
    queryKey: ['construction-plans', portfolioId ?? 'none'],
    queryFn: () => request<PortfolioConstructionPlan[]>(withQuery('portfolio-construction/plans/', {portfolio: portfolioId})),
    enabled: Boolean(portfolioId),
    staleTime: 15_000,
  }),
  constructionRuns: (portfolioId?: number | null) => queryOptions({
    queryKey: ['construction-runs', portfolioId ?? 'none'],
    queryFn: () => request<PortfolioConstructionRun[]>(withQuery('portfolio-construction/runs/', {portfolio: portfolioId})),
    enabled: Boolean(portfolioId),
    refetchInterval: (query) => (query.state.data || []).some((run) =>
      ['QUEUED', 'DISPATCHED', 'CALCULATING'].includes(run.status)
      || ['QUEUED', 'APPLYING', 'ACTIVATING'].includes(run.application_status)
    ) ? 1_000 : IDLE_WORKFLOW_POLL_INTERVAL,
  }),
  builderReadiness: (portfolioId?: number | null, planId?: number | null) => queryOptions({
    queryKey: ['portfolio-builder-readiness', portfolioId ?? 'none', planId ?? 'none'],
    queryFn: () => request<PortfolioBuilderReadiness>(withQuery('portfolio-construction/readiness/', {
      portfolio: portfolioId,
      plan: planId,
    })),
    enabled: Boolean(portfolioId),
    refetchInterval: 10_000,
  }),
  recommendationBatch: (batchId?: number | null) => queryOptions({
    queryKey: ['recommendation-batch', batchId ?? 'none'],
    queryFn: () => request<RecommendationBatch>(`portfolio-construction/recommendation-batches/${batchId}/`),
    enabled: Boolean(batchId),
    refetchInterval: (query) => ['QUEUED', 'RUNNING'].includes(query.state.data?.status || '') ? 1_000 : false,
  }),
}
