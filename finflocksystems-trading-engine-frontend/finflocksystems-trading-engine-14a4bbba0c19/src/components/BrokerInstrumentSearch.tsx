import {useEffect, useId, useState} from 'react'
import {useMutation, useQuery} from '@tanstack/react-query'
import {Radar} from 'lucide-react'

import {mutationOptions, request, withQuery} from '../api/client'
import {queries} from '../api/queries'
import type {InstrumentResolution, InstrumentSearchResult, OptionChain} from '../api/types'
import {usePreferencesStore} from '../stores/preferences'
import {ErrorState, StatusBadge} from './ui'

const MINIMUM_QUERY_LENGTH = 2
const SEARCH_DEBOUNCE_MS = 400

export function BrokerInstrumentSearch({value, onValueChange, onContractSelected, onResolved, suggestions = [], autoFocus = false, searchLabel = 'Ticker', portfolioId, gatewaySessionId, allowOptions = false, includeIndices = false}: {
  value: string
  onValueChange: (value: string) => void
  onContractSelected?: (contract: InstrumentSearchResult) => void
  onResolved: (resolution: InstrumentResolution | null) => void
  suggestions?: {id: number; symbol: string}[]
  autoFocus?: boolean
  searchLabel?: string
  portfolioId?: number | null
  gatewaySessionId?: string | null
  allowOptions?: boolean
  includeIndices?: boolean
}) {
  const [assetClass, setAssetClass] = useState<'STK' | 'OPT'>('STK')
  const [selected, setSelected] = useState<InstrumentSearchResult | null>(null)
  const [resolution, setResolution] = useState<InstrumentResolution | null>(null)
  const [searchQuery, setSearchQuery] = useState('')
  const [chainIndex, setChainIndex] = useState(0)
  const [expiration, setExpiration] = useState('')
  const [strike, setStrike] = useState('')
  const [right, setRight] = useState<'C' | 'P'>('C')
  const globallySelectedSessionId = usePreferencesStore((state) => state.selectedSessionId)
  const sessionId = gatewaySessionId === undefined
    ? globallySelectedSessionId
    : gatewaySessionId
  const sessions = useQuery(queries.brokerSessions())
  const selectedSession = (sessions.data || []).find((item) => item.id === sessionId)
  const sessionReady = Boolean(selectedSession?.connected && selectedSession.commands_enabled)
  const suggestionsId = useId()
  useEffect(() => {
    if (!value) {
      setSelected(null)
      setResolution(null)
    }
  }, [value])
  useEffect(() => {
    const next = value.trim()
    if (next.length < MINIMUM_QUERY_LENGTH) {
      setSearchQuery('')
      return
    }
    const timer = window.setTimeout(() => setSearchQuery(next), SEARCH_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [value])
  useEffect(() => {
    setSelected(null)
    setResolution(null)
    onResolved(null)
  }, [sessionId])
  const search = useQuery({
    queryKey: ['instrument-search', sessionId, assetClass, searchQuery],
    queryFn: ({signal}) => request<InstrumentSearchResult[]>(
      withQuery('instruments/search/', {
        query: searchQuery,
        session_id: sessionId,
        portfolio_id: portfolioId,
        asset_classes: assetClass === 'OPT' || includeIndices ? 'STK,IND' : 'STK',
      }),
      {signal},
    ),
    enabled: sessionReady && searchQuery.length >= MINIMUM_QUERY_LENGTH,
    staleTime: 60_000,
  })
  const chain = useMutation({
    mutationFn: (underlying: InstrumentResolution) => request<OptionChain>('instruments/options/chain/', mutationOptions('POST', {
      underlying_instrument_id: underlying.instrument_id, session_id: sessionId, portfolio_id: portfolioId,
    }, true)),
    onSuccess: (data) => {
      setChainIndex(0)
      setExpiration(data.chains[0]?.expirations[0] || '')
      setStrike(data.chains[0]?.strikes[0] || '')
    },
  })
  const resolve = useMutation({
    mutationFn: () => {
      if (!selected) throw new Error('Select an exact IBKR contract first.')
      return request<InstrumentResolution>('instruments/resolve/', mutationOptions('POST', {
        ...selected, ticker: selected.symbol, qualify: true, session_id: sessionId,
        portfolio_id: portfolioId,
      }, true))
    },
    onSuccess: (data) => {
      setResolution(data)
      if (assetClass === 'OPT') chain.mutate(data)
      else onResolved(data)
    },
  })
  const resolveOption = useMutation({
    mutationFn: () => {
      const definition=chain.data?.chains[chainIndex]
      if (!resolution || !definition || !expiration || !strike) throw new Error('Select a complete option contract first.')
      return request<InstrumentResolution>('instruments/options/resolve/', mutationOptions('POST', {
        underlying_instrument_id:resolution.instrument_id,expiration,strike,right,
        multiplier:definition.multiplier,trading_class:definition.trading_class,exchange:definition.exchange,
        session_id:sessionId,portfolio_id:portfolioId,
      }, true))
    },
    onSuccess: (data) => {setResolution(data); onResolved(data)},
  })
  const updateValue = (next: string) => {
    setSelected(null)
    setResolution(null)
    onResolved(null)
    onValueChange(next.toUpperCase())
  }
  const changeAssetClass = (next: 'STK' | 'OPT') => {
    setAssetClass(next)
    setSelected(null)
    setResolution(null)
    setSearchQuery('')
    chain.reset()
    resolveOption.reset()
    onResolved(null)
    onValueChange('')
  }
  const select = (contract: InstrumentSearchResult) => {
    setSelected(contract)
    setResolution(null)
    onResolved(null)
    onValueChange(contract.symbol)
    onContractSelected?.(contract)
  }
  return <div className="broker-instrument-search">
    {allowOptions && <div className="instrument-type-switch" role="group" aria-label="Instrument type"><button type="button" className={assetClass === 'STK' ? 'selected' : ''} aria-pressed={assetClass === 'STK'} onClick={() => changeAssetClass('STK')}>Stocks</button><button type="button" className={assetClass === 'OPT' ? 'selected' : ''} aria-pressed={assetClass === 'OPT'} onClick={() => changeAssetClass('OPT')}>Options</button></div>}
    <label>IBKR {allowOptions ? (assetClass === 'OPT' ? 'option' : 'stock') : 'instrument'} search<input aria-label={searchLabel} value={value} list={suggestionsId} placeholder={assetClass === 'OPT' ? 'Underlying or option symbol' : 'Ticker or company name'} onChange={(event) => updateValue(event.target.value)} autoFocus={autoFocus} /><datalist id={suggestionsId}>{suggestions.map((item) => <option key={item.id} value={item.symbol} />)}</datalist></label>
    <div className="contract-search-results" aria-live="polite">
      {!sessionId && <p>Select a connected broker session before searching.</p>}
      {sessionId && !sessionReady && <p>The selected broker session is not connected or command-ready.</p>}
      {sessionReady && value.trim().length > 0 && value.trim().length < MINIMUM_QUERY_LENGTH && <p>Enter at least {MINIMUM_QUERY_LENGTH} characters.</p>}
      {search.isFetching && <p>Searching IBKR contracts...</p>}
      {!search.isFetching && search.isSuccess && searchQuery === value.trim() && !search.data?.length && <p>No matching IBKR contracts.</p>}
      {(search.data || []).map((contract) => <button type="button" className={selected?.conid === contract.conid ? 'selected' : ''} key={contract.conid} aria-label={`Select ${contract.symbol} ${contract.primary_exchange || contract.exchange} ${contract.currency}`} onClick={() => select(contract)}><span><strong>{contract.symbol}</strong><small>{contract.description || contract.local_symbol}</small></span><span><code>{contract.local_symbol}</code><small>{contract.asset_class} / {contract.exchange} / {contract.primary_exchange || 'No primary'} / {contract.currency}</small></span><code>conId {contract.conid}</code></button>)}
    </div>
    <div className="contract-card"><div><Radar /><div><strong>{assetClass === 'OPT' ? 'Option underlying qualification' : 'Exact IBKR contract qualification'}</strong><p>{resolution?.conid ? `${resolution.symbol} conId ${resolution.conid} qualified on ${resolution.primary_exchange || resolution.exchange}.` : selected ? `${selected.symbol} on ${selected.primary_exchange || selected.exchange} is selected and ready to qualify.` : assetClass === 'OPT' ? 'Select the stock or index underlying first.' : 'Select one search result. Ambiguous matches are never chosen automatically.'}</p></div></div><StatusBadge status={resolution?.conid ? 'QUALIFIED' : selected ? 'SELECTED' : 'NOT SELECTED'} /><button type="button" className="button-secondary" disabled={resolve.isPending || chain.isPending || !selected || !sessionReady} onClick={() => resolve.mutate()}>{resolve.isPending || chain.isPending ? 'Loading...' : assetClass === 'OPT' ? 'Qualify underlying & load chain' : 'Qualify selected contract'}</button>{selected && <code>conId {selected.conid}</code>}</div>
    {assetClass === 'OPT' && chain.data && <div className="option-chain-picker">
      <label>Trading class<select aria-label="Option trading class" value={chainIndex} onChange={(event) => {const index=Number(event.target.value);const next=chain.data?.chains[index];setChainIndex(index);setExpiration(next?.expirations[0] || '');setStrike(next?.strikes[0] || '')}}>{chain.data.chains.map((item,index) => <option key={`${item.exchange}:${item.trading_class}:${item.multiplier}`} value={index}>{item.trading_class} · {item.exchange} · lot {item.multiplier}</option>)}</select></label>
      <label>Expiration<select aria-label="Option expiration" value={expiration} onChange={(event) => setExpiration(event.target.value)}>{chain.data.chains[chainIndex]?.expirations.map((item) => <option key={item}>{item}</option>)}</select></label>
      <label>Strike<select aria-label="Option strike" value={strike} onChange={(event) => setStrike(event.target.value)}>{chain.data.chains[chainIndex]?.strikes.map((item) => <option key={item}>{item}</option>)}</select></label>
      <label>Right<select aria-label="Option right" value={right} onChange={(event) => setRight(event.target.value as 'C' | 'P')}><option value="C">Call</option><option value="P">Put</option></select></label>
      <button type="button" className="button-primary" disabled={resolveOption.isPending || !expiration || !strike} onClick={() => resolveOption.mutate()}>{resolveOption.isPending ? 'Qualifying option...' : 'Qualify exact option'}</button>
    </div>}
    {(search.isError || resolve.isError || chain.isError || resolveOption.isError) && <ErrorState title={search.isError ? 'Instrument search failed' : chain.isError ? 'Option chain failed' : 'Contract qualification failed'} error={search.error || resolve.error || chain.error || resolveOption.error} compact />}
  </div>
}
