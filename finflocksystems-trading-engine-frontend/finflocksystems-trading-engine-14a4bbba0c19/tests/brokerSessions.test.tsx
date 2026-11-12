import {fireEvent, render, screen, waitFor, within} from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import {beforeEach, afterEach, expect, test, vi} from 'vitest'

import App from '../src/App'
import {queryClient} from '../src/app/queryClient'
import {usePreferencesStore} from '../src/stores/preferences'


const now='2026-07-20T06:00:00Z'
const sessions=[
  {id:'11111111-1111-1111-1111-111111111111',display_name:'Paper alpha',username_hint:'pa••er',mode:'paper',status:'CONNECTED',connected:true,commands_enabled:true,container_status:'RUNNING',account_count:1,last_error:'',last_gateway_state:{reconciled:true},created_at:now,updated_at:now,provisioned_at:now,connected_at:now,last_checked_at:now,deleted_at:null,needs_novnc:false,novnc_url:'http://backend/api/v1/broker-sessions/111/novnc/connect/#access_token=paper',market_data_type:'DELAYED'},
  {id:'22222222-2222-2222-2222-222222222222',display_name:'Live beta',username_hint:'li••ve',mode:'live',status:'WAITING_FOR_2FA',connected:false,commands_enabled:false,container_status:'RUNNING',account_count:1,last_error:'',last_gateway_state:{connected:false},created_at:now,updated_at:now,provisioned_at:now,connected_at:null,last_checked_at:now,deleted_at:null,needs_novnc:true,novnc_url:'http://backend/api/v1/broker-sessions/222/novnc/connect/#access_token=live',market_data_type:'LIVE'},
]
const accounts={
  [sessions[0].id]:[{id:1,account_id:'DU-PAPER',alias:'Paper account',base_currency:'USD',net_liquidation:100000,available_cash:50000,buying_power:200000,daily_pnl:50,is_reconciled:true,kill_switch:false,updated_at:now,available:true,last_seen_at:now,default_portfolio_id:10}],
  [sessions[1].id]:[{id:2,account_id:'U-LIVE',alias:'Live account',base_currency:'USD',net_liquidation:200000,available_cash:75000,buying_power:300000,daily_pnl:-10,is_reconciled:false,kill_switch:false,updated_at:now,available:true,last_seen_at:now,default_portfolio_id:20}],
}

function pathFor(input:string){return (new URL(input,'http://localhost').pathname.split('/api/v1/')[1]||'').replace(/\/$/,'')}

let requests:{path:string;method:string;body:Record<string,unknown>}[]=[]
let brokerDeployment={available:true,ready:true,missing:[] as string[],invalid:[] as string[]}

beforeEach(()=>{
  window.history.replaceState({},'','/ibkr-sessions')
  localStorage.clear();queryClient.clear();requests=[]
  brokerDeployment={available:true,ready:true,missing:[],invalid:[]}
  usePreferencesStore.setState({selectedSessionId:null,selectedAccountId:null,selectedPortfolioId:null})
  vi.stubGlobal('fetch',vi.fn(async(input:string,init?:RequestInit)=>{
    const path=pathFor(input);const method=init?.method||'GET';const body=init?.body?JSON.parse(String(init.body)):{}
    requests.push({path,method,body})
    let data:unknown=[]
    if(path==='broker-sessions'&&method==='GET')data=sessions
    else if(path===`broker-sessions/${sessions[0].id}/accounts`)data=accounts[sessions[0].id]
    else if(path===`broker-sessions/${sessions[1].id}/accounts`)data=accounts[sessions[1].id]
    else if(path==='broker-sessions'&&method==='POST')data={...sessions[0],id:'33333333-3333-3333-3333-333333333333',display_name:String(body.display_name)}
    else if(method==='DELETE')data={session:{...sessions[0],status:'DELETED'},container_deleted:true}
    else if(path==='accounts')data=[...accounts[sessions[0].id],...accounts[sessions[1].id]]
    else if(path==='portfolios')data=[{id:10,name:'Paper route',account_id:1,gateway_session_id:sessions[0].id},{id:20,name:'Live route',account_id:2,gateway_session_id:sessions[1].id}]
    else if(path==='system')data={mode:'MULTI_SESSION',broker_deployment:brokerDeployment,global_kill_switch:false,material_breaks:0,time:now}
    else if(path==='positions')data=[]
    return {ok:true,status:method==='POST'?202:200,json:async()=>({ok:true,data,error:null,meta:{}})} as Response
  }))
})

afterEach(()=>vi.unstubAllGlobals())

test('starts and manages multiple paper/live sessions without persisting passwords',async()=>{
  const user=userEvent.setup();render(<App />)
  expect(await screen.findByRole('heading',{name:'IBKR Sessions'})).toBeInTheDocument()
  expect(screen.getAllByRole('radio')).toHaveLength(4)
  expect(await screen.findByRole('heading',{name:'Paper alpha'})).toBeInTheDocument()
  expect(screen.getByRole('heading',{name:'Live beta'})).toBeInTheDocument()
  expect(screen.getByText('Open noVNC and complete IBKR 2FA.')).toBeInTheDocument()
  expect(screen.getAllByRole('link',{name:/Open noVNC/})[1]).toHaveAttribute('href',sessions[1].novnc_url)

  await user.type(screen.getByLabelText('Display name'),'Another live')
  await user.type(screen.getByLabelText('IBKR username'),'operator-user')
  await user.type(screen.getByLabelText('IBKR password'),'never-store-this')
  await user.click(screen.getByLabelText('Live'))
  const start=screen.getByRole('button',{name:'Start session'})
  // The market-data question has no default, and live cannot answer "No".
  expect(start).toBeDisabled()
  expect(screen.getByLabelText('No, use delayed data')).toBeDisabled()
  await user.click(screen.getByLabelText('Yes, use real-time data'))
  await waitFor(()=>expect(start).toBeEnabled())
  await user.click(start)
  await waitFor(()=>expect(requests.some((item)=>item.path==='broker-sessions'&&item.method==='POST'&&item.body.mode==='live'&&item.body.market_data_subscription===true)).toBe(true))
  expect(screen.getByLabelText('IBKR password')).toHaveValue('')
  expect(localStorage.getItem('finflock-broker-selection')).not.toContain('never-store-this')

  await user.click(within(screen.getByRole('heading',{name:'Live beta'}).closest('article')!).getByRole('button',{name:'Select'}))
  expect(await within(await screen.findByLabelText('Session account')).findByRole('option',{name:'Live account'})).toBeInTheDocument()
  await waitFor(()=>expect(localStorage.getItem('finflock-broker-selection')).toContain(sessions[1].id))

  await user.click(within(screen.getByRole('heading',{name:'Paper alpha'}).closest('article')!).getByRole('button',{name:'Delete'}))
  expect(screen.getByText(/existing IBKR orders are not automatically cancelled/i)).toBeInTheDocument()
  await user.click(screen.getByRole('button',{name:'Delete session'}))
  await waitFor(()=>expect(requests.some((item)=>item.method==='DELETE')).toBe(true))
})


test('asks whether the login has market-data subscriptions and sends the answer',async()=>{
  const user=userEvent.setup();render(<App />)
  expect(await screen.findByRole('heading',{name:'Paper alpha'})).toBeInTheDocument()
  expect(within(screen.getByRole('heading',{name:'Paper alpha'}).closest('article')!).getByText('Delayed')).toBeInTheDocument()
  expect(within(screen.getByRole('heading',{name:'Live beta'}).closest('article')!).getByText('Real-time')).toBeInTheDocument()

  // A paper login without subscriptions asks for IBKR's delayed feed.
  await user.type(screen.getByLabelText('IBKR username'),'paper-user')
  await user.type(screen.getByLabelText('IBKR password'),'paper-pass')
  await user.click(screen.getByLabelText('No, use delayed data'))
  await user.click(screen.getByRole('button',{name:'Start session'}))
  await waitFor(()=>expect(requests.some((item)=>item.path==='broker-sessions'&&item.method==='POST'&&item.body.mode==='paper'&&item.body.market_data_subscription===false)).toBe(true))

  // Re-entering credentials starts from the session's current answer and can change it.
  const paper=screen.getByRole('heading',{name:'Paper alpha'}).closest('article')!
  await user.click(within(paper).getByRole('button',{name:/Re-enter credentials/}))
  expect(within(paper).getByLabelText('No, use delayed data')).toBeChecked()
  await user.type(within(paper).getByLabelText('Replacement username for Paper alpha'),'subscribed-user')
  await user.type(within(paper).getByLabelText('Replacement password for Paper alpha'),'subscribed-pass')
  await user.click(within(paper).getByLabelText('Yes, use real-time data'))
  await user.click(within(paper).getByRole('button',{name:'Submit credentials'}))
  await waitFor(()=>expect(requests.some((item)=>item.path===`broker-sessions/${sessions[0].id}/credentials`&&item.body.market_data_subscription===true)).toBe(true))
})


test('keeps sessions viewable and blocks creation when managed Gateway configuration is missing',async()=>{
  brokerDeployment={
    available:false,
    ready:false,
    missing:['IBKR_GATEWAY_IMAGE','QCH_API_HOST','QCH_SERVICE_TOKEN'],
    invalid:[],
  }
  render(<App />)

  expect(await screen.findByRole('heading',{name:'Paper alpha'})).toBeInTheDocument()
  expect(screen.getByRole('heading',{name:'Live beta'})).toBeInTheDocument()
  const message=await screen.findByRole('status')
  expect(message).toHaveTextContent('Managed IB Gateway session creation is unavailable.')
  expect(message).toHaveTextContent('IBKR_GATEWAY_IMAGE, QCH_API_HOST, QCH_SERVICE_TOKEN')
  expect(message).not.toHaveTextContent('qch-secret')

  const start=screen.getByRole('button',{name:'Start session'})
  expect(start).toBeDisabled()
  fireEvent.submit(start.closest('form')!)
  await waitFor(()=>expect(requests.filter((item)=>item.path==='broker-sessions'&&item.method==='POST')).toHaveLength(0))
})
