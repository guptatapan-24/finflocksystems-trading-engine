import type {DecimalValue} from '../api/types'
import {formatNumber, toNumber} from './ui'

export function FillProgress({filled, total}: {filled: DecimalValue; total: DecimalValue}) {
  const totalNumber = toNumber(total)
  const filledNumber = toNumber(filled)
  const progress = totalNumber > 0 ? Math.min(100, Math.max(0, filledNumber / totalNumber * 100)) : 0
  return <div className="fill-progress"><div><span>{formatNumber(filled)} / {formatNumber(total)}</span><span>{progress.toFixed(0)}%</span></div><div className="progress-track"><span style={{width: `${progress}%`}} /></div></div>
}
