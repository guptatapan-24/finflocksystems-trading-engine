import {useEffect, useState} from 'react'
import {AlertTriangle, X} from 'lucide-react'

interface DeleteStrategyDialogProps {
  open: boolean
  strategyName: string
  pending?: boolean
  onClose: () => void
  onConfirm: () => void | Promise<void>
}

export function DeleteStrategyDialog({open, strategyName, pending = false, onClose, onConfirm}: DeleteStrategyDialogProps) {
  const [confirmation, setConfirmation] = useState('')
  useEffect(() => { setConfirmation('') }, [open, strategyName])
  if (!open) return null
  const matches = confirmation === strategyName
  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    if (matches && !pending) void onConfirm()
  }
  return <div className="dialog-layer" role="presentation">
    <form className="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="delete-strategy-title" onSubmit={submit}>
      <header><AlertTriangle /><div><h2 id="delete-strategy-title">Delete {strategyName}?</h2><p>This permanently removes the strategy configuration and runtime state. Completed fills, ledger entries, reconciliation records, and audit history are preserved. Deletion is blocked while orders, executions, rebalances, or positions remain active.</p></div><button type="button" className="icon-button" aria-label="Close deletion confirmation" onClick={onClose}><X /></button></header>
      <label>Type <strong>{strategyName}</strong> to confirm<input aria-label="Strategy name confirmation" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" autoFocus /></label>
      <footer><button type="button" className="button-secondary" onClick={onClose}>Go back</button><button type="submit" className="button-danger" disabled={pending || !matches}>{pending ? 'Deleting…' : 'Delete strategy'}</button></footer>
    </form>
  </div>
}
