import {render, screen, within} from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import {MemoryRouter} from 'react-router-dom'

import type {StrategyLedgerRow} from '../src/api/types'
import {StrategyLedgerGrid, filterLedger} from '../src/features/strategies/StrategyLedger'
import {pnlCurve} from '../src/features/strategies/StrategyPerformance'
import {plotTime, priceTraces, snapToBar} from '../src/features/strategies/strategyPriceChart'

function row(overrides: Partial<StrategyLedgerRow>): StrategyLedgerRow {
  return {
    id: 'run:1', run_id: 1, time: '2026-09-03T10:00:00Z', evaluated_at: '2026-09-03T10:00:03Z',
    bar_close: '22050', spot: 22050, decision: 'NO_TRADE', signal_type: 'NO_ACTION', status: 'NO_ELIGIBLE_STRUCTURE',
    gate: 'IV / POP / EV / TQS', rps: '71.5', rps_threshold: '60', rps_passed: true,
    iv_score: 48, iv_score_threshold: '60', iv_passed: false, iv_rank: 31, iv_percentile: 40,
    tqs: '58.4', tqs_threshold: '65', tqs_passed: false,
    atr_support: 21750, atr_resistance: 22350, oi_support: null, oi_resistance: null,
    selected_strategy: null, candidates: [], orders: [], target_count: 0,
    reason: 'No structure passed the approved range/IV/breach entry gate',
    reason_details: ['IRON CONDOR: IV score 48.00 below 60.00', 'PUT CREDIT SPREAD: minimum POP not met', 'CALL CREDIT SPREAD: minimum POP not met'],
    ...overrides,
  }
}

const refused = row({})
const entered = row({
  id: 'run:2', run_id: 2, time: '2026-09-04T10:00:00Z', decision: 'ENTERED', signal_type: 'ENTER_OPTIONS', status: 'READY', gate: null,
  rps: '74.2', iv_score: 68, iv_passed: true, tqs: '72.5', tqs_passed: true, selected_strategy: 'IRON_CONDOR',
  atr_support: 21700, atr_resistance: 22300, spot: 22000,
  reason: 'Entered IRON CONDOR; RPS 74.20 ≥ 60.00; IV score 68.00 ≥ 60.00; TQS 72.50 ≥ 65.00',
  reason_details: ['RPS 74.2 selected IRON_CONDOR'],
  candidates: [{strategy_key: 'IRON_CONDOR', eligible: true, iv_score: 68, tqs: 72.5, pop: 0.71, expected_pnl: 1450}],
  orders: [{id: 'o-1', symbol: 'NIFTY-21700-P', side: 'SELL', quantity: '1', filled_quantity: '1', average_fill_price: '60', status: 'FILLED'}],
})
const failed = row({id: 'job:9', run_id: null, time: '2026-09-02T10:00:00Z', decision: 'NOT_EVALUATED', status: 'INFRASTRUCTURE_RETRYABLE', gate: null,
  rps: null, iv_score: null, tqs: null, rps_passed: null, iv_passed: null, tqs_passed: null, atr_support: null, atr_resistance: null,
  reason: 'Bar not evaluated: IBKR option chain timed out', reason_details: []})

function renderGrid(rows: StrategyLedgerRow[]) {
  return render(<MemoryRouter><StrategyLedgerGrid rows={rows} /></MemoryRouter>)
}

test('each ledger row shows RPS, IV score and TQS against their floors and exactly why', () => {
  renderGrid([refused, entered, failed])
  const grid = screen.getByRole('grid', {name: 'Strategy decision ledger'})
  const rows = within(grid).getAllByRole('row').slice(1)
  // Newest bar first.
  expect(rows[0]).toHaveTextContent('Trade entered')
  expect(rows[0]).toHaveTextContent('Entered IRON CONDOR; RPS 74.20 ≥ 60.00; IV score 68.00 ≥ 60.00; TQS 72.50 ≥ 65.00')
  expect(rows[0]).toHaveTextContent('IRON CONDOR')
  expect(rows[0]).toHaveTextContent('1 filled')
  expect(rows[0]).toHaveTextContent('spot inside band')

  expect(rows[1]).toHaveTextContent('No trade')
  expect(rows[1]).toHaveTextContent('IV / POP / EV / TQS')
  expect(rows[1]).toHaveTextContent('IRON CONDOR: IV score 48.00 below 60.00')
  expect(rows[1]).toHaveTextContent('+1 more')
  // The IV score failed its floor and TQS failed its floor; RPS cleared its.
  const failing = rows[1].querySelectorAll('.ledger-fail')
  expect([...failing].map((node) => node.textContent)).toEqual(['48', '58.4'])
  expect(rows[1].querySelector('.ledger-pass')).toHaveTextContent('71.5')

  expect(rows[2]).toHaveTextContent('Not evaluated')
  expect(rows[2]).toHaveTextContent('Bar not evaluated: IBKR option chain timed out')
  expect(screen.getByLabelText('Ledger summary')).toHaveTextContent('3 bars1 entries0 exits1 no trade1 not evaluated / errors')
})

test('ledger filters to trades and opens the full evidence for a row', async () => {
  const user = userEvent.setup()
  renderGrid([refused, entered, failed])
  await user.click(screen.getByRole('button', {name: 'Trades'}))
  const grid = screen.getByRole('grid', {name: 'Strategy decision ledger'})
  expect(within(grid).getAllByRole('row').slice(1)).toHaveLength(1)
  await user.click(within(grid).getByText(/Entered IRON CONDOR/))
  const drawer = screen.getByRole('dialog', {name: 'Trade entered'})
  expect(drawer).toHaveTextContent('Candidate structures')
  expect(drawer).toHaveTextContent('71%')
  expect(drawer).toHaveTextContent('Orders placed')
  expect(drawer).toHaveTextContent('NIFTY-21700-P')
})

test('ledger filter groups decisions', () => {
  expect(filterLedger([refused, entered, failed], 'no-trade').map((item) => item.id)).toEqual(['run:1'])
  expect(filterLedger([refused, entered, failed], 'problems').map((item) => item.id)).toEqual(['job:9'])
})

test('trade points land on the bar that contains the fill', () => {
  const bars = ['2026-09-04T10:00:00Z', '2026-09-04T10:05:00Z', '2026-09-04T10:10:00Z'].map((time) => new Date(time).getTime())
  expect(snapToBar('2026-09-04T10:02:00Z', bars)).toBe('2026-09-04T10:05:00.000Z')
  expect(snapToBar('2026-09-04T11:00:00Z', bars)).toBe('2026-09-04T10:10:00.000Z')
})

test('the price chart draws the ATR range, OI walls and trades like the backtest audit', () => {
  const bar = (time: string, close: number) => ({time, open: close, high: close + 5, low: close - 5, close, volume: 1, version: 1})
  const traces = priceTraces({
    bars: [bar('2026-09-04T10:00:00Z', 22000), bar('2026-09-04T10:05:00Z', 22040)], indicators: [], markers: [], source: 'x',
    atr_bands: [{time: '2026-09-04T10:00:00Z', support: '21700', resistance: '22300'}],
    trade_markers: [
      {time: '2026-09-04T10:02:00Z', type: 'ENTRY', status: 'FILLED', label: 'Entry · 3 fills'},
      {time: '2026-09-04T10:04:00Z', type: 'EXIT', status: 'FILLED', label: 'Exit · 1 fill'},
    ],
  }, [
    row({id: 'run:2', time: '2026-09-04T10:05:00Z', decision: 'ENTERED', spot: 22040, oi_support: 21800, oi_resistance: 22200, selected_strategy: 'IRON_CONDOR', rps: 0.71,
      candidates: [{strategy_key: 'IRON_CONDOR', eligible: true, pop: 0.64, expected_pnl: 120, max_loss: 900}]}),
    row({id: 'run:0', time: '2026-09-03T10:00:00Z', oi_support: 1, oi_resistance: 2}),
  ])
  const byName = Object.fromEntries(traces.map((trace) => [trace.name, trace])) as Record<string, Record<string, unknown>>
  expect(Object.keys(byName)).toEqual(['Underlying OHLC', 'ATR support', 'ATR resistance', 'Put OI wall', 'Call OI wall', 'Entry fill', 'Exit fill', 'RPS + POP entry'])
  expect(byName['ATR support'].line).toEqual({color: '#30d79b', width: 2, shape: 'hv'})
  expect(byName['ATR resistance'].y).toEqual([22300])
  // Ledger rows before the first bar would stretch the time axis; they are left out.
  expect(byName['Put OI wall'].y).toEqual([21800])
  expect(byName['Entry fill'].y).toEqual([22040])
  expect(byName['Entry fill'].x).toEqual([plotTime('2026-09-04T10:05:00Z')])
  expect(String((byName['RPS + POP entry'].text as string[])[0])).toContain('IRON CONDOR<br>RPS 0.71 · POP 64%')
  expect(byName['Exit fill'].mode).toBe('markers')
})

test('the running P&L curve ends on the headline figure', () => {
  const series = [
    {time: '2026-09-25T14:50:00Z', realized: '0', unrealized: '0', total: '-50', mark_basis: 'LAST_FILL'},
    {time: '2026-09-25T14:57:06Z', realized: '-396.83', unrealized: '0', total: '-396.83', mark_basis: 'LAST_FILL'},
  ]
  const pnl = {running_pnl: '-484.58', as_of: '2026-09-25T15:00:00Z', series} as unknown as Parameters<typeof pnlCurve>[0]
  expect(pnlCurve(pnl)[0].data.at(-1)).toEqual({time: '2026-09-25T15:00:00.000Z', value: -484.58})
  expect(pnlCurve({...pnl, as_of: '2026-09-25T14:57:06.400Z'})[0].data).toHaveLength(2)
})

test('trades come from the ledger orders, not from fill times that lie past the last bar', () => {
  const bar = (time: string, close: number) => ({time, open: close, high: close + 5, low: close - 5, close, volume: 1, version: 1})
  const order = (side: string, symbol: string) => ({id: `${side}${symbol}`, symbol, side, quantity: 65, filled_quantity: 65, average_fill_price: 87.25, status: 'FILLED'})
  // As the live ledger has it: an entry row carries the two opening legs and
  // the two closing legs the execution layer sent before the next decision.
  const roundTrip = [order('BUY', 'NIFTY26SEP23200CE'), order('SELL', 'NIFTY26SEP23150CE'), order('SELL', 'NIFTY26SEP23200CE'), order('BUY', 'NIFTY26SEP23150CE')]
  const traces = priceTraces({
    bars: ['09:31', '09:32', '09:33', '09:34'].map((minute, index) => bar(`2026-09-25T${minute}:00Z`, 23110 + index * 10)), indicators: [], markers: [], source: 'x',
    trade_markers: [{time: '2026-09-25T14:47:40Z', type: 'ENTRY', status: 'FILLED', label: 'Entry · 3 fills'}],
  }, [
    row({id: 'run:1', time: '2026-09-25T09:31:00Z', decision: 'ENTERED', orders: roundTrip}),
    row({id: 'run:2', time: '2026-09-25T09:32:00Z', decision: 'HOLDING', orders: []}),
    row({id: 'run:3', time: '2026-09-25T09:33:00Z', decision: 'ENTERED', orders: roundTrip.slice(0, 2)}),
    row({id: 'run:4', time: '2026-09-25T09:34:00Z', decision: 'HOLDING', orders: []}),
  ])
  const byName = Object.fromEntries(traces.map((trace) => [trace.name, trace])) as Record<string, Record<string, unknown>>
  expect(byName['RPS + POP entry'].x).toEqual([plotTime('2026-09-25T09:31:00Z'), plotTime('2026-09-25T09:33:00Z')])
  // Closed after the bar that still held it; the second position is still open.
  expect(byName['Exit fill'].x).toEqual([plotTime('2026-09-25T09:32:00Z')])
  const [entryY] = byName['RPS + POP entry'].y as number[]
  const [exitY] = byName['Exit fill'].y as number[]
  expect(entryY).toBeLessThan(23105)
  expect(exitY).toBeGreaterThan(23125)
  const entryText = String((byName['RPS + POP entry'].text as string[])[0])
  expect(entryText).toContain('BUY 65 NIFTY26SEP23200CE @ 87.25')
  expect(entryText).not.toContain('SELL 65 NIFTY26SEP23200CE')
  expect(String((byName['Exit fill'].text as string[])[0])).toContain('SELL 65 NIFTY26SEP23200CE')
  expect(byName['Entry fill']).toBeUndefined()
})

test('without order attribution a run of entry signals is drawn once', () => {
  const bar = (time: string) => ({time, open: 1, high: 2, low: 0, close: 1, volume: 1, version: 1})
  const traces = priceTraces({bars: ['09:31', '09:32', '09:33'].map((minute) => bar(`2026-09-25T${minute}:00Z`)), indicators: [], markers: [], source: 'x'},
    ['09:31', '09:32', '09:33'].map((minute, index) => row({id: `run:${index}`, time: `2026-09-25T${minute}:00Z`, decision: 'ENTERED', orders: []})))
  expect(traces.find((trace) => trace.name === 'RPS + POP entry')?.x).toEqual([plotTime('2026-09-25T09:31:00Z')])
})
