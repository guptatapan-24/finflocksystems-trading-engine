import {useMemo, useState} from 'react'
import {useQuery} from '@tanstack/react-query'
import {DataTable, DetailDrawer, EmptyState, ErrorState, PageHeader, Skeleton, StatusBadge, TerminalMetric, TerminalPanel, formatDateTime, formatNumber} from '../../components/ui'
import {intelligenceQueries} from './queries'
import {Link} from 'react-router-dom'
import {IntelligenceTabs} from './IntelligenceTabs'
import {ReviewPanel} from './ReviewPanel'
import type {IntelligenceEpisode} from './types'

const FLAG_HELP: Record<string, string> = {
  PAPER_DELAYED_DATA: 'Paper trading on delayed market data: bar timestamps lag the fills.',
  OPTION_PATH_UNAVAILABLE: 'The option price path between entry and exit was not recorded.',
  MISSING_REMOTE_FEATURES: 'The remote feature database was off, so some scores use neutral values.',
  OPTION_RISK_BYPASS: 'Option pre-trade risk checks were disabled for this trade.',
  MANUAL_INTERVENTION: 'A manual or imported order touched the same contract.',
  ATTRIBUTION_DRIFT_POSSIBLE: 'An order was resized by risk, so attributed quantities may differ from the broker.',
  PNL_ESTIMATED_AT_EXPIRY: 'No closing fill existed; P&L is estimated from expiry intrinsic values.',
  EXPIRY_SETTLEMENT_UNKNOWN: 'Expired without a closing fill and no settlement price was available.',
  OPTION_SNAPSHOT_UNLINKED: 'The entry decision snapshot was matched by time, not by an explicit link.',
  PARTIAL_FILLS: 'An order filled in several pieces.',
  RECONCILIATION_BREAK_DURING_TRADE: 'A material reconciliation break occurred while the trade was open.',
  MIXED_CURRENCY: 'Fills were in more than one currency, so P&L is not comparable.',
}

function duration(seconds: number | null) {
  if (seconds === null) return 'open'
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`
  if (seconds < 172800) return `${(seconds / 3600).toFixed(1)} h`
  return `${(seconds / 86400).toFixed(1)} d`
}

const tone = (value: string | number) => Number(value) > 0 ? 'positive-text' : Number(value) < 0 ? 'negative-text' : ''

export function EpisodesPage() {
  const [status, setStatus] = useState('')
  const [currency, setCurrency] = useState('')
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const episodes = useQuery(intelligenceQueries.episodes({status, currency}))
  const detail = useQuery(intelligenceQueries.episode(selectedId))
  const rows = useMemo(() => episodes.data ?? [], [episodes.data])
  // Money is never added across currencies: one summary per currency, always with n.
  const summary = useMemo(() => {
    const byCurrency = new Map<string, {n: number; net: number; wins: number}>()
    rows.filter((row) => row.status !== 'OPEN').forEach((row) => {
      const entry = byCurrency.get(row.currency) ?? {n: 0, net: 0, wins: 0}
      entry.n += 1
      entry.net += Number(row.net_pnl)
      entry.wins += Number(row.net_pnl) > 0 ? 1 : 0
      byCurrency.set(row.currency, entry)
    })
    return [...byCurrency.entries()]
  }, [rows])
  const currencies = [...new Set(rows.map((row) => row.currency))].sort()

  const columns = [
    {id: 'strategy', header: 'Strategy', cell: (row: IntelligenceEpisode) => <div className="primary-cell"><strong>{row.instance_name}</strong><span>{row.strategy_key} · v{row.strategy_version}</span></div>},
    {id: 'instrument', header: 'Instrument', cell: (row: IntelligenceEpisode) => <div className="primary-cell mono"><strong>{row.instrument}</strong><span>{row.asset_family} · {row.direction.replace('_', ' ')}</span></div>},
    {id: 'mode', header: 'Mode', cell: (row: IntelligenceEpisode) => <StatusBadge status={row.execution_mode} />},
    {id: 'status', header: 'Status', cell: (row: IntelligenceEpisode) => <StatusBadge status={row.status} />},
    {id: 'opened', header: 'Opened', cell: (row: IntelligenceEpisode) => formatDateTime(row.opened_at)},
    {id: 'hold', header: 'Held', cell: (row: IntelligenceEpisode) => duration(row.hold_seconds)},
    {id: 'exit', header: 'Exit', cell: (row: IntelligenceEpisode) => row.exit_reason ? row.exit_reason.replaceAll('_', ' ') : '—'},
    {id: 'net', header: 'Net P&L', align: 'right' as const, className: 'mono', cell: (row: IntelligenceEpisode) => <span className={tone(row.net_pnl)}>{formatNumber(row.net_pnl, 2)} {row.currency}{row.pnl_basis !== 'FILLS' ? ' (est.)' : ''}{row.status === 'OPEN' ? ' (so far)' : ''}</span>},
    {id: 'flags', header: 'Flags', align: 'right' as const, cell: (row: IntelligenceEpisode) => row.data_quality_flags.length ? <span title={row.data_quality_flags.join(', ')}>{row.data_quality_flags.length}</span> : '—'},
  ]

  return <>
    <PageHeader eyebrow="Intelligence" title="Trade episodes" description="Every round trip of every strategy, rebuilt from attributed fills. Advisory only: nothing here places or changes a trade." />
    <IntelligenceTabs />
    {episodes.isError && <ErrorState title="Could not load episodes" error={episodes.error} onRetry={() => void episodes.refetch()} />}
    {episodes.isLoading && <Skeleton lines={6} height={320} />}
    {episodes.isSuccess && <>
      <TerminalPanel id="intelligence-episodes-summary" title="Closed-trade summary (per currency)">
        {summary.length === 0
          ? <EmptyState title="No closed episodes yet" description="Statistics appear after the first closed trade. One or two trades are not evidence of anything." />
          : <div className="metric-grid">{summary.map(([code, item]) => <TerminalMetric key={code} label={`Net P&L ${code}`} value={<span className={tone(item.net)}>{formatNumber(item.net, 2)}</span>} helper={`n = ${item.n} · ${item.wins} winning · net of costs`} trend={item.net > 0 ? 'positive' : item.net < 0 ? 'negative' : 'neutral'} />)}</div>}
      </TerminalPanel>
      <TerminalPanel id="intelligence-episodes-list" title="Episodes" actions={<div className="row-actions">
        <select aria-label="Status filter" value={status} onChange={(event) => setStatus(event.target.value)}><option value="">All statuses</option><option value="OPEN">Open</option><option value="CLOSED">Closed</option><option value="EXPIRED">Expired</option></select>
        <select aria-label="Currency filter" value={currency} onChange={(event) => setCurrency(event.target.value)}><option value="">All currencies</option>{currencies.map((code) => <option key={code} value={code}>{code}</option>)}</select>
      </div>}>
        <DataTable rows={rows} columns={columns} getRowKey={(row) => row.id} selectedRowKey={selectedId} onRowSelect={(row) => setSelectedId(row.id)} caption="Trade episodes" emptyTitle="No trade episodes" emptyDescription="Episodes appear once a strategy has attributed fills and the intelligence layer is enabled." />
      </TerminalPanel>
    </>}
    <DetailDrawer open={selectedId !== null} title="Episode details" subtitle={detail.data ? `${detail.data.instance_name} · ${detail.data.instrument}` : undefined} onClose={() => setSelectedId(null)}>
      {detail.isLoading && <Skeleton lines={5} />}
      {detail.isError && <ErrorState error={detail.error} compact />}
      {detail.data && <>
        <dl className="detail-list">
          <div><dt>Status</dt><dd><StatusBadge status={detail.data.status} /></dd></div>
          <div><dt>Gross P&amp;L</dt><dd className="mono">{formatNumber(detail.data.gross_pnl, 2)} {detail.data.currency}</dd></div>
          <div><dt>Commissions</dt><dd className="mono">{formatNumber(detail.data.commissions, 2)} {detail.data.currency}</dd></div>
          <div><dt>Net P&amp;L</dt><dd className={`mono ${tone(detail.data.net_pnl)}`}>{formatNumber(detail.data.net_pnl, 2)} {detail.data.currency}</dd></div>
          <div><dt>P&amp;L basis</dt><dd>{detail.data.pnl_basis === 'FILLS' ? 'Fills' : 'Estimated at expiry'}</dd></div>
          <div><dt>Exit</dt><dd>{detail.data.exit_reason ? `${detail.data.exit_reason.replaceAll('_', ' ')}${detail.data.exit_subtype ? ` · ${detail.data.exit_subtype}` : ''}` : '—'}</dd></div>
          <div><dt>Evidence frozen</dt><dd className="mono">{detail.data.evidence_hash ? detail.data.evidence_hash.slice(0, 12) : 'not yet (trade open)'}</dd></div>
        </dl>
        <p><Link className="button-secondary" to={`/intelligence/episodes/${detail.data.id}/report`}>Open the full report</Link></p>
        {detail.data.data_quality_flags.length > 0 && <TerminalPanel id="intelligence-episode-flags" title="Data-quality flags"><ul>{detail.data.data_quality_flags.map((flag) => <li key={flag}><StatusBadge status={flag} tone="warning" /> <span>{FLAG_HELP[flag] ?? ''}</span></li>)}</ul></TerminalPanel>}
        <TerminalPanel id="intelligence-episode-legs" title="Legs">
          <DataTable rows={detail.data.legs} getRowKey={(leg) => leg.symbol} caption="Episode legs" columns={[
            {id: 'symbol', header: 'Contract', cell: (leg) => <span className="mono">{leg.symbol}</span>},
            {id: 'entry', header: 'Entry', cell: (leg) => `${leg.entry_side} ${formatNumber(leg.entry_quantity)} @ ${formatNumber(leg.entry_avg_price, 4)}`},
            {id: 'exit', header: 'Exit', cell: (leg) => Number(leg.exit_quantity) ? `${formatNumber(leg.exit_quantity)} @ ${formatNumber(leg.exit_avg_price, 4)}` : '—'},
            {id: 'slip', header: 'Slippage bps (in / out)', align: 'right' as const, className: 'mono', cell: (leg) => `${leg.entry_slippage_bps ?? '—'} / ${leg.exit_slippage_bps ?? '—'}`},
            {id: 'pnl', header: 'Realised', align: 'right' as const, className: 'mono', cell: (leg) => formatNumber(leg.realized_pnl, 2)},
          ]} />
        </TerminalPanel>
        {detail.data.status !== 'OPEN' && <ReviewPanel episodeId={detail.data.id} />}
        <TerminalPanel id="intelligence-episode-metrics" title={`Deterministic metrics (v${detail.data.metrics_version ?? '?'})`} defaultOpen={false}>
          <table className="data-table"><tbody>{Object.entries(detail.data.metrics).filter(([key]) => key !== 'flags').sort(([a], [b]) => a.localeCompare(b)).map(([key, value]) => <tr key={key}><td className="mono">{key}</td><td className="mono" style={{textAlign: 'right'}}>{String(value)}</td></tr>)}</tbody></table>
        </TerminalPanel>
      </>}
    </DetailDrawer>
  </>
}
