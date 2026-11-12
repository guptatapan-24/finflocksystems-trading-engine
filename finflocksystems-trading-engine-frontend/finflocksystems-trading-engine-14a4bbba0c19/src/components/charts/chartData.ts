import type {UTCTimestamp} from 'lightweight-charts'

export interface RawLinePoint {time: string; value: number}
export interface RawCandlePoint {time: string; open: number; high: number; low: number; close: number; volume?: number}
export interface PreparedLinePoint {time: UTCTimestamp; value: number}
export interface PreparedCandlePoint {time: UTCTimestamp; open: number; high: number; low: number; close: number; volume?: number}

const RANGE_MS: Record<string, number> = {
  '1D': 86_400_000,
  '5D': 5 * 86_400_000,
  '1M': 31 * 86_400_000,
  '3M': 93 * 86_400_000,
  '6M': 186 * 86_400_000,
  '1Y': 366 * 86_400_000,
}

export function timestamp(value: string): UTCTimestamp | null {
  const milliseconds = new Date(value).getTime()
  return Number.isFinite(milliseconds) ? Math.floor(milliseconds / 1000) as UTCTimestamp : null
}

function bounded<T extends {time: UTCTimestamp}>(values: T[], maximumPoints: number) {
  const unique = new Map<number, T>()
  values.forEach((item) => unique.set(Number(item.time), item))
  return [...unique.values()].sort((left, right) => Number(left.time) - Number(right.time)).slice(-maximumPoints)
}

export function prepareLineData(points: RawLinePoint[], maximumPoints = 5_000): PreparedLinePoint[] {
  return bounded(points.flatMap((point) => {
    const time = timestamp(point.time)
    return time === null || !Number.isFinite(point.value) ? [] : [{time, value: point.value}]
  }), maximumPoints)
}

export function prepareCandleData(points: RawCandlePoint[], maximumPoints = 5_000): PreparedCandlePoint[] {
  return bounded(points.flatMap((point) => {
    const time = timestamp(point.time)
    const prices = [point.open, point.high, point.low, point.close]
    const invalidVolume = point.volume !== undefined && (!Number.isFinite(point.volume) || point.volume < 0)
    const invalidOhlc = !prices.every(Number.isFinite) || point.high < Math.max(point.open, point.close, point.low) || point.low > Math.min(point.open, point.close, point.high)
    return time === null || invalidVolume || invalidOhlc ? [] : [{...point, time}]
  }), maximumPoints)
}

export function filterByRange<T extends {time: UTCTimestamp}>(points: T[], range: string): T[] {
  if (!points.length || range === 'MAX') return points
  const latest = Number(points[points.length - 1].time) * 1000
  const cutoff = range === 'YTD'
    ? Date.UTC(new Date(latest).getUTCFullYear(), 0, 1)
    : latest - (RANGE_MS[range] || Number.POSITIVE_INFINITY)
  return points.filter((point) => Number(point.time) * 1000 >= cutoff)
}
