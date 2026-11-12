import {useQuery} from '@tanstack/react-query'
import {Lightbulb} from 'lucide-react'
import {StatusBadge} from '../../components/ui'
import {intelligenceQueries} from './queries'

/** Context from past trades shown before a strategy is created or changed. It never blocks anything and renders
 *  nothing when the intelligence API is unavailable. */
export function AdvisoryBanner({strategyKey, parameters, instrument = ''}: {strategyKey: string; parameters: Record<string, unknown>; instrument?: string}) {
  const advisory = useQuery(intelligenceQueries.advisoryPreview(strategyKey, parameters, instrument))
  // Advice must never get in the way: an unexpected payload simply renders nothing.
  if (!advisory.data || !Array.isArray(advisory.data.lines)) return null
  const data = advisory.data
  const tone = data.severity === 'HIGH' ? 'critical' : data.severity === 'WARNING' ? 'warning' : 'neutral'
  return <aside className="panel" role="note" aria-label="Advice from past trades">
    <header className="row-actions"><Lightbulb aria-hidden="true" /><strong>Advice from past trades</strong><StatusBadge status={data.severity} tone={tone} /></header>
    <ul>{data.lines.map((line, index) => <li key={index}>{line}</li>)}</ul>
    <small>{data.note} You can ignore it.</small>
  </aside>
}
