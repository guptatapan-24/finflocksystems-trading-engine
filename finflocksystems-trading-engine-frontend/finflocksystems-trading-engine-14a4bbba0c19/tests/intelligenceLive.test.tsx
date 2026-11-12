import {QueryClient, QueryClientProvider} from '@tanstack/react-query'
import {render, screen, waitFor, within} from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import {MemoryRouter, Route, Routes} from 'react-router-dom'
import {afterEach, expect, test, vi} from 'vitest'
import {LivePage} from '../src/features/intelligence/LivePage'
import {ReportPage} from '../src/features/intelligence/ReportPage'
import {SessionsPage} from '../src/features/intelligence/SessionsPage'

const episode = {
  id: 9, strategy_key: 'RPS_OPTIONS_AUTO_SELECTOR', instance_id: 5, instance_name: 'NIFTY condor', strategy_version: 1,
  instrument: 'NIFTY', asset_family: 'OPTION', execution_mode: 'PAPER', currency: 'INR', direction: 'NET_CREDIT', status: 'OPEN',
  opened_at: '2026-10-02T09:30:00Z', closed_at: null, gross_pnl: '0', commissions: '8', net_pnl: '-8', pnl_basis: 'FILLS',
  return_on_risk: null, hold_seconds: null, exit_reason: '', exit_subtype: '', data_quality_flags: [], fill_count: 4,
}
const latest = {
  id: 1, observed_at: '2026-10-02T10:00:00Z', quote_freshness: 'FRESH', marks: {}, mark_pnl: '252', pnl_bps: '12', pct_of_max_loss: '13.62',
  pct_of_max_profit: '18.00', mae_bps_so_far: '3', mfe_bps_so_far: '20', material: false,
  distances: {iv_source: 'EVALUATION_TIME_ONLY', dte: 7, breakeven_upper: 24524, breakeven_lower: 21976,
    short_strikes: [{symbol: 'NIFTY-24500-C', strike: 24500, right: 'C', distance_pct: 0.41}]},
}
const alert = {id: 3, episode_id: 9, rule_id: 'SHORT_STRIKE_PROXIMITY', severity: 'MEDIUM', text: 'The underlying is 0.41% from the short C strike 24500.', occurrences: 2, first_seen: '2026-10-02T10:00:00Z', last_seen: '2026-10-02T10:01:00Z'}

type Call = {path: string; method: string; headers: Headers}
let calls: Call[] = []

function mockApi(extra?: (path: string, method: string) => Response | undefined) {
  calls = []
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    const path = new URL(input, 'http://localhost').pathname
    const method = init?.method ?? 'GET'
    calls.push({path, method, headers: new Headers(init?.headers)})
    const custom = extra?.(path, method)
    if (custom) return custom
    const ok = (data: unknown) => new Response(JSON.stringify({ok: true, data, error: null, meta: {}}))
    if (path.endsWith('/live-commentary')) return ok({commentary: 'The open mark P&L is 252 and the underlying is far from the strikes.', watch_items: ['Watch the short call.'], label: 'Model commentary on the live numbers. It is not an alert and not advice.', snapshot_id: 1, model: 'm'})
    if (path.endsWith('/live/open-episodes')) return ok([{...episode, latest, alerts: {total: 1, high: 0}}])
    if (/episodes\/\d+\/live$/.test(path)) return ok({episode, latest, series: [{t: 'a', mark_pnl: '10'}, {t: 'b', mark_pnl: '252'}], alerts: [alert], notes: ['Implied volatility is only measured when the strategy evaluates.'], is_open: true})
    if (/report$/.test(path)) return ok({episode_id: 9, title: 'NIFTY net credit trade report', sections: [
      {id: 'header', title: 'Trade', blocks: [{type: 'kv', rows: [{label: 'Net P&L', value: '829 INR'}]}]},
      {id: 'parameters', title: 'Parameters used', blocks: [{type: 'list', items: ['RPS vs threshold: margin +1 (61 against 60)']}]},
      {id: 'suggestions', title: 'Suggestions', blocks: [{type: 'table', columns: ['Id', 'Kind'], rows: [[7, 'PARAMETER']]}]}]})
    return ok([])
  }))
}

function wrap(node: React.ReactNode, route = '/') {
  const client = new QueryClient({defaultOptions: {queries: {retry: false}}})
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[route]}>{node}</MemoryRouter></QueryClientProvider>)
}

afterEach(() => vi.unstubAllGlobals())

test('live page shows open trades, deterministic numbers, alerts and the IV caveat', async () => {
  mockApi()
  wrap(<LivePage />)
  expect(await screen.findByText('NIFTY condor')).toBeInTheDocument()
  expect(await screen.findByText(/The underlying is 0.41% from the short C strike 24500./)).toBeInTheDocument()
  expect(await screen.findByText('Call 24500: 0.41% away')).toBeInTheDocument()
  expect(screen.getByText(/Implied volatility is only measured/)).toBeInTheDocument()
  expect(screen.getByLabelText('Open mark P&L over time')).toBeInTheDocument()
  expect(screen.getByText('18.00% / 13.62%')).toBeInTheDocument()
})

test('commentary is on demand, idempotent and clearly labelled', async () => {
  mockApi()
  const user = userEvent.setup()
  wrap(<LivePage />)
  await user.click(await screen.findByRole('button', {name: 'Explain these numbers'}))
  expect(await screen.findByText(/not an alert and not advice/)).toBeInTheDocument()
  const post = calls.find((c) => c.method === 'POST')!
  expect(post.path).toContain('/live-commentary')
  expect(post.headers.get('Idempotency-Key')).toBeTruthy()
})

test('a non-staff user sees a clear message on the live page', async () => {
  mockApi((path) => path.endsWith('/live/open-episodes') ? new Response(JSON.stringify({ok: false, data: null, error: {code: 'ADMIN_REQUIRED', message: 'staff', details: {}}, meta: {}}), {status: 403}) : undefined)
  wrap(<LivePage />)
  await waitFor(() => expect(screen.getByText('Staff sign-in required')).toBeInTheDocument())
})

test('the report page renders every block type and offers a markdown download', async () => {
  mockApi()
  wrap(<Routes><Route path="/intelligence/episodes/:episodeId/report" element={<ReportPage />} /></Routes>, '/intelligence/episodes/9/report')
  expect(await screen.findByText('NIFTY net credit trade report')).toBeInTheDocument()
  expect(screen.getByText('829 INR')).toBeInTheDocument()
  expect(screen.getByText(/margin \+1 \(61 against 60\)/)).toBeInTheDocument()
  const table = screen.getByRole('table')
  expect(within(table).getByText('PARAMETER')).toBeInTheDocument()
  expect(screen.getByRole('button', {name: 'Download Markdown'})).toBeEnabled()
})

test('the report page downloads PDF and HTML through the server, keeps the server file name, and shows the frozen hash', async () => {
  const requested: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    const url = new URL(input, 'http://localhost')
    if (/report$/.test(url.pathname) && url.searchParams.get('format')) {
      requested.push(url.searchParams.get('format') as string)
      return new Response('%PDF-1.4 test', {headers: {'Content-Type': 'application/pdf', 'Content-Disposition': 'attachment; filename="finflock_SMA_CROSSOVER_AAPL_episode9_20261023.pdf"'}})
    }
    if (/report$/.test(url.pathname)) return new Response(JSON.stringify({ok: true, error: null, meta: {}, data: {
      episode_id: 9, title: 'AAPL net long trade report', report_version: '2', frozen: true, content_hash: 'ab'.repeat(32), artifact_id: 1,
      sections: [{id: 'selection', title: 'What you selected', blocks: [{type: 'kv', rows: [{label: 'Risk profile', value: 'NEUTRAL'}]}]}]}}))
    return new Response(JSON.stringify({ok: true, error: null, meta: {}, data: []}))
  }))
  const clicks: string[] = []
  const create = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:test')
  vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
  const anchor = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) { clicks.push(this.download) })
  wrap(<Routes><Route path="/intelligence/episodes/:episodeId/report" element={<ReportPage />} /></Routes>, '/intelligence/episodes/9/report')
  expect(await screen.findByText('What you selected')).toBeInTheDocument()
  expect(screen.getByLabelText('Report provenance')).toHaveTextContent('Frozen report · version 2 · content hash abababababababab')
  await userEvent.click(screen.getByRole('button', {name: 'Download PDF'}))
  await userEvent.click(screen.getByRole('button', {name: 'Download HTML'}))
  await waitFor(() => expect(clicks).toHaveLength(2))
  expect(requested).toEqual(['pdf', 'html'])
  expect(clicks[0]).toBe('finflock_SMA_CROSSOVER_AAPL_episode9_20261023.pdf')
  create.mockRestore()
  anchor.mockRestore()
})

test('a failed download shows the server message instead of saving an error page', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    const url = new URL(input, 'http://localhost')
    if (/report$/.test(url.pathname) && url.searchParams.get('format')) {
      return new Response(JSON.stringify({ok: false, data: null, error: {code: 'REPORT_PDF_UNAVAILABLE', message: 'The PDF library is not installed on this server'}, meta: {}}), {status: 501})
    }
    return new Response(JSON.stringify({ok: true, error: null, meta: {}, data: {episode_id: 9, title: 'T', sections: []}}))
  }))
  const save = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
  wrap(<Routes><Route path="/intelligence/episodes/:episodeId/report" element={<ReportPage />} /></Routes>, '/intelligence/episodes/9/report')
  await userEvent.click(await screen.findByRole('button', {name: 'Download PDF'}))
  expect(await screen.findByText(/The PDF library is not installed on this server/)).toBeInTheDocument()
  expect(save).not.toHaveBeenCalled()
  save.mockRestore()
})

const dailyReport = {
  scope: 'daily', date: '2026-10-14', title: 'Intelligence Report - 14 October 2026', report_version: 'D1', content_hash: 'cd'.repeat(32), frozen: false, artifact_id: null,
  sections: [
    {id: 'xnse-headline', title: 'NSE: headline (INR only)', blocks: [{type: 'kv', rows: [{label: 'Net P&L after costs', value: '829.00 INR'}]}]},
    {id: 'xnse-not-taken', title: 'NSE: decisions not taken', blocks: [{type: 'table', columns: ['Strategy', 'Closest to trading'], rows: [['NIFTY condor', 'RPS 59 vs 60 (-1.0) at 10:30:00']]}]},
  ],
}

function mockSessions(captured: string[]) {
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    const url = new URL(input, 'http://localhost')
    captured.push(`${url.pathname}${url.search}`)
    if (/reports\/daily$/.test(url.pathname) && url.searchParams.get('format')) {
      return new Response('%PDF-1.4 test', {headers: {'Content-Type': 'application/pdf', 'Content-Disposition': 'attachment; filename="finflock_intelligence_report_2026-10-14.pdf"'}})
    }
    if (/reports\/daily$/.test(url.pathname)) return new Response(JSON.stringify({ok: true, error: null, meta: {}, data: dailyReport}))
    if (/sessions$/.test(url.pathname)) return new Response(JSON.stringify({ok: true, error: null, meta: {total: 1}, data: [
      {zone: 'XNSE', session_date: '2026-10-14', status: 'FINAL', currency: 'INR', headline: {closed_trades: 1, net_pnl: '829.00', currency: 'INR', decisions_total: 5, open_at_asof: 0}, narrative_status: 'OK'}]}))
    return new Response(JSON.stringify({ok: true, error: null, meta: {}, data: []}))
  }))
}

test('the sessions page shows the daily report, the live-view notice and stored sessions', async () => {
  const captured: string[] = []
  mockSessions(captured)
  wrap(<SessionsPage />)
  expect(await screen.findByText('Intelligence Report - 14 October 2026')).toBeInTheDocument()
  expect(screen.getByText('829.00 INR')).toBeInTheDocument()
  expect(screen.getByText('RPS 59 vs 60 (-1.0) at 10:30:00')).toBeInTheDocument()
  expect(screen.getByLabelText('Report provenance')).toHaveTextContent('Live view (not frozen')
  expect(screen.getByLabelText('Report provenance')).toHaveTextContent('content hash cdcdcdcdcdcdcdcd')
  expect(await screen.findByLabelText('Stored sessions')).toHaveTextContent('XNSE 2026-10-14 (final)')
  expect(screen.getByRole('tab', {name: 'Sessions'})).toBeInTheDocument()
})

test('choosing an exchange asks the server for exactly that report and downloads the PDF under the server file name', async () => {
  const captured: string[] = []
  mockSessions(captured)
  const clicks: string[] = []
  vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:test')
  vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
  const anchor = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) { clicks.push(this.download) })
  wrap(<SessionsPage />)
  await screen.findByText('Intelligence Report - 14 October 2026')
  await userEvent.selectOptions(screen.getByLabelText('Exchange'), 'XNSE')
  await waitFor(() => expect(captured.some((c) => c.includes('zone=XNSE') && !c.includes('format='))).toBe(true))
  await userEvent.click(screen.getByRole('button', {name: 'Download PDF'}))
  await waitFor(() => expect(clicks).toEqual(['finflock_intelligence_report_2026-10-14.pdf']))
  expect(captured.some((c) => c.includes('format=pdf') && c.includes('zone=XNSE'))).toBe(true)
  anchor.mockRestore()
})

test('a failed session report download shows the server message', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    const url = new URL(input, 'http://localhost')
    if (/reports\/daily$/.test(url.pathname) && url.searchParams.get('format')) {
      return new Response(JSON.stringify({ok: false, data: null, error: {code: 'REPORT_PDF_DISABLED', message: 'PDF export is switched off'}, meta: {}}), {status: 501})
    }
    if (/reports\/daily$/.test(url.pathname)) return new Response(JSON.stringify({ok: true, error: null, meta: {}, data: dailyReport}))
    return new Response(JSON.stringify({ok: true, error: null, meta: {}, data: []}))
  }))
  const save = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
  wrap(<SessionsPage />)
  await userEvent.click(await screen.findByRole('button', {name: 'Download PDF'}))
  expect(await screen.findByText(/PDF export is switched off/)).toBeInTheDocument()
  expect(save).not.toHaveBeenCalled()
  save.mockRestore()
})
