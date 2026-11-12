import {Activity, CircleDollarSign, Crosshair, FileClock, Radio} from 'lucide-react'
import {EmptyState, StatusBadge, formatDateTime} from './ui'

export interface ActivityItem {
  id: string | number
  time?: string | null
  type: string
  title: string
  detail?: string
  status?: string
}

function icon(type: string) {
  if (/FILL|EXECUTION/.test(type)) return <CircleDollarSign />
  if (/ORDER/.test(type)) return <FileClock />
  if (/SIGNAL/.test(type)) return <Radio />
  if (/TARGET/.test(type)) return <Crosshair />
  return <Activity />
}

export function ActivityTimeline({items, emptyDescription = 'Operational events will appear here as they are persisted.'}: {items: ActivityItem[]; emptyDescription?: string}) {
  if (!items.length) return <EmptyState title="No recent activity" description={emptyDescription} />
  return <ol className="activity-timeline">{items.map((item) => <li key={`${item.type}-${item.id}`}><div className="timeline-icon">{icon(item.type.toUpperCase())}</div><div className="timeline-copy"><div><strong>{item.title}</strong>{item.status && <StatusBadge status={item.status} />}</div>{item.detail && <p>{item.detail}</p>}{item.time ? <time dateTime={item.time}>{formatDateTime(item.time)}</time> : <time>Not reached</time>}</div></li>)}</ol>
}

