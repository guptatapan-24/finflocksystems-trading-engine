import type {CSSProperties} from 'react'
import createPlotlyComponent from 'react-plotly.js/factory'
import Plotly from 'plotly.js-dist-min'

// The minified bundle, not the full `plotly.js` build react-plotly.js would
// pull in by default. This module is only reached through React.lazy, so it
// stays out of the main chunk.
const Plot = createPlotlyComponent(Plotly)

export type PlotTrace = Record<string, unknown>
export type PlotLayout = Record<string, unknown> & {xaxis?: Record<string, unknown>; yaxis?: Record<string, unknown>}

export const plotConfig = {
  responsive: true,
  displaylogo: false,
  scrollZoom: true,
  modeBarButtonsToRemove: ['lasso2d', 'select2d'],
}

const axis = {gridcolor: '#252a2e', zerolinecolor: '#3a4045', color: '#81878d', automargin: true}

export function terminalLayout(layout: PlotLayout = {}): PlotLayout {
  return {
    paper_bgcolor: 'transparent',
    plot_bgcolor: 'transparent',
    font: {color: '#9aa0a5', family: 'ui-monospace, SFMono-Regular, Consolas, monospace', size: 10},
    margin: {l: 58, r: 24, t: 38, b: 52},
    hovermode: 'closest',
    autosize: true,
    legend: {orientation: 'h', y: 1.12, font: {size: 9}},
    ...layout,
    xaxis: {...axis, ...layout.xaxis},
    yaxis: {...axis, ...layout.yaxis},
  }
}

export default function PlotlyChart({data, layout, style}: {data: PlotTrace[]; layout?: PlotLayout; style?: CSSProperties}) {
  return <Plot useResizeHandler className="plot" style={{width: '100%', height: 390, ...style}} config={plotConfig} data={data} layout={terminalLayout(layout)} />
}
