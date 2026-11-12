import type {StrategyChartData, StrategyLedgerOrder, StrategyLedgerRow} from '../../api/types'
import type {PlotTrace} from '../../components/charts/PlotlyChart'
import {formatNumber, formatPercent} from '../../components/ui'

// Plotly trace builders for the strategy price chart, drawn the same way as the
// options-research backtest's "causal range audit": candles, the frozen daily
// ATR range as solid step lines, the OI walls, and trades as hover-only points.

export const MARKER_COLORS = {ENTRY: '#7fa58c', EXIT: '#a17176', PENDING: '#aa9b76'}

/** Plotly draws date strings as wall-clock time, so hand it the viewer's
 * local time rather than a UTC ISO string. */
export function plotTime(value: string) {
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return value
  const pad = (number: number) => String(number).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

const number = (value: unknown) => value === null || value === undefined || value === '' ? null : Number(value)

/** Place a point on the bar that contains it: bars are stamped with their
 * window end, so that is the first bar ending at or after the event. */
export function snapToBar(time: string, barTimes: number[]) {
  const target = new Date(time).getTime()
  if (!barTimes.length || !Number.isFinite(target)) return time
  const match = barTimes.find((barTime) => barTime >= target) ?? barTimes[barTimes.length - 1]
  return new Date(match).toISOString()
}

const filledOrders = (row: StrategyLedgerRow) => row.orders.filter((order) => Number(order.filled_quantity) > 0)
const orderLine = (order: StrategyLedgerOrder) => `${order.side} ${formatNumber(order.filled_quantity)} ${order.symbol} @ ${formatNumber(order.average_fill_price, 2)}`
const TRADE_DECISIONS = new Set(['ENTERED', 'EXITED', 'TARGET_SET'])

function entryText(row: StrategyLedgerRow, opening: StrategyLedgerOrder[]) {
  const chosen = row.candidates.find((item) => item.strategy_key === row.selected_strategy)
  return [
    row.selected_strategy?.replaceAll('_', ' ') || 'Entry',
    `RPS ${formatNumber(row.rps ?? undefined, 2)} · POP ${chosen?.pop == null ? '—' : formatPercent(chosen.pop)}`,
    `EV ${formatNumber(chosen?.expected_pnl ?? undefined, 2)} · max loss ${formatNumber(chosen?.max_loss ?? undefined, 2)}`,
    ...opening.map(orderLine),
  ].join('<br>')
}

interface LedgerTrade {row: StrategyLedgerRow; orders: StrategyLedgerOrder[]}

/** Entries and exits from the orders the ledger attaches to each decision, in
 * order: an entry is the strategy leaving flat, an exit is it returning to
 * flat. Fill timestamps are not used, so a broker clock that disagrees with
 * the bars cannot move a trade off the candle it was decided on. */
export function ledgerTrades(rows: StrategyLedgerRow[]) {
  const position = new Map<string, number>()
  const flat = () => [...position.values()].every((quantity) => Math.abs(quantity) < 1e-9)
  const entries: LedgerTrade[] = []
  const exits: LedgerTrade[] = []
  rows.forEach((row, index) => {
    let entry: LedgerTrade | null = null
    let exit: LedgerTrade | null = null
    filledOrders(row).forEach((order) => {
      const before = position.get(order.symbol) ?? 0
      const after = before + (order.side === 'SELL' ? -1 : 1) * Number(order.filled_quantity)
      const wasFlat = flat()
      position.set(order.symbol, after)
      const opening = Math.abs(after) > Math.abs(before)
      if (wasFlat && !flat()) entries.push(entry = {row, orders: []})
      if (opening && entry) entry.orders.push(order)
      if (!opening) (exit ??= {row, orders: []}).orders.push(order)
      if (!wasFlat && flat() && exit) {
        // Closed inside an entry's window without an exit decision: the
        // execution layer exited after the last bar that still held it.
        if (row.decision !== 'EXITED' && entry) {
          const next = rows.findIndex((item, later) => later > index && TRADE_DECISIONS.has(item.decision))
          const held = rows.slice(index + 1, next === -1 ? undefined : next).filter((item) => item.decision === 'HOLDING').at(-1)
          if (held) exit.row = held
        }
        exits.push(exit)
        exit = null
      }
    })
  })
  return {entries, exits}
}

export function priceTraces(data: StrategyChartData, ledger: StrategyLedgerRow[] = []): PlotTrace[] {
  const bars = [...data.bars].sort((left, right) => Date.parse(left.time) - Date.parse(right.time))
  const barTimes = bars.map((bar) => Date.parse(bar.time))
  const closes = new Map(bars.map((bar) => [Date.parse(bar.time), Number(bar.close)]))
  const start = barTimes[0] ?? Number.NEGATIVE_INFINITY
  const rows = ledger.filter((row) => row.time && Date.parse(row.time) >= start)
    .sort((left, right) => Date.parse(left.time!) - Date.parse(right.time!))
  const bands = data.atr_bands || []
  // A decision sits on the candle it was made on, not on its own spot figure,
  // which is the frozen daily observation and would float off the candles.
  // Entries hang just under the candle and exits just over it, so neither
  // hides the bar they mark.
  const byTime = new Map(bars.map((bar) => [Date.parse(bar.time), bar]))
  // Sized off everything plotted: the ATR band usually sets the price range.
  const plotted = [
    ...bars.flatMap((bar) => [Number(bar.high), Number(bar.low)]),
    ...bands.flatMap((band) => [number(band.support), number(band.resistance)]),
    ...rows.flatMap((row) => [number(row.oi_support), number(row.oi_resistance)]),
  ].filter((value): value is number => value !== null && Number.isFinite(value))
  const gap = plotted.length ? (Math.max(...plotted) - Math.min(...plotted)) * 0.025 : 0
  const at = (row: StrategyLedgerRow, side: 'below' | 'above') => {
    const bar = byTime.get(Date.parse(row.time!))
    if (!bar) return number(row.bar_close) ?? number(row.spot)
    return side === 'below' ? Number(bar.low) - gap : Number(bar.high) + gap
  }

  // The ledger attaches every order to the decision that produced it, so a
  // trade is drawn on that decision's bar. Fill wall-clock times can lie past
  // the last candle (bars evaluated late), which would pile every trade onto it.
  const attributed = rows.some((row) => row.orders.length > 0)
  const traded = attributed ? ledgerTrades(rows) : null
  // Without order attribution, a run of entry signals is one entry.
  const entryTrades = traded?.entries
    ?? rows.filter((row, index) => row.decision === 'ENTERED' && rows[index - 1]?.decision !== 'ENTERED').map((row) => ({row, orders: []}))

  const trades = (attributed ? (data.trade_markers || []).filter((marker) => marker.status === 'PENDING') : data.trade_markers || []).map((marker) => {
    const time = snapToBar(marker.time, barTimes)
    return {...marker, x: plotTime(time), y: closes.get(Date.parse(time)) ?? null}
  })
  const tradeTrace = (name: string, color: string, items: {x: string; y: number | null; label: string}[]) => ({
    type: 'scatter', mode: 'markers', name,
    x: items.map((item) => item.x), y: items.map((item) => item.y), text: items.map((item) => item.label),
    marker: {size: 9, color},
    hovertemplate: '%{text}<br>%{x}<extra></extra>',
  })
  const exits = traded
    ? traded.exits.map(({row, orders}) => ({x: plotTime(row.time!), y: at(row, 'above'), label: ['Exit', ...orders.map(orderLine)].join('<br>')}))
    : trades.filter((item) => item.type === 'EXIT' && item.status === 'FILLED')

  return [
    {
      type: 'candlestick', name: 'Underlying OHLC',
      x: bars.map((bar) => plotTime(bar.time)),
      open: bars.map((bar) => Number(bar.open)), high: bars.map((bar) => Number(bar.high)),
      low: bars.map((bar) => Number(bar.low)), close: bars.map((bar) => Number(bar.close)),
      increasing: {line: {color: '#30d79b'}}, decreasing: {line: {color: '#d66b78'}},
    },
    {
      type: 'scatter', mode: 'lines', name: 'ATR support',
      x: bands.map((band) => plotTime(band.time)), y: bands.map((band) => number(band.support)),
      line: {color: '#30d79b', width: 2, shape: 'hv'}, connectgaps: false,
    },
    {
      type: 'scatter', mode: 'lines', name: 'ATR resistance',
      x: bands.map((band) => plotTime(band.time)), y: bands.map((band) => number(band.resistance)),
      line: {color: '#f6b84a', width: 2, shape: 'hv'}, connectgaps: false,
    },
    {
      type: 'scatter', mode: 'lines+markers', name: 'Put OI wall',
      x: rows.map((row) => plotTime(row.time!)), y: rows.map((row) => number(row.oi_support)),
      line: {color: '#56b9ff', width: 1, dash: 'dot'}, marker: {size: 4}, connectgaps: false,
    },
    {
      type: 'scatter', mode: 'lines+markers', name: 'Call OI wall',
      x: rows.map((row) => plotTime(row.time!)), y: rows.map((row) => number(row.oi_resistance)),
      line: {color: '#cc7dff', width: 1, dash: 'dot'}, marker: {size: 4}, connectgaps: false,
    },
    tradeTrace('Entry fill', MARKER_COLORS.ENTRY, attributed ? [] : trades.filter((item) => item.type === 'ENTRY' && item.status === 'FILLED')),
    {...tradeTrace('Exit fill', MARKER_COLORS.EXIT, exits), marker: {size: 10, color: MARKER_COLORS.EXIT, symbol: 'triangle-down', line: {color: '#151d24', width: 1}}},
    tradeTrace('Signal awaiting fill', MARKER_COLORS.PENDING, trades.filter((item) => item.status === 'PENDING')),
    // Last, so an entry is never hidden under a fill point on the same bar.
    {
      type: 'scatter', mode: 'markers', name: 'RPS + POP entry',
      x: entryTrades.map(({row}) => plotTime(row.time!)), y: entryTrades.map(({row}) => at(row, 'below')), text: entryTrades.map(({row, orders}) => entryText(row, orders)),
      marker: {size: 12, color: '#f6b84a', symbol: 'triangle-up', line: {color: '#151d24', width: 1}},
      hovertemplate: '%{text}<br>%{x}<extra></extra>',
    },
    // A line with no values would only add an empty legend entry.
  ].filter((trace) => trace.type === 'candlestick' || (trace.y as (number | null)[]).some((value) => value !== null))
}
