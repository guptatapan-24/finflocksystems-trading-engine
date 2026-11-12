import {useState} from 'react'
import {useQuery} from '@tanstack/react-query'
import {ErrorState, PageHeader, Skeleton} from '../../components/ui'
import {downloadReport, type DownloadFormat} from './download'
import {IntelligenceTabs} from './IntelligenceTabs'
import {intelligenceQueries} from './queries'
import {ReportSections} from './ReportBlocks'

const ZONES = [{value: '', label: 'All exchanges'}, {value: 'XNSE', label: 'NSE (INR)'}, {value: 'XNYS', label: 'NYSE (USD)'}]
const today = () => new Date().toISOString().slice(0, 10)

/** One exchange session (or the day across exchanges) built only from stored data, with downloadable PDF / HTML / Markdown. */
export function SessionsPage() {
  const [date, setDate] = useState('')
  const [zone, setZone] = useState('')
  const [downloadError, setDownloadError] = useState<string | null>(null)
  const report = useQuery(intelligenceQueries.dailyReport(date, zone))
  const recent = useQuery(intelligenceQueries.sessions())

  async function download(format: DownloadFormat) {
    setDownloadError(null)
    try {
      const query = [date && `date=${date}`, zone && `zone=${zone}`].filter(Boolean).join('&')
      await downloadReport(`intelligence/reports/daily${query ? `?${query}` : ''}`, format, `intelligence-report-${date || today()}`)
    } catch (error) {
      setDownloadError(error instanceof Error ? error.message : 'Download failed')
    }
  }

  return <>
    <PageHeader eyebrow="Intelligence / Sessions" title={report.data?.title ?? 'Intelligence Report'}
      description="What happened in the market session: trades, decisions not taken, your inputs, alerts and data quality. Each exchange and currency is shown separately."
      actions={<>
        <button className="button-secondary" onClick={() => void download('pdf')} disabled={!report.data}>Download PDF</button>
        <button className="button-secondary" onClick={() => void download('html')} disabled={!report.data}>Download HTML</button>
        <button className="button-secondary" onClick={() => void download('markdown')} disabled={!report.data}>Download Markdown</button></>} />
    <IntelligenceTabs />
    <div className="form-grid two-columns" role="group" aria-label="Report selection">
      <label>Session date (exchange local)<input type="date" aria-label="Session date" value={date} max={today()} onChange={(event) => setDate(event.target.value)} /></label>
      <label>Exchange<select aria-label="Exchange" value={zone} onChange={(event) => setZone(event.target.value)}>
        {ZONES.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
    </div>
    {report.data && <p className="inline-note" aria-label="Report provenance">
      {report.data.frozen ? 'Frozen report' : 'Live view (not frozen: a session is still open or has no stored record yet)'} · version {report.data.report_version} · content hash {report.data.content_hash.slice(0, 16)}</p>}
    {recent.data && recent.data.length > 0 && <p className="inline-note" aria-label="Stored sessions">
      Stored sessions: {recent.data.slice(0, 6).map((row) => `${row.zone} ${row.session_date} (${row.status.toLowerCase()})`).join(' · ')}</p>}
    {downloadError && <ErrorState title="Could not download" error={new Error(downloadError)} compact />}
    {report.isError && <ErrorState title="Could not load the report" error={report.error} onRetry={() => void report.refetch()} />}
    {report.isLoading && <Skeleton lines={8} height={360} />}
    {report.data && <ReportSections sections={report.data.sections} idPrefix="intelligence-session" />}
  </>
}
