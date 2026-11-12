import {canCancelOrder, canModifyOrder} from '../src/features/orders/orderEligibility'

test('only permits order actions in backend-supported states', () => {
  for (const status of ['QUEUED', 'SUBMITTED', 'ACKNOWLEDGED', 'PARTIALLY_FILLED']) {
    expect(canModifyOrder({status})).toBe(true)
    expect(canCancelOrder({status})).toBe(true)
  }
  expect(canCancelOrder({status: 'UNKNOWN'})).toBe(true)
  expect(canModifyOrder({status: 'UNKNOWN'})).toBe(false)
  for (const status of ['FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED']) {
    expect(canModifyOrder({status})).toBe(false)
    expect(canCancelOrder({status})).toBe(false)
  }
})

