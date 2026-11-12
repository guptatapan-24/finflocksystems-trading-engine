import {useCallback, useEffect, useMemo, useRef, useState} from 'react'
import {
  PriceScaleMode,
  type CandlestickData,
  type HistogramData,
  type IChartApi,
  type ISeriesApi,
  type LineData,
  type LineStyle,
  type LineType,
  type MouseEventParams,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts'
import {useLocation} from 'react-router-dom'
import {useWorkspacePreferences, type ChartPreferences, type ChartType} from '../../stores/workspacePreferences'
import {EmptyState} from '../ui'
import {ChartLegend, type LegendValue} from './ChartLegend'
import {ChartToolbar} from './ChartToolbar'
import {filterByRange, prepareCandleData, prepareLineData, timestamp, type RawCandlePoint, type RawLinePoint} from './chartData'
import {buildChartTheme} from './chartTheme'
import {useLightweightChart} from './useLightweightChart'

export interface ChartPoint extends RawLinePoint {}
export interface CandlePoint extends RawCandlePoint {}
export interface ChartLine {name: string; data: ChartPoint[]; color?: string; type?: 'line' | 'area'; kind?: 'primary' | 'indicator'; dashed?: boolean; step?: boolean}
export interface ChartMarker {time: string; label: string; kind: 'signal' | 'target' | 'order' | 'fill' | 'entry' | 'exit' | 'entry-pending' | 'exit-pending'}

// Numeric values of lightweight-charts' LineStyle.LargeDashed and LineType.WithSteps,
// kept as literals so the chart works against a mocked module in tests.
const DASHED = 3 as LineStyle
const STEPPED = 1 as LineType

type AnySeries = ISeriesApi<'Candlestick'> | ISeriesApi<'Line'> | ISeriesApi<'Area'> | ISeriesApi<'Histogram'>
type SeriesDatum = CandlestickData<UTCTimestamp> | LineData<UTCTimestamp> | HistogramData<UTCTimestamp>

interface SeriesRecord {
  api: AnySeries
  type: 'candlestick' | 'line' | 'area' | 'histogram'
  previous: SeriesDatum[]
  setData: (data: SeriesDatum[]) => void
  update: (data: SeriesDatum) => void
}

function isValueData(value: unknown): value is {value: number} {
  return value !== null && typeof value === 'object' && 'value' in value && typeof (value as {value?: unknown}).value === 'number'
}

function isCandleData(value: unknown): value is {open: number; high: number; low: number; close: number} {
  if (!value || typeof value !== 'object') return false
  const item = value as {open?: unknown; high?: unknown; low?: unknown; close?: unknown}
  return [item.open, item.high, item.low, item.close].every((number) => typeof number === 'number')
}

function syncSeries(record: SeriesRecord, next: SeriesDatum[]) {
  const previous = record.previous
  const samePrefix = previous.length > 0 && previous.slice(0, -1).every((item, index) => Number(item.time) === Number(next[index]?.time))
  if (previous.length === next.length && samePrefix && next.length && Number(previous[previous.length - 1].time) === Number(next[next.length - 1].time)) {
    record.update(next[next.length - 1])
  } else if (previous.length && next.length > previous.length && previous.every((item, index) => Number(item.time) === Number(next[index]?.time))) {
    next.slice(previous.length).forEach(record.update)
  } else {
    record.setData(next)
  }
  record.previous = next
}

function markerStyle(kind: ChartMarker['kind'], theme: ReturnType<typeof buildChartTheme>): Pick<SeriesMarker<Time>, 'position' | 'shape' | 'color'> {
  if (kind === 'entry') return {position: 'belowBar', shape: 'arrowUp', color: theme.positive}
  if (kind === 'exit') return {position: 'aboveBar', shape: 'arrowDown', color: theme.negative}
  if (kind === 'entry-pending') return {position: 'belowBar', shape: 'circle', color: theme.palette[3]}
  if (kind === 'exit-pending') return {position: 'aboveBar', shape: 'circle', color: theme.palette[3]}
  if (kind === 'fill') return {position: 'belowBar', shape: 'arrowUp', color: theme.positive}
  if (kind === 'order') return {position: 'aboveBar', shape: 'circle', color: theme.palette[3]}
  if (kind === 'target') return {position: 'aboveBar', shape: 'square', color: theme.palette[1]}
  return {position: 'belowBar', shape: 'arrowUp', color: theme.palette[0]}
}

function remove(chart: IChartApi | null, record: SeriesRecord | null) {
  if (chart && record) chart.removeSeries(record.api)
}

export function TerminalChart({id, lines = [], candles = [], markers = [], height = 320, ariaLabel = 'Terminal chart', defaultChartType, intervals = []}: {
  id: string
  lines?: ChartLine[]
  candles?: CandlePoint[]
  markers?: ChartMarker[]
  height?: number
  ariaLabel?: string
  defaultChartType?: ChartType
  intervals?: string[]
}) {
  const location = useLocation()
  const chartKey = `${location.pathname}:${id}`
  const stored = useWorkspacePreferences((state) => state.chartPreferences[chartKey])
  const setChartPreferences = useWorkspacePreferences((state) => state.setChartPreferences)
  const fullscreenPanelId = useWorkspacePreferences((state) => state.fullscreenPanelId)
  const setFullscreenPanelId = useWorkspacePreferences((state) => state.setFullscreenPanelId)
  const hasCandles = candles.length > 0
  const defaults: ChartPreferences = {
    range: 'MAX', interval: intervals[0] || '', chartType: defaultChartType || (hasCandles ? 'candlestick' : 'area'),
    volumeVisible: true, indicatorsVisible: true, percentageScale: false,
  }
  const requestedType = stored?.chartType || defaults.chartType
  const preferences: ChartPreferences = {...defaults, ...stored, chartType: !hasCandles && requestedType === 'candlestick' ? defaults.chartType : requestedType}
  const fullscreenKey = `chart:${chartKey}`
  const fullscreen = fullscreenPanelId === fullscreenKey
  const container = useRef<HTMLDivElement>(null)
  const primaryRef = useRef<SeriesRecord | null>(null)
  const volumeRef = useRef<SeriesRecord | null>(null)
  const lineRefs = useRef(new Map<string, SeriesRecord>())
  const [crosshair, setCrosshair] = useState<LegendValue | null>(null)
  const theme = useMemo(() => buildChartTheme(), [])

  const preparedCandles = useMemo(() => filterByRange(prepareCandleData(candles), preferences.range), [candles, preferences.range])
  const preparedLines = useMemo(() => lines
    .filter((line) => line.kind !== 'indicator' || preferences.indicatorsVisible)
    .map((line) => ({...line, data: filterByRange(prepareLineData(line.data), preferences.range)}))
    .filter((line) => line.data.length), [lines, preferences.indicatorsVisible, preferences.range])
  const hasData = preparedCandles.length > 0 || preparedLines.length > 0

  const onCrosshair = useCallback((event: MouseEventParams<Time>) => {
    if (!event.time) { setCrosshair(null); return }
    const primary = primaryRef.current ? event.seriesData.get(primaryRef.current.api) : undefined
    const candle = isCandleData(primary) ? primary : undefined
    const primaryLine = isValueData(primary) ? primary : undefined
    const volume = volumeRef.current ? event.seriesData.get(volumeRef.current.api) : undefined
    const values = [...lineRefs.current.entries()].flatMap(([name, record]) => {
      const item = event.seriesData.get(record.api)
      return isValueData(item) ? [{name, value: item.value}] : []
    })
    if (primaryLine && !values.some((item) => item.name === 'Price')) values.unshift({name: 'Price', value: primaryLine.value})
    setCrosshair({time: Number(event.time), open: candle?.open, high: candle?.high, low: candle?.low, close: candle?.close, volume: isValueData(volume) ? volume.value : undefined, lines: values})
  }, [])
  const {chartRef, ready} = useLightweightChart(container, fullscreen ? Math.max(420, window.innerHeight - 112) : height, onCrosshair)

  useEffect(() => {
    const chart = chartRef.current
    if (!ready || !chart) return
    remove(chart, primaryRef.current)
    primaryRef.current = null
    if (!hasCandles) return
    if (preferences.chartType === 'candlestick') {
      const api = chart.addCandlestickSeries({upColor: theme.positive, downColor: theme.negative, wickUpColor: theme.positive, wickDownColor: theme.negative, borderVisible: false, priceLineVisible: true, lastValueVisible: true})
      primaryRef.current = {api, type: 'candlestick', previous: [], setData: (data) => api.setData(data as CandlestickData<UTCTimestamp>[]), update: (data) => api.update(data as CandlestickData<UTCTimestamp>)}
    } else if (preferences.chartType === 'area') {
      const color = theme.palette[0]
      const api = chart.addAreaSeries({lineColor: color, topColor: `${color}35`, bottomColor: `${color}03`, lineWidth: 2, title: 'Price', priceLineVisible: true, lastValueVisible: true})
      primaryRef.current = {api, type: 'area', previous: [], setData: (data) => api.setData(data as LineData<UTCTimestamp>[]), update: (data) => api.update(data as LineData<UTCTimestamp>)}
    } else {
      const api = chart.addLineSeries({color: theme.palette[0], lineWidth: 2, title: 'Price', priceLineVisible: true, lastValueVisible: true})
      primaryRef.current = {api, type: 'line', previous: [], setData: (data) => api.setData(data as LineData<UTCTimestamp>[]), update: (data) => api.update(data as LineData<UTCTimestamp>)}
    }
  }, [chartRef, hasCandles, preferences.chartType, ready, theme])

  useEffect(() => {
    const chart = chartRef.current
    if (!ready || !chart) return
    const primaryData: SeriesDatum[] = preferences.chartType === 'candlestick'
      ? preparedCandles
      : preparedCandles.map((item) => ({time: item.time, value: item.close}))
    if (primaryRef.current) syncSeries(primaryRef.current, primaryData)

    const activeNames = new Set(preparedLines.map((line) => line.name))
    lineRefs.current.forEach((record, name) => {
      if (!activeNames.has(name)) { remove(chart, record); lineRefs.current.delete(name) }
    })
    preparedLines.forEach((line, index) => {
      const type = line.type || 'line'
      let record = lineRefs.current.get(line.name)
      if (record && record.type !== type) { remove(chart, record); lineRefs.current.delete(line.name); record = undefined }
      if (!record) {
        const color = line.color || theme.palette[(index + (hasCandles ? 1 : 0)) % theme.palette.length]
        if (type === 'area') {
          const api = chart.addAreaSeries({lineColor: color, topColor: `${color}30`, bottomColor: `${color}03`, lineWidth: 2, title: line.name, priceLineVisible: false, lastValueVisible: true})
          record = {api, type, previous: [], setData: (data) => api.setData(data as LineData<UTCTimestamp>[]), update: (data) => api.update(data as LineData<UTCTimestamp>)}
        } else {
          const api = chart.addLineSeries({color, lineWidth: 2, title: line.name, priceLineVisible: false, lastValueVisible: true, ...(line.dashed ? {lineStyle: DASHED} : {}), ...(line.step ? {lineType: STEPPED} : {})})
          record = {api, type, previous: [], setData: (data) => api.setData(data as LineData<UTCTimestamp>[]), update: (data) => api.update(data as LineData<UTCTimestamp>)}
        }
        lineRefs.current.set(line.name, record)
      }
      syncSeries(record, line.data)
    })

    if (preferences.volumeVisible && preparedCandles.some((item) => item.volume !== undefined)) {
      if (!volumeRef.current) {
        const api = chart.addHistogramSeries({priceFormat: {type: 'volume'}, priceScaleId: '', lastValueVisible: false, priceLineVisible: false})
        api.priceScale().applyOptions({scaleMargins: {top: .82, bottom: 0}})
        volumeRef.current = {api, type: 'histogram', previous: [], setData: (data) => api.setData(data as HistogramData<UTCTimestamp>[]), update: (data) => api.update(data as HistogramData<UTCTimestamp>)}
      }
      syncSeries(volumeRef.current, preparedCandles.filter((item) => item.volume !== undefined).map((item) => ({time: item.time, value: item.volume || 0, color: item.close >= item.open ? theme.volumeUp : theme.volumeDown})))
    } else if (volumeRef.current) {
      remove(chart, volumeRef.current)
      volumeRef.current = null
    }

    const markerTarget = primaryRef.current || lineRefs.current.values().next().value as SeriesRecord | undefined
    if (markerTarget && 'setMarkers' in markerTarget.api) {
      markerTarget.api.setMarkers(markers.flatMap((item) => {
        const time = timestamp(item.time)
        return time === null ? [] : [{time, text: item.label, ...markerStyle(item.kind, theme)}]
      }).sort((left, right) => Number(left.time) - Number(right.time)))
    }
  }, [chartRef, hasCandles, markers, preferences.chartType, preferences.volumeVisible, preparedCandles, preparedLines, ready, theme])

  // Frame the whole series once it first arrives; otherwise a short history is
  // drawn at default bar spacing and squeezed against the right edge.
  const fitted = useRef(false)
  useEffect(() => {
    if (!ready || !hasData || fitted.current) return
    fitted.current = true
    chartRef.current?.timeScale().fitContent()
  }, [chartRef, hasData, ready])

  useEffect(() => {
    chartRef.current?.priceScale('right').applyOptions({mode: preferences.percentageScale ? PriceScaleMode.Percentage : PriceScaleMode.Normal, autoScale: true})
  }, [chartRef, preferences.percentageScale])

  useEffect(() => {
    if (!fullscreen) return
    const close = (event: KeyboardEvent) => { if (event.key === 'Escape') setFullscreenPanelId(null) }
    window.addEventListener('keydown', close)
    return () => window.removeEventListener('keydown', close)
  }, [fullscreen, setFullscreenPanelId])

  const latest = useMemo<LegendValue>(() => {
    const candle = preparedCandles[preparedCandles.length - 1]
    return {
      time: Number(candle?.time || preparedLines[0]?.data.at(-1)?.time || 0) || undefined,
      open: candle?.open, high: candle?.high, low: candle?.low, close: candle?.close, volume: candle?.volume,
      lines: preparedLines.flatMap((line) => line.data.length ? [{name: line.name, value: line.data[line.data.length - 1].value}] : []),
    }
  }, [preparedCandles, preparedLines])
  const change = (next: Partial<ChartPreferences>) => setChartPreferences(chartKey, next)
  const reset = () => { setChartPreferences(chartKey, defaults); chartRef.current?.timeScale().fitContent() }

  return <div className={`terminal-chart ${fullscreen ? 'terminal-chart-fullscreen' : ''}`}>
    <ChartToolbar preferences={preferences} hasCandles={hasCandles} hasVolume={candles.some((item) => item.volume !== undefined)} hasIndicators={lines.some((line) => line.kind === 'indicator')} intervals={intervals} fullscreen={fullscreen} onChange={change} onFit={() => chartRef.current?.timeScale().fitContent()} onAutoScale={() => chartRef.current?.priceScale('right').applyOptions({autoScale: true})} onReset={reset} onFullscreen={() => setFullscreenPanelId(fullscreen ? null : fullscreenKey)} />
    <ChartLegend value={crosshair || latest} />
    {!hasData && <EmptyState title="No time-series data" description="The chart will populate from persisted market and portfolio observations." />}
    <div ref={container} className="terminal-chart-canvas" style={{height: fullscreen ? Math.max(420, window.innerHeight - 112) : height, display: hasData ? undefined : 'none'}} role="img" aria-label={ariaLabel} />
  </div>
}
