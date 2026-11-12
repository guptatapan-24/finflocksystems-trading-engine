import {Expand, Focus, Maximize2, Minimize2, RotateCcw} from 'lucide-react'
import type {ChartPreferences, ChartType} from '../../stores/workspacePreferences'

const ranges = ['1D', '5D', '1M', '3M', '6M', 'YTD', '1Y', 'MAX']

export function ChartToolbar({preferences, hasCandles, hasVolume, hasIndicators, intervals = [], fullscreen, onChange, onFit, onAutoScale, onReset, onFullscreen}: {
  preferences: ChartPreferences
  hasCandles: boolean
  hasVolume: boolean
  hasIndicators: boolean
  intervals?: string[]
  fullscreen: boolean
  onChange: (preferences: Partial<ChartPreferences>) => void
  onFit: () => void
  onAutoScale: () => void
  onReset: () => void
  onFullscreen: () => void
}) {
  const chartTypes: ChartType[] = hasCandles ? ['candlestick', 'line', 'area'] : ['line', 'area']
  return <div className="chart-toolbar" aria-label="Chart controls">
    <div className="chart-button-group" aria-label="Chart range">{ranges.map((range) => <button type="button" key={range} className={preferences.range === range ? 'active' : ''} aria-pressed={preferences.range === range} onClick={() => onChange({range})}>{range}</button>)}</div>
    <div className="chart-button-group" aria-label="Chart type">{chartTypes.map((chartType) => <button type="button" key={chartType} className={preferences.chartType === chartType ? 'active' : ''} aria-pressed={preferences.chartType === chartType} onClick={() => onChange({chartType})}>{chartType === 'candlestick' ? 'Candles' : chartType}</button>)}</div>
    {intervals.length > 0 && <label className="chart-interval"><span className="sr-only">Chart interval</span><select aria-label="Chart interval" value={preferences.interval} onChange={(event) => onChange({interval: event.target.value})}>{intervals.map((interval) => <option key={interval}>{interval}</option>)}</select></label>}
    {hasVolume && <button type="button" className={preferences.volumeVisible ? 'active' : ''} aria-pressed={preferences.volumeVisible} onClick={() => onChange({volumeVisible: !preferences.volumeVisible})}>Volume</button>}
    {hasIndicators && <button type="button" className={preferences.indicatorsVisible ? 'active' : ''} aria-pressed={preferences.indicatorsVisible} onClick={() => onChange({indicatorsVisible: !preferences.indicatorsVisible})}>Indicators</button>}
    <button type="button" className={preferences.percentageScale ? 'active' : ''} aria-pressed={preferences.percentageScale} onClick={() => onChange({percentageScale: !preferences.percentageScale})}>%</button>
    <span className="chart-toolbar-spacer" />
    <button type="button" className="icon-button" aria-label="Fit chart content" title="Fit content" onClick={onFit}><Focus /></button>
    <button type="button" className="icon-button" aria-label="Auto scale chart" title="Auto scale" onClick={onAutoScale}><Expand /></button>
    <button type="button" className="icon-button" aria-label="Reset chart" title="Reset chart" onClick={onReset}><RotateCcw /></button>
    <button type="button" className="icon-button" aria-label={fullscreen ? 'Exit chart fullscreen' : 'Open chart fullscreen'} onClick={onFullscreen}>{fullscreen ? <Minimize2 /> : <Maximize2 />}</button>
  </div>
}
