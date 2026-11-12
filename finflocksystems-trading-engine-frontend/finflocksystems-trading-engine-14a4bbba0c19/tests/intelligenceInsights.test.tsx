import {QueryClient, QueryClientProvider} from '@tanstack/react-query'
import {render, screen, waitFor} from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import {MemoryRouter} from 'react-router-dom'
import {afterEach, expect, test, vi} from 'vitest'
import {AdvisoryBanner} from '../src/features/intelligence/AdvisoryBanner'
import {LearningPage} from '../src/features/intelligence/LearningPage'

type Call = {path: string; method: string; body: unknown; headers: Headers}
let calls: Call[] = []

function mockApi(routes: Record<string, unknown>, failing: string[] = []) {
  calls = []
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    const path = new URL(input, 'http://localhost').pathname
    const method = init?.method ?? 'GET'
    calls.push({path, method, body: init?.body ? JSON.parse(String(init.body)) : undefined, headers: new Headers(init?.headers)})
    if (failing.some((f) => path.endsWith(f))) return new Response(JSON.stringify({ok: false, data: null, error: {code: 'ADMIN_REQUIRED', message: 'staff', details: {}}, meta: {}}), {status: 403})
    const key = Object.keys(routes).find((k) => path.endsWith(k))
    return new Response(JSON.stringify({ok: true, data: key ? routes[key] : [], error: null, meta: {}}))
  }))
}

function wrap(node: React.ReactNode) {
  const client = new QueryClient({defaultOptions: {queries: {retry: false}}})
  return render(<QueryClientProvider client={client}><MemoryRouter>{node}</MemoryRouter></QueryClientProvider>)
}

afterEach(() => vi.unstubAllGlobals())

const advisory = {trigger: 'PREVIEW', severity: 'HIGH', summary: 's', lines: ['Established lesson (applies, n = 12): Very oversold entries lose', 'Track record, all instruments (USD): n = 8, 7 winning.'],
  lessons: [], requires_ack: false, note: 'Advice only. It never blocks creating, changing or activating a strategy.'}

test('advisory banner shows the lines with severity and says it can be ignored', async () => {
  mockApi({'/advisories/preview': advisory})
  wrap(<AdvisoryBanner strategyKey="SMA_CROSSOVER" parameters={{fast_window: 5}} instrument="AAPL" />)
  expect(await screen.findByText(/Established lesson \(applies, n = 12\)/)).toBeInTheDocument()
  expect(screen.getByText('HIGH')).toBeInTheDocument()
  expect(screen.getByText(/You can ignore it/)).toBeInTheDocument()
  const post = calls.find((c) => c.method === 'POST')!
  expect(post.body).toEqual({strategy_key: 'SMA_CROSSOVER', parameters: {fast_window: 5}, instrument: 'AAPL'})
})

test('advisory banner renders nothing when the API refuses (never gets in the way)', async () => {
  mockApi({}, ['/advisories/preview'])
  const view = wrap(<AdvisoryBanner strategyKey="SMA_CROSSOVER" parameters={{}} />)
  await waitFor(() => expect(calls.length).toBeGreaterThan(0))
  expect(view.container.querySelector('aside')).toBeNull()
})

test('weekly view lists reviews, labels rule-based ones and queues a review idempotently', async () => {
  mockApi({
    '/reviews': [{id: 1, strategy_key: 'SMA_CROSSOVER', verdict: 'Insufficient data', summary: 'Insufficient data: 3 closed trade(s).', confidence: null, is_fallback: false, sample_n: 3, model: '', created_at: '2026-10-05T10:00:00Z'},
                 {id: 2, strategy_key: 'DONCHIAN', verdict: 'x', summary: 'The model review was not available.', confidence: null, is_fallback: true, sample_n: 12, model: 'm', created_at: '2026-10-05T10:00:00Z'}],
    '/digest': {window_days: 7, closed_trades: {total: 0, per_currency: {}}, lessons: {created: [], promoted: [], retired_or_refuted: [], reinforced: 0, contradicted: 0, by_status: {}}, open_suggestions: 0, notes: []},
    '/weekly-review': {status: 'QUEUED'},
  })
  const user = userEvent.setup()
  wrap(<LearningPage />)
  await user.click(await screen.findByRole('tab', {name: 'Weekly'}))
  expect(await screen.findByText('Insufficient data: 3 closed trade(s).')).toBeInTheDocument()
  expect(screen.getByText('RULE-BASED')).toBeInTheDocument()
  await user.type(screen.getByLabelText('Strategy key'), 'sma_crossover')
  await user.click(screen.getByRole('button', {name: 'Review now'}))
  await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true))
  const post = calls.find((c) => c.method === 'POST')!
  expect(post.path).toContain('/strategies/SMA_CROSSOVER/weekly-review')
  expect(post.headers.get('Idempotency-Key')).toBeTruthy()
})

test('what-if view states that results are hypotheses and how many comparisons were made', async () => {
  mockApi({
    '/counterfactuals/options': {strategy_key: 'RPS', n_trades: 14, comparisons: 17, warnings: ['Hypotheses only. These filters are tested on trades that already passed the live gates, so they can only be tightened, never loosened.'],
      results: [{filter: 'rps_threshold', parameter: 'opt.entry_score', threshold: 66, n_kept: 8, n_removed: 6, mean_kept_bps: 150, mean_removed_bps: -200, delta_bps: 350, holds_after_correction: true, too_few: false}]},
    '/digest': {window_days: 7, closed_trades: {total: 0, per_currency: {}}, lessons: {created: [], promoted: [], retired_or_refuted: [], reinforced: 0, contradicted: 0, by_status: {}}, open_suggestions: 0, notes: []},
  })
  const user = userEvent.setup()
  wrap(<LearningPage />)
  await user.click(await screen.findByRole('tab', {name: 'What-if'}))
  await user.type(screen.getByLabelText('Strategy key'), 'rps')
  expect(await screen.findByText(/can only be tightened, never loosened/)).toBeInTheDocument()
  expect(screen.getByText(/17 filter settings were compared/)).toBeInTheDocument()
  expect(screen.getByText('8 / 6')).toBeInTheDocument()
})
