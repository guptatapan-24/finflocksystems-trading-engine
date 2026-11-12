export interface LegendValue {
  time?: number
  open?: number
  high?: number
  low?: number
  close?: number
  volume?: number
  lines: {name: string; value: number}[]
}

function number(value?: number) {
  return value === undefined ? '—' : new Intl.NumberFormat(undefined, {maximumFractionDigits: 4}).format(value)
}

export function ChartLegend({value}: {value: LegendValue}) {
  return <div className="terminal-chart-legend" aria-live="polite">
    <span>{value.time ? new Date(value.time * 1000).toLocaleString() : 'Latest'}</span>
    {value.open !== undefined && <><span>O <strong>{number(value.open)}</strong></span><span>H <strong>{number(value.high)}</strong></span><span>L <strong>{number(value.low)}</strong></span><span>C <strong>{number(value.close)}</strong></span></>}
    {value.volume !== undefined && <span>VOL <strong>{number(value.volume)}</strong></span>}
    {value.lines.map((item) => <span key={item.name}>{item.name.toUpperCase()} <strong>{number(item.value)}</strong></span>)}
  </div>
}
