import {useEffect, useId, type ReactNode} from 'react'
import {ChevronDown, Maximize2, Minimize2} from 'lucide-react'
import {useLocation} from 'react-router-dom'
import {useWorkspacePreferences} from '../../stores/workspacePreferences'

export interface TerminalPanelProps {
  id: string
  title?: string
  description?: string
  badge?: ReactNode
  actions?: ReactNode
  collapsible?: boolean
  defaultOpen?: boolean
  fullscreenable?: boolean
  loading?: boolean
  className?: string
  children: ReactNode
}

export function TerminalPanel({id, title, description, badge, actions, collapsible = true, defaultOpen = true, fullscreenable = false, loading = false, className = '', children}: TerminalPanelProps) {
  const location = useLocation()
  const contentId = useId()
  const key = `${location.pathname}:${id}`
  const storedCollapsed = useWorkspacePreferences((state) => state.collapsedPanels[key])
  const setPanelCollapsed = useWorkspacePreferences((state) => state.setPanelCollapsed)
  const fullscreenPanelId = useWorkspacePreferences((state) => state.fullscreenPanelId)
  const setFullscreenPanelId = useWorkspacePreferences((state) => state.setFullscreenPanelId)
  const collapsed = collapsible ? storedCollapsed ?? !defaultOpen : false
  const fullscreen = fullscreenPanelId === key

  useEffect(() => {
    if (!fullscreen) return
    const close = (event: KeyboardEvent) => { if (event.key === 'Escape') setFullscreenPanelId(null) }
    window.addEventListener('keydown', close)
    return () => window.removeEventListener('keydown', close)
  }, [fullscreen, setFullscreenPanelId])

  const toggle = () => setPanelCollapsed(key, !collapsed)
  const hasHeader = Boolean(title || description || badge || actions || fullscreenable)
  return <section className={`panel terminal-panel ${fullscreen ? 'terminal-panel-fullscreen' : ''} ${className}`} data-panel-id={id} aria-busy={loading || undefined}>
    {hasHeader && <header className="panel-header">
      <div className="panel-heading">
        {collapsible ? <button type="button" className="panel-heading-toggle" aria-expanded={!collapsed} aria-controls={contentId} onClick={toggle}><span>{title && <h2>{title}</h2>}{description && <p>{description}</p>}</span><ChevronDown className={collapsed ? '' : 'expanded'} aria-hidden="true" /></button> : <div>{title && <h2>{title}</h2>}{description && <p>{description}</p>}</div>}
        {badge && <div className="panel-badge">{badge}</div>}
      </div>
      <div className="panel-actions">
        {actions}
        {fullscreenable && <button type="button" className="icon-button" aria-label={fullscreen ? `Exit ${title || id} fullscreen` : `Open ${title || id} fullscreen`} onClick={() => setFullscreenPanelId(fullscreen ? null : key)}>{fullscreen ? <Minimize2 /> : <Maximize2 />}</button>}
      </div>
    </header>}
    <div id={contentId} className="panel-content" hidden={collapsed}>{children}</div>
  </section>
}
