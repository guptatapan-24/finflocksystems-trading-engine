import {useMemo, useState} from 'react'
import Chip from '@mui/material/Chip'
import ToggleButton from '@mui/material/ToggleButton'
import ToggleButtonGroup from '@mui/material/ToggleButtonGroup'
import {DataGrid, type GridColDef} from '@mui/x-data-grid'
import type {UseQueryResult} from '@tanstack/react-query'
import type {DecimalValue, StrategyLedgerDecision, StrategyLedgerRow} from '../../api/types'
import {DataTable, DetailDrawer, EmptyState, ErrorState, Skeleton, StatusBadge, TerminalPanel, formatDateTime, formatNumber, formatPercent} from '../../components/ui'
import {TerminalMuiProvider, terminalGridSx} from '../../components/ui/muiTheme'

type ChipColor = 'success' | 'error' | 'warning' | 'info' | 'default'

export const DECISION_LABELS: Record<StrategyLedgerDecision, {label: string; color: ChipColor}> = {
  ENTERED: {label: 'Trade entered', color: 'success'},
  EXITED: {label: 'Trade exited', color: 'error'},
  HOLDING: {label: 'Holding', color: 'info'},
  ORDERS_WORKING: {label: 'Orders working', color: 'info'},
  TARGET_SET: {label: 'Target set', color: 'info'},
  NO_TRADE: {label: 'No trade', color: 'default'},
  AWAITING_QUOTES: {label: 'Awaiting quotes', color: 'warning'},
  NOT_EVALUATED: {label: 'Not evaluated', color: 'warning'},
  ERROR: {label: 'Error', color: 'warning'},
}

const TRADE_DECISIONS = new Set<StrategyLedgerDecision>(['ENTERED', 'EXITED', 'TARGET_SET'])
const PROBLEM_DECISIONS = new Set<StrategyLedgerDecision>(['ERROR', 'NOT_EVALUATED', 'AWAITING_QUOTES'])
type LedgerFilter = 'all' | 'trades' | 'no-trade' | 'problems'

function numeric(value: DecimalValue | null | undefined) {
  if (value === null || value === undefined || value === '') return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

export function DecisionChip({decision}: {decision: StrategyLedgerDecision}) {
  const config = DECISION_LABELS[decision] || {label: decision, color: 'default' as const}
  return <Chip size="small" label={config.label} color={config.color} variant={config.color === 'default' ? 'outlined' : 'filled'} sx={{fontWeight: 650, fontSize: 12}} />
}

/** A score against its floor: green when it cleared, red when it did not. */
export function ScoreCell({value, threshold, passed}: {value: DecimalValue | null; threshold: DecimalValue | null; passed: boolean | null}) {
  if (numeric(value) === null) return <span className="ledger-muted">—</span>
  const tone = passed === true ? 'ledger-pass' : passed === false ? 'ledger-fail' : ''
  return <span className="ledger-score">
    <strong className={`mono ${tone}`}>{formatNumber(value, 2)}</strong>
    {numeric(threshold) !== null && <small>{passed ? '≥' : '<'} {formatNumber(threshold, 2)}</small>}
  </span>
}

const compactTime = new Intl.DateTimeFormat(undefined, {day: '2-digit', month: 'short', year: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false})

export function formatBarTime(value: string | null) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : compactTime.format(date)
}

function bandPosition(row: StrategyLedgerRow) {
  const spot = numeric(row.spot), support = numeric(row.atr_support), resistance = numeric(row.atr_resistance)
  if (spot === null || support === null || resistance === null) return null
  if (spot > resistance) return 'above band'
  if (spot < support) return 'below band'
  return 'inside band'
}

function orderSummary(row: StrategyLedgerRow) {
  if (!row.orders.length) return TRADE_DECISIONS.has(row.decision) ? 'No order yet' : '—'
  const counts = new Map<string, number>()
  row.orders.forEach((order) => counts.set(order.status, (counts.get(order.status) || 0) + 1))
  return [...counts.entries()].map(([status, count]) => `${count} ${status.replaceAll('_', ' ').toLowerCase()}`).join(' · ')
}

export function filterLedger(rows: StrategyLedgerRow[], filter: LedgerFilter) {
  if (filter === 'trades') return rows.filter((row) => TRADE_DECISIONS.has(row.decision))
  if (filter === 'no-trade') return rows.filter((row) => row.decision === 'NO_TRADE')
  if (filter === 'problems') return rows.filter((row) => PROBLEM_DECISIONS.has(row.decision))
  return rows
}

const columns: GridColDef<StrategyLedgerRow>[] = [
  {
    field: 'time', headerName: 'Bar', width: 142, type: 'dateTime',
    valueGetter: (value) => (value ? new Date(value as string) : null),
    renderCell: ({row}) => <span className="mono ledger-time">{formatBarTime(row.time)}</span>,
  },
  {
    field: 'decision', headerName: 'Decision', width: 165, type: 'singleSelect',
    valueOptions: Object.entries(DECISION_LABELS).map(([value, item]) => ({value, label: item.label})),
    renderCell: ({row}) => <span className="ledger-score">
      <DecisionChip decision={row.decision} />
      {row.selected_strategy ? <small>{row.selected_strategy.replaceAll('_', ' ')}</small>
        : row.gate && !TRADE_DECISIONS.has(row.decision) ? <small>stopped at {row.gate}</small> : null}
    </span>,
  },
  {
    field: 'rps', headerName: 'RPS', width: 86, type: 'number', headerAlign: 'left', align: 'left',
    valueGetter: (value) => numeric(value as DecimalValue),
    renderCell: ({row}) => <ScoreCell value={row.rps} threshold={row.rps_threshold} passed={row.rps_passed} />,
  },
  {
    field: 'iv_score', headerName: 'IV score', width: 86, type: 'number', headerAlign: 'left', align: 'left',
    valueGetter: (value) => numeric(value as DecimalValue),
    renderCell: ({row}) => <ScoreCell value={row.iv_score} threshold={row.iv_score_threshold} passed={row.iv_passed} />,
  },
  {
    field: 'tqs', headerName: 'TQS', width: 86, type: 'number', headerAlign: 'left', align: 'left',
    valueGetter: (value) => numeric(value as DecimalValue),
    renderCell: ({row}) => <ScoreCell value={row.tqs} threshold={row.tqs_threshold} passed={row.tqs_passed} />,
  },
  {
    field: 'spot', headerName: 'Spot', width: 92, type: 'number', headerAlign: 'left', align: 'left',
    valueGetter: (value, row) => numeric((value as DecimalValue) ?? row.bar_close),
    renderCell: ({row}) => <span className="mono">{formatNumber(row.spot ?? row.bar_close, 2)}</span>,
  },
  {
    field: 'atr_band', headerName: 'ATR band', width: 132, sortable: false,
    valueGetter: (_value, row) => (numeric(row.atr_support) === null ? '' : `${row.atr_support} – ${row.atr_resistance}`),
    renderCell: ({row}) => numeric(row.atr_support) === null ? <span className="ledger-muted">—</span> : <span className="ledger-score">
      <span className="mono">{formatNumber(row.atr_support, 0)} – {formatNumber(row.atr_resistance, 0)}</span>
      {bandPosition(row) && <small>spot {bandPosition(row)}</small>}
    </span>,
  },
  {
    field: 'reason', headerName: 'Why', flex: 1, minWidth: 300, sortable: false,
    renderCell: ({row}) => <div className="ledger-reason">
      <span>{row.reason}</span>
      {row.reason_details.slice(0, 2).map((detail) => <small key={detail}>{detail}</small>)}
      {row.reason_details.length > 2 && <small className="ledger-more">+{row.reason_details.length - 2} more — open row for all</small>}
    </div>,
  },
  {
    field: 'orders', headerName: 'Orders', width: 118, sortable: false,
    valueGetter: (_value, row) => orderSummary(row),
    renderCell: ({value}) => <small className="ledger-orders">{value as string}</small>,
  },
]

function LedgerSummary({rows}: {rows: StrategyLedgerRow[]}) {
  const count = (decision: StrategyLedgerDecision) => rows.filter((row) => row.decision === decision).length
  return <div className="ledger-summary" aria-label="Ledger summary">
    <span><strong>{rows.length}</strong> bars</span>
    <span className="ledger-pass"><strong>{count('ENTERED')}</strong> entries</span>
    <span className="ledger-fail"><strong>{count('EXITED')}</strong> exits</span>
    <span><strong>{count('NO_TRADE')}</strong> no trade</span>
    {rows.some((row) => PROBLEM_DECISIONS.has(row.decision)) && <span className="ledger-warn"><strong>{rows.filter((row) => PROBLEM_DECISIONS.has(row.decision)).length}</strong> not evaluated / errors</span>}
  </div>
}

export function StrategyLedgerGrid({rows, pageSize = 25}: {rows: StrategyLedgerRow[]; pageSize?: number}) {
  const [filter, setFilter] = useState<LedgerFilter>('all')
  const [selected, setSelected] = useState<StrategyLedgerRow | null>(null)
  const visible = useMemo(() => filterLedger(rows, filter), [rows, filter])
  return <TerminalMuiProvider>
    <div className="ledger-toolbar">
      <LedgerSummary rows={rows} />
      <ToggleButtonGroup size="small" exclusive value={filter} onChange={(_event, value: LedgerFilter | null) => value && setFilter(value)} aria-label="Filter ledger rows">
        <ToggleButton value="all">All bars</ToggleButton>
        <ToggleButton value="trades">Trades</ToggleButton>
        <ToggleButton value="no-trade">No trade</ToggleButton>
        <ToggleButton value="problems">Errors</ToggleButton>
      </ToggleButtonGroup>
    </div>
    <DataGrid
      rows={visible}
      columns={columns}
      density="compact"
      autoHeight
      getRowHeight={() => 'auto'}
      disableVirtualization
      disableRowSelectionOnClick
      showToolbar
      label="Strategy decision ledger"
      slotProps={{toolbar: {showQuickFilter: true, csvOptions: {fileName: 'strategy-ledger'}, printOptions: {disableToolbarButton: true}}}}
      initialState={{
        sorting: {sortModel: [{field: 'time', sort: 'desc'}]},
        pagination: {paginationModel: {pageSize}},
      }}
      pageSizeOptions={[10, 25, 50, 100]}
      getRowClassName={({row}) => row.decision === 'ENTERED' ? 'grid-row-entered' : row.decision === 'EXITED' ? 'grid-row-exited' : PROBLEM_DECISIONS.has(row.decision) ? 'grid-row-error' : ''}
      onRowClick={({row}) => setSelected(row)}
      localeText={{noRowsLabel: filter === 'all' ? 'No evaluated bars yet' : 'No bars match this filter'}}
      sx={terminalGridSx}
    />
    <LedgerRowDrawer row={selected} onClose={() => setSelected(null)} />
  </TerminalMuiProvider>
}

function LedgerRowDrawer({row, onClose}: {row: StrategyLedgerRow | null; onClose: () => void}) {
  if (!row) return null
  return <DetailDrawer open title={DECISION_LABELS[row.decision]?.label || row.decision} subtitle={`Bar ${formatDateTime(row.time)}${row.run_id ? ` · run ${row.run_id}` : ''}`} onClose={onClose}>
    <section className="ledger-drawer-reason">
      <DecisionChip decision={row.decision} />
      <p>{row.reason}</p>
      {row.reason_details.length > 0 && <ul>{row.reason_details.map((detail) => <li key={detail}>{detail}</li>)}</ul>}
    </section>
    <dl className="detail-list columns">
      <div><dt>RPS</dt><dd><ScoreCell value={row.rps} threshold={row.rps_threshold} passed={row.rps_passed} /></dd></div>
      <div><dt>IV score</dt><dd><ScoreCell value={row.iv_score} threshold={row.iv_score_threshold} passed={row.iv_passed} /></dd></div>
      <div><dt>TQS</dt><dd><ScoreCell value={row.tqs} threshold={row.tqs_threshold} passed={row.tqs_passed} /></dd></div>
      <div><dt>IV rank / percentile</dt><dd className="mono">{formatNumber(row.iv_rank, 2)} / {formatNumber(row.iv_percentile, 2)}</dd></div>
      <div><dt>Spot</dt><dd className="mono">{formatNumber(row.spot ?? row.bar_close, 2)}</dd></div>
      <div><dt>ATR band</dt><dd className="mono">{formatNumber(row.atr_support, 2)} – {formatNumber(row.atr_resistance, 2)}</dd></div>
      <div><dt>OI walls</dt><dd className="mono">{formatNumber(row.oi_support, 2)} – {formatNumber(row.oi_resistance, 2)}</dd></div>
      <div><dt>Gate status</dt><dd><StatusBadge status={row.status || 'UNKNOWN'} /></dd></div>
    </dl>
    {row.candidates.length > 0 && <>
      <h4 className="ledger-drawer-heading">Candidate structures</h4>
      <DataTable
        rows={row.candidates}
        getRowKey={(item) => item.strategy_key || JSON.stringify(item)}
        columns={[
          {id: 'key', header: 'Structure', cell: (item) => <strong>{item.strategy_key?.replaceAll('_', ' ') || '—'}</strong>},
          {id: 'outcome', header: 'Outcome', cell: (item) => <StatusBadge status={item.eligible ? 'ELIGIBLE' : 'REFUSED'} />},
          {id: 'iv', header: 'IV score', align: 'right', cell: (item) => formatNumber(item.iv_score, 2)},
          {id: 'tqs', header: 'TQS', align: 'right', cell: (item) => formatNumber(item.tqs, 2)},
          {id: 'pop', header: 'POP', align: 'right', cell: (item) => item.pop == null ? '—' : formatPercent(item.pop)},
          {id: 'ev', header: 'Expected P&L', align: 'right', cell: (item) => formatNumber(item.expected_pnl, 2)},
          {id: 'reason', header: 'Reason', cell: (item) => <small>{item.rejection_reason || '—'}</small>},
        ]}
      />
    </>}
    {row.orders.length > 0 && <>
      <h4 className="ledger-drawer-heading">Orders placed</h4>
      <DataTable
        rows={row.orders}
        getRowKey={(order) => order.id}
        columns={[
          {id: 'symbol', header: 'Contract', cell: (order) => <strong>{order.symbol}</strong>},
          {id: 'side', header: 'Side', cell: (order) => <StatusBadge status={order.side} />},
          {id: 'qty', header: 'Filled / qty', align: 'right', cell: (order) => <span className="mono">{formatNumber(order.filled_quantity)} / {formatNumber(order.quantity)}</span>},
          {id: 'price', header: 'Avg fill', align: 'right', cell: (order) => <span className="mono">{formatNumber(order.average_fill_price, 2)}</span>},
          {id: 'status', header: 'Status', cell: (order) => <StatusBadge status={order.status} />},
        ]}
      />
    </>}
  </DetailDrawer>
}

export function StrategyLedgerPanel({query, pageSize, title = 'Decision ledger'}: {query: UseQueryResult<StrategyLedgerRow[], Error>; pageSize?: number; title?: string}) {
  const description = 'Every evaluated bar: RPS, IV score and TQS against their floors, the ATR band, and exactly why a trade was or was not placed'
  if (query.isLoading) return <TerminalPanel id="strategy-ledger" title={title} description={description}><Skeleton lines={6} /></TerminalPanel>
  if (query.isError) return <ErrorState title="Ledger is unavailable" error={query.error} onRetry={() => void query.refetch()} />
  const rows = Array.isArray(query.data) ? query.data : []
  return <TerminalPanel id="strategy-ledger" title={title} description={description} className="detail-wide">
    {rows.length ? <StrategyLedgerGrid rows={rows} pageSize={pageSize} /> : <EmptyState title="No evaluated bars yet" description="A ledger row is written for every final bar the strategy evaluates." />}
  </TerminalPanel>
}
