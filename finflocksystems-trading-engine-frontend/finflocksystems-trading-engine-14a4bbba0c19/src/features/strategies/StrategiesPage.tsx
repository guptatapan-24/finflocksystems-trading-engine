import {useMemo, useState} from 'react'
import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query'
import {CirclePause, Filter, Plus, Power, Search, SlidersHorizontal, Trash2} from 'lucide-react'
import {Link} from 'react-router-dom'
import {mutationOptions, request} from '../../api/client'
import {queries} from '../../api/queries'
import type {StrategyInstance} from '../../api/types'
import {ConfirmActionDialog, DataTable, DeleteStrategyDialog, ErrorState, Freshness, PageHeader, Skeleton, StatusBadge, TerminalPanel, formatDateTime, formatNumber} from '../../components/ui'
import {EXECUTION_MODES} from '../../executionMode'
import {useSelection} from '../../stores/useSelection'
import {canEnable, canFlatten, canPause, refreshAfterStrategyDeletion} from './strategyActions'

export function StrategiesPage() {
  const {selectedPortfolioId} = useSelection()
  const queryClient = useQueryClient()
  const strategies = useQuery(queries.strategies({portfolioId: selectedPortfolioId}))
  const [search, setSearch] = useState('')
  const [state, setState] = useState('')
  const [mode, setMode] = useState('')
  const [timeframe, setTimeframe] = useState('')
  const [symbol, setSymbol] = useState('')
  const [flattening, setFlattening] = useState<StrategyInstance | null>(null)
  const [deleting, setDeleting] = useState<StrategyInstance | null>(null)

  const action = useMutation({
    mutationFn: ({id, name, reason}: {id: number; name: 'enable' | 'pause' | 'flatten'; reason?: string}) => request<unknown>(`strategy-instances/${id}/${name}/`, mutationOptions('POST', reason ? {reason, event_id: `operator-${name}-${crypto.randomUUID()}`} : {}, true)),
    onSuccess: async () => {
      setFlattening(null)
      await queryClient.invalidateQueries({queryKey: ['strategy-instances']})
      await queryClient.invalidateQueries({queryKey: ['dashboard']})
    },
  })
  const deleteAction = useMutation({
    mutationFn: (item: StrategyInstance) => request<{id: number}>(`strategy-instances/${item.id}/`, mutationOptions('DELETE', {strategy_name: item.name}, true)),
    onSuccess: async (_, item) => {
      setDeleting(null)
      await refreshAfterStrategyDeletion(queryClient, item.id)
    },
  })
  const rows = useMemo(() => (strategies.data || []).filter((item) => {
    const text = `${item.name} ${item.symbol} ${item.definition_name} ${item.definition_key}`.toLowerCase()
    return (!search || text.includes(search.toLowerCase())) && (!state || item.state === state) && (!mode || item.execution_mode === mode) && (!timeframe || item.timeframe === timeframe) && (!symbol || item.symbol === symbol)
  }), [mode, search, state, strategies.data, symbol, timeframe])
  const states = [...new Set((strategies.data || []).map((item) => item.state))].sort()
  const timeframes = [...new Set((strategies.data || []).map((item) => item.timeframe))].sort()
  const symbols = [...new Set((strategies.data || []).map((item) => item.symbol))].sort()

  const columns = [
    {id: 'strategy', header: 'Strategy', cell: (item: StrategyInstance) => <div className="primary-cell"><Link to={`/strategies/${item.id}`}>{item.name}</Link><span>{item.definition_name}</span></div>},
    {id: 'instrument', header: 'Instrument', cell: (item: StrategyInstance) => <div className="primary-cell mono"><strong>{item.symbol}</strong><span>{item.exchange} · {item.timeframe}</span></div>},
    {id: 'mode', header: 'Mode', cell: (item: StrategyInstance) => <StatusBadge status={item.execution_mode} />},
    {id: 'enabled', header: 'Enabled', cell: (item: StrategyInstance) => <StatusBadge status={item.enabled ? 'ENABLED' : 'DISABLED'} />},
    {id: 'state', header: 'State', cell: (item: StrategyInstance) => <StatusBadge status={item.state} />},
    {id: 'warmup', header: 'Warm-up', cell: (item: StrategyInstance) => !item.enabled && item.state === 'DISABLED' ? <StatusBadge status="DISABLED" /> : <div className="compact-progress"><span>{Math.min(item.warmup_progress, item.warmup_required)} / {item.warmup_required}</span><div><i style={{width: `${item.warmup_required ? Math.min(100, item.warmup_progress / item.warmup_required * 100) : 100}%`}} /></div></div>},
    {id: 'target', header: 'Target', align: 'right' as const, className: 'mono', cell: (item: StrategyInstance) => formatNumber(item.current_target)},
    {id: 'signal', header: 'Signal', cell: (item: StrategyInstance) => <span className="mono">{item.latest_signal || '—'}</span>},
    {id: 'stream', header: 'Stream', cell: (item: StrategyInstance) => <StatusBadge status={item.streaming?.status || 'UNKNOWN'} />},
    {id: 'order', header: 'Active order', cell: (item: StrategyInstance) => item.active_order ? <code>{item.active_order.slice(0, 10)}</code> : '—'},
    {id: 'updated', header: 'Updated', cell: (item: StrategyInstance) => formatDateTime(item.updated_at)},
    {id: 'actions', header: '', align: 'right' as const, cell: (item: StrategyInstance) => <div className="row-actions">
      <button className="button-quiet" aria-label={`Enable ${item.name}`} disabled={!canEnable(item) || action.isPending || deleteAction.isPending} onClick={() => action.mutate({id: item.id, name: 'enable'})}><Power />Enable</button>
      <button className="button-quiet" aria-label={`Pause ${item.name}`} disabled={!canPause(item) || action.isPending || deleteAction.isPending} onClick={() => action.mutate({id: item.id, name: 'pause'})}><CirclePause />Pause</button>
      <button className="button-danger-subtle" aria-label={`Delete ${item.name}`} disabled={action.isPending || deleteAction.isPending} onClick={() => setDeleting(item)}><Trash2 />Delete</button>
      <button className="button-quiet" aria-label={`Flatten ${item.name}`} disabled={!canFlatten(item) || action.isPending || deleteAction.isPending} onClick={() => setFlattening(item)}><SlidersHorizontal />Flatten</button>
    </div>},
  ]

  return <div className="page-stack">
    <PageHeader eyebrow="Signal to execution trace" title="Strategies" description="Create portable, schema-driven strategies and follow each target through the shared risk and execution path." actions={<><Freshness updatedAt={strategies.dataUpdatedAt} stale={strategies.isStale} fetching={strategies.isFetching} onRefresh={() => void strategies.refetch()} /><Link to="/strategies/new" className="button-primary"><Plus />Create strategy</Link></>} />
    <TerminalPanel id="strategy-table" title="Strategy instances" description="Backend-defined strategies and their current execution state">
      <div className="filter-bar">
        <label className="search-field"><Search /><span className="sr-only">Search strategies</span><input aria-label="Search strategies" placeholder="Search ticker, name, or definition" value={search} onChange={(event) => setSearch(event.target.value)} /></label>
        <label><Filter /><span className="sr-only">Filter strategy state</span><select aria-label="Filter strategy state" value={state} onChange={(event) => setState(event.target.value)}><option value="">All states</option>{states.map((value) => <option key={value}>{value}</option>)}</select></label>
        <label><span className="sr-only">Filter execution mode</span><select aria-label="Filter execution mode" value={mode} onChange={(event) => setMode(event.target.value)}><option value="">All modes</option>{EXECUTION_MODES.map((value) => <option key={value}>{value}</option>)}</select></label>
        <label><span className="sr-only">Filter timeframe</span><select aria-label="Filter timeframe" value={timeframe} onChange={(event) => setTimeframe(event.target.value)}><option value="">All timeframes</option>{timeframes.map((value) => <option key={value}>{value}</option>)}</select></label>
        <label><span className="sr-only">Filter symbol</span><select aria-label="Filter symbol" value={symbol} onChange={(event) => setSymbol(event.target.value)}><option value="">All symbols</option>{symbols.map((value) => <option key={value}>{value}</option>)}</select></label>
        <span className="filter-count">{rows.length} of {(strategies.data || []).length}</span>
      </div>
      {strategies.isLoading ? <Skeleton lines={5} height={300} /> : strategies.isError ? <ErrorState error={strategies.error} onRetry={() => void strategies.refetch()} /> : <DataTable rows={rows} columns={columns} getRowKey={(item) => item.id} emptyTitle="No strategies match" emptyDescription="Adjust the filters or create a strategy for this portfolio." />}
    </TerminalPanel>
    {action.isError && <ErrorState title="Strategy action failed" error={action.error} compact />}
    {deleteAction.isError && <ErrorState title="Strategy deletion blocked" error={deleteAction.error} compact />}
    <ConfirmActionDialog open={Boolean(flattening)} title={`Flatten ${flattening?.name || 'strategy'} target?`} description="This creates an explicit strategy-attributed flat target. Any executable change still passes through allocation, sizing, risk, OMS, Gateway, ledger, and reconciliation." confirmLabel="Create flat target" pending={action.isPending} onClose={() => setFlattening(null)} onConfirm={(reason) => {if (flattening) action.mutate({id: flattening.id, name: 'flatten', reason})}} />
    <DeleteStrategyDialog open={Boolean(deleting)} strategyName={deleting?.name || ''} pending={deleteAction.isPending} onClose={() => setDeleting(null)} onConfirm={() => {if (deleting) deleteAction.mutate(deleting)}} />
  </div>
}
