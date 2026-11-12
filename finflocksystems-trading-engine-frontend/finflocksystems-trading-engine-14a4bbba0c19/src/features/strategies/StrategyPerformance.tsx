import {DataGrid, type GridColDef} from '@mui/x-data-grid'
import {lazy, Suspense} from 'react'
import type {UseQueryResult} from '@tanstack/react-query'
import type {DecimalValue, StrategyChartData, StrategyLedgerRow, StrategyPnl, StrategyPnlLeg} from '../../api/types'
import {TerminalChart, type ChartLine} from '../../components/charts/TerminalChart'
import {EmptyState, ErrorState, Skeleton, StatusBadge, TerminalPanel, formatDateTime, formatMoney, formatNumber} from '../../components/ui'
import {TerminalMuiProvider, terminalGridSx} from '../../components/ui/muiTheme'
import {priceTraces} from './strategyPriceChart'

const PlotlyChart = lazy(() => import('../../components/charts/PlotlyChart'))

function signed(value: DecimalValue | undefined) {
  const number = Number(value ?? 0)
  return number > 0 ? 'positive' : number < 0 ? 'negative' : 'neutral'
}

function Money({value, currency}: {value: DecimalValue | undefined; currency: string}) {
  const number = Number(value ?? 0)
  return <span className={`pnl-${signed(value)}`}>{number > 0 ? '+' : ''}{formatMoney(value, currency)}</span>
}

function isPnl(value: unknown): value is StrategyPnl {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value) && 'running_pnl' in (value as object)
}

/** Always-visible headline: this strategy's running P&L, not the account's. */
export function RunningPnlStrip({query}: {query: UseQueryResult<StrategyPnl, Error>}) {
  if (query.isLoading) return <div className="running-pnl-strip" aria-busy="true"><span>Running P&L</span><strong>…</strong></div>
  if (query.isError || !isPnl(query.data)) return <div className="running-pnl-strip"><span>Running P&L</span><strong className="pnl-neutral">Unavailable</strong></div>
  const pnl = query.data
  return <div className="running-pnl-strip" aria-label="Strategy running P&L">
    <div><span>Running P&L</span><strong><Money value={pnl.running_pnl} currency={pnl.currency} /></strong></div>
    <div><span>Realized (net)</span><b><Money value={pnl.realized_pnl} currency={pnl.currency} /></b></div>
    <div><span>Unrealized</span><b><Money value={pnl.unrealized_pnl} currency={pnl.currency} /></b></div>
    <div><span>Position</span><b>{pnl.open_position ? 'Open' : 'Flat'}</b></div>
    <small>Strategy-attributed fills only · updated {formatDateTime(pnl.as_of)}</small>
  </div>
}

const legColumns = (currency: string): GridColDef<StrategyPnlLeg>[] => [
  {field: 'symbol', headerName: 'Leg', flex: 1, minWidth: 160, renderCell: ({row}) => <strong>{row.symbol}</strong>},
  {field: 'quantity', headerName: 'Qty', width: 80, type: 'number', valueGetter: (value) => Number(value), renderCell: ({row}) => <span className="mono">{formatNumber(row.quantity)}</span>},
  {field: 'average_price', headerName: 'Avg price', width: 105, type: 'number', valueGetter: (value) => Number(value), renderCell: ({row}) => <span className="mono">{row.open ? formatNumber(row.average_price, 2) : '—'}</span>},
  {field: 'mark_price', headerName: 'Mark', width: 150, type: 'number', valueGetter: (value) => (value == null ? null : Number(value)), renderCell: ({row}) => <span className="ledger-score"><span className="mono">{formatNumber(row.mark_price, 2)}</span>{row.open && <small>{row.mark_fresh ? 'fresh' : 'not fresh'} · {(row.mark_source || '—').replaceAll('_', ' ').toLowerCase()}</small>}</span>},
  {field: 'unrealized_pnl', headerName: 'Unrealized', width: 130, type: 'number', valueGetter: (value) => Number(value), renderCell: ({row}) => <Money value={row.unrealized_pnl} currency={currency} />},
  {field: 'realized_pnl', headerName: 'Realized (net)', width: 130, type: 'number', valueGetter: (value) => Number(value), renderCell: ({row}) => <Money value={row.realized_pnl} currency={currency} />},
]

export function StrategyPnlPanel({query}: {query: UseQueryResult<StrategyPnl, Error>}) {
  const description = 'Realized plus open mark-to-market on the quantity attributed to this strategy, net of its share of commissions'
  if (query.isLoading) return <TerminalPanel id="strategy-pnl" title="Running P&L" description={description} className="detail-wide"><Skeleton lines={3} /></TerminalPanel>
  if (query.isError) return <ErrorState title="Strategy P&L is unavailable" error={query.error} onRetry={() => void query.refetch()} />
  if (!isPnl(query.data)) return null
  const pnl = query.data
  const curve = pnlCurve(pnl)
  const markBadge = !pnl.open_position ? null : pnl.marks_complete && pnl.marks_fresh
    ? <StatusBadge status="MARKS FRESH" tone="positive" />
    : <StatusBadge status={pnl.marks_complete ? 'MARKS NOT FRESH' : 'MARKED AT LAST FILL'} tone="warning" />
  return <TerminalPanel id="strategy-pnl" title="Running P&L" description={description} className="detail-wide" badge={markBadge}>
    <div className="pnl-grid">
      <article className="pnl-card pnl-card-primary"><span>Running P&L</span><strong><Money value={pnl.running_pnl} currency={pnl.currency} /></strong><small>{pnl.open_position ? 'Position open' : 'Flat'} · {pnl.fill_count} fills</small></article>
      <article className="pnl-card"><span>Realized (net of fees)</span><strong><Money value={pnl.realized_pnl} currency={pnl.currency} /></strong><small>Gross {formatMoney(pnl.gross_realized_pnl, pnl.currency)}</small></article>
      <article className="pnl-card"><span>Unrealized</span><strong><Money value={pnl.unrealized_pnl} currency={pnl.currency} /></strong><small>Open legs marked to market</small></article>
      <article className="pnl-card"><span>Commissions</span><strong className="pnl-neutral">{formatMoney(pnl.commissions, pnl.currency)}</strong><small>This strategy's share</small></article>
    </div>
    {pnl.fill_count === 0 ? <EmptyState title="No fills yet" description="Running P&L starts with the first fill attributed to this strategy." /> : <>
      {curve.length > 0 && <TerminalChart id="strategy-pnl-curve" height={200} lines={curve} ariaLabel="Strategy running P&L over time" />}
      <TerminalMuiProvider>
        <DataGrid rows={pnl.legs} columns={legColumns(pnl.currency)} getRowId={(row) => row.instrument_id} density="compact" autoHeight hideFooter disableVirtualization disableRowSelectionOnClick label="Strategy legs P&L" sx={{...terminalGridSx, mt: 1.5}} />
      </TerminalMuiProvider>
    </>}
  </TerminalPanel>
}

/** The curve ends on the headline figure: the running P&L at `as_of`, on
 * current marks, so the chart and the cards never disagree. */
export function pnlCurve(pnl: StrategyPnl): ChartLine[] {
  const data = pnl.series.map((point) => ({time: point.time, value: Number(point.total)}))
  const last = data.at(-1)
  const asOf = new Date(pnl.as_of).getTime()
  if (last && Number.isFinite(asOf)) {
    const lastAt = new Date(last.time).getTime()
    const end = {time: new Date(Math.max(asOf, lastAt)).toISOString(), value: Number(pnl.running_pnl)}
    // The chart keys points by whole second; replace rather than duplicate.
    if (Math.floor(asOf / 1000) <= Math.floor(lastAt / 1000)) data[data.length - 1] = end
    else data.push(end)
  }
  return data.length > 1 ? [{name: 'Running P&L', type: 'area', data}] : []
}

export function StrategyTradeChart({query, ledger = [], symbol}: {query: UseQueryResult<StrategyChartData, Error>; ledger?: StrategyLedgerRow[]; symbol?: string}) {
  if (query.isLoading) return <TerminalPanel id="strategy-chart-loading" collapsible={false}><Skeleton height={420} /></TerminalPanel>
  if (query.isError || !query.data) return <ErrorState title="Strategy chart is unavailable" error={query.error} onRetry={() => void query.refetch()} />
  const data = query.data
  const band = data.atr_bands?.at(-1)
  const lastClose = data.bars.length ? Number(data.bars.at(-1)!.close) : undefined
  const position = band && lastClose !== undefined
    ? lastClose > Number(band.resistance) ? 'above the band' : lastClose < Number(band.support) ? 'below the band' : 'inside the band'
    : null
  const summary = band
    ? `Latest band ${formatNumber(band.support, 2)} – ${formatNumber(band.resistance, 2)}${position ? ` · close ${formatNumber(lastClose, 2)} ${position}` : ''}`
    : 'No ATR boundary measured yet'
  return <TerminalPanel id="strategy-chart" title="Underlying candles · daily ATR range · OI confirmation · trades" description={`${summary}. Source: ${data.source}.`} badge={<span className="chart-pill">PRIOR DAILY INPUTS</span>} className="detail-wide">
    {data.bars.length === 0
      ? <EmptyState title="No bars yet" description="The chart fills in from persisted final bars of this strategy's timeframe." />
      : <div role="img" aria-label="Strategy price chart with ATR boundary, entry and exit points">
        <Suspense fallback={<Skeleton height={390} />}>
          <PlotlyChart data={priceTraces(data, ledger)} layout={{
            xaxis: {title: {text: 'Timestamp'}, rangeslider: {visible: false}},
            yaxis: {title: {text: `${symbol || 'Underlying'} price`}},
            hovermode: 'x unified',
          }} />
        </Suspense>
      </div>}
  </TerminalPanel>
}
