import {QueryClient, QueryClientProvider} from '@tanstack/react-query'
import {render, screen, waitFor, within} from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import {MemoryRouter} from 'react-router-dom'
import {afterEach, expect, test, vi} from 'vitest'
import {ReviewPanel} from '../src/features/intelligence/ReviewPanel'

const suggestion = (overrides: Record<string, unknown> = {}) => ({
  id: 7, review_id: 3, instance_id: 5, instance_name: 'AAPL SMA', kind: 'PARAMETER', parameter_key: 'fast_window',
  current_value: 20, proposed_value: 15, rationale: 'A faster average would have entered earlier.',
  expected_effect: 'Earlier entries.', risk_of_change: 'More whipsaws.', confidence: '0.3000', evidence_needed: 'More closed trades.',
  evidence_refs: ['params.fast_window'], is_hypothesis: true, status: 'PROPOSED', decided_by: '', decision_reason: '',
  applied_version_id: null, ...overrides,
})

const review = (overrides: Record<string, unknown> = {}) => ({
  id: 3, episode_id: 1, verdict: 'Rule followed; the outcome is one sample.', process_grade: 'B',
  outcome_class: 'GOOD_PROCESS_GOOD_OUTCOME', summary: 'The trade made 248 USD net.', confidence: '0.6000', is_fallback: false,
  prompt_version: 'trade_review:v1', model: 'gemini-x', sample_n: 1, strengths: [{description: 'Exit followed the rule.', evidence_refs: []}],
  open_questions: ['Is one trade enough?'], rejected_items: [], needs_rereview: false,
  mistakes: [{id: 1, category: 'COST_DRAG', severity: 'LOW', description: 'Costs were small.', evidence_refs: ['episode.commissions'], source: 'LLM', rule_id: '', is_defect: false},
             {id: 2, category: 'DATA_QUALITY', severity: 'MEDIUM', description: 'Delayed data.', evidence_refs: [], source: 'RULE', rule_id: 'PAPER_DELAYED_DATA_CAVEAT', is_defect: false}],
  suggestions: [suggestion()], ...overrides,
})

type Call = {path: string; method: string; headers: Headers; body: unknown}
let calls: Call[] = []

function mockApi(data: unknown, postResponse?: {status: number; body: unknown}) {
  calls = []
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    const path = new URL(input, 'http://localhost').pathname
    const method = init?.method ?? 'GET'
    calls.push({path, method, headers: new Headers(init?.headers), body: init?.body ? JSON.parse(String(init.body)) : undefined})
    if (method === 'POST') return new Response(JSON.stringify(postResponse?.body ?? {ok: true, data: {}, error: null, meta: {}}), {status: postResponse?.status ?? 200})
    if (data === null) return new Response(JSON.stringify({ok: false, data: null, error: {code: 'REVIEW_NOT_FOUND', message: 'none', details: {}}, meta: {}}), {status: 404})
    return new Response(JSON.stringify({ok: true, data, error: null, meta: {}}))
  }))
}

function renderPanel() {
  const client = new QueryClient({defaultOptions: {queries: {retry: false}}})
  return render(<QueryClientProvider client={client}><MemoryRouter><ReviewPanel episodeId={1} /></MemoryRouter></QueryClientProvider>)
}

afterEach(() => vi.unstubAllGlobals())

test('shows verdict, sample size, rule-based vs model findings, and a hypothesis suggestion', async () => {
  mockApi(review())
  renderPanel()
  expect(await screen.findByText('Rule followed; the outcome is one sample.')).toBeInTheDocument()
  expect(screen.getByText('n = 1 closed trade')).toBeInTheDocument()
  expect(screen.getByText(/outcome is mostly noise/)).toBeInTheDocument()
  expect(screen.getByText('RULE-BASED')).toBeInTheDocument()
  expect(screen.getByText('MODEL')).toBeInTheDocument()
  expect(screen.getByText('HYPOTHESIS')).toBeInTheDocument()
  expect(screen.getByText(/never enables or activates anything/)).toBeInTheDocument()
})

test('accepting a parameter suggestion asks for confirmation and sends an idempotent request', async () => {
  mockApi(review())
  const user = userEvent.setup()
  renderPanel()
  await user.click(await screen.findByRole('button', {name: 'Accept: create new version'}))
  const dialog = await screen.findByRole('dialog')
  expect(within(dialog).getByText(/never enables or activates it/)).toBeInTheDocument()
  expect(calls.filter((c) => c.method === 'POST')).toHaveLength(0)  // nothing is sent before confirming
  await user.click(within(dialog).getByRole('button', {name: 'Accept'}))
  await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true))
  const post = calls.find((c) => c.method === 'POST')!
  expect(post.path).toContain('/intelligence/suggestions/7/accept')
  expect(post.headers.get('Idempotency-Key')).toBeTruthy()
})

test('rejecting requires a reason', async () => {
  mockApi(review())
  const user = userEvent.setup()
  renderPanel()
  await user.click(await screen.findByRole('button', {name: 'Reject'}))
  const dialog = await screen.findByRole('dialog')
  expect(within(dialog).getByRole('button', {name: 'Reject'})).toBeDisabled()
  await user.type(within(dialog).getByLabelText('Reason'), 'not now')
  await user.click(within(dialog).getByRole('button', {name: 'Reject'}))
  await waitFor(() => expect(calls.find((c) => c.method === 'POST')?.body).toEqual({reason: 'not now'}))
})

test('a refused apply explains why and offers acknowledge instead', async () => {
  mockApi(review(), {status: 409, body: {ok: false, data: null, meta: {}, error: {code: 'INSTANCE_NOT_SAFE_TO_CHANGE', message: 'x', details: {reasons: ['INSTANCE_ENABLED', 'OPEN_EPISODE']}}}})
  const user = userEvent.setup()
  renderPanel()
  await user.click(await screen.findByRole('button', {name: 'Accept: create new version'}))
  await user.click(within(await screen.findByRole('dialog')).getByRole('button', {name: 'Accept'}))
  expect(await screen.findByText(/the strategy is enabled \(pause or disable it first\); a trade is still open/)).toBeInTheDocument()
  expect(screen.getByRole('button', {name: 'Acknowledge only'})).toBeInTheDocument()
})

test('a fallback review is labelled and a missing review is explained', async () => {
  mockApi(review({is_fallback: true, process_grade: '', suggestions: [], sample_n: 12}))
  const view = renderPanel()
  expect(await screen.findByText('Rule-based review only')).toBeInTheDocument()
  expect(screen.getByText('not graded')).toBeInTheDocument()
  view.unmount()
  mockApi(null)
  renderPanel()
  expect(await screen.findByText('No review yet')).toBeInTheDocument()
})

// ----------------------------------------------------------------------------------------- Phase 11
const experiment = (overrides: Record<string, unknown> = {}) => suggestion({
  id: 21, kind: 'EXPERIMENT', parameter_key: '', current_value: null, proposed_value: null, rationale: 'A scheduled event fell inside the holding period.',
  expected_effect: 'Net return is lower when a relevant event falls inside the hold.', risk_of_change: 'None until you act: this is a measurement plan, nothing is changed.',
  evidence_needed: '8 later trades that match the condition.',
  experiment: {title: 'Trades held through a scheduled market event', try: 'Say so in the intent note when you knowingly hold through an event.', metric: 'outcome.net_return_bps',
               direction: 'negative', min_n: 8, paper_only: true, stop_rule: 'Resolve after 8 matching trades close; stop after 90 days if fewer have.',
               history_check: {n_match: 6, n_other: 12, mean_match_bps: -60, mean_other_bps: 20, delta_bps: -80, p_value: 0.012,
                               verdict: 'SUGGESTIVE: matching trades averaged -60.0 bps against 20.0 bps (n = 6 vs 12). Not enough on its own.'}}, ...overrides,
})

test('measurement plans get their own panel with the history check, a stop rule and no accept-as-advice button', async () => {
  mockApi(review({suggestions: [suggestion(), experiment()]}))
  renderPanel()
  const panel = await waitFor(() => {
    const found = document.querySelector('[data-panel-id="intelligence-review-playbook"]')
    expect(found).not.toBeNull()
    return found as HTMLElement
  })
  expect(within(panel).getByText('Trades held through a scheduled market event')).toBeInTheDocument()
  expect(within(panel).getByText('8 (paper first)')).toBeInTheDocument()
  expect(within(panel).getByText(/SUGGESTIVE: matching trades averaged -60\.0 bps/)).toBeInTheDocument()
  expect(within(panel).getByText(/stop after 90 days/)).toBeInTheDocument()
  expect(within(panel).getByText('HYPOTHESIS')).toBeInTheDocument()
  expect(within(panel).queryByRole('button', {name: 'Accept as advice'})).toBeNull()
  expect(within(panel).getByRole('button', {name: 'Track this plan'})).toBeInTheDocument()
  expect(screen.getByRole('button', {name: 'Accept: create new version'})).toBeInTheDocument()  // the parameter suggestion is unchanged and stays separate
})

test('tracking a plan sends an acknowledge request and never an accept', async () => {
  mockApi(review({suggestions: [experiment()]}))
  const user = userEvent.setup()
  renderPanel()
  await user.click(await screen.findByRole('button', {name: 'Track this plan'}))
  const dialog = await screen.findByRole('dialog')
  await user.click(within(dialog).getByRole('button', {name: 'Acknowledge'}))
  await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true))
  const posts = calls.filter((c) => c.method === 'POST')
  expect(posts).toHaveLength(1)
  expect(posts[0].path).toBe('/api/v1/intelligence/suggestions/21/acknowledge')
  expect(posts[0].headers.get('Idempotency-Key')).toBeTruthy()
})

test('with only measurement plans the advice panel says there is no suggestion', async () => {
  mockApi(review({suggestions: [experiment()]}))
  renderPanel()
  expect(await screen.findByText('No suggestions')).toBeInTheDocument()
  expect(screen.getByText('Track this plan', {selector: 'button'})).toBeInTheDocument()
})
