import {filterByRange, prepareCandleData, prepareLineData} from '../src/components/charts/chartData'
import {buildChartTheme} from '../src/components/charts/chartTheme'
import {useWorkspacePreferences} from '../src/stores/workspacePreferences'

test('sorts, deduplicates, bounds, and rejects invalid chart points', () => {
  const lines = prepareLineData([
    {time: '2026-01-03T00:00:00Z', value: 3},
    {time: 'invalid', value: 2},
    {time: '2026-01-01T00:00:00Z', value: 1},
    {time: '2026-01-03T00:00:00Z', value: 4},
    {time: '2026-01-02T00:00:00Z', value: Number.NaN},
  ], 2)
  expect(lines.map((item) => item.value)).toEqual([1, 4])

  const candles = prepareCandleData([
    {time: '2026-01-02T00:00:00Z', open: 10, high: 9, low: 8, close: 9, volume: 10},
    {time: '2026-01-01T00:00:00Z', open: 8, high: 11, low: 7, close: 10, volume: 12},
    {time: '2026-01-03T00:00:00Z', open: 10, high: 12, low: 9, close: 11, volume: -1},
  ])
  expect(candles).toHaveLength(1)
  expect(candles[0].close).toBe(10)
})

test('filters real prepared points by chart range', () => {
  const points = prepareLineData([
    {time: '2025-01-01T00:00:00Z', value: 1},
    {time: '2026-06-15T00:00:00Z', value: 2},
    {time: '2026-07-15T00:00:00Z', value: 3},
  ])
  expect(filterByRange(points, '1M').map((item) => item.value)).toEqual([2, 3])
  expect(filterByRange(points, 'YTD').map((item) => item.value)).toEqual([2, 3])
  expect(filterByRange(points, 'MAX')).toEqual(points)
})

test('derives chart configuration from semantic theme variables', () => {
  const variables: Record<string, string> = {'--chart-1': 'accent-token', '--positive': 'up-token', '--critical': 'down-token'}
  const theme = buildChartTheme((name) => variables[name] || '')
  expect(theme.palette[0]).toBe('accent-token')
  expect(theme.positive).toBe('up-token')
  expect(theme.negative).toBe('down-token')
})

test('persists range and interval as local chart preferences only', () => {
  useWorkspacePreferences.getState().resetWorkspace()
  useWorkspacePreferences.getState().setChartPreferences('/strategy:price', {range: '3M', interval: '5m'})
  expect(useWorkspacePreferences.getState().chartPreferences['/strategy:price']).toMatchObject({range: '3M', interval: '5m'})
  expect(localStorage.getItem('finflock-workspace-v1')).toContain('3M')
})
