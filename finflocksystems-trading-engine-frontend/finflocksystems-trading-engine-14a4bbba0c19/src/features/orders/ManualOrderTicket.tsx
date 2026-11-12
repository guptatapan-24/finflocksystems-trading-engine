import {AlertTriangle, ShieldCheck, SlidersHorizontal, X} from 'lucide-react'
import {useEffect, useMemo, useState} from 'react'
import {ApiError} from '../../api/client'
import type {
  ExecutionMode,
  Instrument,
  ManualOrderIntentStatus,
  ManualOrderQuoteStatus,
  Position,
} from '../../api/types'
import {StatusBadge, formatMoney, formatNumber} from '../../components/ui'
import {executionModeForSession} from '../../executionMode'
import {
  buildManualOrderPayload,
  estimateManualOrderNotional,
  initialManualOrderDraft,
  manualOrderBlockingReasons,
  manualOrderResultKind,
  orderTypeNeedsLimitPrice,
  orderTypeNeedsStopPrice,
  validateManualOrderDraft,
  type ManualOrderDraft,
  type ManualOrderPayload,
  type ManualOrderSelection,
  type ManualOrderType,
} from './manualOrder'

interface ManualOrderTicketProps extends ManualOrderSelection {
  instruments: Instrument[]
  positions: Position[]
  pending: boolean
  polling: boolean
  pollTimedOut: boolean
  error: unknown
  result?: ManualOrderIntentStatus
  allowLiveTrading: boolean
  quote?: ManualOrderQuoteStatus
  quotePending: boolean
  quoteError: unknown
  onInstrumentChange: (instrumentId: number | null) => void
  selectedInstrumentId?: number | null
  onSubmit: (payload: ManualOrderPayload) => void
  onWarningDecision?: (confirmed: boolean) => void
  warningDecisionPending?: boolean
  warningDecisionError?: unknown
}

export function ManualOrderTicket({
  instruments,
  positions,
  session,
  account,
  portfolio,
  pending,
  polling,
  pollTimedOut,
  error,
  result,
  allowLiveTrading,
  quote,
  quotePending,
  quoteError,
  onInstrumentChange,
  selectedInstrumentId,
  onSubmit,
  onWarningDecision,
  warningDecisionPending,
  warningDecisionError,
}: ManualOrderTicketProps) {
  const [draft, setDraft] = useState<ManualOrderDraft>(initialManualOrderDraft)
  const [validationErrors, setValidationErrors] = useState<Record<string, string>>({})
  const [confirmation, setConfirmation] = useState<ManualOrderDraft | null>(null)
  const instrument = instruments.find((item) => item.id === Number(draft.instrumentId)) || null
  const position = positions.find((item) => item.instrument_id === instrument?.id)
  const executionMode = executionModeForSession(session)
  const requiresLiveReference = draft.orderType === 'MKT' || draft.orderType === 'STP'
  const readyQuote = quote && quote.instrument_id === instrument?.id && quote.execution_usable ? quote : null
  const quoteReady = readyQuote !== null
  const blockers = [
    ...manualOrderBlockingReasons({session, account, portfolio}, allowLiveTrading),
    ...(instrument && requiresLiveReference && !quoteReady
      ? ['Wait for a fresh persisted live market price before submitting this order.']
      : []),
  ]
  const trustedReferencePrice = readyQuote?.reference_price ?? null
  const estimatedNotional = estimateManualOrderNotional(draft, trustedReferencePrice, instrument?.multiplier ?? 1)
  const busy = pending || polling

  useEffect(() => {
    if (!selectedInstrumentId) return
    setDraft((current) => ({...current, instrumentId: String(selectedInstrumentId)}))
    setValidationErrors((current) => {
      if (!current.instrumentId) return current
      const next = {...current}
      delete next.instrumentId
      return next
    })
  }, [selectedInstrumentId])

  const update = <K extends keyof ManualOrderDraft>(field: K, value: ManualOrderDraft[K]) => {
    setDraft((current) => ({...current, [field]: value}))
    setValidationErrors((current) => {
      if (!current[field]) return current
      const next = {...current}
      delete next[field]
      return next
    })
  }
  const changeInstrument = (value: string) => {
    update('instrumentId', value)
    const instrumentId = Number(value)
    onInstrumentChange(value && Number.isSafeInteger(instrumentId) ? instrumentId : null)
  }
  const changeOrderType = (orderType: ManualOrderType) => {
    setDraft((current) => ({
      ...current,
      orderType,
      limitPrice: orderTypeNeedsLimitPrice(orderType) ? current.limitPrice : '',
      stopPrice: orderTypeNeedsStopPrice(orderType) ? current.stopPrice : '',
    }))
    setValidationErrors({})
  }
  const submit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const nextErrors = validateManualOrderDraft(draft, instruments)
    setValidationErrors(nextErrors)
    if (Object.keys(nextErrors).length || blockers.length || busy) return
    setConfirmation({...draft})
  }
  const confirm = () => {
    if (!confirmation || busy) return
    setConfirmation(null)
    onSubmit(buildManualOrderPayload(confirmation))
  }

  return <>
    <div className="manual-order-context" aria-label="Manual order routing context">
      <ContextValue label="Session" value={session?.display_name || 'Not selected'} detail={session ? session.status : undefined} />
      <ContextValue label="Mode" value={session?.mode.toUpperCase() || '—'} critical={session?.mode.toLowerCase() === 'live'} />
      <ContextValue label="Account" value={account?.account_id || 'Not selected'} detail={account?.alias} />
      <ContextValue label="Portfolio" value={portfolio?.name || 'Not selected'} />
    </div>
    {session?.mode === 'live' && <div className="inline-warning manual-live-warning"><AlertTriangle /><div><strong>{allowLiveTrading ? 'LIVE order warning' : 'LIVE routing blocked'}</strong><p>{allowLiveTrading ? `This order will route to the live IBKR account through ${session.display_name}. Review every confirmation detail.` : 'ALLOW_LIVE_TRADING is disabled. The backend will reject Live execution.'}</p></div></div>}
    {blockers.length > 0 && <div className="manual-order-blockers" role="status"><strong>Submission unavailable</strong><ul>{blockers.map((reason) => <li key={reason}>{reason}</li>)}</ul></div>}
    <form className="form-grid four-columns manual-order-form" onSubmit={submit} noValidate>
      <label>Instrument
        <select aria-label="Instrument" value={draft.instrumentId} onChange={(event) => changeInstrument(event.target.value)} aria-invalid={Boolean(validationErrors.instrumentId)}>
          <option value="">Choose</option>
          {instruments.map((item) => <option key={item.id} value={item.id} disabled={!item.active || !item.tradable}>{item.symbol} · {item.exchange}{!item.active ? ' · inactive' : !item.tradable ? ' · not tradable' : ''}</option>)}
        </select>
        {validationErrors.instrumentId && <span className="field-error">{validationErrors.instrumentId}</span>}
      </label>
      {instrument && <div className="manual-order-estimate" role="status" aria-label="Execution market price">
        <span>Execution market price</span>
        <strong>{readyQuote ? formatMoney(readyQuote.reference_price, instrument.currency) : quotePending ? 'Requesting live price…' : 'Not ready'}</strong>
        <small>{readyQuote ? `${readyQuote.provider} · ${readyQuote.source} · ${Math.round(readyQuote.age_seconds || 0)}s old` : quote?.display_status || 'Waiting for a trusted persisted quote'}</small>
      </div>}
      {instrument?.asset_class === 'OPT' && <div className="manual-order-estimate" role="status" aria-label="Selected option contract">
        <span>Qualified option contract</span>
        <strong>{instrument.expiration} · {instrument.strike} · {instrument.right === 'C' ? 'Call' : 'Put'}</strong>
        <small>{instrument.trading_class || instrument.symbol} · {instrument.exchange} · multiplier {String(instrument.multiplier)}</small>
      </div>}
      <label>Side
        <select aria-label="Side" value={draft.side} onChange={(event) => update('side', event.target.value as ManualOrderDraft['side'])} aria-invalid={Boolean(validationErrors.side)}><option>BUY</option><option>SELL</option></select>
        {validationErrors.side && <span className="field-error">{validationErrors.side}</span>}
      </label>
      <label>Order type
        <select aria-label="Order type" value={draft.orderType} onChange={(event) => changeOrderType(event.target.value as ManualOrderType)} aria-invalid={Boolean(validationErrors.orderType)}><option>MKT</option><option>LMT</option><option>STP</option><option>STP_LMT</option></select>
        {validationErrors.orderType && <span className="field-error">{validationErrors.orderType}</span>}
      </label>
      <label>Quantity
        <input aria-label="Quantity" value={draft.quantity} onChange={(event) => update('quantity', event.target.value)} type="number" min={instrument?.asset_class === 'OPT' ? '1' : '0.00000001'} step={instrument?.asset_class === 'OPT' ? '1' : '0.00000001'} inputMode={instrument?.asset_class === 'OPT' ? 'numeric' : 'decimal'} aria-invalid={Boolean(validationErrors.quantity)} />
        {validationErrors.quantity && <span className="field-error">{validationErrors.quantity}</span>}
        {instrument?.asset_class === 'OPT' && <small>Number of contracts; whole numbers only.</small>}
      </label>
      {orderTypeNeedsStopPrice(draft.orderType) && <label>Stop price
        <input aria-label="Stop price" value={draft.stopPrice} onChange={(event) => update('stopPrice', event.target.value)} type="number" min="0.00000001" step="0.00000001" inputMode="decimal" aria-invalid={Boolean(validationErrors.stopPrice)} />
        {validationErrors.stopPrice && <span className="field-error">{validationErrors.stopPrice}</span>}
      </label>}
      {orderTypeNeedsLimitPrice(draft.orderType) && <label>Limit price
        <input aria-label="Limit price" value={draft.limitPrice} onChange={(event) => update('limitPrice', event.target.value)} type="number" min="0.00000001" step="0.00000001" inputMode="decimal" aria-invalid={Boolean(validationErrors.limitPrice)} />
        {validationErrors.limitPrice && <span className="field-error">{validationErrors.limitPrice}</span>}
      </label>}
      <label>Time in force
        <select aria-label="Time in force" value={draft.timeInForce} onChange={(event) => update('timeInForce', event.target.value as ManualOrderDraft['timeInForce'])} aria-invalid={Boolean(validationErrors.timeInForce)}><option>DAY</option><option>GTC</option></select>
        {validationErrors.timeInForce && <span className="field-error">{validationErrors.timeInForce}</span>}
      </label>
      <div className="manual-order-estimate">
        <span>Estimated notional</span>
        <strong>{estimatedNotional === null ? 'Available after a safe price is known' : formatMoney(estimatedNotional, instrument?.currency)}</strong>
        {draft.side === 'SELL' && <small>Available position: {position ? formatNumber(position.quantity) : 'No position reported'}</small>}
      </div>
      <button className="button-primary form-submit" disabled={busy || blockers.length > 0}><SlidersHorizontal />{pending ? 'Submitting…' : polling ? 'Awaiting OMS…' : 'Review manual order'}</button>
    </form>
    <p className="manual-order-risk-note"><ShieldCheck />Final quantity may be resized by the common pre-trade risk pipeline.</p>
    <ManualOrderError error={error || quoteError} />
    {result && <ManualOrderResult result={result} polling={polling} pollTimedOut={pollTimedOut} />}
    {pollTimedOut && !result?.internal_id && <div className="inline-warning" role="status"><AlertTriangle /><div><strong>Status polling timed out</strong><p>The intent was not submitted again. Refresh Orders & Activity or check the intent ID below.</p></div></div>}
    {confirmation && executionMode && <ManualOrderConfirmation
      draft={confirmation}
      instrument={instruments.find((item) => item.id === Number(confirmation.instrumentId)) || null}
      sessionName={session?.display_name || 'Not selected'}
      mode={executionMode}
      accountName={account?.account_id || 'Not selected'}
      portfolioName={portfolio?.name || 'Not selected'}
      referencePrice={trustedReferencePrice}
      estimatedNotional={estimateManualOrderNotional(confirmation, trustedReferencePrice, instruments.find((item) => item.id === Number(confirmation.instrumentId))?.multiplier ?? 1)}
      pending={pending}
      onClose={() => setConfirmation(null)}
      onConfirm={confirm}
    />}
    {result?.confirmation?.required && <IbkrWarningConfirmation
      warning={result.confirmation}
      pending={warningDecisionPending === true}
      onCancel={() => onWarningDecision?.(false)}
      onConfirm={() => onWarningDecision?.(true)}
    />}
    {warningDecisionError && <ManualOrderError error={warningDecisionError} />}
  </>
}

function IbkrWarningConfirmation({warning, pending, onCancel, onConfirm}: {
  warning: NonNullable<ManualOrderIntentStatus['confirmation']>
  pending: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  const message = warning.warning_message
    .replace(/\\?<br\s*\/?\s*>/gi, '\n')
    .replace(/reason\\\s*:/gi, 'reason:')
    .replace(/^Error\s+\d+\s*,\s*reqId\s+[-\d]+\s*:\s*/i, '')
    .trim()
  return <div className="dialog-layer" role="presentation">
    <div className="confirm-dialog manual-order-confirmation" role="dialog" aria-modal="true" aria-labelledby="ibkr-warning-title" aria-describedby="ibkr-warning-message">
      <header><AlertTriangle /><div><h2 id="ibkr-warning-title">IBKR Order Warning</h2><p>IBKR requires your confirmation before this order can continue.</p></div></header>
      <div className="inline-warning"><AlertTriangle /><div><strong>IBKR warning {warning.warning_code}</strong><p id="ibkr-warning-message" style={{whiteSpace: 'pre-line'}}>{message}</p>{warning.broker_order_id && <p>Broker order <code>{warning.broker_order_id}</code></p>}</div></div>
      {warning.can_confirm && warning.override_options && warning.override_options.length > 0 && <div className="manual-order-risk-note">
        <strong>IBKR acknowledgement</strong>
        {warning.override_options.map((option) => <p key={option.code}>{option.text || 'Continue with this broker warning.'} <small>(IBKR code <code>{option.code}</code>)</small></p>)}
      </div>}
      {!warning.can_confirm && <p className="manual-order-risk-note">IBKR did not provide a programmatic override code. Continue is disabled; review the Gateway configuration and captured advanced reject JSON.</p>}
      <footer><button type="button" className="button-secondary" disabled={pending} onClick={onCancel}>{pending ? 'Updating…' : 'Cancel'}</button><button type="button" className="button-primary" disabled={pending || !warning.can_confirm} onClick={onConfirm}>{pending ? 'Resubmitting…' : 'Continue'}</button></footer>
    </div>
  </div>
}

function ContextValue({label, value, detail, critical = false}: {label: string; value: string; detail?: string; critical?: boolean}) {
  return <div><span>{label}</span><strong className={critical ? 'critical-text' : ''}>{value}</strong>{detail && <small>{detail}</small>}</div>
}

function ManualOrderConfirmation({
  draft,
  instrument,
  sessionName,
  mode,
  accountName,
  portfolioName,
  referencePrice,
  estimatedNotional,
  pending,
  onClose,
  onConfirm,
}: {
  draft: ManualOrderDraft
  instrument: Instrument | null
  sessionName: string
  mode: ExecutionMode
  accountName: string
  portfolioName: string
  referencePrice?: string | number | null
  estimatedNotional: number | null
  pending: boolean
  onClose: () => void
  onConfirm: () => void
}) {
  const rows = useMemo(() => [
    ['Mode', mode],
    ['Gateway', sessionName],
    ['Account', accountName],
    ['Portfolio', portfolioName],
    ['Instrument', instrument ? `${instrument.symbol} · ${instrument.exchange}` : 'Not selected'],
    ['Side', draft.side],
    ['Quantity', draft.quantity],
    ['Order type', draft.orderType],
    ...(orderTypeNeedsStopPrice(draft.orderType) ? [['Stop price', draft.stopPrice]] : []),
    ...(orderTypeNeedsLimitPrice(draft.orderType) ? [['Limit price', draft.limitPrice]] : []),
    ['Price details', draft.orderType === 'MKT'
      ? `Market order · current reference ${formatMoney(referencePrice, instrument?.currency)}`
      : draft.orderType === 'STP'
        ? `Stop ${formatMoney(draft.stopPrice, instrument?.currency)}`
        : `Limit ${formatMoney(draft.limitPrice, instrument?.currency)}${draft.orderType === 'STP_LMT' ? ` · stop ${formatMoney(draft.stopPrice, instrument?.currency)}` : ''}`],
    ['Time in force', draft.timeInForce],
    ['Estimated notional', estimatedNotional === null ? 'Not available' : formatMoney(estimatedNotional, instrument?.currency)],
  ], [accountName, draft, estimatedNotional, instrument, mode, portfolioName, referencePrice, sessionName])
  return <div className="dialog-layer" role="presentation">
    <div className="confirm-dialog manual-order-confirmation" role="dialog" aria-modal="true" aria-labelledby="manual-order-confirm-title">
      <header><AlertTriangle /><div><h2 id="manual-order-confirm-title">Confirm {mode} manual order</h2><p>Review the exact order routed through risk, OMS, and the selected Gateway.</p></div><button type="button" className="icon-button" aria-label="Close manual order confirmation" onClick={onClose}><X /></button></header>
      <dl className="detail-list">{rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
      <div className="inline-warning"><AlertTriangle /><div><strong>Risk may resize or reject this order</strong><p>Confirmation creates one durable manual OrderIntent; it does not bypass backend validation.</p></div></div>
      <footer><button type="button" className="button-secondary" onClick={onClose}>Go back</button><button type="button" className="button-primary" disabled={pending} onClick={onConfirm}>{pending ? 'Submitting…' : `Confirm ${mode} order`}</button></footer>
    </div>
  </div>
}

function ManualOrderResult({result, polling, pollTimedOut}: {result: ManualOrderIntentStatus; polling: boolean; pollTimedOut: boolean}) {
  const kind = manualOrderResultKind(result)
  if (kind === 'OMS') return <div className="manual-order-result inline-success" role="status">
    <StatusBadge status={result.status || result.operation_status} />
    <div><strong>{manualOrderLifecycleLabel(result)}</strong><p>Intent <code>{result.intent_id}</code> · internal order <code>{result.internal_id}</code>{result.broker_order_id ? <> · broker order <code>{result.broker_order_id}</code></> : ''}</p><p>Approved: {formatNumber(result.approved_quantity)} · filled: {formatNumber(result.filled_quantity)} · current status: {result.status}{polling ? ' · tracking broker lifecycle' : ''}</p>{result.broker_command && <p>Broker command: {result.broker_command.command_type} · {result.broker_command.status} · attempts {result.broker_command.attempt_count}{result.broker_command.last_error ? ` · ${result.broker_command.last_error}` : ''}</p>}{result.operation_error && <p>{result.operation_error}</p>}</div>
  </div>
  if (kind === 'HELD') return <div className="manual-order-result inline-warning" role="status">
    <AlertTriangle /><div><strong>HELD · {result.operation_status}</strong><p>{result.message}</p><p>Intent <code>{result.intent_id}</code> · origin {result.origin} · the system will retry: {result.retryable ? 'yes' : 'no'}{polling ? ' · polling durable status' : ''}</p></div>
  </div>
  if (kind === 'REJECTED') return <div className="manual-order-result manual-order-rejected" role="alert">
    <StatusBadge status="REJECTED" /><div><strong>RISK_REJECTED</strong><p>{result.message}</p><p>Intent <code>{result.intent_id}</code> · origin {result.origin} · retryable: {result.retryable ? 'yes' : 'no'}</p></div>
  </div>
  return <div className="manual-order-result manual-order-queued" role="status">
    <StatusBadge status={result.operation_status} /><div><strong>Manual order accepted and queued for risk and execution.</strong><p>Intent <code>{result.intent_id}</code> · origin {result.origin}</p><p>Operation: {result.operation_status} · retryable: {result.retryable ? 'yes' : 'no'}{polling ? ' · polling durable status' : pollTimedOut ? ' · polling stopped' : ''}</p><p>{result.message}</p></div>
  </div>
}

function manualOrderLifecycleLabel(result: ManualOrderIntentStatus) {
  if (result.status === 'QUEUED' && result.broker_command) return 'BROKER_PENDING'
  if (result.status === 'QUEUED') return 'OMS_QUEUED'
  return result.status || result.operation_status
}

function ManualOrderError({error}: {error: unknown}) {
  if (!error) return null
  const apiError = error instanceof ApiError ? error : null
  const code = apiError?.code || 'REQUEST_FAILED'
  const message = error instanceof Error ? error.message : 'The manual order request failed.'
  const explanation = specificErrorExplanation(code, message)
  return <div className="manual-order-api-error" role="alert">
    <StatusBadge status="REJECTED" />
    <div><strong>{code}</strong><p>{apiError ? `HTTP ${apiError.status || 'network'} · ` : ''}{message}</p>{explanation !== message && <p>{explanation}</p>}{apiError?.details !== null && apiError?.details !== undefined && <details><summary>Structured details</summary><pre>{JSON.stringify(apiError.details, null, 2)}</pre></details>}</div>
  </div>
}

function specificErrorExplanation(code: string, message: string) {
  const value = `${code} ${message}`.toLowerCase()
  if (code === 'IDEMPOTENCY_CONFLICT') return 'This idempotency key was already used for materially different order contents. Review the form and start a new submission.'
  if (code === 'LIVE_MANUAL_TRADING_DISABLED' || value.includes('live manual')) return 'Live manual routing is blocked by ALLOW_LIVE_TRADING. Enable the backend safety gate before retrying.'
  if (code === 'MARKET_PRICE_UNAVAILABLE' || value.includes('stale') || value.includes('market price')) return 'A sufficiently fresh trusted market price is unavailable; no client reference price will be substituted.'
  if (value.includes('reconcil')) return 'The selected account must complete reconciliation before commands can be dispatched.'
  if (value.includes('disconnected') || value.includes('not active') || value.includes('gateway')) return 'Reconnect the selected Gateway session and verify commands are enabled.'
  if (value.includes('cash') || value.includes('buying power')) return 'Risk reported insufficient available cash or buying power for this order.'
  if (value.includes('position') || value.includes('short')) return 'Risk reported insufficient unreserved position quantity for this SELL order.'
  if (value.includes('inactive') || value.includes('tradable')) return 'The selected instrument is inactive or not tradable.'
  if (value.includes('kill switch')) return 'A kill switch is preventing execution for the selected route.'
  if (code === 'RISK_REJECTED' || value.includes('risk')) return 'The common pre-trade risk policy rejected this intent.'
  return message
}
