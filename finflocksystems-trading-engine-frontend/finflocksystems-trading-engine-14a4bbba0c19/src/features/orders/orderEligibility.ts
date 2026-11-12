import type {Order} from '../../api/types'

const MODIFIABLE = new Set(['QUEUED', 'SUBMITTED', 'ACKNOWLEDGED', 'PARTIALLY_FILLED'])
const CANCELLABLE = new Set([...MODIFIABLE, 'UNKNOWN'])

export function canModifyOrder(order: Pick<Order, 'status'>) {
  return MODIFIABLE.has(order.status.toUpperCase())
}

export function canCancelOrder(order: Pick<Order, 'status'>) {
  return CANCELLABLE.has(order.status.toUpperCase())
}

