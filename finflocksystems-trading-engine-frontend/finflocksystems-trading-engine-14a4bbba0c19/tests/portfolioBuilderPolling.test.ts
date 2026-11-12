import {pollConstruction} from '../src/features/portfolio-builder/PortfolioBuilderPage'
import type {PortfolioConstructionRun} from '../src/api/types'


const initial: PortfolioConstructionRun = {
  id: 900,
  plan_id: 1,
  portfolio_id: 10,
  status: 'QUEUED',
  application_status: 'NOT_APPLIED',
  retryable: false,
  last_error: '',
  attempt_count: 1,
  nav: 100000,
  final_target_weights: {},
  metrics: {},
  warnings: [],
  applied_at: null,
  created_at: '',
  started_at: null,
  completed_at: null,
}

afterEach(() => vi.unstubAllGlobals())


test('Portfolio Builder keeps polling durable processing beyond the former 60-second cutoff', async () => {
  let observations = 0
  vi.stubGlobal('fetch', vi.fn(async () => {
    observations += 1
    const completed = observations > 125
    return {
      ok: true,
      status: 200,
      json: async () => ({
        ok: true,
        data: {
          ...initial,
          status: completed ? 'COMPLETED' : 'CALCULATING',
          completed_at: completed ? '2026-07-30T10:03:00Z' : null,
        },
        error: null,
        meta: {},
      }),
    } as Response
  }))

  const result = await pollConstruction(initial, false, 0)
  expect(observations).toBe(126)
  expect(result.status).toBe('COMPLETED')
})
