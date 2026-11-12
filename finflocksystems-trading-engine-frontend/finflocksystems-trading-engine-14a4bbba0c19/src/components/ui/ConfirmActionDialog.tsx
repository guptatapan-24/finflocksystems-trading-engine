import {useEffect, useState} from 'react'
import {AlertTriangle, X} from 'lucide-react'

interface ConfirmActionDialogProps {
  open: boolean
  title: string
  description: string
  confirmLabel: string
  onClose: () => void
  onConfirm: (reason: string) => void | Promise<void>
  requireReason?: boolean
  pending?: boolean
  danger?: boolean
}

export function ConfirmActionDialog({open, title, description, confirmLabel, onClose, onConfirm, requireReason = true, pending = false, danger = true}: ConfirmActionDialogProps) {
  const [reason, setReason] = useState('')
  useEffect(() => { if (!open) setReason('') }, [open])
  if (!open) return null
  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    if (!requireReason || reason.trim()) void onConfirm(reason.trim())
  }
  return (
    <div className="dialog-layer" role="presentation">
      <form className="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="confirm-title" onSubmit={submit}>
        <header><AlertTriangle /><div><h2 id="confirm-title">{title}</h2><p>{description}</p></div><button type="button" className="icon-button" aria-label="Close confirmation" onClick={onClose}><X /></button></header>
        {requireReason && <label>Reason<textarea aria-label="Reason" value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Required for the audit trail" autoFocus /></label>}
        <footer><button type="button" className="button-secondary" onClick={onClose}>Go back</button><button type="submit" className={danger ? 'button-danger' : 'button-primary'} disabled={pending || (requireReason && !reason.trim())}>{pending ? 'Working…' : confirmLabel}</button></footer>
      </form>
    </div>
  )
}

