import type {StrategyInstance} from '../src/api/types'
import {buildStrategyPatch} from '../src/features/strategies/EditStrategyPanel'

const strategy = {
  id: 3,
  name: 'NIFTY automatic',
  timeframe: '1d',
  version: 4,
  parameters: {lots: 1, rps_threshold: 60},
  risk_policy_id: null,
  order_policy_id: null,
} as unknown as StrategyInstance

const draft = {
  name: 'NIFTY automatic',
  timeframe: '1d',
  parameters: JSON.stringify(strategy.parameters, null, 2),
  riskPolicyId: '',
  orderPolicyId: '',
}

test('an untouched form sends nothing', () => {
  const built = buildStrategyPatch(strategy, draft)
  expect('error' in built).toBe(false)
  if ('error' in built) return
  expect(built.payload).toEqual({})
  expect(built.material).toEqual([])
})

test('a renamed strategy keeps its running version', () => {
  const built = buildStrategyPatch(strategy, {...draft, name: 'NIFTY renamed'})
  if ('error' in built) throw new Error(built.error)
  expect(built.payload).toEqual({name: 'NIFTY renamed'})
  // A name is not material, so the Backend does not retire the version.
  expect(built.material).toEqual([])
})

test('a parameter change is reported as material', () => {
  const built = buildStrategyPatch(strategy, {...draft, parameters: '{"lots": 2, "rps_threshold": 60}'})
  if ('error' in built) throw new Error(built.error)
  expect(built.payload).toEqual({parameters: {lots: 2, rps_threshold: 60}})
  expect(built.material).toEqual(['parameters'])
})

test('a timeframe change is material, which is what restarts warm-up', () => {
  const built = buildStrategyPatch(strategy, {...draft, timeframe: '1m'})
  if ('error' in built) throw new Error(built.error)
  expect(built.payload).toEqual({timeframe: '1m'})
  expect(built.material).toEqual(['timeframe'])
})

test('reordered parameter keys are not a change', () => {
  const built = buildStrategyPatch(strategy, {...draft, parameters: '{"lots": 1, "rps_threshold": 60}'})
  if ('error' in built) throw new Error(built.error)
  expect(built.payload).toEqual({})
})

test('a policy is sent as a number and cleared as null', () => {
  const attached = buildStrategyPatch(strategy, {...draft, riskPolicyId: '7'})
  if ('error' in attached) throw new Error(attached.error)
  expect(attached.payload).toEqual({risk_policy_id: 7})

  const withPolicy = {...strategy, risk_policy_id: 7} as unknown as StrategyInstance
  const cleared = buildStrategyPatch(withPolicy, draft)
  if ('error' in cleared) throw new Error(cleared.error)
  expect(cleared.payload).toEqual({risk_policy_id: null})
})

test('malformed parameters are refused before they reach the Backend', () => {
  expect(buildStrategyPatch(strategy, {...draft, parameters: '{lots: 1}'})).toEqual({error: 'Parameters must be valid JSON.'})
  expect(buildStrategyPatch(strategy, {...draft, parameters: '[1, 2]'})).toEqual({error: 'Parameters must be a JSON object.'})
})

test('an empty name is refused', () => {
  expect(buildStrategyPatch(strategy, {...draft, name: '   '})).toEqual({error: 'A strategy needs a name.'})
})
