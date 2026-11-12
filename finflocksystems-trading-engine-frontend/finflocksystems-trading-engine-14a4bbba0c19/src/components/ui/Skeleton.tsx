export function Skeleton({lines = 3, height}: {lines?: number; height?: number}) {
  return <div className="skeleton" aria-label="Loading" style={height ? {minHeight: height} : undefined}>{Array.from({length: lines}, (_, index) => <span key={index} style={{width: `${Math.max(38, 100 - index * 14)}%`}} />)}</div>
}

