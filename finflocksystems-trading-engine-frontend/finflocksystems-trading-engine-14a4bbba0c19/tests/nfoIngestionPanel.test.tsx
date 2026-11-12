import {QueryClient, QueryClientProvider} from '@tanstack/react-query'
import {render, screen} from '@testing-library/react'
import {MemoryRouter} from 'react-router-dom'
import {afterEach, expect, test, vi} from 'vitest'
import type {NfoIngestionStatus} from '../src/api/types'
import {formatNumber} from '../src/components/ui'
import {NfoIngestionPanel} from '../src/features/system/NfoIngestionPanel'

const ok: NfoIngestionStatus = {
  configured: true, available: true, state: 'UP_TO_DATE', message: 'Session 2026-11-04 is ingested and verified.',
  expected_session: '2026-11-04', latest_session: '2026-11-04', latest_completed_at: '2026-11-04T17:05:00Z',
  latest_rows: 812345, latest_source: 'x.zip', failure: null, checked_at: '2026-11-05T02:00:00Z',
  recent: [{session: '2026-11-04', state: 'COMPLETE', rows: 812345, completed_at: '2026-11-04T17:05:00Z', updated_at: null, error_category: null}],
}

function mount(body: unknown, status = 200) {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(
    status === 200 ? {ok: true, data: body, error: null, meta: {}} : {ok: false, data: null, error: {code: 'X', message: 'boom', details: {}}, meta: {}},
  ), {status, headers: {'Content-Type': 'application/json'}})))
  const client = new QueryClient({defaultOptions: {queries: {retry: false}}})
  return render(<QueryClientProvider client={client}><MemoryRouter><NfoIngestionPanel /></MemoryRouter></QueryClientProvider>)
}

afterEach(() => vi.unstubAllGlobals())

test('shows the ingested session, row count and state', async () => {
  mount(ok)
  expect(await screen.findByText('Up to date')).toBeInTheDocument()
  expect(screen.getByText('Session 2026-11-04 is ingested and verified.')).toBeInTheDocument()
  expect(screen.getAllByText('2026-11-04').length).toBeGreaterThan(0)
  expect(screen.getAllByText(formatNumber(812345, 0)).length).toBeGreaterThan(0)
  expect(screen.getByText('Ingested')).toBeInTheDocument()
})

test('shows what the last nightly run did', async () => {
  mount({...ok, last_run: {run_date: '2026-11-04', attempt: 2, status: 'FAILED', exit_code: 20, code: 'spot_unavailable', message: 'gateway not connected',
    sessions: [], failed: null, at: '2026-11-04T17:00:00Z'}})
  expect(await screen.findByText(/Last nightly run \(2026-11-04, attempt 2\): stopped \(spot_unavailable\)/)).toBeInTheDocument()
})

test('surfaces a failed night', async () => {
  mount({...ok, state: 'FAILED', message: 'Session 2026-11-05 failed to ingest; the last good session is 2026-11-04.',
    failure: {session: '2026-11-05', state: 'FAILED_RETRYABLE', error_category: 'TimeoutError', updated_at: null}})
  expect(await screen.findByText('Failed')).toBeInTheDocument()
  expect(screen.getByRole('alert')).toHaveTextContent('TimeoutError')
})

test('says so when the ledger cannot be read', async () => {
  mount({...ok, available: false, state: 'UNAVAILABLE', latest_session: null, recent: [], message: 'The ingestion ledger could not be read (OSError). Check the database connection.'})
  expect(await screen.findByText('Status unavailable')).toBeInTheDocument()
  expect(screen.getByRole('status')).toHaveTextContent('could not be read')
})

test('shows an error state when the backend call fails', async () => {
  mount(null, 500)
  expect(await screen.findByText('Ingestion status is unavailable')).toBeInTheDocument()
})
