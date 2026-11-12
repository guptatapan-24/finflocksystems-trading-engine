import type {ReactNode} from 'react'

export interface TerminalMetricProps {
  label: string
  value: ReactNode
  helper?: ReactNode
  icon?: ReactNode
  trend?: 'positive' | 'negative' | 'neutral'
}

export function TerminalMetric({label, value, helper, icon, trend = 'neutral'}: TerminalMetricProps) {
  return <article className="metric-card terminal-metric">
    <div className="metric-card-label">{label}{icon && <span className="metric-card-icon">{icon}</span>}</div>
    <div className={`metric-card-value metric-${trend}`}>{value}</div>
    {helper && <div className="metric-card-helper">{helper}</div>}
  </article>
}
