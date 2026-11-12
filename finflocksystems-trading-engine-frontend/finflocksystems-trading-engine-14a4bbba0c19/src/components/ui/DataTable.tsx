import type {ReactNode} from 'react'
import {EmptyState} from './EmptyState'

export interface DataColumn<T> {
  id: string
  header: string
  cell: (row: T) => ReactNode
  align?: 'left' | 'right' | 'center'
  className?: string
}

interface DataTableProps<T> {
  rows: T[]
  columns: DataColumn<T>[]
  getRowKey: (row: T) => string | number
  emptyTitle?: string
  emptyDescription?: string
  caption?: string
  selectedRowKey?: string | number | null
  onRowSelect?: (row: T) => void
}

export function DataTable<T>({rows, columns, getRowKey, emptyTitle = 'Nothing here yet', emptyDescription, caption, selectedRowKey, onRowSelect}: DataTableProps<T>) {
  if (!rows.length) return <EmptyState title={emptyTitle} description={emptyDescription} />
  return (
    <div className="data-table-wrap">
      <table className="data-table">
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead><tr>{columns.map((column) => <th key={column.id} className={column.className} style={{textAlign: column.align}}>{column.header}</th>)}</tr></thead>
        <tbody>{rows.map((row) => (
          <tr key={getRowKey(row)} aria-selected={selectedRowKey === getRowKey(row) || undefined} tabIndex={onRowSelect ? 0 : undefined} onClick={onRowSelect ? () => onRowSelect(row) : undefined} onKeyDown={onRowSelect ? (event) => {if (event.key === 'Enter' || event.key === ' ') {event.preventDefault(); onRowSelect(row)}} : undefined}>{columns.map((column) => (
            <td key={column.id} className={column.className} style={{textAlign: column.align}}>{column.cell(row)}</td>
          ))}</tr>
        ))}</tbody>
      </table>
    </div>
  )
}
