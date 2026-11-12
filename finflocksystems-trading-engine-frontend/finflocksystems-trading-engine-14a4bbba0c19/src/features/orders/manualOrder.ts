import type {
  BrokerAccount,
  BrokerGatewaySession,
  BrokerSessionAccount,
  Instrument,
  ManualOrderIntentStatus,
  Portfolio,
} from '../../api/types'

export type ManualOrderSide = 'BUY' | 'SELL'
export type ManualOrderType = 'MKT' | 'LMT' | 'STP' | 'STP_LMT'
export type ManualOrderTimeInForce = 'DAY' | 'GTC'

export interface ManualOrderDraft {
  instrumentId: string
  side: ManualOrderSide
  orderType: ManualOrderType
  quantity: string
  limitPrice: string
  stopPrice: string
  timeInForce: ManualOrderTimeInForce
}

export interface ManualOrderPayload {
  instrument_id: number
  side: ManualOrderSide
  order_type: ManualOrderType
  quantity: string
  time_in_force: ManualOrderTimeInForce
  limit_price?: string
  stop_price?: string
}

export interface ManualOrderSelection {
  session: BrokerGatewaySession | null
  account: (BrokerAccount & Partial<Pick<BrokerSessionAccount, 'available' | 'last_seen_at' | 'default_portfolio_id'>>) | null
  portfolio: Portfolio | null
}

export const initialManualOrderDraft: ManualOrderDraft = {
  instrumentId: '',
  side: 'BUY',
  orderType: 'MKT',
  quantity: '',
  limitPrice: '',
  stopPrice: '',
  timeInForce: 'DAY',
}

export const orderTypeNeedsLimitPrice = (orderType: ManualOrderType) =>
  orderType === 'LMT' || orderType === 'STP_LMT'

export const orderTypeNeedsStopPrice = (orderType: ManualOrderType) =>
  orderType === 'STP' || orderType === 'STP_LMT'

const DECIMAL_PATTERN = /^(?:\d+(?:\.\d*)?|\.\d+)$/

export function isPositiveBackendDecimal(value: string) {
  const normalized = value.trim()
  if (!DECIMAL_PATTERN.test(normalized) || !/[1-9]/.test(normalized)) return false
  const [integer = '', fractional = ''] = normalized.split('.')
  const significantInteger = integer.replace(/^0+/, '')
  return significantInteger.length <= 16 && fractional.length <= 8
}

export function validateManualOrderDraft(draft: ManualOrderDraft, instruments: Instrument[]) {
  const errors: Record<string, string> = {}
  const instrumentId = Number(draft.instrumentId)
  const instrument = instruments.find((item) => item.id === instrumentId)

  if (!draft.instrumentId || !Number.isSafeInteger(instrumentId) || !instrument) {
    errors.instrumentId = 'Select an instrument.'
  } else if (!instrument.active) {
    errors.instrumentId = `${instrument.symbol} is inactive.`
  } else if (!instrument.tradable) {
    errors.instrumentId = `${instrument.symbol} is not tradable.`
  }
  if (!['BUY', 'SELL'].includes(draft.side)) errors.side = 'Side must be BUY or SELL.'
  if (!['MKT', 'LMT', 'STP', 'STP_LMT'].includes(draft.orderType)) errors.orderType = 'Select a supported order type.'
  if (!isPositiveBackendDecimal(draft.quantity)) {
    errors.quantity = 'Enter a positive quantity with at most 8 decimal places.'
  } else if (instrument?.asset_class === 'OPT' && !Number.isInteger(Number(draft.quantity))) {
    errors.quantity = 'Option quantity must be a positive whole number of contracts.'
  }
  if (orderTypeNeedsLimitPrice(draft.orderType) && !isPositiveBackendDecimal(draft.limitPrice)) {
    errors.limitPrice = 'Enter a positive limit price with at most 8 decimal places.'
  }
  if (orderTypeNeedsStopPrice(draft.orderType) && !isPositiveBackendDecimal(draft.stopPrice)) {
    errors.stopPrice = 'Enter a positive stop price with at most 8 decimal places.'
  }
  if (!['DAY', 'GTC'].includes(draft.timeInForce)) errors.timeInForce = 'Time in force must be DAY or GTC.'
  return errors
}

export function buildManualOrderPayload(draft: ManualOrderDraft): ManualOrderPayload {
  const payload: ManualOrderPayload = {
    instrument_id: Number(draft.instrumentId),
    side: draft.side,
    order_type: draft.orderType,
    quantity: draft.quantity.trim(),
    time_in_force: draft.timeInForce,
  }
  if (orderTypeNeedsLimitPrice(draft.orderType)) payload.limit_price = draft.limitPrice.trim()
  if (orderTypeNeedsStopPrice(draft.orderType)) payload.stop_price = draft.stopPrice.trim()
  return payload
}

export function manualOrderBlockingReasons(
  {session, account, portfolio}: ManualOrderSelection,
  allowLiveTrading: boolean,
) {
  const reasons: string[] = []
  if (!portfolio) reasons.push('Select an eligible portfolio.')
  if (!session) reasons.push('Select a broker session.')
  if (!account) reasons.push('Select an account available through the broker session.')
  if (!session || !portfolio || !account) return reasons

  if (!portfolio.gateway_session_id) reasons.push(`Portfolio ${portfolio.name} is not bound to a Gateway session.`)
  else if (portfolio.gateway_session_id !== session.id) reasons.push(`Portfolio ${portfolio.name} is bound to a different Gateway session.`)
  if (portfolio.account_id !== account.id) reasons.push(`Portfolio ${portfolio.name} is not bound to account ${account.account_id}.`)
  if (!session.connected || session.status !== 'CONNECTED') {
    reasons.push(session.last_error
      ? `Gateway session ${session.display_name} is disconnected: ${session.last_error}`
      : `Gateway session ${session.display_name} is not connected (${session.status}).`)
  }
  if (!session.commands_enabled) reasons.push(`Gateway commands are disabled for ${session.display_name}.`)
  if (account.available !== true) reasons.push(`Account ${account.account_id} is unavailable through ${session.display_name}.`)
  if (!account.is_reconciled || session.last_gateway_state.reconciled === false) {
    reasons.push(`Account ${account.account_id} has unresolved reconciliation requirements.`)
  }
  if (session.mode === 'live' && !allowLiveTrading) {
    reasons.push('Live manual order routing is blocked by ALLOW_LIVE_TRADING.')
  }
  if (account.kill_switch) reasons.push(`The kill switch is enabled for account ${account.account_id}.`)
  if (portfolio.kill_switch) reasons.push(`The kill switch is enabled for portfolio ${portfolio.name}.`)
  return [...new Set(reasons)]
}

export function estimateManualOrderNotional(draft: ManualOrderDraft, marketPrice?: string | number | null, multiplier: string | number = 1) {
  const quantity = Number(draft.quantity)
  let price: number | null = null
  if (draft.orderType === 'LMT') price = Number(draft.limitPrice)
  else if (draft.orderType === 'STP_LMT') price = Math.max(Number(draft.limitPrice), Number(draft.stopPrice))
  else if (draft.orderType === 'STP') price = Math.max(Number(draft.stopPrice), Number(marketPrice))
  else if (marketPrice !== null && marketPrice !== undefined) price = Number(marketPrice)
  if (!Number.isFinite(quantity) || quantity <= 0 || !Number.isFinite(price) || (price ?? 0) <= 0) return null
  const contractMultiplier = Number(multiplier)
  if (!Number.isFinite(contractMultiplier) || contractMultiplier <= 0) return null
  const notional = quantity * (price as number) * contractMultiplier
  return Number.isSafeInteger(Math.trunc(notional)) || Math.abs(notional) < Number.MAX_SAFE_INTEGER ? notional : null
}

export type ManualOrderResultKind = 'QUEUED' | 'HELD' | 'REJECTED' | 'OMS'

export function manualOrderResultKind(result: ManualOrderIntentStatus): ManualOrderResultKind {
  if (['RISK_REJECTED', 'FAILED', 'USER_CANCELLED'].includes(result.operation_status)) return 'REJECTED'
  if (result.internal_id) return 'OMS'
  if (result.retryable) return 'HELD'
  return 'QUEUED'
}

export function isManualIntentTerminal(result?: ManualOrderIntentStatus) {
  if (!result) return false
  if (['RISK_REJECTED', 'FAILED', 'USER_CANCELLED'].includes(result.operation_status)) return true
  return Boolean(result.internal_id && ['FILLED', 'REJECTED', 'CANCELLED', 'EXPIRED'].includes(result.status || ''))
}
