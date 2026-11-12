import type {ReactNode} from 'react'
import {X} from 'lucide-react'
import {useEffect} from 'react'

interface DetailDrawerProps {
  open: boolean
  title: string
  subtitle?: string
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
}

export function DetailDrawer({open, title, subtitle, onClose, children, footer}: DetailDrawerProps) {
  useEffect(() => {
    if (!open) return
    const close = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', close)
    return () => window.removeEventListener('keydown', close)
  }, [open, onClose])
  if (!open) return null
  return (
    <div className="drawer-layer" role="presentation" onMouseDown={(event) => {if (event.currentTarget === event.target) onClose()}}>
      <aside className="detail-drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title">
        <header><div><h2 id="drawer-title">{title}</h2>{subtitle && <p>{subtitle}</p>}</div><button className="icon-button" aria-label="Close details" onClick={onClose}><X /></button></header>
        <div className="detail-drawer-body">{children}</div>
        {footer && <footer>{footer}</footer>}
      </aside>
    </div>
  )
}

