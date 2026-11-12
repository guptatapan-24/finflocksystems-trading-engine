import {useEffect, useMemo, useState} from 'react'
import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query'
import {Banknote, Layers3, PieChart, Scale, WalletCards} from 'lucide-react'
import {mutationOptions, request} from '../../api/client'
import {queries} from '../../api/queries'
import type {AllocationPolicy, ExecutionMode, Instrument, PortfolioOptimizationPolicy, PortfolioOptimizationRun, PortfolioUniverse, Position, PositionSizingDecision, RebalanceRun, RebalanceTarget} from '../../api/types'
import {TerminalChart} from '../../components/charts/TerminalChart'
import {DataTable, ErrorState, Freshness, PageHeader, Skeleton, StatusBadge, TerminalMetric, TerminalPanel, formatCompact, formatMoney, formatNumber, formatPercent, toNumber} from '../../components/ui'
import {executionModeForSession, executionModeLabel} from '../../executionMode'
import {useSelection} from '../../stores/useSelection'

async function pollRun<T>(initial: T, path: string, complete: (value: T) => boolean): Promise<T> {
  let value = initial
  for (let attempt = 0; attempt < 120 && !complete(value); attempt += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 500))
    value = await request<T>(path)
  }
  return value
}

export function PortfolioPage() {
  const queryClient = useQueryClient()
  const {portfolio, account, session, selectedPortfolioId} = useSelection()
  const executionMode = portfolio?.gateway_session_id === session?.id
    ? executionModeForSession(session)
    : null
  const positions = useQuery(queries.positions(selectedPortfolioId))
  const series = useQuery(queries.portfolioSeries(selectedPortfolioId))
  const allocationPolicies = useQuery(queries.allocationPolicies())
  const allocationRuns = useQuery(queries.allocationRuns())
  const rebalancePolicies = useQuery(queries.rebalancePolicies())
  const rebalanceRuns = useQuery(queries.rebalanceRuns())
  const instruments = useQuery(queries.instruments())
  const universes = useQuery(queries.portfolioUniverse(selectedPortfolioId))
  const optimizationPolicies = useQuery(queries.optimizationPolicies(selectedPortfolioId))
  const optimizationRuns = useQuery(queries.optimizationRuns(selectedPortfolioId))
  const [preview, setPreview] = useState<RebalanceRun | null>(null)
  const [optimizationPreview, setOptimizationPreview] = useState<PortfolioOptimizationRun | null>(null)
  const [sizing, setSizing] = useState<PositionSizingDecision | null>(null)
  useEffect(() => {setPreview(null); setOptimizationPreview(null); setSizing(null)}, [selectedPortfolioId])

  const portfolioPositions = useMemo(() => (positions.data || []).filter((item) => !selectedPortfolioId || item.portfolio_id === selectedPortfolioId), [positions.data, selectedPortfolioId])
  const gross = portfolioPositions.reduce((sum, item) => sum + Math.abs(toNumber(item.market_value)), 0)
  const net = portfolioPositions.reduce((sum, item) => sum + toNumber(item.market_value), 0)
  const concentration = gross ? Math.max(0, ...portfolioPositions.map((item) => Math.abs(toNumber(item.market_value)) / gross)) : 0
  const policyRows = (allocationPolicies.data || []).filter((item) => !selectedPortfolioId || item.portfolio_id === selectedPortfolioId)
  const rebalancePolicyRows = (rebalancePolicies.data || []).filter((item) => !selectedPortfolioId || item.portfolio_id === selectedPortfolioId)

  const flow = useMutation({
    mutationFn: async (payload: {flow_type: string; amount: string; liquidation_policy: string; allocation_mode: string}) => {
      const run=await request<{id: number; status: string; allocation_mode: string}>('allocations/flows/', mutationOptions('POST', {portfolio_id: selectedPortfolioId, ...payload}, true))
      return pollRun(run,`allocations/runs/${run.id}/`,(value) => !['QUEUED','CALCULATING'].includes(value.status))
    },
    onSuccess: async () => { await Promise.all([queryClient.invalidateQueries({queryKey: ['allocation-runs']}), queryClient.invalidateQueries({queryKey: ['optimization-runs']})]) },
  })
  const rebalance = useMutation({
    mutationFn: async () => {
      const run=await request<RebalanceRun>('rebalancing/preview/', mutationOptions('POST', {portfolio_id: selectedPortfolioId, trigger: 'MANUAL'}, true))
      return pollRun(run,`rebalancing/runs/${run.id}/`,(value) => !['QUEUED','CALCULATING'].includes(value.status))
    },
    onSuccess: async (data) => {setPreview(data); await queryClient.invalidateQueries({queryKey: ['rebalance-runs']})},
  })
  const size = useMutation({
    mutationFn: (payload: Record<string, string | number | null>) => request<PositionSizingDecision>('position-sizing/preview/', mutationOptions('POST', {portfolio_id: selectedPortfolioId, ...payload}, true)),
    onSuccess: setSizing,
  })

  const holdingColumns = [
    {id: 'instrument', header: 'Holding', cell: (item: Position) => <div className="primary-cell"><strong className="mono">{item.symbol}</strong><span>{item.asset_class} · {item.currency}</span></div>},
    {id: 'quantity', header: 'Quantity', align: 'right' as const, className: 'mono', cell: (item: Position) => formatNumber(item.quantity)},
    {id: 'average', header: 'Average cost', align: 'right' as const, className: 'mono', cell: (item: Position) => formatMoney(item.average_cost, item.currency)},
    {id: 'price', header: 'Market price', align: 'right' as const, className: 'mono', cell: (item: Position) => formatMoney(item.market_price, item.currency)},
    {id: 'value', header: 'Market value', align: 'right' as const, className: 'mono', cell: (item: Position) => formatMoney(item.market_value, item.currency)},
    {id: 'weight', header: 'Gross weight', align: 'right' as const, cell: (item: Position) => formatPercent(gross ? Math.abs(toNumber(item.market_value)) / gross : 0)},
  ]
  const driftColumns = [
    {id: 'instrument', header: 'Instrument', cell: (item: RebalanceTarget) => <code>{instruments.data?.find((value) => value.id === item.instrument_id)?.symbol || item.instrument_id}</code>},
    {id: 'current', header: 'Current', align: 'right' as const, cell: (item: RebalanceTarget) => formatPercent(item.current_weight)},
    {id: 'target', header: 'Target', align: 'right' as const, cell: (item: RebalanceTarget) => formatPercent(item.target_weight)},
    {id: 'drift', header: 'Drift', align: 'right' as const, cell: (item: RebalanceTarget) => formatPercent(item.drift)},
    {id: 'trade', header: 'Preview quantity', align: 'right' as const, className: 'mono', cell: (item: RebalanceTarget) => formatNumber(item.trade_quantity)},
    {id: 'state', header: 'State', cell: (item: RebalanceTarget) => <StatusBadge status={item.suppressed ? item.suppression_reason || 'SUPPRESSED' : 'PLANNED'} />},
  ]

  return <div className="page-stack">
    <PageHeader eyebrow="Capital & allocation" title={portfolio?.name || 'Portfolio'} description="Holdings, cash, exposure, allocation, and drift for the selected broker-backed portfolio." actions={<Freshness updatedAt={Math.max(positions.dataUpdatedAt, series.dataUpdatedAt)} stale={positions.isStale || series.isStale} fetching={positions.isFetching || series.isFetching} onRefresh={() => {void positions.refetch(); void series.refetch()}} />} />
    <section className="metric-grid compact">
      <TerminalMetric label="NAV" value={formatMoney(account?.net_liquidation, account?.base_currency)} icon={<WalletCards />} />
      <TerminalMetric label="Cash" value={formatMoney(account?.available_cash, account?.base_currency)} icon={<Banknote />} helper={portfolio ? `${formatPercent(portfolio.cash_buffer_pct)} buffer policy` : undefined} />
      <TerminalMetric label="Gross exposure" value={formatMoney(gross, account?.base_currency)} icon={<Layers3 />} />
      <TerminalMetric label="Net exposure" value={formatMoney(net, account?.base_currency)} icon={<Scale />} />
      <TerminalMetric label="Largest concentration" value={formatPercent(concentration)} icon={<PieChart />} />
    </section>
    <div className="portfolio-grid">
      <TerminalPanel id="nav-pnl-history" title="NAV & P&L history" description={`Source: ${series.data?.source || 'loading persisted observations'}`} className="portfolio-history">{series.isLoading ? <Skeleton height={280} /> : series.isError ? <ErrorState error={series.error} onRetry={() => void series.refetch()} compact /> : <TerminalChart id="portfolio-nav" height={320} defaultChartType="area" lines={[{name: 'NAV', data: series.data?.nav || [], type: 'area', kind: 'primary'}, {name: 'P&L', data: series.data?.pnl || [], kind: 'primary'}]} />}</TerminalPanel>
      <TerminalPanel id="instrument-allocation" title="Allocation by instrument" description="Current marked market value as a share of gross exposure"><AllocationBars rows={(series.data?.allocation_by_instrument || []).map((item) => ({id: item.instrument_id, name: item.symbol, weight: item.weight, value: item.value}))} currency={account?.base_currency} /></TerminalPanel>
      <TerminalPanel id="strategy-allocation" title="Allocation by strategy" description="Configured capital shares; actual attribution remains ledger-backed"><AllocationBars rows={policyRows.map((item) => ({id: item.id, name: item.strategy, weight: toNumber(item.target_share), value: null}))} currency={account?.base_currency} /></TerminalPanel>
    </div>
    <TerminalPanel id="advanced-target-optimizer" title="Advanced target optimizer" description="Operator controls for a single-universe long-only Markowitz target. Everyday goal construction is available in Portfolio Builder." defaultOpen={false}>
      <PortfolioConstruction portfolioId={selectedPortfolioId} instruments={instruments.data || []} universe={universes.data?.[0]} policy={optimizationPolicies.data?.[0]} preview={optimizationPreview} executionMode={executionMode} onPreview={setOptimizationPreview} onChanged={async () => {await Promise.all([queryClient.invalidateQueries({queryKey: ['portfolio-universe']}), queryClient.invalidateQueries({queryKey: ['optimization-policies']}), queryClient.invalidateQueries({queryKey: ['optimization-runs']}), queryClient.invalidateQueries({queryKey: ['rebalance-runs']})])}} />
      {!optimizationPreview && (optimizationRuns.data || []).length > 0 && <p className="inline-note">Most recent optimization: run {(optimizationRuns.data || [])[0].id} · {(optimizationRuns.data || [])[0].status}.</p>}
    </TerminalPanel>
    <TerminalPanel id="holdings" title="Holdings" description={`${portfolioPositions.length} marked positions`}>{positions.isLoading ? <Skeleton lines={5} /> : positions.isError ? <ErrorState error={positions.error} onRetry={() => void positions.refetch()} /> : <DataTable rows={portfolioPositions} columns={holdingColumns} getRowKey={(item) => item.id} emptyTitle="No holdings" emptyDescription="Broker-synchronized positions for the selected portfolio will appear here." />}</TerminalPanel>
    <TerminalPanel id="drift" title="Drift" description="Preview rebalance compares current and net strategy targets. Preview creates no orders." actions={<button className="button-secondary" disabled={!selectedPortfolioId || rebalance.isPending} onClick={() => rebalance.mutate()}>{rebalance.isPending ? 'Calculating…' : 'Preview rebalance'}</button>}>{rebalance.isError && <ErrorState title="Rebalance preview was blocked" error={rebalance.error} compact />}<DataTable rows={preview?.targets || []} columns={driftColumns} getRowKey={(item) => item.id} emptyTitle="No drift preview" emptyDescription="Preview creates no orders or broker commands." /></TerminalPanel>
    <section className="advanced-stack" aria-label="Advanced portfolio tools">
      <TerminalPanel id="portfolio-flow-allocation" title="Portfolio flow allocation" description="Deposit and withdrawal allocation, kept out of the everyday holdings view." defaultOpen={false}><FlowForm pending={flow.isPending} error={flow.error} result={flow.data} onSubmit={(payload) => flow.mutate(payload)} /><DataTable rows={(allocationRuns.data || []).filter((item) => !selectedPortfolioId || item.portfolio_id === selectedPortfolioId).slice(0, 10)} columns={allocationRunColumns} getRowKey={(item) => item.id} emptyTitle="No allocation runs" /></TerminalPanel>
      <TerminalPanel id="position-sizing" title="Position sizing details" description="Operator preview of the constraints applied before risk and OMS." defaultOpen={false}><SizingForm instruments={instruments.data || []} pending={size.isPending} error={size.error} result={sizing} onSubmit={(payload) => size.mutate(payload)} /></TerminalPanel>
      <TerminalPanel id="policy-internals" title="Policy internals" description="Allocation and rebalance policy values used by the planning services." defaultOpen={false}><PolicyTables allocation={policyRows} rebalance={rebalancePolicyRows} runs={(rebalanceRuns.data || []).filter((item) => !selectedPortfolioId || item.portfolio_id === selectedPortfolioId)} /></TerminalPanel>
    </section>
  </div>
}

function AllocationBars({rows, currency}: {rows: {id: number; name: string; weight: number; value: number | null}[]; currency?: string}) {
  if (!rows.length) return <div className="empty-inline">No allocation data for this portfolio.</div>
  const maximum = Math.max(...rows.map((item) => Math.abs(item.weight)), 0.01)
  return <ul className="allocation-bars">{rows.map((item) => <li key={item.id}><div><strong>{item.name}</strong><span>{formatPercent(item.weight)}{item.value !== null ? ` · ${formatMoney(item.value, currency)}` : ''}</span></div><div><i style={{width: `${Math.abs(item.weight) / maximum * 100}%`}} /></div></li>)}</ul>
}

function FlowForm({pending, error, result, onSubmit}: {pending: boolean; error: unknown; result?: {id: number; status: string; allocation_mode: string}; onSubmit: (payload: {flow_type: string; amount: string; liquidation_policy: string; allocation_mode: string}) => void}) {
  const submit = (event: React.FormEvent<HTMLFormElement>) => {event.preventDefault(); const form = new FormData(event.currentTarget); onSubmit({flow_type: String(form.get('flow_type')), amount: String(form.get('amount')), allocation_mode: String(form.get('allocation_mode')), liquidation_policy: String(form.get('liquidation_policy'))})}
  return <><form className="form-grid four-columns" onSubmit={submit}><label>Flow type<select name="flow_type"><option>DEPOSIT</option><option>WITHDRAWAL</option><option>INTERNAL_TRANSFER_IN</option><option>INTERNAL_TRANSFER_OUT</option></select></label><label>Amount<input name="amount" type="number" min="0.01" step="0.01" required /></label><label>Allocation mode<select name="allocation_mode" defaultValue="AUTO"><option value="AUTO">Automatic</option><option value="PORTFOLIO_OPTIMIZATION">Portfolio optimization</option><option value="STRATEGY_ALLOCATION">Strategy allocation</option></select></label><label>Liquidation policy<select name="liquidation_policy"><option>PROPORTIONAL</option><option>LOWEST_CONVICTION_FIRST</option><option>MOST_LIQUID_FIRST</option><option>LOWEST_COST_FIRST</option><option>PRIORITY_ORDER</option></select></label><button className="button-primary form-submit" disabled={pending}>{pending ? 'Calculating…' : 'Calculate post-flow targets'}</button></form>{error && <ErrorState title="Flow was blocked" error={error} compact />}{result && <div className="inline-success"><StatusBadge status={result.status} />Allocation run {result.id} recorded using <strong>{result.allocation_mode}</strong>.</div>}</>
}

function SizingForm({instruments, pending, error, result, onSubmit}: {instruments: {id: number; symbol: string}[]; pending: boolean; error: unknown; result: PositionSizingDecision | null; onSubmit: (payload: Record<string, string | number | null>) => void}) {
  const submit = (event: React.FormEvent<HTMLFormElement>) => {event.preventDefault(); const form = new FormData(event.currentTarget); onSubmit({instrument_id: Number(form.get('instrument_id')), side: String(form.get('side')), target_quantity: String(form.get('target_quantity')), entry_price: String(form.get('entry_price')), stop_price: form.get('stop_price') ? String(form.get('stop_price')) : null, adv: String(form.get('adv'))})}
  return <><form className="form-grid four-columns" onSubmit={submit}><label>Instrument<select name="instrument_id" required><option value="">Choose</option>{instruments.map((item) => <option key={item.id} value={item.id}>{item.symbol}</option>)}</select></label><label>Side<select name="side"><option>BUY</option><option>SELL</option></select></label><label>Target quantity<input name="target_quantity" type="number" min="0" step="any" required /></label><label>Entry price<input name="entry_price" type="number" min="0.0001" step="any" required /></label><label>Stop price<input name="stop_price" type="number" min="0" step="any" /></label><label>Average daily volume<input name="adv" type="number" min="0" step="any" required /></label><button className="button-primary form-submit" disabled={pending}>{pending ? 'Sizing…' : 'Preview sizing'}</button></form>{error && <ErrorState title="Sizing preview was blocked" error={error} compact />}{result && <div className="sizing-result"><div><span>Target</span><strong>{formatNumber(result.target_quantity)}</strong></div><div><span>Approved</span><strong>{formatNumber(result.approved_quantity)}</strong></div><div><span>Binding constraint</span><strong>{result.binding_constraint || 'None'}</strong></div><div><span>Decision</span><StatusBadge status={result.rejected_reason ? 'REJECTED' : 'PREVIEW'} /></div></div>}</>
}

const allocationRunColumns = [
  {id: 'type', header: 'Type', cell: (item: import('../../api/types').AllocationRun) => item.flow_type},
  {id: 'amount', header: 'Amount', align: 'right' as const, cell: (item: import('../../api/types').AllocationRun) => formatNumber(item.amount)},
  {id: 'approved', header: 'Approved', align: 'right' as const, cell: (item: import('../../api/types').AllocationRun) => formatNumber(item.approved_amount)},
  {id: 'mode', header: 'Resolved mode', cell: (item: import('../../api/types').AllocationRun) => item.allocation_mode},
  {id: 'policy', header: 'Policy', cell: (item: import('../../api/types').AllocationRun) => item.liquidation_policy},
  {id: 'status', header: 'Status', cell: (item: import('../../api/types').AllocationRun) => <StatusBadge status={item.status} />},
]

function PolicyTables({allocation, rebalance, runs}: {allocation: AllocationPolicy[]; rebalance: import('../../api/types').RebalancePolicy[]; runs: RebalanceRun[]}) {
  return <div className="page-stack"><h3>Strategy capital policy</h3><DataTable rows={allocation} columns={[{id: 'strategy', header: 'Strategy', cell: (item) => item.strategy}, {id: 'share', header: 'Target share', cell: (item) => formatPercent(item.target_share)}, {id: 'bounds', header: 'Min / max', cell: (item) => `${formatPercent(item.minimum_share)} / ${formatPercent(item.maximum_share)}`}, {id: 'priority', header: 'Priority', cell: (item) => item.priority}, {id: 'state', header: 'State', cell: (item) => <StatusBadge status={item.enabled ? 'ENABLED' : 'DISABLED'} />}]} getRowKey={(item) => item.id} /><h3>Rebalance policy</h3><DataTable rows={rebalance} columns={[{id: 'mode', header: 'Mode', cell: (item) => <StatusBadge status={item.mode} />}, {id: 'instrument', header: 'Instrument drift', cell: (item) => formatPercent(item.instrument_drift_threshold)}, {id: 'portfolio', header: 'Portfolio drift', cell: (item) => formatPercent(item.portfolio_drift_threshold)}, {id: 'turnover', header: 'Max turnover', cell: (item) => formatPercent(item.maximum_turnover)}, {id: 'sequence', header: 'Sequence', cell: (item) => item.sell_before_buy ? 'Sells before buys' : 'Net sequence'}]} getRowKey={(item) => item.id} /><h3>Recent rebalance runs</h3><DataTable rows={runs.slice(0, 10)} columns={[{id: 'trigger', header: 'Trigger', cell: (item) => item.trigger}, {id: 'mode', header: 'Mode', cell: (item) => <StatusBadge status={item.mode} />}, {id: 'phase', header: 'Phase', cell: (item) => item.phase}, {id: 'drift', header: 'Total drift', cell: (item) => formatPercent(item.total_drift)}, {id: 'status', header: 'Status', cell: (item) => <StatusBadge status={item.status} />}]} getRowKey={(item) => item.id} /></div>
}

function PortfolioConstruction({portfolioId, instruments, universe, policy, preview, executionMode, onPreview, onChanged}: {
  portfolioId?: number | null
  instruments: Instrument[]
  universe?: PortfolioUniverse
  policy?: PortfolioOptimizationPolicy
  preview: PortfolioOptimizationRun | null
  executionMode: ExecutionMode | null
  onPreview: (run: PortfolioOptimizationRun) => void
  onChanged: () => Promise<void>
}) {
  const [instrumentIds, setInstrumentIds] = useState<number[]>([])
  const [maximumInstruments, setMaximumInstruments] = useState(50)
  useEffect(() => {
    setInstrumentIds(universe ? universe.instruments.filter((item) => item.enabled).map((item) => item.instrument_id) : [])
    setMaximumInstruments(universe?.maximum_instruments || 50)
  }, [portfolioId, universe])

  const save = useMutation({
    mutationFn: async (form: HTMLFormElement) => {
      if (!portfolioId) throw new Error('Select a portfolio')
      if (instrumentIds.length < 2) throw new Error('Select at least two stocks')
      if (instrumentIds.length > maximumInstruments) throw new Error(`Selected instrument count ${instrumentIds.length} exceeds maximum ${maximumInstruments}`)
      const values = new FormData(form)
      await request<PortfolioUniverse>('portfolio-universe/', mutationOptions('POST', {
        portfolio_id: portfolioId,
        instrument_ids: instrumentIds,
        include_strategy_instruments: values.get('include_strategy_instruments') === 'on',
        minimum_history_observations: Number(values.get('minimum_history_observations')),
        maximum_instruments: Number(values.get('maximum_instruments')),
      }))
      return request<PortfolioOptimizationPolicy>('portfolio-optimization/policies/', mutationOptions('POST', {
        portfolio_id: portfolioId,
        method: String(values.get('method')),
        lookback_days: Number(values.get('lookback_days')),
        risk_free_rate: String(values.get('risk_free_rate')),
        target_cash_weight: String(values.get('target_cash_weight')),
        minimum_weight: String(values.get('minimum_weight')),
        maximum_weight: String(values.get('maximum_weight')),
        maximum_turnover: String(values.get('maximum_turnover')),
        transaction_cost_penalty: String(values.get('transaction_cost_penalty')),
        long_only: true,
        enabled: true,
      }))
    },
    onSuccess: onChanged,
  })
  const optimize = useMutation({
    mutationFn: async () => {
      const run=await request<PortfolioOptimizationRun>('portfolio-optimization/preview/', mutationOptions('POST', {portfolio_id: portfolioId, universe_id: universe?.id, policy_id: policy?.id, trigger: 'PREVIEW', refresh_history: true}, true))
      return pollRun(run,`portfolio-optimization/runs/${run.id}/`,(value) => !['QUEUED','DISPATCHED','CALCULATING'].includes(value.status))
    },
    onSuccess: async (run) => {onPreview(run); await onChanged()},
  })
  const apply = useMutation({
    mutationFn: async () => {
      if (!executionMode) throw new Error('The selected portfolio must have a matching Paper or Live Gateway session')
      const run=await request<PortfolioOptimizationRun>('portfolio-optimization/run/', mutationOptions('POST', {optimization_run_id: preview?.id, portfolio_id: portfolioId, universe_id: preview?.universe_id, policy_id: preview?.policy_id}, true))
      return pollRun(run,`portfolio-optimization/runs/${run.id}/`,(value) => !['QUEUED','APPLYING'].includes(value.application_status))
    },
    onSuccess: async (run) => {onPreview(run); await onChanged()},
  })
  const stockInstruments = instruments.filter((item) => item.asset_class === 'STK' && item.active && item.tradable)
  const allocationColumns = [
    {id: 'symbol', header: 'Instrument', cell: (item: import('../../api/types').OptimizedPortfolioTarget) => <strong className="mono">{item.symbol}</strong>},
    {id: 'current', header: 'Current', align: 'right' as const, cell: (item: import('../../api/types').OptimizedPortfolioTarget) => formatPercent(item.current_weight)},
    {id: 'target', header: 'Optimized', align: 'right' as const, cell: (item: import('../../api/types').OptimizedPortfolioTarget) => formatPercent(item.optimized_weight)},
    {id: 'change', header: 'Change', align: 'right' as const, cell: (item: import('../../api/types').OptimizedPortfolioTarget) => formatPercent(item.weight_change)},
    {id: 'value', header: 'Target value', align: 'right' as const, cell: (item: import('../../api/types').OptimizedPortfolioTarget) => formatMoney(item.target_value)},
    {id: 'constraint', header: 'Constraint', cell: (item: import('../../api/types').OptimizedPortfolioTarget) => item.constraint_status || '—'},
  ]
  const tradeColumns = [
    {id: 'symbol', header: 'Instrument', cell: (item: import('../../api/types').PlannedOptimizationTrade) => <strong className="mono">{item.symbol}</strong>},
    {id: 'side', header: 'Side', cell: (item: import('../../api/types').PlannedOptimizationTrade) => <StatusBadge status={item.side} />},
    {id: 'quantity', header: 'Quantity', align: 'right' as const, cell: (item: import('../../api/types').PlannedOptimizationTrade) => formatNumber(item.quantity)},
    {id: 'price', header: 'Reference', align: 'right' as const, cell: (item: import('../../api/types').PlannedOptimizationTrade) => formatMoney(item.reference_price)},
    {id: 'state', header: 'Planning state', cell: (item: import('../../api/types').PlannedOptimizationTrade) => <StatusBadge status={item.suppressed ? item.suppression_reason || 'SUPPRESSED' : 'PLANNED'} />},
  ]
  return <div className="page-stack">
    <form key={`${universe?.updated_at || 'new'}:${policy?.version || 0}`} className="page-stack" onSubmit={(event) => {event.preventDefault(); save.mutate(event.currentTarget)}}>
      <div className="form-grid four-columns">
        <label>Universe<select aria-label="Portfolio universe" multiple value={instrumentIds.map(String)} onChange={(event) => setInstrumentIds(Array.from(event.currentTarget.selectedOptions).map((option) => Number(option.value)))}>{stockInstruments.map((item) => <option key={item.id} value={item.id}>{item.symbol} · {item.sector || item.exchange}</option>)}</select><span className={instrumentIds.length > maximumInstruments ? 'field-error' : 'field-help'}>Selected {instrumentIds.length} of {maximumInstruments} maximum</span></label>
        <label>Method<select name="method" defaultValue={policy?.method || 'MINIMUM_VARIANCE'}><option value="MINIMUM_VARIANCE">Minimum variance</option><option value="MAXIMUM_SHARPE">Maximum Sharpe</option></select></label>
        <label>Lookback days<input name="lookback_days" type="number" min="30" defaultValue={policy?.lookback_days || 252} /></label>
        <label>Minimum observations<input name="minimum_history_observations" type="number" min="20" defaultValue={universe?.minimum_history_observations || 60} /></label>
        <label>Minimum weight<input name="minimum_weight" type="number" min="0" max="1" step="0.001" defaultValue={String(policy?.minimum_weight ?? 0)} /></label>
        <label>Maximum weight<input name="maximum_weight" type="number" min="0" max="1" step="0.001" defaultValue={String(policy?.maximum_weight ?? 1)} /></label>
        <label>Target cash weight<input name="target_cash_weight" type="number" min="0" max="0.99" step="0.001" defaultValue={String(policy?.target_cash_weight ?? 0.02)} /></label>
        <label>Maximum turnover<input name="maximum_turnover" type="number" min="0" max="10" step="0.01" defaultValue={String(policy?.maximum_turnover ?? 1)} /></label>
        <label>Risk-free rate<input name="risk_free_rate" type="number" step="0.001" defaultValue={String(policy?.risk_free_rate ?? 0)} /></label>
        <label>Transaction-cost penalty<input name="transaction_cost_penalty" type="number" min="0" step="0.001" defaultValue={String(policy?.transaction_cost_penalty ?? 0)} /></label>
        <label>Maximum instruments<input name="maximum_instruments" type="number" min="2" max="100" value={maximumInstruments} onChange={(event) => setMaximumInstruments(Number(event.target.value))} /></label>
        <label className="checkbox-field"><input name="include_strategy_instruments" type="checkbox" defaultChecked={universe?.include_strategy_instruments || false} />Include active strategy stocks</label>
      </div>
      <div className="system-actions"><button className="button-secondary" disabled={save.isPending || instrumentIds.length > maximumInstruments}>{save.isPending ? 'Saving…' : 'Save universe & policy'}</button><button type="button" className="button-primary" disabled={!universe || !policy || optimize.isPending} onClick={() => optimize.mutate()}>{optimize.isPending ? 'Optimizing…' : 'Preview optimization'}</button>{preview && <button type="button" className="button-secondary" disabled={!executionMode || apply.isPending || preview.application_status === 'APPLIED' || Boolean(preview.applied_rebalance)} onClick={() => apply.mutate()}>{preview.applied_rebalance ? 'Optimization already applied' : apply.isPending ? 'Planning…' : executionMode ? `Apply to ${executionModeLabel(executionMode)}` : 'Apply unavailable'}</button>}</div>
      <p className="inline-note">Preview creates no orders. Apply routes through the selected portfolio’s {executionMode ? executionModeLabel(executionMode) : 'unavailable'} Gateway session.</p>
    </form>
    {save.isError && <ErrorState title="Advanced optimizer settings were not saved" error={save.error} compact />}
    {optimize.isError && <ErrorState title="Optimization preview failed" error={optimize.error} compact />}
    {apply.isError && <ErrorState title="Optimized rebalance was blocked" error={apply.error} compact />}
    {preview?.applied_rebalance && <div className="inline-success"><StatusBadge status={preview.applied_rebalance.status} />Applied rebalance {preview.applied_rebalance.id} · {preview.applied_rebalance.mode}</div>}
    {preview && <>
      <section className="metric-grid compact"><TerminalMetric label="Expected return" value={formatPercent(preview.expected_return)} /><TerminalMetric label="Expected volatility" value={formatPercent(preview.expected_volatility)} /><TerminalMetric label="Sharpe ratio" value={formatNumber(preview.sharpe_ratio)} /><TerminalMetric label="Estimated turnover" value={formatPercent(preview.turnover)} /><TerminalMetric label="Cash target" value={formatPercent(preview.cash_weight)} /><TerminalMetric label="Preview run type" value={<StatusBadge status={preview.rebalance?.run_type || 'PREVIEW'} />} /><TerminalMetric label="Apply route" value={<StatusBadge status={executionMode || 'UNAVAILABLE'} />} /></section>
      <div><h3>Current versus optimized allocation</h3><DataTable rows={preview.targets || []} columns={allocationColumns} getRowKey={(item) => item.id} emptyTitle="No optimized targets" /></div>
      <div><h3>Planned trades</h3><DataTable rows={preview.planned_trades || []} columns={tradeColumns} getRowKey={(item) => item.instrument_id} emptyTitle="No trades required" emptyDescription="Targets are already within planning tolerances." /></div>
      {preview.warnings.length > 0 && <div className="inline-warning"><StatusBadge status="WARNING" /><div><strong>Solver warnings</strong><p>{formatCompact(preview.warnings)}</p></div></div>}
    </>}
  </div>
}
