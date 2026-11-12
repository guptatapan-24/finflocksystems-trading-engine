import {useState} from 'react'
import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query'
import {ApiError, mutationOptions, request} from '../../api/client'
import {ConfirmActionDialog, DataTable, EmptyState, ErrorState, Skeleton, StatusBadge, TerminalPanel} from '../../components/ui'
import {intelligenceQueries} from './queries'
import type {IntelligenceMistake, IntelligenceSuggestion} from './types'

type Decision = 'accept' | 'acknowledge' | 'reject'

const SAFE_REASON_TEXT: Record<string, string> = {
  INSTANCE_ENABLED: 'the strategy is enabled (pause or disable it first)',
  OPEN_EPISODE: 'a trade is still open',
  NON_ZERO_ATTRIBUTED_POSITION: 'the strategy still holds a position',
  ACTIVE_ORDERS: 'orders are still working',
}

function explain(error: unknown) {
  if (error instanceof ApiError && error.code === 'INSTANCE_NOT_SAFE_TO_CHANGE') {
    const reasons = ((error.details as {reasons?: string[]} | null)?.reasons ?? []).map((reason) => SAFE_REASON_TEXT[reason] ?? reason.toLowerCase().replaceAll('_', ' '))
    return `Not applied: ${reasons.join('; ') || 'the strategy is not in a safe state'}. You can still acknowledge the suggestion.`
  }
  if (error instanceof ApiError && error.code === 'SUGGESTION_STALE') return 'Not applied: the parameter changed since this suggestion was made.'
  return error instanceof Error ? error.message : 'The request failed.'
}

const show = (value: unknown) => (value === null || value === undefined || value === '' ? '—' : String(value))

function SuggestionCard({suggestion, onDecide, busy}: {suggestion: IntelligenceSuggestion; onDecide: (decision: Decision) => void; busy: boolean}) {
  const open = suggestion.status === 'PROPOSED'
  return <article className="panel" aria-label={`Suggestion ${suggestion.id}`}>
    <header className="row-actions">
      <StatusBadge status={suggestion.status} />
      <StatusBadge status={suggestion.is_hypothesis ? 'HYPOTHESIS' : 'DEFECT-BASED'} tone={suggestion.is_hypothesis ? 'warning' : 'info'} />
      <strong>{suggestion.kind}{suggestion.parameter_key ? ` · ${suggestion.parameter_key}` : ''}</strong>
    </header>
    {suggestion.kind === 'PARAMETER' && <p className="mono">{show(suggestion.current_value)} → {show(suggestion.proposed_value)}</p>}
    {suggestion.kind === 'EXPERIMENT' && suggestion.experiment && <>
      <p><strong>{suggestion.experiment.title}</strong></p>
      <p>{suggestion.experiment.try}</p>
      <dl className="detail-list">
        <div><dt>Matching trades needed</dt><dd>{suggestion.experiment.min_n} (paper first)</dd></div>
        <div><dt>What past trades say</dt><dd>{suggestion.experiment.history_check?.verdict ?? 'no automatic check'}</dd></div>
        <div><dt>When it stops</dt><dd>{suggestion.experiment.stop_rule}</dd></div>
      </dl>
    </>}
    <p>{suggestion.rationale}</p>
    <dl className="detail-list">
      <div><dt>Expected effect</dt><dd>{show(suggestion.expected_effect)}</dd></div>
      <div><dt>Risk of the change</dt><dd>{show(suggestion.risk_of_change)}</dd></div>
      <div><dt>Evidence needed</dt><dd>{show(suggestion.evidence_needed)}</dd></div>
      {suggestion.decision_reason && <div><dt>Decision</dt><dd>{suggestion.decision_reason}</dd></div>}
    </dl>
    {open && <div className="row-actions">
      {suggestion.kind !== 'EXPERIMENT' && <button className="button-primary" disabled={busy} onClick={() => onDecide('accept')}>{suggestion.kind === 'PARAMETER' ? 'Accept: create new version' : 'Accept as advice'}</button>}
      <button className={suggestion.kind === 'EXPERIMENT' ? 'button-primary' : 'button-secondary'} disabled={busy} onClick={() => onDecide('acknowledge')}>{suggestion.kind === 'EXPERIMENT' ? 'Track this plan' : 'Acknowledge only'}</button>
      <button className="button-secondary" disabled={busy} onClick={() => onDecide('reject')}>Reject</button>
    </div>}
    {open && suggestion.kind === 'PARAMETER' && <small>Accepting creates a new strategy version for a disabled, flat strategy. It never enables or activates anything.</small>}
  </article>
}

export function ReviewPanel({episodeId}: {episodeId: number}) {
  const queryClient = useQueryClient()
  const review = useQuery(intelligenceQueries.review(episodeId))
  const [pending, setPending] = useState<{suggestion: IntelligenceSuggestion; decision: Decision} | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const decide = useMutation({
    mutationFn: ({suggestion, decision, reason}: {suggestion: IntelligenceSuggestion; decision: Decision; reason: string}) =>
      request<IntelligenceSuggestion>(`intelligence/suggestions/${suggestion.id}/${decision}/`, mutationOptions('POST', reason ? {reason} : {}, true)),
    onSuccess: async () => {
      setPending(null)
      setProblem(null)
      await queryClient.invalidateQueries({queryKey: ['intelligence']})
    },
    onError: (error) => {
      setPending(null)
      setProblem(explain(error))
    },
  })
  const feedback = useMutation({
    mutationFn: ({id, rating}: {id: number; rating: number}) => request<unknown>(`intelligence/reviews/${id}/feedback/`, mutationOptions('POST', {rating}, true)),
  })

  if (review.isLoading) return <Skeleton lines={4} />
  if (review.error instanceof ApiError && review.error.status === 404) return <EmptyState title="No review yet" description="A review is written after a trade closes, if LLM reviews are enabled." />
  if (review.isError) return <ErrorState title="Could not load the review" error={review.error} compact />
  const item = review.data
  if (!item) return null
  const mistakeColumns = [
    {id: 'source', header: 'Source', cell: (m: IntelligenceMistake) => <StatusBadge status={m.source === 'RULE' ? 'RULE-BASED' : 'MODEL'} tone={m.source === 'RULE' ? 'info' : 'neutral'} />},
    {id: 'category', header: 'Category', cell: (m: IntelligenceMistake) => m.category.replaceAll('_', ' ')},
    {id: 'severity', header: 'Severity', cell: (m: IntelligenceMistake) => <StatusBadge status={m.severity} tone={m.severity === 'HIGH' ? 'critical' : m.severity === 'MEDIUM' ? 'warning' : 'neutral'} />},
    {id: 'description', header: 'What was found', cell: (m: IntelligenceMistake) => <span title={`Evidence: ${m.evidence_refs.join(', ')}`}>{m.description}</span>},
  ]
  return <>
    {item.is_fallback && <ErrorState title="Rule-based review only" error={new Error('The language-model review was not available for this trade, so only deterministic findings are shown and no suggestions were generated.')} compact />}
    {item.needs_rereview && <ErrorState title="Net P&L changed after this review" error={new Error('A late commission correction moved the result materially. A re-run is recommended.')} compact />}
    <TerminalPanel id="intelligence-review-summary" title="Review" badge={<StatusBadge status={item.is_fallback ? 'FALLBACK' : item.outcome_class} tone="neutral" />}>
      <dl className="detail-list">
        <div><dt>Process grade</dt><dd>{item.process_grade || 'not graded'}</dd></div>
        <div><dt>Sample size</dt><dd>n = {item.sample_n} closed trade{item.sample_n === 1 ? '' : 's'}</dd></div>
        <div><dt>Model confidence</dt><dd>{item.confidence ?? '—'}</dd></div>
        <div><dt>Written by</dt><dd className="mono">{item.model || 'rules only'} · {item.prompt_version}</dd></div>
      </dl>
      <p><strong>{item.verdict}</strong></p>
      <p>{item.summary}</p>
      {item.sample_n < 8 && <small>With this few trades the outcome is mostly noise; read the process findings, not the profit or loss.</small>}
      <div className="row-actions">
        <button className="button-secondary" onClick={() => feedback.mutate({id: item.id, rating: 1})} disabled={feedback.isPending || feedback.isSuccess}>Useful</button>
        <button className="button-secondary" onClick={() => feedback.mutate({id: item.id, rating: -1})} disabled={feedback.isPending || feedback.isSuccess}>Wrong or misleading</button>
        {feedback.isSuccess && <small>Thanks, recorded.</small>}
      </div>
    </TerminalPanel>
    <TerminalPanel id="intelligence-review-mistakes" title="Findings">
      <DataTable rows={item.mistakes} columns={mistakeColumns} getRowKey={(m) => m.id} caption="Review findings" emptyTitle="No findings" />
    </TerminalPanel>
    {item.strengths.length > 0 && <TerminalPanel id="intelligence-review-strengths" title="Strengths"><ul>{item.strengths.map((s, index) => <li key={index}>{s.description}</li>)}</ul></TerminalPanel>}
    <TerminalPanel id="intelligence-review-suggestions" title="Suggestions (advice only)">
      {problem && <ErrorState title="Suggestion not applied" error={new Error(problem)} compact />}
      {item.suggestions.filter((s) => s.kind !== 'EXPERIMENT').length === 0 ? <EmptyState title="No suggestions" description="One trade is rarely evidence for a change." /> :
        item.suggestions.filter((s) => s.kind !== 'EXPERIMENT').map((s) => <SuggestionCard key={s.id} suggestion={s} busy={decide.isPending} onDecide={(decision) => {setProblem(null); setPending({suggestion: s, decision})}} />)}
    </TerminalPanel>
    {item.suggestions.some((s) => s.kind === 'EXPERIMENT') && <TerminalPanel id="intelligence-review-playbook" title="What to try next time (measurement plans, nothing is changed)">
      {item.suggestions.filter((s) => s.kind === 'EXPERIMENT').map((s) => <SuggestionCard key={s.id} suggestion={s} busy={decide.isPending} onDecide={(decision) => {setProblem(null); setPending({suggestion: s, decision})}} />)}
    </TerminalPanel>}
    {item.open_questions.length > 0 && <TerminalPanel id="intelligence-review-questions" title="Open questions" defaultOpen={false}><ul>{item.open_questions.map((q, index) => <li key={index}>{q}</li>)}</ul></TerminalPanel>}
    <ConfirmActionDialog
      open={pending !== null}
      title={pending?.decision === 'reject' ? 'Reject suggestion' : pending?.decision === 'accept' && pending.suggestion.kind === 'PARAMETER' ? 'Create a new strategy version' : 'Confirm'}
      description={pending?.decision === 'reject' ? 'The suggestion is closed and will never be applied.' : pending?.decision === 'accept' && pending.suggestion.kind === 'PARAMETER' ? 'This changes the strategy parameter in a new version. It works only on a disabled, flat strategy and never enables or activates it.' : 'This records your decision. Nothing about the strategy changes.'}
      confirmLabel={pending?.decision === 'reject' ? 'Reject' : pending?.decision === 'accept' ? 'Accept' : 'Acknowledge'}
      requireReason={pending?.decision === 'reject'}
      danger={pending?.decision === 'reject'}
      pending={decide.isPending}
      onClose={() => setPending(null)}
      onConfirm={(reason) => { if (pending) decide.mutate({suggestion: pending.suggestion, decision: pending.decision, reason}) }}
    />
  </>
}
