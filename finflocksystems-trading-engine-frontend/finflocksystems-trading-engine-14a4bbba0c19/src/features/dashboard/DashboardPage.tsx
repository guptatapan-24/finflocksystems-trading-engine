import {useQuery} from '@tanstack/react-query'
import {Activity, AlertTriangle, Banknote, Bot, CircleDollarSign, Landmark, Scale, ShoppingCart} from 'lucide-react'
import {queries} from '../../api/queries'
import type {Order, Position} from '../../api/types'
import {ActivityTimeline} from '../../components/ActivityTimeline'
import {TerminalChart} from '../../components/charts/TerminalChart'
import {DataTable, ErrorState, Freshness, PageHeader, Skeleton, StatusBadge, TerminalMetric, TerminalPanel, formatMoney, formatNumber} from '../../components/ui'
import {useSelection} from '../../stores/useSelection'

export function DashboardPage() {
  const {portfolio, selectedPortfolioId} = useSelection()
  const summary = useQuery(queries.dashboard(selectedPortfolioId))
  const series = useQuery(queries.portfolioSeries(selectedPortfolioId))
  const positions = useQuery(queries.positions(selectedPortfolioId))
  const orders = useQuery(queries.orders({portfolioId: selectedPortfolioId}))

  if (summary.isLoading) return <><PageHeader title="Dashboard" description="A live operating view of the selected portfolio." /><Skeleton lines={6} height={420} /></>
  if (summary.isError && !summary.data) return <ErrorState title="Dashboard summary is unavailable" error={summary.error} onRetry={() => void summary.refetch()} />
  const data = summary.data
  if (!data) return null
  const activity = (data.recent_activity || []).map((item) => ({id: item.id, time: item.created_at, type: item.event_type, title: item.event_type.replaceAll('.', ' '), detail: `${item.actor} · ${item.aggregate_type} ${item.aggregate_id}`}))
  const attention = data.attention || []
  const exposure = series.data?.exposure || []
  const positionColumns = [
    {id: 'symbol', header: 'Symbol', cell: (item: Position) => <strong className="mono">{item.symbol}</strong>},
    {id: 'quantity', header: 'Qty', align: 'right' as const, className: 'mono', cell: (item: Position) => formatNumber(item.quantity)},
    {id: 'value', header: 'Value', align: 'right' as const, className: 'mono', cell: (item: Position) => formatMoney(item.market_value, item.currency)},
  ]
  const orderColumns = [
    {id: 'symbol', header: 'Symbol', cell: (item: Order) => <div className="primary-cell"><strong className="mono">{item.symbol}</strong><span>{item.side} · {item.order_type}</span></div>},
    {id: 'status', header: 'Status', cell: (item: Order) => <StatusBadge status={item.status} />},
    {id: 'quantity', header: 'Qty', align: 'right' as const, className: 'mono', cell: (item: Order) => formatNumber(item.quantity)},
  ]

  return <div className="page-stack">
    <PageHeader eyebrow="Portfolio command center" title={`Good overview${portfolio ? `, ${portfolio.name}` : ''}`} description="Monitor capital, execution, and the operating conditions that can block new risk." actions={<Freshness updatedAt={summary.dataUpdatedAt} stale={summary.isStale} fetching={summary.isFetching} onRefresh={() => void summary.refetch()} />} />
    {summary.isError && <ErrorState title="Some dashboard data may be stale" error={summary.error} onRetry={() => void summary.refetch()} compact />}
    <section className="operating-strip" aria-label="Operating status">
      <div><span>Portfolio mode</span><StatusBadge status={data.mode || 'UNKNOWN'} /></div>
      <div><span>IBKR</span><StatusBadge status={data.gateway?.connected ? 'CONNECTED' : 'DISCONNECTED'} /></div>
      <div><span>Reconciliation</span><StatusBadge status={data.reconciliation_status} /></div>
      <div><span>Portfolio</span><strong>{data.portfolio?.name || 'Not selected'}</strong></div>
    </section>
    <section className="metric-grid" aria-label="Portfolio metrics">
      <TerminalMetric label="Net asset value" value={formatMoney(data.nav, data.account?.base_currency)} icon={<Landmark />} helper="Broker-reported" />
      <TerminalMetric label="Available cash" value={formatMoney(data.cash, data.account?.base_currency)} icon={<Banknote />} helper="After broker updates" />
      <TerminalMetric label="Buying power" value={formatMoney(data.buying_power, data.account?.base_currency)} icon={<CircleDollarSign />} />
      <TerminalMetric label="Daily P&L" value={formatMoney(data.daily_pnl, data.account?.base_currency)} icon={<Activity />} trend={Number(data.daily_pnl) > 0 ? 'positive' : Number(data.daily_pnl) < 0 ? 'negative' : 'neutral'} />
      <TerminalMetric label="Gross exposure" value={formatMoney(data.gross_exposure, data.account?.base_currency)} icon={<Scale />} />
      <TerminalMetric label="Active strategies" value={formatNumber(data.active_strategies)} icon={<Bot />} />
      <TerminalMetric label="Open orders" value={formatNumber(data.open_orders)} icon={<ShoppingCart />} />
      <TerminalMetric label="Attention items" value={formatNumber(attention.length)} icon={<AlertTriangle />} trend={attention.some((item) => item.severity === 'CRITICAL') ? 'negative' : 'neutral'} />
    </section>
    <div className="dashboard-grid">
      <TerminalPanel id="nav-pnl" title="NAV & portfolio P&L" description={`Persisted portfolio observations · ${series.data?.source || 'waiting for data'}`} className="dashboard-chart">
        {series.isError ? <ErrorState error={series.error} onRetry={() => void series.refetch()} compact /> : series.isLoading ? <Skeleton height={270} /> : <TerminalChart id="dashboard-nav" height={330} ariaLabel="Portfolio NAV and P&L chart" defaultChartType="area" lines={[
          {name: 'NAV', data: series.data?.nav || [], type: 'area', kind: 'primary'},
          {name: 'P&L', data: series.data?.pnl || [], kind: 'primary'},
        ]} />}
      </TerminalPanel>
      <TerminalPanel id="attention" title="Needs attention" description="Conditions that may affect readiness" className="dashboard-attention">
        {attention.length ? <ul className="attention-list">{attention.map((item) => <li key={item.id}><StatusBadge status={item.severity} /><div><strong>{item.title}</strong><p>{item.detail}</p></div></li>)}</ul> : <div className="healthy-callout"><StatusBadge status="HEALTHY" /><div><strong>No immediate operator action</strong><p>Gateway, reconciliation, and recent risk checks show no active attention items.</p></div></div>}
      </TerminalPanel>
      <TerminalPanel id="recent-activity" title="Recent activity" description="Latest persisted audit events" className="dashboard-activity"><ActivityTimeline items={activity.slice(0, 8)} /></TerminalPanel>
      <TerminalPanel id="holdings" title="Holdings" description={`${positions.data?.length || 0} marked positions`} className="dashboard-holdings">{positions.isError ? <ErrorState error={positions.error} compact /> : <DataTable rows={positions.data || []} columns={positionColumns} getRowKey={(item) => item.id} emptyTitle="No holdings" />}</TerminalPanel>
      <TerminalPanel id="open-orders" title="Open orders" description="Current OMS state" className="dashboard-orders">{orders.isError ? <ErrorState error={orders.error} compact /> : <DataTable rows={(orders.data || []).filter((item) => !['FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED'].includes(item.status))} columns={orderColumns} getRowKey={(item) => item.internal_id} emptyTitle="No open orders" />}</TerminalPanel>
      <TerminalPanel id="exposure" title="Exposure" description="Gross and net marked exposure from real portfolio and market records" className="dashboard-exposure" defaultOpen={false}>
        {series.isError ? <ErrorState error={series.error} compact /> : <TerminalChart id="dashboard-exposure" height={270} ariaLabel="Portfolio exposure chart" lines={[
          {name: 'Gross', data: exposure.map((point) => ({time: point.time, value: point.gross})), kind: 'primary'},
          {name: 'Net', data: exposure.map((point) => ({time: point.time, value: point.net})), kind: 'primary'},
        ]} />}
      </TerminalPanel>
    </div>
  </div>
}
