import {useState} from 'react'
import {useMutation, useQuery, useQueryClient, type UseQueryResult} from '@tanstack/react-query'
import {ArrowLeft, CirclePause, Power, RefreshCw, SlidersHorizontal, Trash2} from 'lucide-react'
import {Link, useNavigate, useParams, useSearchParams} from 'react-router-dom'
import {mutationOptions, request} from '../../api/client'
import {queries} from '../../api/queries'
import type {StrategyChartData, StrategyInstance, StrategyLedgerRow, StrategyPnl, StrategyTimelineItem} from '../../api/types'
import {ActivityTimeline} from '../../components/ActivityTimeline'
import {ConfirmActionDialog, DataTable, DeleteStrategyDialog, EmptyState, ErrorState, Freshness, PageHeader, Skeleton, StatusBadge, TerminalMetric, TerminalPanel, formatCompact, formatDateTime, formatNumber, formatPercent} from '../../components/ui'
import {EditStrategyPanel} from './EditStrategyPanel'
import {StrategyLedgerPanel} from './StrategyLedger'
import {RunningPnlStrip, StrategyPnlPanel, StrategyTradeChart} from './StrategyPerformance'
import {canEnable, canFlatten, canPause, refreshAfterStrategyDeletion} from './strategyActions'

const tabs = ['Overview', 'Ledger', 'Chart', 'Activity', 'Configuration', 'Advanced'] as const
type Tab = typeof tabs[number]

export function StrategyDetailPage() {
  const {strategyId = ''} = useParams()
  const id = Number(strategyId)
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const requestedTab = searchParams.get('tab')
  const tab: Tab = tabs.find((item) => item.toLowerCase() === requestedTab?.toLowerCase()) || 'Overview'
  const [flattenOpen, setFlattenOpen] = useState(false)
  const [deleteOpen, setDeleteOpen] = useState(false)
  const strategy = useQuery(queries.strategy(id))
  const timeline = useQuery({...queries.strategyTimeline(id), enabled: id > 0 && (tab === 'Overview' || tab === 'Activity')})
  const chart = useQuery({...queries.strategyChart(id), enabled: id > 0 && (tab === 'Overview' || tab === 'Chart')})
  const ledger = useQuery({...queries.strategyLedger(id), enabled: id > 0 && (tab === 'Overview' || tab === 'Ledger' || tab === 'Chart')})
  const pnl = useQuery(queries.strategyPnl(id))
  const action = useMutation({
    mutationFn: ({name, reason, retryKey}: {name: 'enable' | 'pause' | 'flatten'; reason?: string; retryKey?: string}) => {
      const options = mutationOptions(
        'POST',
        reason ? {reason, event_id: `operator-${name}-${crypto.randomUUID()}`} : {},
        true,
        retryKey,
      )
      if (retryKey) {
        const headers = new Headers(options.headers)
        headers.set('Idempotency-Retry', 'true')
        options.headers = headers
      }
      return request<unknown>(`strategy-instances/${id}/${name}/`, options)
    },
    onSuccess: async () => {
      setFlattenOpen(false)
      await Promise.all([
        queryClient.invalidateQueries({queryKey: ['strategy-instance', id]}),
        queryClient.invalidateQueries({queryKey: ['strategy-instances']}),
        queryClient.invalidateQueries({queryKey: ['strategy-timeline', id]}),
        queryClient.invalidateQueries({queryKey: ['strategy-chart', id]}),
        queryClient.invalidateQueries({queryKey: ['strategy-ledger', id]}),
        queryClient.invalidateQueries({queryKey: ['strategy-pnl', id]}),
      ])
    },
  })
  const deleteAction = useMutation({
    mutationFn: (name: string) => request<{id: number}>(`strategy-instances/${id}/`, mutationOptions('DELETE', {strategy_name: name}, true)),
    onSuccess: async () => {
      setDeleteOpen(false)
      navigate('/strategies', {replace: true})
      await refreshAfterStrategyDeletion(queryClient, id)
    },
  })

  if (!Number.isFinite(id)) return <ErrorState title="Invalid strategy link" error={new Error('The strategy identifier is not valid.')} />
  if (strategy.isLoading) return <><PageHeader title="Strategy" description="Loading strategy state and immutable configuration…" /><Skeleton lines={7} height={500} /></>
  if (strategy.isError || !strategy.data) return <ErrorState title="Strategy not found" error={strategy.error} onRetry={() => void strategy.refetch()} />
  const item = strategy.data

  return <div className="page-stack">
    <PageHeader eyebrow={`${item.portfolio} / ${item.symbol}`} title={item.name} description={`${item.definition_name} · ${item.timeframe} · version ${item.version}`} actions={<><Freshness updatedAt={strategy.dataUpdatedAt} stale={strategy.isStale} fetching={strategy.isFetching} onRefresh={() => void strategy.refetch()} /><Link className="button-secondary" to="/strategies"><ArrowLeft />All strategies</Link></>} />
    <div className="strategy-control-bar"><div><StatusBadge status={item.enabled ? 'ENABLED' : 'DISABLED'} /><StatusBadge status={item.execution_mode} /><StatusBadge status={item.state} /><StatusBadge status={item.execution_workflow?.status || 'UNKNOWN'} />{item.conid ? <StatusBadge status="CONTRACT QUALIFIED" /> : <StatusBadge status="CONTRACT PENDING" />}</div><div><button className="button-secondary" disabled={!canEnable(item) || item.activation_operation?.status === 'FAILED' || action.isPending || deleteAction.isPending} onClick={() => action.mutate({name: 'enable'})}><Power />Enable</button>{item.activation_operation?.status === 'FAILED' && item.activation_operation.retryable && <button className="button-secondary" disabled={action.isPending || deleteAction.isPending} onClick={() => action.mutate({name: 'enable', retryKey: item.activation_operation!.idempotency_key})}><RefreshCw />Retry activation</button>}<button className="button-secondary" disabled={!canPause(item) || action.isPending || deleteAction.isPending} onClick={() => action.mutate({name: 'pause'})}><CirclePause />Pause</button><button className="button-danger-subtle" aria-label={`Delete ${item.name}`} disabled={action.isPending || deleteAction.isPending} onClick={() => setDeleteOpen(true)}><Trash2 />Delete</button><button className="button-danger-subtle" disabled={!canFlatten(item) || action.isPending || deleteAction.isPending} onClick={() => setFlattenOpen(true)}><SlidersHorizontal />Flatten target</button></div></div>
    <RunningPnlStrip query={pnl} />
    {item.block_reason && <div className="inline-warning"><StatusBadge status="BLOCKED" /><div><strong>Strategy is not ready</strong><p>{item.block_reason}</p><p>Backend retryable: {item.activation_operation?.retryable ? 'Yes' : 'No'}</p></div></div>}
    {action.isError && <ErrorState title="Strategy action failed" error={action.error} compact />}
    {deleteAction.isError && <ErrorState title="Strategy deletion blocked" error={deleteAction.error} compact />}
    <div className="tabs" role="tablist" aria-label="Strategy details">{tabs.map((name) => <button key={name} role="tab" aria-selected={tab === name} className={tab === name ? 'active' : ''} onClick={() => setSearchParams({tab: name.toLowerCase()})}>{name}</button>)}</div>
    {tab === 'Overview' && <OverviewTab strategy={item} timeline={timeline.data || []} timelineLoading={timeline.isLoading} chart={chart} ledger={ledger} pnl={pnl} />}
    {tab === 'Ledger' && <StrategyLedgerPanel query={ledger} pageSize={50} />}
    {tab === 'Chart' && <StrategyTradeChart query={chart} ledger={ledger.data} symbol={item.symbol} />}
    {tab === 'Activity' && <ActivityTab query={timeline} />}
    {tab === 'Configuration' && <ConfigurationTab strategy={item} />}
    {tab === 'Advanced' && <AdvancedTab strategy={item} />}
    <ConfirmActionDialog open={flattenOpen} title={`Flatten ${item.name}?`} description="This records a zero target for this strategy’s attributed exposure. It does not place an order directly." confirmLabel="Create flat target" pending={action.isPending} onClose={() => setFlattenOpen(false)} onConfirm={(reason) => action.mutate({name: 'flatten', reason})} />
    <DeleteStrategyDialog open={deleteOpen} strategyName={item.name} pending={deleteAction.isPending} onClose={() => setDeleteOpen(false)} onConfirm={() => deleteAction.mutate(item.name)} />
  </div>
}

function OverviewTab({strategy, timeline, timelineLoading, chart, ledger, pnl}: {strategy: StrategyInstance; timeline: StrategyTimelineItem[]; timelineLoading: boolean; chart: UseQueryResult<StrategyChartData, Error>; ledger: UseQueryResult<StrategyLedgerRow[], Error>; pnl: UseQueryResult<StrategyPnl, Error>}) {
  const warmup = strategy.warmup_required ? Math.min(1, strategy.warmup_progress / strategy.warmup_required) : 1
  return <div className="page-stack">
    <section className="workflow-summary" aria-label="Automatic execution workflow">
      <div><span>Workflow</span><StatusBadge status={strategy.execution_workflow?.status || 'UNKNOWN'} /></div>
      <div><span>Current stage</span><strong>{strategy.execution_workflow?.current_stage?.replaceAll('_', ' ') || 'Created'}</strong></div>
      <div><span>Trace ID</span><code>{strategy.execution_workflow?.trace_id || 'Not available'}</code></div>
      <div><span>Polling</span><strong>{strategy.execution_workflow?.terminal ? 'Stopped at terminal state' : strategy.execution_workflow?.active ? 'Every second' : 'Every 12 seconds'}</strong></div>
    </section>
    <div className="detail-grid">
      <StrategyPnlPanel query={pnl} />
      <StrategyTradeChart query={chart} ledger={ledger.data} symbol={strategy.symbol} />
      <StrategyLedgerPanel query={ledger} pageSize={10} title="Recent decisions" />
    </div>
    <section className="metric-grid compact">
      <TerminalMetric label="Latest signal" value={strategy.latest_signal || 'No signal'} helper={strategy.last_final_bar ? `Final bar ${formatDateTime(strategy.last_final_bar)}` : 'Awaiting a final bar'} />
      <TerminalMetric label="Current target" value={strategy.option_execution ? (strategy.option_execution.selected_strategy?.replaceAll('_', ' ') || 'Awaiting ATR/OI entry') : formatPercent(strategy.current_target)} helper={strategy.option_execution ? 'Locked multi-leg structure' : 'Strategy-attributed target'} />
      <TerminalMetric label="Attributed quantity" value={<span className="mono">{formatNumber(strategy.attributed_quantity)}</span>} helper="Ledger-derived" />
      <TerminalMetric label="Active order" value={strategy.active_order ? <code>{strategy.active_order.slice(0, 12)}</code> : 'None'} helper={strategy.last_fill ? `Last fill ${strategy.last_fill}` : 'No recent fill'} />
    </section>
    <div className="detail-grid">
      {strategy.option_execution && <OptionExecutionPanel strategy={strategy} defaultOpen={false} />}
      <TerminalPanel id="execution-price" title="Execution price" description="Persisted price provenance and execution freshness" defaultOpen={false}><dl className="detail-list"><div><dt>Price</dt><dd className="mono">{formatNumber(strategy.current_price?.value)}</dd></div><div><dt>Provider</dt><dd><StatusBadge status={strategy.current_price?.provider || 'UNKNOWN'} /></dd></div><div><dt>Data kind</dt><dd><StatusBadge status={strategy.current_price?.data_kind || 'UNKNOWN'} /></dd></div><div><dt>Source</dt><dd>{strategy.current_price?.source || 'Unknown'}</dd></div><div><dt>Timestamp</dt><dd>{formatDateTime(strategy.current_price?.timestamp)}</dd></div><div><dt>Fresh for execution</dt><dd><StatusBadge status={strategy.current_price?.fresh_for_execution ? 'FRESH' : 'NOT FRESH'} /></dd></div></dl></TerminalPanel>
      <TerminalPanel id="readiness" title="Readiness" description="Contract, subscription, and persisted streaming progress" defaultOpen={false}><dl className="detail-list"><div><dt>Enabled</dt><dd><StatusBadge status={strategy.enabled ? 'ENABLED' : 'DISABLED'} /></dd></div><div><dt>Activation</dt><dd><StatusBadge status={strategy.activation_status} /></dd></div><div><dt>Data path</dt><dd><StatusBadge status={strategy.streaming?.status || 'UNKNOWN'} /></dd></div><div><dt>Contract</dt><dd>{strategy.conid ? <><StatusBadge status="QUALIFIED" /><code>{strategy.conid}</code></> : <StatusBadge status="PENDING" />}</dd></div><div><dt>Subscription</dt><dd><StatusBadge status={strategy.streaming?.subscription_state || 'MISSING'} /></dd></div><div><dt>Active provider</dt><dd><StatusBadge status={strategy.streaming?.active_provider || 'NONE'} /></dd></div><div><dt>Warm-up</dt><dd>{!strategy.enabled && strategy.state === 'DISABLED' ? <StatusBadge status="DISABLED" /> : <div className="wide-progress"><span>{strategy.warmup_progress} / {strategy.warmup_required} bars</span><div><i style={{width: `${warmup * 100}%`}} /></div></div>}</dd></div><div><dt>Last raw event</dt><dd>{formatDateTime(strategy.streaming?.last_raw_event)}</dd></div><div><dt>Last canonical event</dt><dd>{formatDateTime(strategy.streaming?.last_canonical_event)}</dd></div><div><dt>Last final bar</dt><dd>{formatDateTime(strategy.last_final_bar)}</dd></div><div><dt>Last indicator</dt><dd>{formatDateTime(strategy.streaming?.last_indicator)}</dd></div><div><dt>Last strategy run</dt><dd>{formatDateTime(strategy.streaming?.last_strategy_run)}</dd></div><div><dt>State</dt><dd><StatusBadge status={strategy.state} /></dd></div><div><dt>Mode</dt><dd><StatusBadge status={strategy.execution_mode} /></dd></div>{strategy.block_reason && <div><dt>Block reason</dt><dd>{strategy.block_reason}</dd></div>}{strategy.streaming?.last_error && <div><dt>Last error</dt><dd>{strategy.streaming.last_error}</dd></div>}</dl></TerminalPanel>
      <TerminalPanel id="latest-indicators" title="Latest indicators" description="Values used by the strategy’s current input bindings" defaultOpen={false}>{Object.entries(strategy.latest_indicators).length ? <dl className="indicator-list">{Object.entries(strategy.latest_indicators).map(([name, value]) => <div key={name}><dt>{name.replaceAll('_', ' ')}</dt><dd className="mono">{formatNumber(value)}</dd></div>)}</dl> : <EmptyState title="Indicators are warming up" description="Persisted final indicator values will appear after the required inputs arrive." />}</TerminalPanel>
      <TerminalPanel id="execution-timeline" title="Execution timeline" description={`One trace across activation, market data, strategy, target, rebalance, intent, order, broker command, and fill: ${strategy.execution_workflow?.trace_id || 'pending'}`} className="detail-wide" defaultOpen={false}>{timelineLoading ? <Skeleton lines={4} /> : <ActivityTimeline items={timeline.map(timelineItem)} />}</TerminalPanel>
    </div>
  </div>
}

export function OptionExecutionPanel({strategy, defaultOpen = true}: {strategy: StrategyInstance; defaultOpen?: boolean}) {
  const option = strategy.option_execution!
  const scoreScale = option.entry_score_scale || 100
  const score = option.entry_score == null ? '—' : `${formatNumber(option.entry_score)} / ${formatNumber(scoreScale)}`
  const scoreWeights = Object.entries(option.entry_score_weights || {})
    .map(([name, weight]) => `${name.replaceAll('_', ' ')} ${formatNumber(weight)}%`)
    .join(' · ')
  const provider = option.selection_provider || 'IBKR'
  const holding = Number(strategy.attributed_quantity || 0) !== 0
  return <TerminalPanel id="option-execution" title="Automatic option execution" description={`RPS entry, TQS structure selection and hold-to-expiry exits; chain, quotes and execution on ${provider}`} className="detail-wide" defaultOpen={defaultOpen}>
    {!holding && option.last_entry_check && <OptionEntryDecision check={option.last_entry_check} option={option} parameters={strategy.parameters || {}} />}
    <dl className="detail-list columns">
      <div><dt>Status</dt><dd><StatusBadge status={option.status} />{option.readiness_reason && <small>{option.readiness_reason}</small>}</dd></div>
      <div><dt>Selected structure</dt><dd><strong>{option.selected_strategy?.replaceAll('_', ' ') || 'Waiting for entry'}</strong></dd></div>
      <div><dt>RPS score</dt><dd className="mono">{score}</dd></div>
      <div><dt>Expiry</dt><dd className="mono">{option.expiration || 'Not selected'}</dd></div>
      <div><dt>ATR range</dt><dd className="mono">{formatNumber(option.atr_support)} – {formatNumber(option.atr_resistance)}</dd></div>
      <div><dt>OI walls</dt><dd className="mono">{formatNumber(option.oi_support)} – {formatNumber(option.oi_resistance)}</dd></div>
      <div><dt>Boundary</dt><dd>Prior completed daily close ± {formatNumber(option.atr_multiplier)} × Wilder ATR(14)</dd></div>
      <div><dt>Observed</dt><dd>{formatDateTime(option.observed_at)}</dd></div>
    </dl>
    <OptionDecisionBreakdown option={option} scoreWeights={scoreWeights} />
    {option.legs.length ? <div className="option-leg-grid" aria-label="Selected option legs">{option.legs.map((leg) => <div key={`${leg.instrument_id}:${leg.role || leg.symbol}`}><span><StatusBadge status={leg.side || (Number(leg.quantity) < 0 ? 'SELL' : 'BUY')} /><strong>{leg.role?.replaceAll('_', ' ') || leg.symbol || `Instrument ${leg.instrument_id}`}</strong></span><code>{leg.expiration} · {leg.strike} {leg.right} · ×{leg.multiplier}</code><small>IBKR conId {leg.conid || '—'}{leg.dhan_oi !== undefined ? ` · OI ${leg.dhan_oi}` : ''}</small></div>)}</div> : <EmptyState title="No contracts selected yet" description="Exact IBKR legs are qualified only once RPS, the IV and breach gate, and the POP and EV filters have all passed." />}
  </TerminalPanel>
}

/** The entry gates in the order the backend applies them. A refusal at one
 * gate means every later gate was never evaluated for that bar. */
const ENTRY_GATES = [
  {label: 'Entry window open', parameters: 'rps_market_open_time, rps_entry_delay_minutes'},
  {label: 'Expiry inside the DTE window', parameters: 'entry_min_dte, entry_max_dte'},
  {label: 'RPS at or above its floor', parameters: 'rps_threshold'},
  {label: 'Liquid structure in the chain', parameters: 'minimum_open_interest, minimum_volume'},
  {label: 'IV score and breach-probability gate', parameters: 'rps_iv_score_threshold, rps_max_upper_breach_probability, rps_max_lower_breach_probability'},
  {label: 'Loss, POP, EV, routing and TQS', parameters: 'rps_max_entry_loss, rps_min_pop, rps_require_positive_ev, rps_tqs_threshold'},
  {label: 'Exact IBKR contracts', parameters: ''},
  {label: 'One entry per expiry', parameters: 'one_entry_per_expiry'},
] as const

type OptionState = NonNullable<StrategyInstance['option_execution']>
type EntryCheck = NonNullable<OptionState['last_entry_check']>

/** Index of the gate that refused, `ENTRY_GATES.length` when all passed, or
 * null for a status this page does not know. */
export function refusingGate(check: EntryCheck): number | null {
  const rankings = check.selection_evidence?.rankings || []
  switch (check.status) {
    case 'ENTRY_WINDOW_CLOSED': return 0
    case 'NO_EXPIRY': return 1
    case 'MISSING_BOUNDARY':
    case 'ENTRY_GATE_CLOSED': return 2
    case 'NO_LIQUID_STRUCTURE':
    case 'UNSUPPORTED_PROVIDER': return 3
    case 'NO_ELIGIBLE_STRUCTURE': return rankings.some((row) => row.entry_signal?.entry_allowed) ? 5 : 4
    case 'NO_IBKR_STRUCTURE': return 6
    case 'EXPIRY_ALREADY_ENTERED': return 7
    case 'READY':
    case 'WAITING_FOR_OPTION_QUOTES': return ENTRY_GATES.length
    default: return null
  }
}

/** Refusal reasons shared by every graded candidate: when all structures fail
 * for the same reason, that reason is the holdup. Informational notes are not
 * blockers, so they are left out. */
export function sharedRefusalReasons(check: EntryCheck): string[] {
  const rankings = check.selection_evidence?.rankings || []
  const lists = rankings.map((row) => (row.entry_signal?.reasons || []).filter((reason) => !reason.includes('informational')))
  if (!lists.length) return []
  return lists[0].filter((reason) => lists.every((list) => list.includes(reason)))
}

function ThresholdMetric({label, value, threshold, unmeasured}: {label: string; value?: string | number | null; threshold?: string | number | null; unmeasured: string}) {
  const measured = value !== null && value !== undefined && value !== ''
  const passed = measured && threshold !== null && threshold !== undefined && Number(value) >= Number(threshold)
  return <div className="entry-threshold">
    <span>{label}</span>
    <strong className="mono">{measured ? formatNumber(value, 2) : '—'}<small> / floor {formatNumber(threshold ?? null, 2)}</small></strong>
    {measured ? <StatusBadge status={passed ? 'PASSED' : 'BELOW FLOOR'} tone={passed ? 'positive' : 'critical'} /> : <small>{unmeasured}</small>}
  </div>
}

/** Why the strategy is not trading right now, for the bar just evaluated. */
export function OptionEntryDecision({check, option, parameters}: {check: EntryCheck; option: OptionState; parameters: Record<string, unknown>}) {
  const refused = refusingGate(check)
  const passedAll = refused === ENTRY_GATES.length
  const rankings = check.selection_evidence?.rankings || []
  const ivSignal = rankings.find((row) => row.entry_signal?.iv_score != null)?.entry_signal
  const shared = sharedRefusalReasons(check)
  const gate = refused !== null && !passedAll ? ENTRY_GATES[refused] : null
  const reachedRps = refused === null || refused >= 2
  const reachedIv = refused === null || refused >= 4
  const headline = passedAll
    ? 'Every entry gate passed on the latest bar'
    : gate ? `No trade: refused at “${gate.label}”` : `No trade: ${check.status.replaceAll('_', ' ').toLowerCase()}`
  return <section className={`entry-decision ${passedAll ? 'passed' : 'refused'}`} aria-label="Latest entry decision">
    <header>
      <div>
        <StatusBadge status={check.status} tone={passedAll ? 'positive' : 'warning'} />
        <strong>{headline}</strong>
      </div>
      <small>Evaluated on the bar ending {formatDateTime(check.evaluated_at)}</small>
    </header>
    <p className="entry-decision-reason">{shared.length ? shared.join(' · ') : check.reason}</p>
    <div className="entry-threshold-grid">
      <ThresholdMetric label="RPS score" value={check.entry_score as string | null} threshold={(check.entry_threshold ?? option.entry_threshold) as string | null} unmeasured={reachedRps ? 'Not measurable from causal daily inputs' : 'Not reached: an earlier gate refused'} />
      <ThresholdMetric label="IV score" value={(ivSignal?.iv_score ?? ivSignal?.score) as string | null} threshold={(check.iv_score_threshold ?? option.iv_score_threshold) as string | null} unmeasured={reachedIv ? 'No candidate reached the IV gate' : 'Not reached: an earlier gate refused'} />
      <div className="entry-threshold">
        <span>IV rank / percentile (252d)</span>
        <strong className="mono">{formatNumber((ivSignal?.iv_rank ?? check.iv_rank_252) as string | null, 2)} / {formatNumber((ivSignal?.iv_percentile ?? check.iv_percentile_252) as string | null, 2)}</strong>
        <small>{ivSignal?.iv_rank_source || (check.chain_features_available ? 'Remote feature database' : 'Remote features unavailable')}</small>
      </div>
      <div className="entry-threshold">
        <span>Spot</span>
        <strong className="mono">{formatNumber(check.spot as string | null, 2)}</strong>
        <small>{rankings.length ? `${rankings.length} candidate structures graded` : 'No candidates graded'}</small>
      </div>
    </div>
    <ol className="entry-gates" aria-label="Entry gates in evaluation order">
      {ENTRY_GATES.map((item, index) => {
        const state = refused === null ? 'unknown' : index < refused ? 'passed' : index === refused ? 'refused' : 'skipped'
        return <li key={item.label} className={`entry-gate ${state}`}>
          <StatusBadge status={state === 'passed' ? 'PASSED' : state === 'refused' ? 'REFUSED' : state === 'skipped' ? 'NOT REACHED' : 'UNKNOWN'} tone={state === 'passed' ? 'positive' : state === 'refused' ? 'critical' : 'neutral'} />
          <span>{item.label}</span>
        </li>
      })}
    </ol>
    {gate?.parameters && <p className="field-help">This gate is controlled by: {gate.parameters.split(', ').map((name, index) => <span key={name}>{index > 0 && ', '}<code>{name}</code>{parameters[name] !== undefined && <> = <code>{formatCompact(parameters[name])}</code></>}</span>)}. Entry happens automatically on the next bar that passes every gate.</p>}
  </section>
}

/** Why this evaluation did or did not enter.
 *
 * Every stage can refuse, and a refusal is the normal outcome most of the time,
 * so the operator question is always "which gate, and on what number". The
 * candidate table answers it per structure. */
export function OptionDecisionBreakdown({option, scoreWeights}: {option: NonNullable<StrategyInstance['option_execution']>; scoreWeights: string}) {
  const components = Object.entries(option.entry_score_components || {})
  const signal = option.entry_signal
  const evidence = option.selection_evidence
  const rankings = evidence?.rankings || []
  const unavailable = evidence?.unavailable_inputs || []
  if (!components.length && !signal && !rankings.length) return null

  return <div className="option-decision">
    {components.length > 0 && <div className="option-decision-block">
      <h4>RPS factors</h4>
      <p className="field-help">{scoreWeights}</p>
      <dl className="detail-list columns">{components.map(([name, value]) => <div key={name}><dt>{name.replaceAll('_', ' ')}</dt><dd className="mono">{value == null ? <span className="field-help">not measurable</span> : formatNumber(value)}</dd></div>)}</dl>
    </div>}
    {signal && <div className="option-decision-block">
      <h4>Entry gate</h4>
      <dl className="detail-list columns">
        <div><dt>Allowed</dt><dd><StatusBadge status={signal.entry_allowed ? 'ALLOWED' : 'BLOCKED'} /></dd></div>
        <div><dt>IV score</dt><dd className="mono">{formatNumber(signal.iv_score ?? signal.score)}</dd></div>
        <div><dt>IV rank</dt><dd className="mono">{formatNumber(signal.iv_rank)}{signal.iv_rank_source ? <small>{signal.iv_rank_source}</small> : null}</dd></div>
        <div><dt>IV percentile</dt><dd className="mono">{formatNumber(signal.iv_percentile)}</dd></div>
        <div><dt>Breach probability</dt><dd className="mono">upper {formatNumber(signal.upper_breach_probability)} · lower {formatNumber(signal.lower_breach_probability)}</dd></div>
      </dl>
      {signal.reasons?.length ? <ul className="option-decision-reasons">{signal.reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul> : null}
    </div>}
    {rankings.length > 0 && <div className="option-decision-block">
      <h4>Candidate structures</h4>
      <DataTable
        rows={rankings}
        getRowKey={(row) => row.strategy_key || JSON.stringify(row)}
        emptyTitle="No candidates were graded"
        columns={[
          {id: 'strategy', header: 'Structure', cell: (row) => <strong>{row.strategy_key?.replaceAll('_', ' ') || '—'}</strong>},
          {id: 'eligible', header: 'Outcome', cell: (row) => <StatusBadge status={row.eligible ? 'ELIGIBLE' : 'REFUSED'} />},
          {id: 'iv', header: 'IV score', align: 'right' as const, cell: (row) => formatNumber(row.entry_signal?.iv_score ?? row.entry_signal?.score, 2)},
          {id: 'loss', header: 'Loss probability', align: 'right' as const, cell: (row) => formatPercent(row.entry_signal?.either_breach_probability)},
          {id: 'pop', header: 'POP', align: 'right' as const, cell: (row) => row.pop == null ? '—' : formatNumber(row.pop)},
          {id: 'pnl', header: 'Expected P&L', align: 'right' as const, cell: (row) => row.expected_pnl == null ? '—' : formatNumber(row.expected_pnl)},
          {id: 'reason', header: 'Reason', cell: (row) => <small>{row.rejection_reason || '—'}</small>},
        ]}
      />
    </div>}
    {unavailable.length > 0 && <p className="field-help">Unmeasured inputs, whose weight is renormalised away rather than scored zero: {unavailable.join(', ')}.</p>}
  </div>
}

function ActivityTab({query}: {query: UseQueryResult<StrategyTimelineItem[], Error>}) {
  if (query.isLoading) return <Skeleton lines={6} />
  if (query.isError) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />
  return <TerminalPanel id="complete-execution-timeline" title="Complete execution timeline" description="All automatic paper-execution stages in workflow order, including pending stages and concrete blockers"><ActivityTimeline items={(query.data || []).map(timelineItem)} /></TerminalPanel>
}

function timelineItem(item: StrategyTimelineItem) {
  if (item.stage) {
    const detail = [
      item.detail,
      item.blocker ? `Blocker: ${item.blocker}` : '',
      item.blocker ? `Retryable: ${item.retryable ? 'Yes' : 'No'}` : '',
      item.entity_type && item.entity_id ? `${item.entity_type} ${item.entity_id}` : '',
    ].filter(Boolean).join(' · ')
    return {
      id: item.entity_id || item.stage,
      time: item.occurred_at,
      type: item.stage,
      title: item.label || item.stage.replaceAll('_', ' '),
      detail,
      status: item.status,
    }
  }
  item.id ||= 0
  item.type ||= 'EVENT'
  return {id: item.id, time: item.time, type: item.type, title: item.type.replaceAll('_', ' '), detail: `${item.detail || ''}${item.version ? `${item.detail ? ' · ' : ''}Version ${item.version}` : ''}`, status: item.status}
}

function ConfigurationTab({strategy}: {strategy: StrategyInstance}) {
  return <div className="detail-grid"><EditStrategyPanel strategy={strategy} /><TerminalPanel id="parameters" title="Parameters" description="Immutable version configuration"><dl className="configuration-list">{Object.entries(strategy.parameters).map(([key, value]) => <div key={key}><dt>{key.replaceAll('_', ' ')}</dt><dd><code>{formatCompact(value)}</code></dd></div>)}</dl></TerminalPanel><TerminalPanel id="target-configuration" title="Target configuration"><dl className="configuration-list">{Object.entries(strategy.target_configuration).map(([key, value]) => <div key={key}><dt>{key.replaceAll('_', ' ')}</dt><dd><code>{formatCompact(value)}</code></dd></div>)}</dl></TerminalPanel><TerminalPanel id="binding" title="Binding" className="detail-wide"><dl className="detail-list columns"><div><dt>Portfolio</dt><dd>{strategy.portfolio}</dd></div><div><dt>Instrument</dt><dd className="mono">{strategy.symbol} · {strategy.exchange}</dd></div><div><dt>Definition</dt><dd>{strategy.definition_name}</dd></div><div><dt>Timeframe</dt><dd className="mono">{strategy.timeframe}</dd></div><div><dt>Risk policy</dt><dd>{strategy.risk_policy_id || 'Platform default'}</dd></div><div><dt>Order policy</dt><dd>{strategy.order_policy_id || 'Platform default'}</dd></div></dl></TerminalPanel></div>
}

function AdvancedTab({strategy}: {strategy: StrategyInstance}) {
  const versionColumns = [
    {id: 'version', header: 'Version', cell: (item: NonNullable<StrategyInstance['versions']>[number]) => <strong>v{item.version}</strong>},
    {id: 'hash', header: 'Parameter hash', cell: (item: NonNullable<StrategyInstance['versions']>[number]) => <code>{item.parameter_hash.slice(0, 16)}…</code>},
    {id: 'created', header: 'Created', cell: (item: NonNullable<StrategyInstance['versions']>[number]) => formatDateTime(item.created_at)},
    {id: 'activated', header: 'Activated', cell: (item: NonNullable<StrategyInstance['versions']>[number]) => formatDateTime(item.activated_at)},
    {id: 'retired', header: 'Retired', cell: (item: NonNullable<StrategyInstance['versions']>[number]) => formatDateTime(item.retired_at)},
  ]
  const requirementColumns = [
    {id: 'type', header: 'Type', cell: (item: NonNullable<StrategyInstance['requirements']>[number]) => <StatusBadge status={item.input_type} />},
    {id: 'name', header: 'Input', cell: (item: NonNullable<StrategyInstance['requirements']>[number]) => <strong>{item.name}</strong>},
    {id: 'parameters', header: 'Parameters', cell: (item: NonNullable<StrategyInstance['requirements']>[number]) => <code>{formatCompact(item.parameters)}</code>},
    {id: 'warmup', header: 'Warm-up', align: 'right' as const, cell: (item: NonNullable<StrategyInstance['requirements']>[number]) => item.warmup_bars},
    {id: 'shared', header: 'Shared by', align: 'right' as const, cell: (item: NonNullable<StrategyInstance['requirements']>[number]) => item.shared_by ?? '—'},
    {id: 'active', header: 'State', cell: (item: NonNullable<StrategyInstance['requirements']>[number]) => <StatusBadge status={item.active ? 'ACTIVE' : 'INACTIVE'} />},
  ]
  return <div className="page-stack"><TerminalPanel id="immutable-versions" title="Immutable versions" defaultOpen={false}><DataTable rows={strategy.versions || []} columns={versionColumns} getRowKey={(item) => item.id} emptyTitle="No version history" /></TerminalPanel><TerminalPanel id="shared-inputs" title="Shared streaming inputs" description="Bindings are deduplicated by instrument, timeframe, input, and parameter hash" defaultOpen={false}><DataTable rows={strategy.requirements || []} columns={requirementColumns} getRowKey={(item) => item.identity_hash || `${item.name}-${JSON.stringify(item.parameters)}`} emptyTitle="No active input bindings" /></TerminalPanel></div>
}
