import {expect, expectTypeOf, test} from 'vitest'
import type {BrokerGatewaySession, ExecutionMode} from '../src/api/types'
import {
  EXECUTION_MODES,
  executionModeForGatewayMode,
  executionModeReadiness,
} from '../src/executionMode'

const now = '2026-07-27T00:00:00Z'

function gateway(mode: 'paper' | 'live'): BrokerGatewaySession {
  return {
    id: `${mode}-session`,
    display_name: `${mode} Gateway`,
    username_hint: 'u***r',
    mode,
    status: 'CONNECTED',
    connected: true,
    commands_enabled: true,
    container_status: 'running',
    account_count: 1,
    last_error: '',
    last_gateway_state: {connected: true, reconciled: true, mode},
    created_at: now,
    updated_at: now,
    provisioned_at: now,
    connected_at: now,
    last_checked_at: now,
    deleted_at: null,
    needs_novnc: false,
    novnc_url: null,
    market_data_type: mode === 'live' ? 'LIVE' : 'DELAYED',
  }
}

test('canonical execution type and options contain only Paper and Live', () => {
  expectTypeOf<'PAPER'>().toMatchTypeOf<ExecutionMode>()
  expectTypeOf<'LIVE'>().toMatchTypeOf<ExecutionMode>()
  expectTypeOf<ExecutionMode>().not.toEqualTypeOf<string>()
  expect(EXECUTION_MODES).toEqual(['PAPER', 'LIVE'])
  expect(executionModeForGatewayMode('paper')).toBe('PAPER')
  expect(executionModeForGatewayMode('live')).toBe('LIVE')
})

test('Paper and Live readiness are evaluated independently', () => {
  const sessions = [gateway('paper'), gateway('live')]
  expect(executionModeReadiness(sessions, 'PAPER', false)).toMatchObject({
    status: 'READY',
    readySessionCount: 1,
  })
  expect(executionModeReadiness(sessions, 'LIVE', false)).toMatchObject({
    status: 'NOT_READY',
    reason: 'Blocked by ALLOW_LIVE_TRADING',
  })
  expect(executionModeReadiness(sessions, 'LIVE', true)).toMatchObject({
    status: 'READY',
    readySessionCount: 1,
  })
})
