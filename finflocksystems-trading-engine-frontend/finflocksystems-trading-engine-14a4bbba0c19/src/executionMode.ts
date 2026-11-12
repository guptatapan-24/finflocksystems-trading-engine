import type {
  BrokerGatewaySession,
  BrokerSessionMode,
  ExecutionMode,
} from './api/types'

export const EXECUTION_MODES = ['PAPER', 'LIVE'] as const satisfies readonly ExecutionMode[]

export function executionModeForGatewayMode(mode?: BrokerSessionMode | null): ExecutionMode | null {
  if (mode === 'paper') return 'PAPER'
  if (mode === 'live') return 'LIVE'
  return null
}

export function executionModeForSession(
  session?: Pick<BrokerGatewaySession, 'mode'> | null,
): ExecutionMode | null {
  return executionModeForGatewayMode(session?.mode)
}

export function executionModeLabel(mode: ExecutionMode) {
  return mode === 'PAPER' ? 'Paper' : 'Live'
}

export interface ModeReadiness {
  mode: ExecutionMode
  ready: boolean
  status: 'READY' | 'NOT_READY'
  reason: string
  sessionCount: number
  readySessionCount: number
}

export function executionModeReadiness(
  sessions: BrokerGatewaySession[],
  mode: ExecutionMode,
  allowLiveTrading: boolean,
): ModeReadiness {
  const gatewayMode = mode.toLowerCase() as BrokerSessionMode
  const matching = sessions.filter((session) => session.mode === gatewayMode && !session.deleted_at)
  if (mode === 'LIVE' && !allowLiveTrading) {
    return {
      mode,
      ready: false,
      status: 'NOT_READY',
      reason: 'Blocked by ALLOW_LIVE_TRADING',
      sessionCount: matching.length,
      readySessionCount: 0,
    }
  }
  const ready = matching.filter((session) => {
    const healthMode = String(session.last_gateway_state.mode || '').toLowerCase()
    return session.status === 'CONNECTED'
      && session.connected
      && session.commands_enabled
      && healthMode === gatewayMode
      && session.last_gateway_state.connected === true
      && session.last_gateway_state.reconciled === true
  })
  return {
    mode,
    ready: ready.length > 0,
    status: ready.length > 0 ? 'READY' : 'NOT_READY',
    reason: ready.length
      ? `${ready.length} command-ready ${executionModeLabel(mode)} Gateway session${ready.length === 1 ? '' : 's'}`
      : matching.length
        ? `No ${executionModeLabel(mode)} Gateway session is connected, reconciled, health-matched, and command-ready`
        : `No ${executionModeLabel(mode)} Gateway session is configured`,
    sessionCount: matching.length,
    readySessionCount: ready.length,
  }
}
