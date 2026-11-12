export type StatusTone = 'positive' | 'warning' | 'critical' | 'neutral' | 'info'

const STATUS_LABELS: Record<string, string> = {
  DISABLED: 'Disabled',
  ACTIVATING: 'Activating',
  SUBSCRIBING: 'Subscribing',
  WARMING_UP: 'Warming up',
  READY_WAITING_FOR_LIVE_BAR: 'Ready, waiting for live bar',
  BLOCKED: 'Blocked',
}

function inferTone(status: string): StatusTone {
  const value = status.toUpperCase()
  if (/CONNECTED|RECONCILED|HEALTHY|APPROVED|FILLED|COMPLETED|ACTIVE|FRESH|PAPER/.test(value)) return 'positive'
  if (/ERROR|REJECT|BLOCK|KILL|DISCONNECT|FAILED|CRITICAL|STALE/.test(value)) return 'critical'
  if (/PENDING|PARTIAL|WARM|UNKNOWN|DEGRADED|HELD|PAUSED|CANCEL|LIVE/.test(value)) return 'warning'
  if (/PREVIEW|INFO/.test(value)) return 'info'
  return 'neutral'
}

export function StatusBadge({status, tone}: {status?: string | null; tone?: StatusTone}) {
  const label = status || 'Unknown'
  const display = STATUS_LABELS[label.toUpperCase()] || label.replaceAll('_', ' ')
  return <span className={`status-badge status-${tone || inferTone(label)}`}><span aria-hidden="true" />{display}</span>
}

