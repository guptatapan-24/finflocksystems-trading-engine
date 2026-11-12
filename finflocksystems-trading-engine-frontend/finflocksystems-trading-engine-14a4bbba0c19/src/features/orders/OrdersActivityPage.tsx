import {useEffect, useMemo, useRef, useState} from 'react'
import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query'
import {ExternalLink, Filter, Search, X} from 'lucide-react'
import {ApiError, mutationOptions, request} from '../../api/client'
import {queries} from '../../api/queries'
import type {Execution, ManualOrderIntentStatus, ManualOrderQuoteStatus, Order, OrderDetail, OrderStatusHistory} from '../../api/types'
import {ActivityTimeline} from '../../components/ActivityTimeline'
import {BrokerInstrumentSearch} from '../../components/BrokerInstrumentSearch'
import {FillProgress} from '../../components/FillProgress'
import {ConfirmActionDialog, DataTable, DetailDrawer, EmptyState, ErrorState, Freshness, PageHeader, Skeleton, StatusBadge, TerminalPanel, formatDateTime, formatMoney, formatNumber} from '../../components/ui'
import {useSelection} from '../../stores/useSelection'
import {ManualOrderTicket} from './ManualOrderTicket'
import {isManualIntentTerminal, type ManualOrderPayload} from './manualOrder'
import {canCancelOrder, canModifyOrder} from './orderEligibility'

export function OrdersActivityPage() {
  const queryClient = useQueryClient()
  const selection = useSelection()
  const {selectedPortfolioId, session, account, portfolio} = selection
  const [status, setStatus] = useState('')
  const [symbol, setSymbol] = useState('')
  const [search, setSearch] = useState('')
  const [selectedOrder, setSelectedOrder] = useState<Order | null>(null)
  const [cancelOrder, setCancelOrder] = useState<Order | null>(null)
  const [manualResult, setManualResult] = useState<ManualOrderIntentStatus>()
  const [manualInstrumentId, setManualInstrumentId] = useState<number | null>(null)
  const [contractQuery, setContractQuery] = useState('')
  const [activeIntent, setActiveIntent] = useState<{intentId: number; startedAt: number} | null>(null)
  const [pollTimedOut, setPollTimedOut] = useState(false)
  const manualSubmissionInFlight = useRef(false)
  const surfacedManualOrder = useRef('')
  const manualQuoteLeaseKey = useMemo(
    () => `manual-ticket-${crypto.randomUUID()}`,
    [selectedPortfolioId, manualInstrumentId],
  )
  const desktopInspector = useDesktopInspector()
  const orders = useQuery(queries.orders({portfolioId: selectedPortfolioId, status, symbol}))
  const executions = useQuery(queries.executions({portfolioId: selectedPortfolioId, symbol}))
  const audit = useQuery(queries.audit({limit: 100}))
  const instruments = useQuery(queries.instruments())
  const positions = useQuery(queries.positions(selectedPortfolioId))
  const system = useQuery(queries.system())
  const orderDetail = useQuery(queries.orderDetail(selectedOrder?.internal_id || ''))
  const manualIntentStatus = useQuery({
    ...queries.manualOrderIntent(activeIntent?.intentId),
    refetchInterval: activeIntent && !pollTimedOut ? 1_000 : false,
    refetchOnWindowFocus: false,
  })
  const manualQuote = useQuery({
    queryKey: ['manual-order-quote', selectedPortfolioId ?? 'none', manualInstrumentId ?? 'none'],
    queryFn: ({signal}) => request<ManualOrderQuoteStatus>('orders/manual-quote/', {
      ...mutationOptions('POST', {
        portfolio_id: selectedPortfolioId,
        instrument_id: manualInstrumentId,
        lease_key: manualQuoteLeaseKey,
      }),
      signal,
    }),
    enabled: Boolean(selectedPortfolioId && manualInstrumentId),
    refetchInterval: 15_000,
    retry: (failureCount, error) => failureCount < 2 && error instanceof ApiError && (error.status === 0 || error.status >= 500),
  })
  const rows = useMemo(() => (orders.data || []).filter((order) => !search || `${order.internal_id} ${order.broker_order_id} ${order.symbol} ${order.side}`.toLowerCase().includes(search.toLowerCase())), [orders.data, search])

  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({queryKey: ['orders']}),
      queryClient.invalidateQueries({queryKey: ['executions']}),
      queryClient.invalidateQueries({queryKey: ['audit']}),
      queryClient.invalidateQueries({queryKey: ['order-detail']}),
    ])
  }
  const modify = useMutation({
    mutationFn: ({order, payload}: {order: Order; payload: Record<string, string>}) => request<unknown>(`orders/${order.internal_id}/`, mutationOptions('PATCH', payload, true)),
    onSuccess: refresh,
  })
  const cancel = useMutation({
    mutationFn: ({order, reason}: {order: Order; reason: string}) => request<unknown>(`orders/${order.internal_id}/cancel/`, mutationOptions('POST', {reason}, true)),
    onSuccess: async () => {setCancelOrder(null); setSelectedOrder(null); await refresh()},
  })
  const createOrder = useMutation({
    mutationFn: ({payload, idempotencyKey}: {payload: ManualOrderPayload; idempotencyKey: string}) =>
      request<ManualOrderIntentStatus>('orders/', mutationOptions('POST', {portfolio_id: selectedPortfolioId, ...payload}, true, idempotencyKey)),
    retry: (failureCount, error) => failureCount < 2 && error instanceof ApiError && (error.status === 0 || error.status >= 500),
    retryDelay: 250,
    onSuccess: (result) => {
      setManualResult(result)
      setPollTimedOut(false)
      if (!isManualIntentTerminal(result)) {
        setActiveIntent({intentId: result.intent_id, startedAt: Date.now()})
      } else {
        setActiveIntent(null)
      }
    },
    onSettled: () => {
      manualSubmissionInFlight.current = false
    },
  })
  const confirmOrder = useMutation({
    mutationFn: ({intentId, confirmed}: {intentId: number; confirmed: boolean}) =>
      request<ManualOrderIntentStatus>(
        `orders/intents/${intentId}/confirmation/`,
        mutationOptions('POST', {confirmed}, true, `ibkr-percentage-confirmation:${intentId}:${confirmed ? 'accept' : 'decline'}`),
      ),
    onSuccess: (result) => {
      setManualResult(result)
      setPollTimedOut(false)
      if (isManualIntentTerminal(result)) setActiveIntent(null)
      else setActiveIntent((current) => current || {intentId: result.intent_id, startedAt: Date.now()})
      void refresh()
    },
  })

  useEffect(() => {
    const result = manualIntentStatus.data
    if (!result) return
    setManualResult(result)
    if (isManualIntentTerminal(result)) setActiveIntent(null)
  }, [manualIntentStatus.data])

  useEffect(() => {
    if (!activeIntent) return
    if (manualResult?.internal_id) return
    const remaining = Math.max(0, 90_000 - (Date.now() - activeIntent.startedAt))
    const timeout = window.setTimeout(() => {
      setPollTimedOut(true)
      setActiveIntent(null)
    }, remaining)
    return () => window.clearTimeout(timeout)
  }, [activeIntent, manualResult?.internal_id])

  useEffect(() => {
    const internalId = manualResult?.internal_id
    if (!internalId || surfacedManualOrder.current === internalId) return
    surfacedManualOrder.current = internalId
    void (async () => {
      await refresh()
      const detail = await queryClient.fetchQuery(queries.orderDetail(internalId))
      setSelectedOrder(detail.order)
    })()
  }, [manualResult?.internal_id])

  useEffect(() => {
    return () => {
      if (!selectedPortfolioId || !manualInstrumentId) return
      void request('orders/manual-quote/', mutationOptions('DELETE', {
        portfolio_id: selectedPortfolioId,
        lease_key: manualQuoteLeaseKey,
      })).catch(() => undefined)
    }
  }, [selectedPortfolioId, manualInstrumentId, manualQuoteLeaseKey])

  const submitManualOrder = (payload: ManualOrderPayload) => {
    if (manualSubmissionInFlight.current || createOrder.isPending || activeIntent) return
    manualSubmissionInFlight.current = true
    surfacedManualOrder.current = ''
    setManualResult(undefined)
    setPollTimedOut(false)
    createOrder.mutate({payload, idempotencyKey: crypto.randomUUID()})
  }

  const orderColumns = [
    {id: 'order', header: 'Order', cell: (order: Order) => <div className="primary-cell"><button className="link-button mono" onClick={() => setSelectedOrder(order)}>{order.internal_id.slice(0, 12)}</button><span>{order.broker_order_id ? `Broker ${order.broker_order_id}` : 'Awaiting broker ID'}</span></div>},
    {id: 'instrument', header: 'Instrument', cell: (order: Order) => <div className="primary-cell"><strong className="mono">{order.symbol}</strong><span>{order.origin ? `${order.origin} · ` : ''}{order.side} · {order.order_type} · {order.time_in_force}</span></div>},
    {id: 'status', header: 'Status', cell: (order: Order) => <StatusBadge status={order.status} />},
    {id: 'fill', header: 'Fill progress', cell: (order: Order) => <FillProgress filled={order.filled_quantity} total={order.quantity} />},
    {id: 'price', header: 'Average fill', align: 'right' as const, className: 'mono', cell: (order: Order) => formatMoney(order.average_fill_price)},
    {id: 'updated', header: 'Updated', cell: (order: Order) => formatDateTime(order.updated_at)},
    {id: 'view', header: '', align: 'right' as const, cell: (order: Order) => <button className="button-quiet" aria-label={`Inspect order ${order.internal_id}`} onClick={() => setSelectedOrder(order)}>Inspect<ExternalLink /></button>},
  ]
  const executionColumns = [
    {id: 'execution', header: 'Execution', cell: (fill: Execution) => <div className="primary-cell"><strong className="mono">{fill.execution_id}</strong><span>Order {fill.order_id.slice(0, 12)}</span></div>},
    {id: 'instrument', header: 'Instrument', cell: (fill: Execution) => <code>{fill.symbol}</code>},
    {id: 'quantity', header: 'Quantity', align: 'right' as const, className: 'mono', cell: (fill: Execution) => formatNumber(fill.quantity)},
    {id: 'price', header: 'Price', align: 'right' as const, className: 'mono', cell: (fill: Execution) => formatMoney(fill.price, fill.currency)},
    {id: 'commission', header: 'Commission', align: 'right' as const, className: 'mono', cell: (fill: Execution) => formatMoney(fill.commission, fill.currency)},
    {id: 'time', header: 'Executed', cell: (fill: Execution) => formatDateTime(fill.executed_at)},
  ]
  const activity = (audit.data || []).map((item) => ({id: item.id, time: item.created_at, type: item.event_type, title: item.event_type.replaceAll('.', ' '), detail: `${item.actor} · ${item.aggregate_type} ${item.aggregate_id}`}))

  return <div className="page-stack">
    <PageHeader eyebrow="OMS & ledger" title="Orders & Activity" description="Follow order progress, executions, and normal operational activity in one place." actions={<Freshness updatedAt={Math.max(orders.dataUpdatedAt, executions.dataUpdatedAt, audit.dataUpdatedAt)} stale={orders.isStale || executions.isStale || audit.isStale} fetching={orders.isFetching || executions.isFetching || audit.isFetching} onRefresh={() => void refresh()} />} />
    <div className={desktopInspector ? 'order-terminal-grid' : ''}>
    <TerminalPanel id="orders-blotter" title="Order blotter" description="Orders for the selected portfolio" fullscreenable>
      <div className="filter-bar"><label className="search-field"><Search /><span className="sr-only">Search orders</span><input aria-label="Search orders" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search order, broker ID, or symbol" /></label><label><Filter /><span className="sr-only">Filter order status</span><select aria-label="Filter order status" value={status} onChange={(event) => setStatus(event.target.value)}><option value="">All statuses</option>{['QUEUED', 'SUBMITTED', 'ACKNOWLEDGED', 'PARTIALLY_FILLED', 'FILLED', 'CANCEL_PENDING', 'CANCELLED', 'REJECTED', 'UNKNOWN'].map((value) => <option key={value}>{value}</option>)}</select></label><label><span className="sr-only">Filter symbol</span><input aria-label="Filter symbol" value={symbol} onChange={(event) => setSymbol(event.target.value.toUpperCase())} placeholder="Ticker" /></label><span className="filter-count">{rows.length} orders</span></div>
      {orders.isLoading ? <Skeleton lines={6} height={330} /> : orders.isError ? <ErrorState title="Orders are unavailable" error={orders.error} onRetry={() => void orders.refetch()} /> : <DataTable rows={rows} columns={orderColumns} getRowKey={(order) => order.internal_id} selectedRowKey={selectedOrder?.internal_id} onRowSelect={setSelectedOrder} emptyTitle="No orders match" emptyDescription="Try clearing the filters or wait for a strategy target to reach OMS." />}
    </TerminalPanel>
    {desktopInspector && <TerminalPanel id="selected-order" title={selectedOrder ? `${selectedOrder.symbol} ${selectedOrder.side}` : 'Selected order'} description={selectedOrder?.internal_id || 'Choose a blotter row'} className="order-inspector" actions={selectedOrder && <button className="icon-button" aria-label="Close selected order" onClick={() => setSelectedOrder(null)}><X /></button>}>
      {selectedOrder ? <OrderDetailContent order={selectedOrder} detail={orderDetail.data} detailLoading={orderDetail.isLoading} executions={(executions.data || []).filter((fill) => fill.order_id === selectedOrder.internal_id)} modifying={modify.isPending} error={modify.error || cancel.error || orderDetail.error} onModify={(payload) => modify.mutate({order: selectedOrder, payload})} onCancel={() => setCancelOrder(selectedOrder)} showCancel /> : <EmptyState title="No order selected" description="Select a blotter row to inspect status, fills, and eligible actions." />}
    </TerminalPanel>}
    </div>
    <div className="activity-grid"><TerminalPanel id="executions" title="Executions" description="Append-only broker fill ledger">{executions.isLoading ? <Skeleton lines={4} /> : executions.isError ? <ErrorState error={executions.error} onRetry={() => void executions.refetch()} compact /> : <DataTable rows={executions.data || []} columns={executionColumns} getRowKey={(fill) => fill.execution_id} emptyTitle="No executions" />}</TerminalPanel><TerminalPanel id="operational-activity" title="Operational activity" description="Recent audit events">{audit.isError ? <ErrorState error={audit.error} onRetry={() => void audit.refetch()} compact /> : <ActivityTimeline items={activity.slice(0, 12)} />}</TerminalPanel></div>
    <TerminalPanel id="broker-contract-search" title="Stock & option search" description="Search IBKR globally, select an exact contract, and qualify it before using it in an order." defaultOpen>
      <BrokerInstrumentSearch
        value={contractQuery}
        onValueChange={setContractQuery}
        onResolved={(resolution) => {
          if (!resolution?.instrument_id) return
          setManualInstrumentId(resolution.instrument_id)
          void queryClient.invalidateQueries({queryKey: ['instruments']})
        }}
        portfolioId={selectedPortfolioId}
        gatewaySessionId={session?.id}
        searchLabel="Stock or option"
        allowOptions
      />
    </TerminalPanel>
    <TerminalPanel id="manual-order-ticket" title="Manual order ticket" description="Advanced operator action. Manual orders use the durable intent, risk, OMS, and Gateway pipeline." defaultOpen={false}><ManualOrderTicket
      instruments={instruments.data || []}
      positions={positions.data || []}
      session={session}
      account={account}
      portfolio={portfolio}
      pending={createOrder.isPending}
      polling={Boolean(activeIntent)}
      pollTimedOut={pollTimedOut}
      error={createOrder.error || manualIntentStatus.error}
      result={manualResult}
      allowLiveTrading={system.data?.allow_live_trading === true}
      quote={manualQuote.data}
      quotePending={manualQuote.isPending || manualQuote.isFetching}
      quoteError={manualQuote.error}
      onInstrumentChange={setManualInstrumentId}
      selectedInstrumentId={manualInstrumentId}
      onSubmit={submitManualOrder}
      onWarningDecision={(confirmed) => {
        if (!manualResult?.confirmation?.required || confirmOrder.isPending) return
        confirmOrder.mutate({intentId: manualResult.intent_id, confirmed})
      }}
      warningDecisionPending={confirmOrder.isPending}
      warningDecisionError={confirmOrder.error}
    /></TerminalPanel>
    {!desktopInspector && <OrderDrawer order={selectedOrder} detail={orderDetail.data} detailLoading={orderDetail.isLoading} executions={(executions.data || []).filter((fill) => fill.order_id === selectedOrder?.internal_id)} modifying={modify.isPending} error={modify.error || cancel.error || orderDetail.error} onClose={() => setSelectedOrder(null)} onModify={(payload) => selectedOrder && modify.mutate({order: selectedOrder, payload})} onCancel={() => selectedOrder && setCancelOrder(selectedOrder)} />}
    <ConfirmActionDialog open={Boolean(cancelOrder)} title={`Cancel ${cancelOrder?.symbol || ''} order?`} description="Cancellation is submitted through OMS and Gateway. A fill may still arrive while the cancel is pending." confirmLabel="Request cancellation" pending={cancel.isPending} onClose={() => setCancelOrder(null)} onConfirm={(reason) => {if (cancelOrder) cancel.mutate({order: cancelOrder, reason})}} />
  </div>
}

function useDesktopInspector() {
  const [desktop, setDesktop] = useState(false)
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return
    const media = window.matchMedia('(min-width: 1200px)')
    const update = () => setDesktop(media.matches)
    update()
    media.addEventListener('change', update)
    return () => media.removeEventListener('change', update)
  }, [])
  return desktop
}

function orderHistoryItem(item: OrderStatusHistory) {
  const reason = item.reason || 'No broker reason received'
  const code = item.reason_code ? `Code ${item.reason_code} · ` : ''
  const broker = item.broker_status ? ` · IBKR ${item.broker_status}` : ''
  const operator = item.operator_requested ? ' · operator requested' : ''
  return {id:item.id,time:item.occurred_at,type:'ORDER_STATUS',title:`${item.from_status || 'START'} → ${item.to_status}`,
    status:item.to_status,detail:`${code}${reason} · ${item.source}${broker}${operator}`}
}

function riskDecisionItem(item: Record<string, unknown>, index: number) {
  const requested = item.requested_quantity === null || item.requested_quantity === undefined ? '—' : String(item.requested_quantity)
  const approved = item.approved_quantity === null || item.approved_quantity === undefined ? '—' : String(item.approved_quantity)
  return {
    id: typeof item.id === 'number' ? item.id : index,
    time: typeof item.created_at === 'string' ? item.created_at : '',
    type: 'RISK_DECISION',
    title: String(item.check_name || 'Pre-trade risk'),
    status: String(item.decision || 'UNKNOWN'),
    detail: `${String(item.reason || 'No risk reason supplied')} · requested ${requested} · approved ${approved}`,
  }
}

function OrderDetailContent({order, detail, detailLoading, executions, modifying, error, onModify, onCancel, showCancel = false}: {order: Order; detail?: OrderDetail; detailLoading: boolean; executions: Execution[]; modifying: boolean; error: unknown; onModify: (payload: Record<string, string>) => void; onCancel: () => void; showCancel?: boolean}) {
  const submit = (event: React.FormEvent<HTMLFormElement>) => {event.preventDefault(); const form = new FormData(event.currentTarget); const payload: Record<string, string> = {quantity: String(form.get('quantity')), time_in_force: String(form.get('time_in_force'))}; if (form.get('limit_price')) payload.limit_price = String(form.get('limit_price')); if (form.get('stop_price')) payload.stop_price = String(form.get('stop_price')); onModify(payload)}
  return <div className="order-detail-content">
    <div className="order-summary"><StatusBadge status={order.status} /><FillProgress filled={order.filled_quantity} total={order.quantity} /><dl className="detail-list"><div><dt>Origin</dt><dd>{order.origin || 'STRATEGY'}</dd></div><div><dt>Instrument</dt><dd>{order.asset_class} · {order.exchange} · {order.currency}</dd></div>{order.asset_class === 'OPT' && <><div><dt>Option</dt><dd>{order.expiration} · {order.strike} · {order.right === 'C' ? 'Call' : 'Put'}</dd></div><div><dt>Contract</dt><dd>{order.trading_class || order.symbol} · multiplier {String(order.multiplier || '—')}</dd></div></>}<div><dt>Type</dt><dd>{order.order_type}</dd></div><div><dt>Time in force</dt><dd>{order.time_in_force}</dd></div><div><dt>Broker order ID</dt><dd><code>{order.broker_order_id || 'Pending'}</code></dd></div><div><dt>Average fill</dt><dd className="mono">{formatMoney(order.average_fill_price, order.currency)}</dd></div><div><dt>Created</dt><dd>{formatDateTime(order.created_at)}</dd></div><div><dt>Updated</dt><dd>{formatDateTime(order.updated_at)}</dd></div></dl></div>
    {canModifyOrder(order) ? <form className="drawer-form" onSubmit={submit}><h3>Modify eligible fields</h3><label>Total quantity<input name="quantity" type="number" min={Number(order.filled_quantity)} step="any" defaultValue={String(order.quantity)} required /></label>{['LMT', 'STP_LMT'].includes(order.order_type) && <label>Limit price<input name="limit_price" type="number" min="0" step="any" /></label>}{['STP', 'STP_LMT'].includes(order.order_type) && <label>Stop price<input name="stop_price" type="number" min="0" step="any" /></label>}<label>Time in force<select name="time_in_force" defaultValue={order.time_in_force}><option>DAY</option><option>GTC</option></select></label><button className="button-primary" disabled={modifying}>{modifying ? 'Submitting…' : 'Submit modification'}</button></form> : <div className="inline-note">This order’s current state does not permit modification.</div>}
    {error ? <ErrorState title="Order action failed" error={error} compact /> : null}
    <h3>Risk decisions</h3>{detailLoading ? <Skeleton lines={2} /> : <ActivityTimeline items={(detail?.risk_decisions || []).map(riskDecisionItem)} emptyDescription="No risk decisions are attached to this order." />}
    <h3>Status timeline</h3>{detailLoading ? <Skeleton lines={3} /> : <ActivityTimeline items={(detail?.status_history || []).map(orderHistoryItem)} emptyDescription="No order status has been persisted." />}
    <h3>Fills</h3><DataTable rows={executions} columns={[{id: 'id', header: 'Execution', cell: (fill) => <code>{fill.execution_id}</code>}, {id: 'qty', header: 'Quantity', cell: (fill) => formatNumber(fill.quantity)}, {id: 'price', header: 'Price', cell: (fill) => formatMoney(fill.price, fill.currency)}, {id: 'time', header: 'Time', cell: (fill) => formatDateTime(fill.executed_at)}]} getRowKey={(fill) => fill.execution_id} emptyTitle="No fills for this order" />
    {showCancel && <div className="drawer-actions"><button className="button-danger-subtle" disabled={!canCancelOrder(order)} onClick={onCancel}>Cancel order</button></div>}
  </div>
}

function OrderDrawer({order, detail, detailLoading, executions, modifying, error, onClose, onModify, onCancel}: {order: Order | null; detail?: OrderDetail; detailLoading: boolean; executions: Execution[]; modifying: boolean; error: unknown; onClose: () => void; onModify: (payload: Record<string, string>) => void; onCancel: () => void}) {
  if (!order) return null
  return <DetailDrawer open title={`${order.symbol} ${order.side}`} subtitle={order.internal_id} onClose={onClose}>
    <OrderDetailContent order={order} detail={detail} detailLoading={detailLoading} executions={executions} modifying={modifying} error={error} onModify={onModify} onCancel={onCancel} showCancel />
  </DetailDrawer>
}
