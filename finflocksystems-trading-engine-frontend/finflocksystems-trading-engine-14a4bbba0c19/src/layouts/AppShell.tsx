import {useIsFetching, useQuery, useQueryClient} from '@tanstack/react-query'
import {Activity, BookOpen, Bot, ChevronRight, Gauge, Lightbulb, LayoutDashboard, Menu, PanelLeftClose, PanelLeftOpen, RefreshCw, Server, ServerCog, Target, X} from 'lucide-react'
import {NavLink, Outlet, useLocation} from 'react-router-dom'
import {queries} from '../api/queries'
import {ErrorState, StatusBadge} from '../components/ui'
import {MarketTickerBar} from '../components/terminal/MarketTickerBar'
import {useSelection} from '../stores/useSelection'
import {useWorkspacePreferences} from '../stores/workspacePreferences'

const navigation = [
  {label: 'Trading', items: [
    {to: '/dashboard', label: 'Dashboard', icon: LayoutDashboard},
    {to: '/strategies', label: 'Strategies', icon: Bot},
    {to: '/portfolio-builder', label: 'Portfolio Builder', icon: Target},
    {to: '/portfolio', label: 'Portfolio', icon: BookOpen},
  ]},
  {label: 'Operations', items: [
    {to: '/activity', label: 'Orders & Activity', icon: Activity},
    {to: '/ibkr-sessions', label: 'IBKR Sessions', icon: Server},
    {to: '/intelligence', label: 'Intelligence', icon: Lightbulb},
    {to: '/system', label: 'System', icon: ServerCog},
  ]},
]

export function AppShell() {
  const location = useLocation()
  const queryClient = useQueryClient()
  const isFetching = useIsFetching()
  const system = useQuery(queries.system())
  const selection = useSelection()
  const positions = useQuery(queries.positions(selection.selectedPortfolioId))
  const sidebarMode = useWorkspacePreferences((state) => state.sidebarMode)
  const setSidebarMode = useWorkspacePreferences((state) => state.setSidebarMode)
  const mobileNavigationOpen = useWorkspacePreferences((state) => state.mobileNavigationOpen)
  const setMobileNavigationOpen = useWorkspacePreferences((state) => state.setMobileNavigationOpen)
  const page = navigation.flatMap((group) => group.items).find((item) => location.pathname.startsWith(item.to))?.label || 'Trading Engine'

  const selectAccount = (accountId: number) => {
    selection.setSelectedAccount(accountId)
    const nextPortfolio = selection.portfolios.find((item) => item.account_id === accountId)
    selection.setSelectedPortfolio(nextPortfolio?.id ?? null, accountId)
  }

  const selectSession = (sessionId: string) => {
    selection.setSelectedSession(sessionId)
    const nextPortfolio=selection.allPortfolios.find((item) => item.gateway_session_id===sessionId)
    selection.setSelectedPortfolio(nextPortfolio?.id ?? null,nextPortfolio?.account_id ?? null)
  }

  return (
    <div className={`app-shell sidebar-${sidebarMode}`}>
      {mobileNavigationOpen && <button className="nav-scrim" aria-label="Close navigation" onClick={() => setMobileNavigationOpen(false)} />}
      <aside className={`sidebar ${mobileNavigationOpen ? 'sidebar-open' : ''}`}>
        <div className="brand"><div className="brand-mark"><Gauge /></div><div className="brand-copy"><strong>Finflock</strong><span>Execution terminal</span></div><button className="icon-button sidebar-mode-toggle" title={sidebarMode === 'expanded' ? 'Use compact sidebar' : 'Use expanded sidebar'} aria-label={sidebarMode === 'expanded' ? 'Use compact sidebar' : 'Use expanded sidebar'} onClick={() => setSidebarMode(sidebarMode === 'expanded' ? 'compact' : 'expanded')}>{sidebarMode === 'expanded' ? <PanelLeftClose /> : <PanelLeftOpen />}</button><button className="icon-button sidebar-close" aria-label="Close navigation" onClick={() => setMobileNavigationOpen(false)}><X /></button></div>
        <nav aria-label="Primary navigation">{navigation.map((group) => <section className="nav-group" key={group.label}><span className="nav-group-label">{group.label}</span>{group.items.map(({to, label, icon: Icon}) => <NavLink key={to} to={to} title={sidebarMode === 'compact' ? label : undefined} aria-label={label} onClick={() => setMobileNavigationOpen(false)} className={({isActive}) => isActive ? 'active' : ''}><Icon /><span>{label}</span><ChevronRight /></NavLink>)}</section>)}</nav>
        <div className="safety-note"><StatusBadge status={system.data?.mode || 'MULTI_SESSION'} /><p>Session-routed. No direct TWS access.</p></div>
      </aside>
      <div className="app-main">
        <header className="app-topbar">
          <div className="topbar-title"><button className="icon-button mobile-menu" aria-label="Open navigation" onClick={() => setMobileNavigationOpen(true)}><Menu /></button><div><span>FINFLOCK / WORKSPACE</span><strong>{page}</strong></div></div>
          <div className="context-selectors">
            <label><span>Session</span><select aria-label="Selected session" value={selection.selectedSessionId ?? ''} disabled={!selection.sessions.length} onChange={(event) => selectSession(event.target.value)}><option value="" disabled>No sessions</option>{selection.sessions.map((session) => <option key={session.id} value={session.id}>{session.display_name}</option>)}</select></label>
            <label><span>Account</span><select aria-label="Selected account" value={selection.selectedAccountId ?? ''} disabled={!selection.accounts.length} onChange={(event) => selectAccount(Number(event.target.value))}><option value="" disabled>No accounts</option>{selection.accounts.map((account) => <option key={account.id} value={account.id}>{account.alias || account.account_id}</option>)}</select></label>
            <label><span>Portfolio</span><select aria-label="Selected portfolio" value={selection.selectedPortfolioId ?? ''} disabled={!selection.portfolios.length} onChange={(event) => {const id = Number(event.target.value); const portfolio = selection.portfolios.find((item) => item.id === id); selection.setSelectedPortfolio(id, portfolio?.account_id)}}><option value="" disabled>No portfolios</option>{selection.portfolios.map((portfolio) => <option key={portfolio.id} value={portfolio.id}>{portfolio.name}</option>)}</select></label>
            <StatusBadge status={system.isError ? 'DEGRADED' : system.data?.mode || 'MULTI_SESSION'} />
            <span className="topbar-freshness"><i className={system.isStale ? 'stale' : ''} />{isFetching ? 'Refreshing' : system.isStale ? 'Data stale' : 'Data current'}</span>
            <button className="icon-button" aria-label="Refresh all data" onClick={() => void queryClient.invalidateQueries()}><RefreshCw className={isFetching ? 'spin' : ''} /></button>
          </div>
        </header>
        <MarketTickerBar account={selection.account} portfolio={selection.portfolio} positions={positions.data || []} system={system.data} />
        {selection.error && <div className="global-error"><ErrorState error={selection.error} compact /></div>}
        <main className="page-content"><Outlet /></main>
      </div>
    </div>
  )
}
