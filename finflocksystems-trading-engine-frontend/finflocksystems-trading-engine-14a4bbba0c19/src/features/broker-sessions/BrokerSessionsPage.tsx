import {useEffect, useMemo, useState} from 'react'
import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query'
import {ExternalLink, KeyRound, Plus, RefreshCw, Trash2} from 'lucide-react'
import {queries} from '../../api/queries'
import {mutationOptions, request} from '../../api/client'
import type {BrokerGatewaySession, BrokerSessionAccount, BrokerSessionMode} from '../../api/types'
import {ConfirmActionDialog, EmptyState, ErrorState, PageHeader, StatusBadge} from '../../components/ui'
import {formatDateTime, formatMoney} from '../../components/ui/format'
import {usePreferencesStore} from '../../stores/preferences'

interface StartPayload {display_name: string; username: string; password: string; mode: BrokerSessionMode; market_data_subscription: boolean}

// IBKR serves real-time data to a subscribed login even when delayed is
// requested, and the backend accepts only the declared type, so the answer
// must match the login's entitlement. A live Gateway never runs on delayed data.
function MarketDataSubscriptionField({name,mode,value,onChange}:{name:string;mode:BrokerSessionMode;value:boolean|null;onChange:(value:boolean)=>void}) {
  return <fieldset className="mode-control market-data-control">
    <legend>Does this IBKR login have market-data subscriptions?</legend>
    <label><input type="radio" name={name} checked={value===true} required onChange={()=>onChange(true)} />Yes, use real-time data</label>
    <label><input type="radio" name={name} checked={value===false} disabled={mode==='live'} onChange={()=>onChange(false)} />No, use delayed data</label>
    {mode==='live'&&<span className="field-hint">Live sessions require real-time subscriptions.</span>}
  </fieldset>
}

export function BrokerSessionsPage() {
  const queryClient=useQueryClient()
  const sessions=useQuery(queries.brokerSessions())
  const system=useQuery(queries.system())
  const selectedSessionId=usePreferencesStore((state)=>state.selectedSessionId)
  const selectedAccountId=usePreferencesStore((state)=>state.selectedAccountId)
  const setSelectedSession=usePreferencesStore((state)=>state.setSelectedSession)
  const setSelectedAccount=usePreferencesStore((state)=>state.setSelectedAccount)
  const setSelectedPortfolio=usePreferencesStore((state)=>state.setSelectedPortfolio)
  const [displayName,setDisplayName]=useState('')
  const [username,setUsername]=useState('')
  const [password,setPassword]=useState('')
  const [mode,setMode]=useState<BrokerSessionMode>('paper')
  const [marketDataSubscription,setMarketDataSubscription]=useState<boolean|null>(null)
  const [credentialsFor,setCredentialsFor]=useState<string|null>(null)
  const [replacementUsername,setReplacementUsername]=useState('')
  const [replacementPassword,setReplacementPassword]=useState('')
  const [replacementSubscription,setReplacementSubscription]=useState<boolean|null>(null)
  const [deleteTarget,setDeleteTarget]=useState<BrokerGatewaySession|null>(null)
  const activeSession=(sessions.data||[]).find((item)=>item.id===selectedSessionId)||(sessions.data||[])[0]||null
  const accounts=useQuery(queries.brokerSessionAccounts(activeSession?.id))
  const brokerDeployment=system.data?.broker_deployment
  const managedGatewayAvailable=Boolean(brokerDeployment&&(brokerDeployment.available??brokerDeployment.ready))

  useEffect(()=>{if(activeSession&&activeSession.id!==selectedSessionId)setSelectedSession(activeSession.id)},[activeSession,selectedSessionId,setSelectedSession])
  useEffect(()=>{
    const rows=accounts.data||[]
    const selected=rows.find((item)=>item.id===selectedAccountId)||rows.find((item)=>item.available)||null
    if(selected&&selected.id!==selectedAccountId){setSelectedAccount(selected.id);setSelectedPortfolio(selected.default_portfolio_id,selected.id)}
  },[accounts.data,selectedAccountId,setSelectedAccount,setSelectedPortfolio])

  const refresh=async()=>{await Promise.all([
    queryClient.invalidateQueries({queryKey:['broker-sessions']}),
    queryClient.invalidateQueries({queryKey:['broker-session-accounts']}),
    queryClient.invalidateQueries({queryKey:['accounts']}),
    queryClient.invalidateQueries({queryKey:['portfolios']}),
  ])}
  const create=useMutation({
    mutationFn:(payload:StartPayload)=>request<BrokerGatewaySession>('broker-sessions/',mutationOptions('POST',payload)),
    onSuccess:async(session)=>{setUsername('');setPassword('');setSelectedSession(session.id);await refresh()},
  })
  const reconnect=useMutation({
    mutationFn:(sessionId:string)=>request<unknown>(`broker-sessions/${sessionId}/reconnect/`,mutationOptions('POST',{})),
    onSuccess:refresh,
  })
  const replaceCredentials=useMutation({
    mutationFn:({sessionId,username,password,marketDataSubscription}:{sessionId:string;username:string;password:string;marketDataSubscription:boolean})=>request<BrokerGatewaySession>(
      `broker-sessions/${sessionId}/credentials/`,mutationOptions('POST',{username,password,market_data_subscription:marketDataSubscription})),
    onSuccess:async()=>{setCredentialsFor(null);setReplacementUsername('');setReplacementPassword('');await refresh()},
  })
  const remove=useMutation({
    mutationFn:(sessionId:string)=>request<unknown>(`broker-sessions/${sessionId}/`,mutationOptions('DELETE')),
    onSuccess:async()=>{setDeleteTarget(null);if(deleteTarget?.id===selectedSessionId)setSelectedSession(null);await refresh()},
  })
  const selectedAccount=useMemo(()=>(accounts.data||[]).find((item)=>item.id===selectedAccountId)||(accounts.data||[])[0]||null,[accounts.data,selectedAccountId])

  const submit=(event:React.FormEvent)=>{
    event.preventDefault()
    if(!managedGatewayAvailable||marketDataSubscription===null)return
    create.mutate({display_name:displayName.trim()||`${mode==='live'?'Live':'Paper'} IBKR`,username,password,mode,market_data_subscription:marketDataSubscription})
  }
  const chooseMode=(next:BrokerSessionMode)=>{setMode(next);if(next==='live'&&marketDataSubscription===false)setMarketDataSubscription(null)}
  const selectAccount=(account:BrokerSessionAccount)=>{setSelectedAccount(account.id);setSelectedPortfolio(account.default_portfolio_id,account.id)}

  return <div className="page-stack broker-sessions-page">
    <PageHeader eyebrow="IBKR connectivity" title="IBKR Sessions" description="Provision and operate isolated paper and live IB Gateway sessions. Credentials are encrypted, consumed once, and never returned." />
    <section className="terminal-panel session-start-panel">
      <header className="panel-header"><div><h2>Start an IBKR session</h2><p>Each session gets its own child container, service token, noVNC password, accounts, and event cursor.</p></div><Plus /></header>
      {brokerDeployment&&!managedGatewayAvailable&&<p className="inline-warning" role="status">Managed IB Gateway session creation is unavailable.
        {brokerDeployment.missing.length>0&&<> Missing configuration: <code>{brokerDeployment.missing.join(', ')}</code>.</>}
        {brokerDeployment.invalid.length>0&&<> Invalid configuration: <code>{brokerDeployment.invalid.join(', ')}</code>.</>}
      </p>}
      {system.isError&&<p className="inline-warning" role="status">Managed IB Gateway availability could not be determined. Session creation is disabled.</p>}
      <form className="form-grid session-start-form" onSubmit={submit}>
        <label>Display name<input aria-label="Display name" value={displayName} maxLength={128} onChange={(event)=>setDisplayName(event.target.value)} placeholder="Primary live" /></label>
        <label>IBKR username<input aria-label="IBKR username" value={username} maxLength={128} required autoComplete="username" onChange={(event)=>setUsername(event.target.value)} /></label>
        <label>Password<input aria-label="IBKR password" type="password" value={password} maxLength={512} required autoComplete="new-password" onChange={(event)=>setPassword(event.target.value)} /></label>
        <fieldset className="mode-control"><legend>Trading mode</legend><label><input type="radio" name="ibkr-mode" value="paper" checked={mode==='paper'} onChange={()=>chooseMode('paper')} />Paper</label><label><input type="radio" name="ibkr-mode" value="live" checked={mode==='live'} onChange={()=>chooseMode('live')} />Live</label></fieldset>
        <MarketDataSubscriptionField name="ibkr-market-data" mode={mode} value={marketDataSubscription} onChange={setMarketDataSubscription} />
        <button className="button-primary form-submit" type="submit" disabled={create.isPending||!managedGatewayAvailable||marketDataSubscription===null}>{create.isPending?'Starting…':'Start session'}</button>
      </form>
      {create.isError&&<ErrorState title="Session was not started" error={create.error} compact />}
    </section>

    {sessions.isError?<ErrorState title="IBKR sessions are unavailable" error={sessions.error} onRetry={()=>void sessions.refetch()} />:
      !(sessions.data||[]).length?<EmptyState title="No IBKR sessions" description="Start a paper or live session above. Multiple child gateways can run concurrently." />:
      <section className="session-card-grid" aria-label="IBKR session list">{(sessions.data||[]).map((session)=><article key={session.id} className={`session-card ${session.id===activeSession?.id?'selected':''}`}>
        <header><div><h2>{session.display_name}</h2><p>{session.username_hint}</p></div><div className="badge-row"><StatusBadge status={session.mode}/><StatusBadge status={session.status}/></div></header>
        <dl className="detail-list"><div><dt>Container</dt><dd>{session.container_status||'Pending'}</dd></div><div><dt>Market data</dt><dd>{session.market_data_type==='LIVE'?'Real-time':'Delayed'}</dd></div><div><dt>Accounts</dt><dd>{session.account_count}</dd></div><div><dt>Last check</dt><dd>{formatDateTime(session.last_checked_at)}</dd></div></dl>
        {session.mode==='live'&&!session.connected&&<p className="inline-warning session-2fa">Open noVNC and complete IBKR 2FA.</p>}
        {session.last_error&&<p className="session-error"><strong>Last error</strong>{session.last_error}</p>}
        {credentialsFor===session.id?<form className="replacement-credentials" onSubmit={(event)=>{event.preventDefault();if(replacementSubscription!==null)replaceCredentials.mutate({sessionId:session.id,username:replacementUsername,password:replacementPassword,marketDataSubscription:replacementSubscription})}}>
          <label>IBKR username<input aria-label={`Replacement username for ${session.display_name}`} value={replacementUsername} required onChange={(event)=>setReplacementUsername(event.target.value)} /></label>
          <label>Password<input aria-label={`Replacement password for ${session.display_name}`} type="password" value={replacementPassword} required autoComplete="new-password" onChange={(event)=>setReplacementPassword(event.target.value)} /></label>
          <MarketDataSubscriptionField name={`market-data-${session.id}`} mode={session.mode} value={replacementSubscription} onChange={setReplacementSubscription} />
          <div><button className="button-primary" disabled={replaceCredentials.isPending||replacementSubscription===null}>Submit credentials</button><button className="button-quiet" type="button" onClick={()=>setCredentialsFor(null)}>Cancel</button></div>
        </form>:null}
        <footer className="session-actions">
          <button className="button-secondary" onClick={()=>setSelectedSession(session.id)} disabled={session.id===activeSession?.id}>Select</button>
          {session.novnc_url?<a className="button-primary" href={session.novnc_url} target="_blank" rel="noreferrer">Open noVNC<ExternalLink /></a>:null}
          <button className="button-secondary" disabled={reconnect.isPending||!session.commands_enabled} onClick={()=>reconnect.mutate(session.id)}><RefreshCw />Reconnect</button>
          <button className="button-secondary" onClick={()=>{setCredentialsFor(session.id);setReplacementUsername('');setReplacementPassword('');setReplacementSubscription(session.market_data_type==='LIVE')}}><KeyRound />Re-enter credentials</button>
          <button className="button-danger-subtle" onClick={()=>setDeleteTarget(session)}><Trash2 />Delete</button>
        </footer>
      </article>)}</section>}

    {activeSession&&<section className="terminal-panel session-account-panel"><header className="panel-header"><div><h2>Selected session and account</h2><p>{activeSession.display_name} · explicit portfolio routing</p></div><StatusBadge status={activeSession.status}/></header>
      {accounts.isError?<ErrorState error={accounts.error} compact />:!(accounts.data||[]).length?<p className="inline-note">No accounts have been discovered yet. Complete gateway login and 2FA, then wait for the next session check.</p>:<>
        <label className="account-picker">Account<select aria-label="Session account" value={selectedAccount?.id||''} onChange={(event)=>{const account=(accounts.data||[]).find((item)=>item.id===Number(event.target.value));if(account)selectAccount(account)}}>{(accounts.data||[]).map((account)=><option key={account.id} value={account.id}>{account.alias||account.account_id}</option>)}</select></label>
        {selectedAccount&&<dl className="metric-grid compact account-summary"><div className="metric-card"><dt>Net liquidation</dt><dd>{formatMoney(selectedAccount.net_liquidation,selectedAccount.base_currency)}</dd></div><div className="metric-card"><dt>Available cash</dt><dd>{formatMoney(selectedAccount.available_cash,selectedAccount.base_currency)}</dd></div><div className="metric-card"><dt>Buying power</dt><dd>{formatMoney(selectedAccount.buying_power,selectedAccount.base_currency)}</dd></div><div className="metric-card"><dt>Daily P&amp;L</dt><dd>{formatMoney(selectedAccount.daily_pnl,selectedAccount.base_currency)}</dd></div><div className="metric-card"><dt>Portfolio route</dt><dd>{selectedAccount.default_portfolio_id||'Pending'}</dd></div></dl>}
      </>}
    </section>}
    {(reconnect.isError||replaceCredentials.isError||remove.isError)&&<ErrorState title="Session action failed" error={reconnect.error||replaceCredentials.error||remove.error} compact />}
    <ConfirmActionDialog open={Boolean(deleteTarget)} title={`Delete ${deleteTarget?.display_name||'IBKR session'}?`} description="The child container and monitoring will stop, bound strategies will pause, and existing IBKR orders are not automatically cancelled." confirmLabel="Delete session" requireReason={false} pending={remove.isPending} onClose={()=>setDeleteTarget(null)} onConfirm={()=>{if(deleteTarget)remove.mutate(deleteTarget.id)}} />
  </div>
}
