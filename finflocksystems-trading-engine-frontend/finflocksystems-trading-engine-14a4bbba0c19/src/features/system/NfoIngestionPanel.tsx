import {useQuery} from '@tanstack/react-query'
import {queries} from '../../api/queries'
import type {NfoIngestionSession, NfoIngestionState} from '../../api/types'
import {DataTable, ErrorState, Skeleton, StatusBadge, TerminalMetric, TerminalPanel, formatDateTime, formatNumber} from '../../components/ui'
import type {StatusTone} from '../../components/ui/StatusBadge'

const STATE_TONE: Record<NfoIngestionState, StatusTone> = {
  UP_TO_DATE: 'positive', BEHIND: 'warning', FAILED: 'critical', EMPTY: 'neutral', UNAVAILABLE: 'warning',
}
const STATE_LABEL: Record<NfoIngestionState, string> = {
  UP_TO_DATE: 'Up to date', BEHIND: 'Behind', FAILED: 'Failed', EMPTY: 'No data yet', UNAVAILABLE: 'Status unavailable',
}
const SESSION_TONE = (state: string): StatusTone => state === 'COMPLETE' ? 'positive' : state.startsWith('FAILED') ? 'critical' : 'warning'

const columns = [
  {id: 'session', header: 'Session', cell: (item: NfoIngestionSession) => <strong>{item.session}</strong>},
  {id: 'state', header: 'State', cell: (item: NfoIngestionSession) => <StatusBadge status={item.state === 'COMPLETE' ? 'Ingested' : item.state} tone={SESSION_TONE(item.state)} />},
  {id: 'rows', header: 'Rows', cell: (item: NfoIngestionSession) => item.state === 'COMPLETE' ? formatNumber(item.rows, 0) : '—'},
  {id: 'when', header: 'Completed', cell: (item: NfoIngestionSession) => formatDateTime(item.completed_at)},
  {id: 'error', header: 'Error', cell: (item: NfoIngestionSession) => item.error_category || '—'},
]

export function NfoIngestionPanel() {
  const status = useQuery(queries.nfoIngestion())
  const data = status.data
  const recent = data?.recent ?? []
  const badge = status.isError ? <StatusBadge status="Status unavailable" tone="warning" />
    : data ? <StatusBadge status={STATE_LABEL[data.state] ?? 'Unknown'} tone={STATE_TONE[data.state] ?? 'neutral'} /> : undefined
  return (
    <TerminalPanel id="nfo-ingestion" title="Options data ingestion" badge={badge}
      description="Nightly NFO index-option data loaded into the market-data database. Read from the ingestion's own ledger; this panel never starts or changes an ingestion.">
      {status.isLoading ? <Skeleton lines={4} /> : status.isError ? (
        <ErrorState title="Ingestion status is unavailable" error={status.error} onRetry={() => void status.refetch()} compact />
      ) : data ? (
        <div className="page-stack">
          <p role="status">{data.message}</p>
          {data.available && data.latest_session ? (
            <section className="metric-grid" aria-label="Latest ingestion">
              <TerminalMetric label="Latest ingested session" value={data.latest_session} />
              <TerminalMetric label="Rows loaded" value={formatNumber(data.latest_rows, 0)} />
              <TerminalMetric label="Completed" value={formatDateTime(data.latest_completed_at)} />
              <TerminalMetric label="Expected by now" value={data.expected_session} />
            </section>
          ) : null}
          {data.last_run ? <p>Last nightly run ({data.last_run.run_date || '—'}, attempt {data.last_run.attempt ?? 1}): {data.last_run.status === 'OK' ? 'completed' : `stopped (${data.last_run.code || `exit ${data.last_run.exit_code}`})`}{data.last_run.sessions.length ? `, ingested ${data.last_run.sessions.join(', ')}` : ''}{data.last_run.message ? ` — ${data.last_run.message}` : ''}</p> : null}
          {data.failure ? <p role="alert">Latest failure: session {data.failure.session} ({data.failure.error_category || data.failure.state}).</p> : null}
          {recent.length ? <><h3>Recent sessions</h3><DataTable rows={recent} columns={columns} getRowKey={(item) => item.session} emptyTitle="No sessions" /></> : null}
        </div>
      ) : null}
    </TerminalPanel>
  )
}
