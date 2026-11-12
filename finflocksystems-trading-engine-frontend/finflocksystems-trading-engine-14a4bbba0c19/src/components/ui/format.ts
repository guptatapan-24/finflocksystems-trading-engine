import type {DecimalValue} from '../../api/types'

export function toNumber(value: DecimalValue | undefined) {
  const parsed = Number(value ?? 0)
  return Number.isFinite(parsed) ? parsed : 0
}

export function formatMoney(value: DecimalValue | undefined, currency = 'USD') {
  if (value === null || value === undefined || value === '') return '—'
  return new Intl.NumberFormat(undefined, {style: 'currency', currency, maximumFractionDigits: 2}).format(toNumber(value))
}

export function formatNumber(value: DecimalValue | undefined, maximumFractionDigits = 4) {
  if (value === null || value === undefined || value === '') return '—'
  return new Intl.NumberFormat(undefined, {maximumFractionDigits}).format(toNumber(value))
}

export function formatPercent(value: DecimalValue | undefined, alreadyPercent = false) {
  if (value === null || value === undefined || value === '') return '—'
  return new Intl.NumberFormat(undefined, {style: 'percent', maximumFractionDigits: 2}).format(
    toNumber(value) / (alreadyPercent ? 100 : 1),
  )
}

export function formatDateTime(value?: string | null) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium', timeStyle: 'short',
  }).format(date)
}

export function formatCompact(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'boolean') return value ? 'Yes' : 'No'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

