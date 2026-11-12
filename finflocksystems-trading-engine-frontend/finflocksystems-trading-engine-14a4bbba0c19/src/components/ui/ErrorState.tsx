import {CircleAlert, RotateCcw} from 'lucide-react'

export function ErrorState({title = 'Could not load this section', error, onRetry, compact = false}: {title?: string; error?: unknown; onRetry?: () => void; compact?: boolean}) {
  const message = error instanceof Error ? error.message : 'An unexpected data error occurred.'
  return <div className={`error-state ${compact ? 'error-state-compact' : ''}`}><CircleAlert aria-hidden="true" /><div><strong>{title}</strong><p>{message}</p></div>{onRetry && <button className="button-secondary" onClick={onRetry}><RotateCcw />Retry</button>}</div>
}

