import {QueryClient, QueryClientProvider} from '@tanstack/react-query'
import {render, screen, waitFor, within} from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import {MemoryRouter} from 'react-router-dom'
import {afterEach, expect, test, vi} from 'vitest'
import {LearningPage, describeEvent, describePredicate} from '../src/features/intelligence/LearningPage'

const lesson = {
  id: 4, strategy_key: 'SMA_CROSSOVER', statement: 'Very oversold entries lose', status: 'HYPOTHESIS',
  predicate: {all: [{metric: 'entry.rsi', op: '<', value: 25}, {metric: 'regime.vol_pctile', op: '>', value: 80}]},
  expected_effect: {outcome_metric: 'outcome.net_return_bps', direction: 'negative'}, supporting_trades: 3, contradicting_trades: 1,
  n: 4, forward_trades: 0, posterior_mean: 0.67, effect_size_bps: -120.5, effect_ci_low: -300, effect_ci_high: 40,
  last_matched_at: null, created_at: '2026-10-01T10:00:00Z',
  evidence: [{episode_id: 1, verdict: 'SUPPORTS', outcome_value: -50, weight: 0, forward: false, origin: true},
             {episode_id: 2, verdict: 'SUPPORTS', outcome_value: -80, weight: 0.5, forward: false, origin: false}],
  note: 'The trade that proposed this lesson is shown with weight 0: it never counts as evidence for it.',
}

type Call = {path: string; method: string; headers: Headers; body: unknown}

function mockApi() {
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    const path = new URL(input, 'http://localhost').pathname
    const ok = (data: unknown) => new Response(JSON.stringify({ok: true, data, error: null, meta: {}}))
    if (/lessons\/\d+$/.test(path)) return ok(lesson)
    if (path.endsWith('/lessons')) return ok([lesson])
    if (path.endsWith('/learning-feed')) return ok([
      {id: 1, type: 'LESSON_CREATED', episode_id: 1, lesson_id: 4, payload: {statement: 'Very oversold entries lose'}, created_at: '2026-10-01T10:00:00Z'},
      {id: 2, type: 'LESSON_REINFORCED', episode_id: 2, lesson_id: 4, payload: {statement: 'Very oversold entries lose', n: 1, status: 'HYPOTHESIS'}, created_at: '2026-10-02T10:00:00Z'}])
    if (path.endsWith('/performance')) return ok([
      {strategy_key: 'SMA_CROSSOVER', strategy_version: null, instrument: '', regime: '', structure: '', mode: '', currency: 'USD', n: 8, wins: 7, mean_return_bps: 93.75, ci_low_bps: 40, ci_high_bps: 130, sum_net_pnl: '75.5'},
      {strategy_key: 'DONCHIAN', strategy_version: null, instrument: '', regime: '', structure: '', mode: '', currency: 'INR', n: 1, wins: 0, mean_return_bps: -20, ci_low_bps: null, ci_high_bps: null, sum_net_pnl: '-2'}])
    if (path.endsWith('/calibration')) return ok([{metric: 'POP', strategy_key: '', bin: 'ALL', n: 20, predicted_mean: 0.7, observed_rate: 0.4, brier: 0.29}])
    if (path.endsWith('/digest')) return ok({window_days: 7, closed_trades: {total: 2, per_currency: {USD: {n: 2, wins: 1, net_pnl: 5}}}, lessons: {created: [], promoted: [], retired_or_refuted: [], reinforced: 0, contradicted: 0, by_status: {HYPOTHESIS: 1}}, open_suggestions: 1, notes: ['Fewer than 8 trades closed in this window: results are noise, not evidence.']})
    return ok([])
  }))
}

function renderPage() {
  const client = new QueryClient({defaultOptions: {queries: {retry: false}}})
  return render(<QueryClientProvider client={client}><MemoryRouter><LearningPage /></MemoryRouter></QueryClientProvider>)
}

afterEach(() => vi.unstubAllGlobals())

test('describes predicates and events in plain language', () => {
  expect(describePredicate(lesson.predicate as never)).toBe('entry.rsi < 25 AND regime.vol_pctile > 80')
  expect(describeEvent({id: 1, type: 'LESSON_PROMOTED', episode_id: null, lesson_id: 1, created_at: '', payload: {statement: 'S', to: 'SUPPORTED', n: 9, posterior: 0.9}})).toContain('Promoted to SUPPORTED')
})

test('feed and digest are honest about small samples', async () => {
  mockApi()
  renderPage()
  expect(await screen.findByText(/results are noise, not evidence/)).toBeInTheDocument()
  expect(await screen.findByText(/New hypothesis to test: Very oversold entries lose/)).toBeInTheDocument()
  expect(screen.getByText(/A trade supported: Very oversold entries lose \(n = 1, now HYPOTHESIS\)/)).toBeInTheDocument()
})

test('lessons show status, n and the interval, and the drawer says the origin trade does not count', async () => {
  mockApi()
  const user = userEvent.setup()
  renderPage()
  await user.click(await screen.findByRole('tab', {name: 'Lessons'}))
  expect(await screen.findByText('n = 4 (3 for, 1 against)')).toBeInTheDocument()
  expect(screen.getByText('HYPOTHESIS', {selector: '.status-badge'})).toBeInTheDocument()
  expect(screen.getByText(/-120\.5 \(-300(\.0)? to 40(\.0)?\)/)).toBeInTheDocument()
  await user.click(screen.getByText('Very oversold entries lose'))
  const dialog = await screen.findByRole('dialog')
  expect(await within(dialog).findByText(/never counts as evidence/)).toBeInTheDocument()
  expect(within(dialog).getByText('#1 (proposed it)')).toBeInTheDocument()
  expect(within(dialog).getByText('does not count')).toBeInTheDocument()
})

test('performance keeps currencies apart and shows no interval for one trade; calibration warns about small bins', async () => {
  mockApi()
  const user = userEvent.setup()
  renderPage()
  await user.click(await screen.findByRole('tab', {name: 'Performance'}))
  expect(await screen.findByText('8 (7 winning)')).toBeInTheDocument()
  expect(screen.getByText('no interval (n < 2)')).toBeInTheDocument()
  expect(screen.getByText('75.5 USD')).toBeInTheDocument()
  expect(screen.getByText('-2 INR')).toBeInTheDocument()
  await user.click(screen.getByRole('tab', {name: 'Calibration'}))
  expect(await screen.findByText('All (Brier score)')).toBeInTheDocument()
  expect(screen.getByText(/never raise an alarm/)).toBeInTheDocument()
})

// ----------------------------------------------------------------------------------------- Phase 11
const experimentRow = (overrides: Record<string, unknown> = {}) => ({
  id: 7, review_id: 3, strategy_key: 'SMA_CROSSOVER', instance_name: 'AAPL SMA', status: 'PROPOSED',
  title: 'Trades held through a scheduled market event', try: 'Say so in the intent note when you knowingly hold through an event.',
  condition: 'ctx.event_in_hold == True', metric: 'outcome.net_return_bps', direction: 'negative', min_n: 8, paper_only: true,
  stop_rule: 'Resolve after 8 matching trades close; stop after 90 days if fewer have.', rule_id: 'EVENT_IN_HOLD_UNACKNOWLEDGED',
  expected_effect: 'Net return is lower when a relevant event falls inside the hold.',
  history_check: {n_match: 0, n_other: 5, mean_match_bps: null, mean_other_bps: 12, delta_bps: null, p_value: null, verdict: 'TOO FEW: 0 past trade(s) matched and 5 did not; no comparison is possible yet.'},
  progress: {matching_forward_trades: 2, needed: 8}, outcome: null, created_at: '2026-10-20T10:00:00Z', ...overrides,
})

const resolvedExperiment = experimentRow({
  id: 9, status: 'ACKNOWLEDGED', title: 'Entries during an unusual news burst', condition: 'ctx.news_burst_zscore >= 2', progress: {matching_forward_trades: 8, needed: 8},
  history_check: {n_match: 6, n_other: 12, mean_match_bps: -60, mean_other_bps: 20, delta_bps: -80, p_value: 0.012, verdict: 'SUGGESTIVE: matching trades averaged -60.0 bps against 20.0 bps (n = 6 vs 12).'},
  outcome: {result: 'HURT', n_condition: 8, n_rest: 15, delta_bps: -85, q_value: 0.012, reading: 'matching trades did worse'},
})

const changePoint = (overrides: Record<string, unknown> = {}) => ({
  strategy_key: 'SMA_CROSSOVER', currency: 'USD', metric: 'RETURN', status: 'EDGE_DECAY', n: 16, statistic: 11.3, threshold: 10, reference_n: 10,
  baseline_mean: 5, baseline_sd: 15.8, change_index: 11, change_at: '2026-10-18T10:00:00Z',
  note: 'Alarm at trade 16 of 16: the cumulative drift reached 11.3 standard deviations; it is estimated to have started at trade 11. A flag to look at the strategy, not proof.', ...overrides,
})

function mockPhase11(captured: Call[]) {
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    const path = new URL(input, 'http://localhost').pathname
    const method = init?.method ?? 'GET'
    captured.push({path, method, headers: new Headers(init?.headers), body: init?.body ? JSON.parse(String(init.body)) : undefined})
    const ok = (data: unknown) => new Response(JSON.stringify({ok: true, data, error: null, meta: {}}))
    if (method === 'POST') return ok({})
    if (path.endsWith('/experiments')) return ok([experimentRow(), resolvedExperiment])
    if (path.endsWith('/changepoints')) return ok([changePoint(), changePoint({strategy_key: 'DONCHIAN', status: 'STABLE', n: 22, statistic: 1.2, baseline_mean: 3, note: 'n = 22: no sustained drift.'})])
    if (path.endsWith('/lessons')) return ok([{...lesson, q_value: 0.034, p_value: 0.012}, {...lesson, id: 5, q_value: null, p_value: null}])
    if (path.endsWith('/performance')) return ok([
      {strategy_key: 'SMA_CROSSOVER', strategy_version: null, instrument: '', regime: '', structure: '', mode: '', risk_profile: 'NEUTRAL', context: '', currency: 'USD', n: 12, wins: 8,
       mean_return_bps: 40, ci_low_bps: 5, ci_high_bps: 75, shrunk_mean_bps: 31.5, shrunk_sd_bps: 9, shrinkage: 0.62, sum_net_pnl: '120'}])
    if (path.endsWith('/digest')) return ok({window_days: 7, closed_trades: {total: 2, per_currency: {USD: {n: 2, wins: 1, net_pnl: 5}}},
      lessons: {created: [], promoted: [], retired_or_refuted: [], reinforced: 0, contradicted: 0, by_status: {HYPOTHESIS: 1}}, stat_shifts: [], calibration_shifts: [],
      suggestion_outcomes: [], open_suggestions: 3, experiments_waiting: 2, change_points: [{strategy_key: 'SMA_CROSSOVER', metric: 'RETURN', status: 'EDGE_DECAY', n: 16}], notes: []})
    if (path.endsWith('/learning-feed')) return ok([])
    return ok([])
  }))
}

test('the experiments view lists measurement plans with their history, progress and results, and says nothing is changed', async () => {
  mockPhase11([])
  const user = userEvent.setup()
  renderPage()
  await user.click(await screen.findByRole('tab', {name: 'Experiments'}))
  expect(await screen.findByText(/nothing is changed/)).toBeInTheDocument()
  expect(screen.getByText('Trades held through a scheduled market event')).toBeInTheDocument()
  expect(screen.getByText('ctx.event_in_hold == True')).toBeInTheDocument()
  expect(screen.getByText('2 of 8')).toBeInTheDocument()
  expect(screen.getByText(/TOO FEW: 0 past trade\(s\) matched/)).toBeInTheDocument()
  expect(screen.getByText('8 of 8')).toBeInTheDocument()
  expect(screen.getByText(/Trades with the condition did worse \(n 8 vs 15, -85 bps\)/)).toBeInTheDocument()
  expect(screen.getAllByRole('button', {name: 'Acknowledge'})).toHaveLength(1)  // only the open plan can be decided
})

test('acknowledging a plan needs no reason, rejecting one does, and both send an idempotent request', async () => {
  const calls: Call[] = []
  mockPhase11(calls)
  const user = userEvent.setup()
  renderPage()
  await user.click(await screen.findByRole('tab', {name: 'Experiments'}))
  await user.click(await screen.findByRole('button', {name: 'Acknowledge'}))
  let dialog = await screen.findByRole('dialog')
  expect(within(dialog).getByText(/Nothing about the strategy changes/)).toBeInTheDocument()
  expect(calls.filter((c) => c.method === 'POST')).toHaveLength(0)  // nothing is sent before confirming
  await user.click(within(dialog).getByRole('button', {name: 'Acknowledge'}))
  await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path.endsWith('/suggestions/7/acknowledge')) ).toBe(true))
  expect(calls.find((c) => c.method === 'POST')!.headers.get('Idempotency-Key')).toBeTruthy()
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())

  await user.click(await screen.findByRole('button', {name: 'Reject'}))
  dialog = await screen.findByRole('dialog')
  expect(within(dialog).getByRole('button', {name: 'Reject'})).toBeDisabled()  // a reason is required for a rejection
  await user.type(within(dialog).getByLabelText('Reason'), 'not relevant for this strategy')
  await user.click(within(dialog).getByRole('button', {name: 'Reject'}))
  await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path.endsWith('/suggestions/7/reject'))).toBe(true))
  expect(calls.filter((c) => c.path.endsWith('/reject'))[0].body).toEqual({reason: 'not relevant for this strategy'})
})

test('the edge monitor shows status, n, baseline and drift, and is honest that an alarm is a flag and not a verdict', async () => {
  mockPhase11([])
  const user = userEvent.setup()
  renderPage()
  await user.click(await screen.findByRole('tab', {name: 'Edge monitor'}))
  const table = await screen.findByRole('table', {name: 'Edge monitor'})
  expect(within(table).getByText('EDGE DECAY')).toBeInTheDocument()
  expect(within(table).getByText('STABLE')).toBeInTheDocument()
  expect(within(table).getByText('5 bps (first 10 trades)')).toBeInTheDocument()
  expect(within(table).getByText('11.3 of 10')).toBeInTheDocument()
  expect(within(table).getByText(/estimated to have started at trade 11/)).toBeInTheDocument()
  expect(screen.getByText(/A flag to look at a strategy, not a verdict/)).toBeInTheDocument()
  expect(screen.getByText(/about 1 stable strategy in 20 within 50 trades/)).toBeInTheDocument()
})

test('the digest points at waiting experiments and edge alarms', async () => {
  mockPhase11([])
  renderPage()
  expect(await screen.findByText(/2 measurement plan\(s\) are waiting for you under Experiments/)).toBeInTheDocument()
  expect(screen.getByText(/Edge monitor: SMA_CROSSOVER Net return is edge decay \(n = 16\)/)).toBeInTheDocument()
})

test('performance shows the risk-profile slice and the pooled mean with how much of it is the slice\'s own data', async () => {
  mockPhase11([])
  const user = userEvent.setup()
  renderPage()
  await user.click(await screen.findByRole('tab', {name: 'Performance'}))
  expect(await screen.findByText(/neutral profile/)).toBeInTheDocument()
  expect(screen.getByText('31.5 (62% own data)')).toBeInTheDocument()
})

test('lessons show the corrected chance of luck, or say the test cannot be run yet', async () => {
  mockPhase11([])
  const user = userEvent.setup()
  renderPage()
  await user.click(await screen.findByRole('tab', {name: 'Lessons'}))
  expect(await screen.findByText('0.034')).toBeInTheDocument()
  expect(screen.getByText('not testable yet')).toBeInTheDocument()
})

test('the feed describes edge alarms and resolved experiments in plain language', () => {
  expect(describeEvent({id: 1, type: 'CHANGE_POINT', episode_id: null, lesson_id: null, created_at: '',
    payload: {strategy_key: 'SMA_CROSSOVER', metric: 'POP_CALIBRATION', status: 'EDGE_DECAY', n: 20, note: 'A flag to look at the strategy, not proof.'}}))
    .toBe('Edge monitor: SMA_CROSSOVER Probability calibration is edge decay (n = 20). A flag to look at the strategy, not proof.')
  expect(describeEvent({id: 2, type: 'SUGGESTION_RESOLVED', episode_id: null, lesson_id: null, created_at: '',
    payload: {experiment: 'News burst entries', result: 'HURT', n_after: 8, n_before: 15}})).toBe('Experiment "News burst entries" judged HURT (n with the condition 8, without 15)')
  expect(describeEvent({id: 3, type: 'SUGGESTION_RESOLVED', episode_id: null, lesson_id: null, created_at: '',
    payload: {parameter: 'fast_window', result: 'HELPED', n_after: 9, n_before: 4}})).toContain('Applied suggestion on fast_window judged HELPED')
})
