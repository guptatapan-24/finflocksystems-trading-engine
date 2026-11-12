declare module 'plotly.js-dist-min' {
  const Plotly: unknown
  export default Plotly
}

declare module 'react-plotly.js/factory' {
  import type {ComponentType, CSSProperties} from 'react'

  export interface PlotParams {
    data: Record<string, unknown>[]
    layout?: Record<string, unknown>
    config?: Record<string, unknown>
    className?: string
    style?: CSSProperties
    useResizeHandler?: boolean
  }

  export default function createPlotlyComponent(plotly: unknown): ComponentType<PlotParams>
}
