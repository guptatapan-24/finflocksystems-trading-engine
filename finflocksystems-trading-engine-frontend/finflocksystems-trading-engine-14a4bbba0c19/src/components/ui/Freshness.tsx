import {RefreshCw} from 'lucide-react'
import {formatDateTime} from './format'

export function Freshness({updatedAt, stale, fetching, onRefresh}: {updatedAt?: number; stale?: boolean; fetching?: boolean; onRefresh?: () => void}) {
  return <div className="freshness"><span className={stale ? 'stale' : ''}>{stale ? 'Stale' : 'Updated'} {updatedAt ? formatDateTime(new Date(updatedAt).toISOString()) : '—'}</span>{onRefresh && <button className="icon-button" aria-label="Refresh data" onClick={onRefresh} disabled={fetching}><RefreshCw className={fetching ? 'spin' : ''} /></button>}</div>
}

