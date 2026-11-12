import '@testing-library/jest-dom/vitest'
import {afterEach,beforeEach,vi} from 'vitest'
import React from 'react'
import {cleanup} from '@testing-library/react'

// jsdom 27 does not expose `localStorage` on the window it hands Vitest, so
// every store built on zustand's `persist` throws on import and the whole
// suite fails before a single assertion runs. An in-memory Storage is enough:
// the tests exercise what the app does with persisted preferences, never the
// browser's own implementation of them.
class MemoryStorage implements Storage {
  private entries = new Map<string, string>()
  get length() {return this.entries.size}
  clear() {this.entries.clear()}
  getItem(key: string) {return this.entries.has(key) ? this.entries.get(key)! : null}
  key(index: number) {return Array.from(this.entries.keys())[index] ?? null}
  removeItem(key: string) {this.entries.delete(key)}
  setItem(key: string, value: string) {this.entries.set(key, String(value))}
}

function installStorage(name: 'localStorage' | 'sessionStorage') {
  const target = globalThis as unknown as Record<string, unknown>
  if (target[name]) return
  const storage = new MemoryStorage()
  Object.defineProperty(globalThis, name, {value: storage, configurable: true, writable: true})
  if (typeof window !== 'undefined') Object.defineProperty(window, name, {value: storage, configurable: true, writable: true})
}

installStorage('localStorage')
installStorage('sessionStorage')

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
})
afterEach(()=>cleanup())
const series = () => ({setData: vi.fn(), update: vi.fn(), setMarkers: vi.fn(), priceScale: () => ({applyOptions: vi.fn()})})
// Plotly needs a real canvas and layout engine; tests only need the traces it is given.
vi.mock('plotly.js-dist-min', () => ({default: {}}))
vi.mock('react-plotly.js/factory', () => ({default: () => ({data}: {data: {name?: string}[]}) => React.createElement('div', {'data-testid': 'plotly-chart', 'data-traces': data.map((trace) => trace.name).join('|')})}))
vi.mock('lightweight-charts', () => ({
  ColorType: {Solid: 'Solid'},
  PriceScaleMode: {Normal: 0, Percentage: 2},
  createChart: () => ({
    addAreaSeries: series, addLineSeries: series, addCandlestickSeries: series, addHistogramSeries: series,
    applyOptions: vi.fn(), timeScale: () => ({fitContent: vi.fn()}), priceScale: () => ({applyOptions: vi.fn()}),
    subscribeCrosshairMove: vi.fn(), unsubscribeCrosshairMove: vi.fn(), removeSeries: vi.fn(), remove: vi.fn(),
  }),
}))
