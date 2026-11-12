import type {BrokerAccount, Portfolio, Position, SystemStatus} from '../../api/types'
import {formatDateTime, formatMoney} from '../ui'

export function MarketTickerBar({account, portfolio, positions, system}: {
  account: BrokerAccount | null
  portfolio: Portfolio | null
  positions: Position[]
  system?: SystemStatus
}) {
  const latestPosition = positions.reduce<string | null>((latest, item) => !latest || item.updated_at > latest ? item.updated_at : latest, null)
  const symbols = positions.map((item) => item.symbol).filter((symbol, index, all) => all.indexOf(symbol) === index)
  const pnl = Number(account?.daily_pnl || 0)
  return <section className="market-ticker" aria-label="Selected portfolio market strip">
    <div className="ticker-state"><span className="ticker-pulse" aria-hidden="true" /><strong>{system?.mode || 'UNKNOWN'}</strong><span>SESSION</span></div>
    <div><span>PORTFOLIO</span><strong>{portfolio?.name || 'Not selected'}</strong></div>
    <div><span>NAV</span><strong className="mono">{formatMoney(account?.net_liquidation, account?.base_currency)}</strong></div>
    <div><span>DAILY P&amp;L</span><strong className={`mono ${pnl > 0 ? 'positive-text' : pnl < 0 ? 'negative-text' : ''}`}>{formatMoney(account?.daily_pnl, account?.base_currency)}</strong></div>
    <div><span>HELD</span><strong className="mono">{symbols.length ? symbols.join(' / ') : 'No positions'}</strong></div>
    <div className="ticker-freshness"><span>POSITION DATA</span><strong>{formatDateTime(latestPosition)}</strong></div>
  </section>
}
