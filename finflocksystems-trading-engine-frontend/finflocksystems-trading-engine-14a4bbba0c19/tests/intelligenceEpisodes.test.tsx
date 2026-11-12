import {QueryClient, QueryClientProvider} from '@tanstack/react-query'
import {render, screen, waitFor, within} from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import {MemoryRouter} from 'react-router-dom'
import {afterEach, expect, test, vi} from 'vitest'
import {EpisodesPage} from '../src/features/intelligence/EpisodesPage'

const episode = (overrides: Record<string, unknown> = {}) => ({
  id: 1, strategy_key: 'SMA_CROSSOVER', instance_id: 5, instance_name: 'AAPL SMA', strategy_version: 1, instrument: 'AAPL',
  asset_family: 'STOCK', execution_mode: 'PAPER', currency: 'USD', direction: 'LONG', status: 'CLOSED',
  opened_at: '2026-10-01T14:30:00Z', closed_at: '2026-10-03T14:30:00Z', gross_pnl: '250', commissions: '2', net_pnl: '248',
  pnl_basis: 'FILLS', return_on_risk: null, hold_seconds: 172800, exit_reason: 'SIGNAL', exit_subtype: '',
  data_quality_flags: ['PAPER_DELAYED_DATA'], fill_count: 2, ...overrides,
})

function mockApi(list: unknown[], status = 200) {
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    const path = new URL(input, 'http://localhost').pathname
    if (status !== 200) return new Response(JSON.stringify({ok: false, data: null, error: {code: 'ADMIN_REQUIRED', message: 'staff only', details: {}}, meta: {}}), {status})
    if (/review\/?$/.test(path)) return new Response(JSON.stringify({ok: false, data: null, error: {code: 'REVIEW_NOT_FOUND', message: 'none', details: {}}, meta: {}}), {status: 404})
    if (/episodes\/\d+\/?$/.test(path)) {
      return new Response(JSON.stringify({ok: true, error: null, meta: {}, data: {...episode(), metrics: {'outcome.net_pnl': 248, flags: ['X']}, metrics_version: '1', evidence_hash: 'abcdef1234567890', legs: [{symbol: 'AAPL', right: '', strike: null, expiration: null, entry_side: 'BUY', entry_quantity: '50', entry_avg_price: '200.05', exit_quantity: '50', exit_avg_price: '205.05', realized_pnl: '250', commission: '2', entry_slippage_bps: '2.5', exit_slippage_bps: null}]}}))
    }
    return new Response(JSON.stringify({ok: true, data: list, error: null, meta: {total: list.length}}))
  }))
}

function renderPage() {
  const client = new QueryClient({defaultOptions: {queries: {retry: false}}})
  return render(<QueryClientProvider client={client}><MemoryRouter><EpisodesPage /></MemoryRouter></QueryClientProvider>)
}

afterEach(() => vi.unstubAllGlobals())

test('shows episodes, a per-currency summary with n, and a detail drawer with flags and frozen evidence', async () => {
  mockApi([episode(), episode({id: 2, currency: 'INR', instrument: 'NIFTY', net_pnl: '-100', gross_pnl: '-90', commissions: '10'}), episode({id: 3, status: 'OPEN', net_pnl: '5', hold_seconds: null})])
  const user = userEvent.setup()
  renderPage()
  expect(await screen.findByText('Net P&L USD')).toBeInTheDocument()
  expect(screen.getByText('Net P&L INR')).toBeInTheDocument()
  expect(screen.getAllByText(/n = 1/).length).toBe(2)  // currencies are never merged; open episodes are excluded from stats
  expect(screen.getByText((_, node) => node?.tagName === 'SPAN' && /^5(\.00)? USD \(so far\)$/.test(node.textContent ?? ''))).toBeInTheDocument()
  await user.click(screen.getAllByText('AAPL SMA')[0])
  const dialog = await screen.findByRole('dialog')
  expect(await within(dialog).findByText('abcdef123456')).toBeInTheDocument()
  expect(within(dialog).getByText(/delayed market data/)).toBeInTheDocument()
  expect(within(dialog).getByText(/BUY 50 @ 200.05/)).toBeInTheDocument()
})

test('an empty list explains that few trades are not evidence', async () => {
  mockApi([])
  renderPage()
  expect(await screen.findByText('No closed episodes yet')).toBeInTheDocument()
  expect(screen.getByText(/not evidence of anything/)).toBeInTheDocument()
})

test('a non-staff user gets a clear message instead of a crash', async () => {
  mockApi([], 403)
  renderPage()
  await waitFor(() => expect(screen.getByText('Staff sign-in required')).toBeInTheDocument())
})
