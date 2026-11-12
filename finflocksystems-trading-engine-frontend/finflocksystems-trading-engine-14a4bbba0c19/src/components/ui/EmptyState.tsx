import {Inbox} from 'lucide-react'

export function EmptyState({title = 'No data', description, action}: {title?: string; description?: string; action?: React.ReactNode}) {
  return <div className="empty-state"><Inbox aria-hidden="true" /><strong>{title}</strong>{description && <p>{description}</p>}{action}</div>
}

