import {useState} from 'react'
import {QueryClient, QueryClientProvider} from '@tanstack/react-query'
import {render, screen, waitFor} from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import {BrokerInstrumentSearch} from '../src/components/BrokerInstrumentSearch'
import {usePreferencesStore} from '../src/stores/preferences'


const session = {
  id: 'session-search',
  display_name: 'Connected paper',
  username_hint: 'du-test',
  mode: 'paper',
  status: 'CONNECTED',
  connected: true,
  commands_enabled: true,
  container_status: 'running',
  account_count: 1,
  last_error: '',
  last_gateway_state: {connected: true},
  created_at: '',
  updated_at: '',
  provisioned_at: null,
  connected_at: null,
  last_checked_at: null,
  deleted_at: null,
  needs_novnc: false,
  novnc_url: null,
}

function envelope(data: unknown) {
  return {ok: true, status: 200, json: async () => ({ok: true, data, error: null, meta: {}})} as Response
}

function Harness({portfolioId, gatewaySessionId, allowOptions}: {portfolioId?: number; gatewaySessionId?: string; allowOptions?: boolean} = {}) {
  const [value,setValue]=useState('')
  return <BrokerInstrumentSearch value={value} onValueChange={setValue} onResolved={() => undefined} portfolioId={portfolioId} gatewaySessionId={gatewaySessionId} allowOptions={allowOptions} />
}

function renderSearch(props: {portfolioId?: number; gatewaySessionId?: string; allowOptions?: boolean} = {}) {
  const client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}})
  return render(<QueryClientProvider client={client}><Harness {...props} /></QueryClientProvider>)
}

beforeEach(() => {
  usePreferencesStore.setState({
    selectedSessionId: session.id,
    selectedAccountId: null,
    selectedPortfolioId: null,
  })
})

afterEach(() => vi.unstubAllGlobals())


test('requires two characters and debounces IBKR symbol searches', async () => {
  const searchUrls:string[]=[]
  vi.stubGlobal('fetch',vi.fn(async (input:string) => {
    const url=String(input)
    if (url.includes('/broker-sessions/')) return envelope([session])
    if (url.includes('/instruments/search/')) {
      searchUrls.push(url)
      return envelope([{
        symbol:'AP',local_symbol:'AP',conid:42,asset_class:'STK',exchange:'SMART',
        primary_exchange:'NYSE',currency:'USD',description:'Ampco-Pittsburgh',instrument_id:null,
      }])
    }
    return envelope([])
  }))
  const user=userEvent.setup()
  renderSearch()
  const input=screen.getByLabelText('Ticker')

  await user.type(input,'A')
  expect(await screen.findByText('Enter at least 2 characters.')).toBeInTheDocument()
  await new Promise((resolve) => window.setTimeout(resolve,450))
  expect(searchUrls).toHaveLength(0)

  await user.type(input,'P')
  expect(await screen.findByRole('button',{name:'Select AP NYSE USD'})).toBeInTheDocument()
  expect(searchUrls).toHaveLength(1)
  expect(searchUrls[0]).toContain('query=AP')
  expect(searchUrls[0]).toContain(`session_id=${session.id}`)
})


test('aborts a stale frontend search when the ticker changes', async () => {
  let firstSignal:AbortSignal|undefined
  vi.stubGlobal('fetch',vi.fn((input:string,init?:RequestInit) => {
    const url=String(input)
    if (url.includes('/broker-sessions/')) return Promise.resolve(envelope([session]))
    if (url.includes('query=AAP')) return Promise.resolve(envelope([{
      symbol:'AAP',local_symbol:'AAP',conid:43,asset_class:'STK',exchange:'SMART',
      primary_exchange:'NYSE',currency:'USD',description:'Advance Auto Parts',instrument_id:null,
    }]))
    if (url.includes('query=AA&')) {
      firstSignal=init?.signal as AbortSignal|undefined
      return new Promise<Response>((_resolve,reject) => {
        firstSignal?.addEventListener('abort',() => reject(new DOMException('Aborted','AbortError')))
      })
    }
    return Promise.resolve(envelope([]))
  }))
  const user=userEvent.setup()
  renderSearch()
  const input=screen.getByLabelText('Ticker')

  await user.type(input,'AA')
  await waitFor(() => expect(firstSignal).toBeDefined(),{timeout:1500})
  await user.type(input,'P')

  expect(await screen.findByRole('button',{name:'Select AAP NYSE USD'})).toBeInTheDocument()
  await waitFor(() => expect(firstSignal?.aborted).toBe(true))
  expect(screen.queryByRole('button',{name:/Select AA /})).not.toBeInTheDocument()
})

test('routes qualification through the portfolio-assigned Gateway instead of the global selection', async () => {
  const assigned = {...session, id: 'portfolio-session', display_name: 'Portfolio Gateway'}
  usePreferencesStore.setState({selectedSessionId: 'different-global-session'})
  const urls:string[]=[]
  vi.stubGlobal('fetch',vi.fn(async (input:string) => {
    const url=String(input)
    if (url.includes('/broker-sessions/')) return envelope([assigned])
    if (url.includes('/instruments/search/')) {
      urls.push(url)
      return envelope([{
        symbol:'NVDA',local_symbol:'NVDA',conid:4815747,asset_class:'STK',exchange:'SMART',
        primary_exchange:'NASDAQ',currency:'USD',description:'NVIDIA',instrument_id:5,
      }])
    }
    if (url.includes('/instruments/resolve/')) return envelope({
      instrument_id:5,symbol:'NVDA',asset_class:'STK',exchange:'SMART',currency:'USD',
      conid:4815747,primary_exchange:'NASDAQ',qualification_command:{},
    })
    return envelope([])
  }))
  const user=userEvent.setup()
  renderSearch({portfolioId: 10, gatewaySessionId: assigned.id})
  await user.type(screen.getByLabelText('Ticker'),'NV')
  await user.click(await screen.findByRole('button',{name:'Select NVDA NASDAQ USD'}))
  await user.click(screen.getByRole('button',{name:'Qualify selected contract'}))
  await screen.findByText('QUALIFIED')
  expect(urls[0]).toContain(`session_id=${assigned.id}`)
  expect(urls[0]).toContain('portfolio_id=10')
  expect(urls[0]).not.toContain('different-global-session')
  const resolveCall=vi.mocked(fetch).mock.calls.find(([input]) => String(input).includes('/instruments/resolve/'))
  expect(JSON.parse(String(resolveCall?.[1]?.body))).toMatchObject({
    portfolio_id:10,
    session_id:assigned.id,
  })
})

test('searches and qualifies an exact option contract without country or currency filtering', async () => {
  const urls:string[]=[]
  vi.stubGlobal('fetch',vi.fn(async (input:string,init?:RequestInit) => {
    const url=String(input)
    if (url.includes('/broker-sessions/')) return envelope([session])
    if (url.includes('/instruments/search/')) {
      urls.push(url)
      return envelope([{
        symbol:'NIFTY',local_symbol:'NIFTY 50',conid:1234,asset_class:'IND',
        exchange:'NSE',primary_exchange:'NSE',currency:'INR',description:'NIFTY 50',instrument_id:null,
      }])
    }
    if (url.includes('/instruments/options/chain/')) return envelope({
      underlying:{symbol:'NIFTY',local_symbol:'NIFTY 50',conid:1234,asset_class:'IND',exchange:'NSE',primary_exchange:'NSE',currency:'INR',description:'NIFTY 50',instrument_id:90},
      chains:[{exchange:'NFO',trading_class:'NIFTY',multiplier:'75',expirations:['2026-08-26'],strikes:['25000']}],
    })
    if (url.includes('/instruments/options/resolve/')) return envelope({
      instrument_id:91,symbol:'NIFTY26AUG25000CE',conid:7654321,asset_class:'OPT',exchange:'NFO',
      primary_exchange:'NSE',currency:'INR',expiration:'2026-08-26',strike:'25000',right:'C',
      multiplier:'75',trading_class:'NIFTY',underlying_conid:1234,qualification_command:null,
    })
    if (url.includes('/instruments/resolve/')) {
      const request=JSON.parse(String(init?.body))
      return envelope({...request,instrument_id:90,conid:1234,qualification_command:null})
    }
    return envelope([])
  }))
  const user=userEvent.setup()
  renderSearch({portfolioId:10,gatewaySessionId:session.id,allowOptions:true})
  await user.click(screen.getByRole('button',{name:'Options'}))
  const input=screen.getByLabelText('Ticker')
  await user.type(input,'NIFTY')
  await user.click(await screen.findByRole('button',{name:'Select NIFTY NSE INR'}))
  await user.click(screen.getByRole('button',{name:'Qualify underlying & load chain'}))
  await screen.findByLabelText('Option expiration')
  await user.click(screen.getByRole('button',{name:'Qualify exact option'}))
  await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([value]) => String(value).includes('/instruments/options/resolve/'))).toBe(true))
  expect(urls[0]).toContain('asset_classes=STK%2CIND')
  expect(urls[0]).not.toContain('country=')
  expect(urls[0]).not.toContain('currency=')
  const resolveCall=vi.mocked(fetch).mock.calls.find(([value]) => String(value).includes('/instruments/options/resolve/'))
  expect(JSON.parse(String(resolveCall?.[1]?.body))).toMatchObject({
    underlying_instrument_id:90,expiration:'2026-08-26',strike:'25000',right:'C',multiplier:'75',
  })
})
