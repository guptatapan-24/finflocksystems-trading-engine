import {useState} from 'react'
import {useQuery} from '@tanstack/react-query'
import {Link, useParams} from 'react-router-dom'
import {ErrorState, PageHeader, Skeleton} from '../../components/ui'
import {downloadReport, type DownloadFormat} from './download'
import {intelligenceQueries} from './queries'
import {ReportSections} from './ReportBlocks'

export function ReportPage() {
  const {episodeId} = useParams()
  const id = Number(episodeId)
  const report = useQuery(intelligenceQueries.report(Number.isFinite(id) ? id : null))
  const [downloadError, setDownloadError] = useState<string | null>(null)

  async function download(format: DownloadFormat) {
    setDownloadError(null)
    try {
      await downloadReport(`intelligence/episodes/${id}/report`, format, `trade-report-${id}`)
    } catch (error) {
      setDownloadError(error instanceof Error ? error.message : 'Download failed')
    }
  }

  return <>
    <PageHeader eyebrow="Intelligence / Report" title={report.data?.title ?? 'Trade report'} description="Facts, parameters, predicted versus actual, and the checked model commentary for one trade."
      actions={<><Link className="button-secondary" to="/intelligence/episodes">Back to episodes</Link>
        <button className="button-secondary" onClick={() => void download('pdf')} disabled={!report.data}>Download PDF</button>
        <button className="button-secondary" onClick={() => void download('html')} disabled={!report.data}>Download HTML</button>
        <button className="button-secondary" onClick={() => void download('markdown')} disabled={!report.data}>Download Markdown</button></>} />
    {report.data?.content_hash && <p className="inline-note" aria-label="Report provenance">
      {report.data.frozen ? 'Frozen report' : 'Live view (the trade is still open, not frozen)'} · version {report.data.report_version ?? '?'} · content hash {report.data.content_hash.slice(0, 16)}</p>}
    {downloadError && <ErrorState title="Could not download" error={new Error(downloadError)} compact />}
    {report.isError && <ErrorState title="Could not load the report" error={report.error} onRetry={() => void report.refetch()} />}
    {report.isLoading && <Skeleton lines={8} height={360} />}
    {report.data && <ReportSections sections={report.data.sections} />}
  </>
}
