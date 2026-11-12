export interface TerminalChartTheme {
  text: string
  grid: string
  border: string
  crosshair: string
  positive: string
  negative: string
  palette: string[]
  volumeUp: string
  volumeDown: string
}

export function buildChartTheme(readVariable?: (name: string) => string): TerminalChartTheme {
  const read = readVariable || ((name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim())
  const value = (name: string, fallback: string) => read(name) || fallback
  return {
    text: value('--text-muted', '#667386'),
    grid: value('--chart-grid', '#1c2634'),
    border: value('--border', '#263244'),
    crosshair: value('--chart-crosshair', '#4b5c73'),
    positive: value('--positive', '#28b781'),
    negative: value('--critical', '#ef5f67'),
    palette: [
      value('--chart-1', '#4f8cff'), value('--chart-2', '#9b7bff'), value('--chart-3', '#28b781'),
      value('--chart-4', '#d9a441'), value('--chart-5', '#49a8c7'),
    ],
    volumeUp: value('--positive-soft', 'rgb(40 183 129 / 35%)'),
    volumeDown: value('--critical-soft', 'rgb(239 95 103 / 35%)'),
  }
}
