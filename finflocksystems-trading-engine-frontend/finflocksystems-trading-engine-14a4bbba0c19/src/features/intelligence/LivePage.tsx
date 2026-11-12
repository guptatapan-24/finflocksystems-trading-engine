import {useMemo, useState} from 'react'
import {useMutation, useQuery} from '@tanstack/react-query'
import {ApiError, mutationOptions, request} from '../../api/client'
import {DataTable, EmptyState, ErrorState, PageHeader, Skeleton, StatusBadge, TerminalMetric, TerminalPanel, formatDateTime, formatNumber} from '../../components/ui'
import {IntelligenceTabs} from './IntelligenceTabs'
import {intelligenceQueries} from './queries'
import type {LiveAlertRow, LiveCommentary, OpenEpisodeRow} from './types'

const tone = (value: string | number | null | undefined) => (Number(value) > 0 ? 'positive-text' : Number(value) < 0 ? 'negative-text' : '')

function Sparkline({points}: {points: {t: string; mark_pnl: string | null}[]}) {
  const values = points.map((point) => Number(point.mark_pnl)).filter((value) => Number.isFinite(value))
  if (values.length < 2) return <small>Not enough points yet for a chart.</small>
  const min = Math.min(...values, 0)
  const max = Math.max(...values, 0)
  const span = max - min || 1
  const path = values.map((value, index) => `${(index / (values.length - 1)) * 300},${60 - ((value - min) / span) * 56 - 2}`).join(' ')
  const zero = 60 - ((0 - min) / span) * 56 - 2
  return <svg viewBox="0 0 300 60" role="img" aria-label="Open mark P&L over time" width="100%" height="80" preserveAspectRatio="none">
    <line x1="0" x2="300" y1={zero} y2={zero} stroke="currentColor" strokeOpacity="0.3" strokeDasharray="4 3" />
    <polyline fill="none" stroke="currentColor" strokeWidth="1.5" points={path} />
  </svg>
}

function AlertList({alerts}: {alerts: LiveAlertRow[]}) {
  if (!alerts.length) return <EmptyState title="No alerts" description="Alerts are templated rules (strike proximity, adverse excursion, stale prices, unfilled legs, manual orders). None have fired." />
  return <ul>{alerts.map((alert) => <li key={alert.id}><StatusBadge status={alert.severity} tone={alert.severity === 'HIGH' ? 'critical' : alert.severity === 'MEDIUM' ? 'warning' : 'neutral'} /> {alert.text} <small>({alert.rule_id.replaceAll('_', ' ').toLowerCase()}, seen {alert.occurrences}×, last {formatDateTime(alert.last_seen)})</small></li>)}</ul>
}

function LiveDetail({episodeId}: {episodeId: number}) {
  const live = useQuery(intelligenceQueries.live(episodeId))
  const [commentary, setCommentary] = useState<LiveCommentary | null>(null)
  const ask = useMutation({
    mutationFn: () => request<LiveCommentary>(`intelligence/episodes/${episodeId}/live-commentary/`, mutationOptions('POST', {}, true)),
    onSuccess: (data) => setCommentary(data),
  })
  if (live.isLoading) return <Skeleton lines={5} />
  if (live.isError) return <ErrorState error={live.error} compact />
  const data = live.data
  if (!data) return null
  const latest = data.latest
  const strikes = latest?.distances.short_strikes ?? []
  return <>
    <div className="metric-grid">
      <TerminalMetric label="Open mark P&L" value={<span className={tone(latest?.mark_pnl)}>{latest?.mark_pnl ? `${formatNumber(latest.mark_pnl, 2)} ${data.episode.currency}` : '—'}</span>} helper={latest ? `as of ${formatDateTime(latest.observed_at)}` : 'waiting for fresh prices'} trend={Number(latest?.mark_pnl) > 0 ? 'positive' : Number(latest?.mark_pnl) < 0 ? 'negative' : 'neutral'} />
      <TerminalMetric label="Of max profit / max loss" value={latest ? `${latest.pct_of_max_profit ?? '—'}% / ${latest.pct_of_max_loss ?? '—'}%` : '—'} helper="from entry-time model values" />
      <TerminalMetric label="Worst / best so far (bps)" value={latest ? `${latest.mae_bps_so_far ?? '—'} / ${latest.mfe_bps_so_far ?? '—'}` : '—'} helper="from live snapshots, not bar highs and lows" />
      <TerminalMetric label="Days to expiry" value={latest?.distances.dte !== undefined ? String(latest.distances.dte) : '—'} helper={latest?.distances.breakeven_lower ? `breakevens ${latest.distances.breakeven_lower} / ${latest.distances.breakeven_upper}` : undefined} />
    </div>
    <TerminalPanel id="intelligence-live-chart" title="Open mark P&L"><Sparkline points={data.series} /></TerminalPanel>
    {strikes.length > 0 && <TerminalPanel id="intelligence-live-strikes" title="Distance of the underlying to short strikes">
      <ul>{strikes.map((item) => <li key={item.symbol} className="mono">{item.right === 'C' ? 'Call' : 'Put'} {item.strike}: {item.distance_pct}% away</li>)}</ul>
    </TerminalPanel>}
    {latest && Object.keys(latest.distances).some((key) => ['sma_gap', 'rsi_distance_to_exit', 'distance_to_donchian_lower'].includes(key)) && <TerminalPanel id="intelligence-live-exit" title="Distance to the exit rule"><dl className="detail-list">
      {typeof latest.distances.sma_gap === 'number' && <div><dt>Fast − slow average</dt><dd className="mono">{String(latest.distances.sma_gap)}</dd></div>}
      {typeof latest.distances.rsi_distance_to_exit === 'number' && <div><dt>RSI distance to exit</dt><dd className="mono">{String(latest.distances.rsi_distance_to_exit)}</dd></div>}
      {typeof latest.distances.distance_to_donchian_lower === 'number' && <div><dt>Distance to lower channel</dt><dd className="mono">{String(latest.distances.distance_to_donchian_lower)}</dd></div>}
    </dl></TerminalPanel>}
    <TerminalPanel id="intelligence-live-alerts" title="Alerts"><AlertList alerts={data.alerts} /></TerminalPanel>
    <TerminalPanel id="intelligence-live-commentary" title="Model commentary (on demand)">
      <div className="row-actions"><button className="button-secondary" disabled={ask.isPending || !latest} onClick={() => ask.mutate()}>{ask.isPending ? 'Asking…' : 'Explain these numbers'}</button></div>
      {ask.isError && <ErrorState title="No commentary" error={ask.error instanceof ApiError ? new Error(ask.error.message) : ask.error} compact />}
      {commentary && <><p><strong>{commentary.label}</strong></p><p>{commentary.commentary}</p><ul>{commentary.watch_items.map((item) => <li key={item}>{item}</li>)}</ul></>}
    </TerminalPanel>
    {data.notes.map((note) => <small key={note}>{note}</small>)}
  </>
}

export function LivePage() {
  const open = useQuery(intelligenceQueries.openEpisodes())
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const rows = useMemo(() => open.data ?? [], [open.data])
  const activeId = selectedId ?? rows[0]?.id ?? null
  const columns = [
    {id: 'strategy', header: 'Strategy', cell: (row: OpenEpisodeRow) => <div className="primary-cell"><strong>{row.instance_name}</strong><span>{row.instrument} · {row.direction.replace('_', ' ')}</span></div>},
    {id: 'mode', header: 'Mode', cell: (row: OpenEpisodeRow) => <StatusBadge status={row.execution_mode} />},
    {id: 'opened', header: 'Opened', cell: (row: OpenEpisodeRow) => formatDateTime(row.opened_at)},
    {id: 'pnl', header: 'Open mark P&L', align: 'right' as const, className: 'mono', cell: (row: OpenEpisodeRow) => row.latest?.mark_pnl ? <span className={tone(row.latest.mark_pnl)}>{formatNumber(row.latest.mark_pnl, 2)} {row.currency}</span> : '—'},
    {id: 'alerts', header: 'Alerts', align: 'right' as const, cell: (row: OpenEpisodeRow) => row.alerts.total ? <StatusBadge status={`${row.alerts.total}${row.alerts.high ? ` · ${row.alerts.high} high` : ''}`} tone={row.alerts.high ? 'critical' : 'warning'} /> : '—'},
  ]
  return <>
    <PageHeader eyebrow="Intelligence" title="Live trades" description="Deterministic monitoring of open trades from stored prices. No model is involved in the numbers or the alerts, and nothing here places or changes an order." />
    <IntelligenceTabs />
    {open.isError && <ErrorState title="Could not load live trades" error={open.error} onRetry={() => void open.refetch()} />}
    {open.isLoading && <Skeleton lines={5} height={260} />}
    {open.isSuccess && <>
      <TerminalPanel id="intelligence-live-list" title="Open trades">
        <DataTable rows={rows} columns={columns} getRowKey={(row) => row.id} selectedRowKey={activeId} onRowSelect={(row) => setSelectedId(row.id)} caption="Open trades" emptyTitle="No open trades" emptyDescription="Trades appear here while a strategy holds a position." />
      </TerminalPanel>
      {activeId !== null && <LiveDetail key={activeId} episodeId={activeId} />}
    </>}
  </>
}
