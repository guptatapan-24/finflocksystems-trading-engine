import {useEffect, useRef, useState, type RefObject} from 'react'
import {ColorType, createChart, type IChartApi, type MouseEventParams, type Time} from 'lightweight-charts'
import {buildChartTheme} from './chartTheme'

// Series times are UTC timestamps; lightweight-charts would print them in UTC,
// so the axis and crosshair are formatted in the viewer's zone like the legend.
function localDate(time: Time) {
  return typeof time === 'number' ? new Date(time * 1000) : null
}

export function localTickMark(time: Time, tickMarkType: number) {
  const date = localDate(time)
  if (!date) return null
  if (tickMarkType === 0) return String(date.getFullYear())
  if (tickMarkType === 1) return date.toLocaleString(undefined, {month: 'short'})
  if (tickMarkType === 2) return date.toLocaleString(undefined, {day: 'numeric', month: 'short'})
  return date.toLocaleTimeString(undefined, {hour: '2-digit', minute: '2-digit', hourCycle: 'h23', ...(tickMarkType === 4 ? {second: '2-digit'} : {})})
}

export function localCrosshairTime(time: Time) {
  const date = localDate(time)
  return date ? date.toLocaleString(undefined, {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'}) : ''
}

export function useLightweightChart(container: RefObject<HTMLDivElement>, height: number, onCrosshair: (event: MouseEventParams<Time>) => void) {
  const chartRef = useRef<IChartApi | null>(null)
  const crosshairRef = useRef(onCrosshair)
  const [ready, setReady] = useState(false)
  crosshairRef.current = onCrosshair

  useEffect(() => {
    const element = container.current
    if (!element) return
    const theme = buildChartTheme()
    const chart = createChart(element, {
      height,
      width: element.clientWidth,
      layout: {background: {type: ColorType.Solid, color: 'transparent'}, textColor: theme.text, fontFamily: '"SFMono-Regular", Consolas, monospace'},
      grid: {vertLines: {color: theme.grid}, horzLines: {color: theme.grid}},
      rightPriceScale: {borderColor: theme.border},
      timeScale: {borderColor: theme.border, timeVisible: true, secondsVisible: false, rightOffset: 4, tickMarkFormatter: localTickMark},
      localization: {timeFormatter: localCrosshairTime},
      crosshair: {vertLine: {color: theme.crosshair, labelBackgroundColor: theme.crosshair}, horzLine: {color: theme.crosshair, labelBackgroundColor: theme.crosshair}},
    })
    const crosshair = (event: MouseEventParams<Time>) => crosshairRef.current(event)
    chart.subscribeCrosshairMove(crosshair)
    chartRef.current = chart
    setReady(true)
    let frame = 0
    const resize = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => chart.applyOptions({width: element.clientWidth}))
    })
    resize?.observe(element)
    return () => {
      cancelAnimationFrame(frame)
      resize?.disconnect()
      chart.unsubscribeCrosshairMove(crosshair)
      chart.remove()
      chartRef.current = null
    }
  }, [container])

  useEffect(() => { chartRef.current?.applyOptions({height}) }, [height])
  return {chartRef, ready}
}
