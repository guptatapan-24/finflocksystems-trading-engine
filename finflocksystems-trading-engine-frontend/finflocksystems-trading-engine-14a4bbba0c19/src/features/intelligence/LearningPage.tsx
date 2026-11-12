import {useState} from 'react'
import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query'
import {mutationOptions, request} from '../../api/client'
import {ConfirmActionDialog, DataTable, DetailDrawer, EmptyState, ErrorState, PageHeader, Skeleton, StatusBadge, TerminalPanel, formatDateTime, formatNumber} from '../../components/ui'
import {IntelligenceTabs} from './IntelligenceTabs'
import {intelligenceQueries} from './queries'
import type {CalibrationRow, ChangePointRow, ExperimentRow, IntelligenceLesson, LearningEventRow, PerformanceRow} from './types'

const VIEWS = ['Feed', 'Lessons', 'Experiments', 'Edge monitor', 'Performance', 'Calibration', 'Weekly', 'What-if'] as const
type View = typeof VIEWS[number]

const CHANGE_TONE: Record<string, 'positive' | 'info' | 'warning' | 'critical' | 'neutral'> = {
  EDGE_DECAY: 'critical', EDGE_IMPROVEMENT: 'positive', STABLE: 'neutral', INSUFFICIENT: 'warning',
}
const SERIES_LABEL: Record<string, string> = {RETURN: 'Net return', POP_CALIBRATION: 'Probability calibration'}

const STATUS_TONE: Record<string, 'positive' | 'info' | 'warning' | 'critical' | 'neutral'> = {
  ESTABLISHED: 'positive', SUPPORTED: 'info', HYPOTHESIS: 'warning', REFUTED: 'critical', RETIRED: 'neutral',
}

export function describePredicate(predicate: IntelligenceLesson['predicate']) {
  const [mode, conditions] = Object.entries(predicate)[0] ?? ['all', []]
  const text = conditions.map((c) => `${c.metric} ${c.op} ${Array.isArray(c.value) ? `[${c.value.join(', ')}]` : String(c.value)}`)
  return text.join(mode === 'any' ? ' OR ' : ' AND ')
}

export function describeEvent(event: LearningEventRow): string {
  const p = event.payload as Record<string, unknown>
  const statement = String(p.statement ?? '')
  switch (event.type) {
    case 'LESSON_CREATED': return `New hypothesis to test: ${statement}`
    case 'LESSON_REINFORCED': return `A trade supported: ${statement} (n = ${String(p.n)}, now ${String(p.status)})`
    case 'LESSON_CONTRADICTED': return `A trade contradicted: ${statement} (n = ${String(p.n)}, now ${String(p.status)})`
    case 'LESSON_PROMOTED': return `Promoted to ${String(p.to)}: ${statement} (n = ${String(p.n)}, posterior ${String(p.posterior)})`
    case 'LESSON_RETIRED': return `${String(p.to ?? 'Retired')}: ${statement}`
    case 'STAT_SHIFT': return `Performance moved from ${String(p.from_bps)} to ${String(p.to_bps)} bps (n = ${String(p.n)}). ${String(p.note ?? '')}`
    case 'CALIBRATION_SHIFT': return `Probability calibration off in bin ${String(p.bin)}: predicted ${String(p.predicted)}, observed ${String(p.observed)} (n = ${String(p.n)})`
    case 'SUGGESTION_RESOLVED': return p.experiment
      ? `Experiment "${String(p.experiment)}" judged ${String(p.result)} (n with the condition ${String(p.n_after)}, without ${String(p.n_before)})`
      : `Applied suggestion on ${String(p.parameter)} judged ${String(p.result)} (n before ${String(p.n_before)}, after ${String(p.n_after)})`
    case 'CHANGE_POINT': return `Edge monitor: ${String(p.strategy_key)} ${SERIES_LABEL[String(p.metric)] ?? String(p.metric)} is ${String(p.status).replaceAll('_', ' ').toLowerCase()} (n = ${String(p.n)}). ${String(p.note ?? '')}`
    default: return event.type
  }
}

const interval = (low: number | null, high: number | null) => (low === null || high === null ? 'no interval (n < 2)' : `${formatNumber(low, 1)} to ${formatNumber(high, 1)}`)

export function LearningPage() {
  const [view, setView] = useState<View>('Feed')
  const [currency, setCurrency] = useState('')
  const [lessonId, setLessonId] = useState<number | null>(null)
  const [strategyKey, setStrategyKey] = useState('')
  const queryClient = useQueryClient()
  const weekly = useQuery({...intelligenceQueries.weeklyReviews(), enabled: view === 'Weekly'})
  const sweep = useQuery({...intelligenceQueries.optionsSweep(strategyKey.trim().toUpperCase()), enabled: view === 'What-if' && Boolean(strategyKey.trim())})
  const requestWeekly = useMutation({
    mutationFn: () => request<{status: string}>(`intelligence/strategies/${strategyKey.trim().toUpperCase()}/weekly-review/`, mutationOptions('POST', {}, true)),
    onSuccess: () => queryClient.invalidateQueries({queryKey: ['intelligence', 'weekly']}),
  })
  const digest = useQuery(intelligenceQueries.digest(7))
  const feed = useQuery({...intelligenceQueries.learningFeed(), enabled: view === 'Feed'})
  const lessons = useQuery({...intelligenceQueries.lessons(), enabled: view === 'Lessons'})
  const lesson = useQuery(intelligenceQueries.lesson(lessonId))
  const performance = useQuery({...intelligenceQueries.performance(currency), enabled: view === 'Performance'})
  const calibration = useQuery({...intelligenceQueries.calibration(), enabled: view === 'Calibration'})
  const experiments = useQuery({...intelligenceQueries.experiments(), enabled: view === 'Experiments'})
  const changePoints = useQuery({...intelligenceQueries.changePoints(), enabled: view === 'Edge monitor'})
  const [pendingExperiment, setPendingExperiment] = useState<{id: number; decision: 'acknowledge' | 'reject'} | null>(null)
  const decideExperiment = useMutation({
    mutationFn: ({id, decision, reason}: {id: number; decision: 'acknowledge' | 'reject'; reason: string}) =>
      request<unknown>(`intelligence/suggestions/${id}/${decision}/`, mutationOptions('POST', reason ? {reason} : {}, true)),
    onSuccess: async () => {
      setPendingExperiment(null)
      await queryClient.invalidateQueries({queryKey: ['intelligence']})
    },
    onError: () => setPendingExperiment(null),
  })

  const lessonColumns = [
    {id: 'statement', header: 'Lesson', cell: (l: IntelligenceLesson) => <div className="primary-cell"><strong>{l.statement}</strong><span className="mono">{describePredicate(l.predicate)}</span></div>},
    {id: 'strategy', header: 'Strategy', cell: (l: IntelligenceLesson) => l.strategy_key},
    {id: 'status', header: 'Status', cell: (l: IntelligenceLesson) => <StatusBadge status={l.status} tone={STATUS_TONE[l.status]} />},
    {id: 'n', header: 'Trades', align: 'right' as const, className: 'mono', cell: (l: IntelligenceLesson) => `n = ${l.n} (${l.supporting_trades} for, ${l.contradicting_trades} against)`},
    {id: 'q', header: 'Chance it is luck (q)', align: 'right' as const, className: 'mono', cell: (l: IntelligenceLesson) => l.q_value === null || l.q_value === undefined ? 'not testable yet' : l.q_value.toFixed(3)},
    {id: 'forward', header: 'Later trades', align: 'right' as const, className: 'mono', cell: (l: IntelligenceLesson) => l.forward_trades},
    {id: 'effect', header: 'Effect (bps)', align: 'right' as const, className: 'mono', cell: (l: IntelligenceLesson) => l.effect_size_bps === null ? '—' : `${formatNumber(l.effect_size_bps, 1)} (${interval(l.effect_ci_low, l.effect_ci_high)})`},
  ]
  const performanceColumns = [
    {id: 'who', header: 'Slice', cell: (r: PerformanceRow) => <div className="primary-cell"><strong>{[r.strategy_key, r.strategy_version ? `v${r.strategy_version}` : '', r.instrument].filter(Boolean).join(' · ') || 'All'}</strong><span>{[r.mode, r.regime, r.structure, r.risk_profile ? `${r.risk_profile.toLowerCase()} profile` : '', r.context ? r.context.replaceAll('_', ' ').toLowerCase() : ''].filter(Boolean).join(' · ')}</span></div>},
    {id: 'currency', header: 'Currency', cell: (r: PerformanceRow) => r.currency},
    {id: 'n', header: 'n', align: 'right' as const, className: 'mono', cell: (r: PerformanceRow) => `${r.n} (${r.wins} winning)`},
    {id: 'mean', header: 'Mean return (bps)', align: 'right' as const, className: 'mono', cell: (r: PerformanceRow) => r.mean_return_bps === null ? '—' : formatNumber(r.mean_return_bps, 1)},
    {id: 'shrunk', header: 'Pooled mean (bps)', align: 'right' as const, className: 'mono', cell: (r: PerformanceRow) => r.shrunk_mean_bps === null || r.shrunk_mean_bps === undefined ? '—' : `${formatNumber(r.shrunk_mean_bps, 1)}${r.shrinkage !== null && r.shrinkage !== undefined && r.shrinkage < 0.95 ? ` (${Math.round(r.shrinkage * 100)}% own data)` : ''}`},
    {id: 'ci', header: '95% interval', align: 'right' as const, className: 'mono', cell: (r: PerformanceRow) => interval(r.ci_low_bps, r.ci_high_bps)},
    {id: 'net', header: 'Net P&L', align: 'right' as const, className: 'mono', cell: (r: PerformanceRow) => `${formatNumber(r.sum_net_pnl, 2)} ${r.currency}`},
  ]
  const calibrationColumns = [
    {id: 'strategy', header: 'Strategy', cell: (r: CalibrationRow) => r.strategy_key || 'All strategies'},
    {id: 'bin', header: 'Predicted probability', cell: (r: CalibrationRow) => r.bin === 'ALL' ? 'All (Brier score)' : r.bin},
    {id: 'n', header: 'n', align: 'right' as const, className: 'mono', cell: (r: CalibrationRow) => r.n},
    {id: 'pred', header: 'Predicted', align: 'right' as const, className: 'mono', cell: (r: CalibrationRow) => r.predicted_mean === null ? '—' : r.predicted_mean.toFixed(2)},
    {id: 'obs', header: 'Observed', align: 'right' as const, className: 'mono', cell: (r: CalibrationRow) => r.observed_rate === null ? '—' : r.observed_rate.toFixed(2)},
    {id: 'brier', header: 'Brier', align: 'right' as const, className: 'mono', cell: (r: CalibrationRow) => r.brier === null ? '' : r.brier.toFixed(3)},
  ]
  const currencies = [...new Set((performance.data ?? []).map((r) => r.currency))].sort()

  return <>
    <PageHeader eyebrow="Intelligence" title="What the system has learned" description="Lessons and statistics kept in the database from your closed trades. Nothing is trained: a lesson is a tested statement, and it stays a hypothesis until it holds up over many trades." />
    <IntelligenceTabs />
    {digest.data && <TerminalPanel id="intelligence-learning-digest" title={`Last ${digest.data.window_days} days`}>
      <dl className="detail-list">
        <div><dt>Closed trades</dt><dd>{digest.data.closed_trades.total}{Object.entries(digest.data.closed_trades.per_currency).map(([code, e]) => ` · ${code}: n = ${e.n}, ${e.wins} winning`).join('')}</dd></div>
        <div><dt>Lessons</dt><dd>{Object.entries(digest.data.lessons.by_status).map(([k, v]) => `${v} ${k.toLowerCase()}`).join(', ')}</dd></div>
        <div><dt>Suggestions waiting for you</dt><dd>{digest.data.open_suggestions}</dd></div>
      </dl>
      {(digest.data.experiments_waiting ?? 0) > 0 && <p><small>{digest.data.experiments_waiting} measurement plan(s) are waiting for you under Experiments.</small></p>}
      {(digest.data.change_points ?? []).map((c) => <p key={`${c.strategy_key}${c.metric}`}><small>Edge monitor: {c.strategy_key} {SERIES_LABEL[c.metric] ?? c.metric} is {c.status.replaceAll('_', ' ').toLowerCase()} (n = {c.n}).</small></p>)}
      {digest.data.notes.map((note) => <p key={note}><small>{note}</small></p>)}
    </TerminalPanel>}
    <div className="tabs" role="tablist" aria-label="Learning views">{VIEWS.map((name) => <button key={name} role="tab" aria-selected={view === name} className={view === name ? 'active' : ''} onClick={() => setView(name)}>{name}</button>)}</div>
    {view === 'Feed' && (feed.isLoading ? <Skeleton lines={5} /> : <TerminalPanel id="intelligence-learning-feed" title="After every trade, what changed">
      {feed.isError ? <ErrorState error={feed.error} compact /> : (feed.data ?? []).length === 0
        ? <EmptyState title="Nothing learned yet" description="After each closed trade this lists new hypotheses, lessons that were supported or contradicted, and shifts in the statistics." />
        : <ul>{[...(feed.data ?? [])].reverse().map((event) => <li key={event.id}><small>{formatDateTime(event.created_at)}</small> {describeEvent(event)}</li>)}</ul>}
    </TerminalPanel>)}
    {view === 'Lessons' && (lessons.isLoading ? <Skeleton lines={5} /> : <TerminalPanel id="intelligence-learning-lessons" title="Lessons">
      {lessons.isError ? <ErrorState error={lessons.error} compact /> : <DataTable rows={lessons.data ?? []} columns={lessonColumns} getRowKey={(l) => l.id} selectedRowKey={lessonId} onRowSelect={(l) => setLessonId(l.id)} caption="Lessons" emptyTitle="No lessons yet" emptyDescription="Lessons appear when a review proposes a testable pattern. They stay hypotheses for a long time, which is correct." />}
    </TerminalPanel>)}
    {view === 'Performance' && (performance.isLoading ? <Skeleton lines={5} /> : <TerminalPanel id="intelligence-learning-performance" title="Performance by slice (per currency, never summed across currencies)" actions={<select aria-label="Currency filter" value={currency} onChange={(event) => setCurrency(event.target.value)}><option value="">All currencies</option>{currencies.map((code) => <option key={code} value={code}>{code}</option>)}</select>}>
      {performance.isError ? <ErrorState error={performance.error} compact /> : <DataTable rows={performance.data ?? []} columns={performanceColumns} getRowKey={(r) => `${r.strategy_key}|${r.strategy_version}|${r.instrument}|${r.regime}|${r.structure}|${r.mode}|${r.currency}`} caption="Performance" emptyTitle="No statistics yet" emptyDescription="Statistics appear after the first closed trade." />}
    </TerminalPanel>)}
    {view === 'Calibration' && (calibration.isLoading ? <Skeleton lines={5} /> : <TerminalPanel id="intelligence-learning-calibration" title="Is the model's probability of profit right?">
      {calibration.isError ? <ErrorState error={calibration.error} compact /> : <DataTable rows={calibration.data ?? []} columns={calibrationColumns} getRowKey={(r) => `${r.strategy_key}|${r.bin}`} caption="Calibration" emptyTitle="No calibration data" emptyDescription="Needs closed option trades with a recorded probability of profit." />}
      <small>Calibration needs many trades. Small bins are shown for transparency and never raise an alarm.</small>
    </TerminalPanel>)}
    {view === 'Weekly' && <TerminalPanel id="intelligence-learning-weekly" title="Aggregated strategy reviews (hypotheses with test plans, never instructions)" actions={<div className="row-actions">
      <input aria-label="Strategy key" placeholder="STRATEGY_KEY" value={strategyKey} onChange={(event) => setStrategyKey(event.target.value)} />
      <button className="button-secondary" disabled={!strategyKey.trim() || requestWeekly.isPending} onClick={() => requestWeekly.mutate()}>{requestWeekly.isPending ? 'Queuing…' : 'Review now'}</button>
    </div>}>
      {requestWeekly.isSuccess && <small>Queued. It appears here when the model has answered.</small>}
      {weekly.isLoading ? <Skeleton lines={4} /> : weekly.isError ? <ErrorState error={weekly.error} compact /> : (weekly.data ?? []).length === 0
        ? <EmptyState title="No weekly reviews yet" description="They run once a week per strategy, and say 'insufficient data' until there are enough closed trades." />
        : (weekly.data ?? []).map((row) => <article key={row.id} className="panel"><header className="row-actions"><strong>{row.strategy_key}</strong><StatusBadge status={row.is_fallback ? 'RULE-BASED' : row.verdict} tone={row.is_fallback ? 'warning' : 'neutral'} /><small>n = {row.sample_n} · {formatDateTime(row.created_at)}</small></header><p>{row.summary}</p></article>)}
    </TerminalPanel>}
    {view === 'What-if' && <TerminalPanel id="intelligence-learning-whatif" title="What if the entry gates had been tighter? (option trades, hypotheses only)" actions={<input aria-label="Strategy key" placeholder="STRATEGY_KEY" value={strategyKey} onChange={(event) => setStrategyKey(event.target.value)} />}>
      {!strategyKey.trim() ? <EmptyState title="Enter a strategy key" description="For example RPS_OPTIONS_AUTO_SELECTOR." /> : sweep.isLoading ? <Skeleton lines={4} /> : sweep.isError ? <ErrorState error={sweep.error} compact /> : sweep.data && <>
        <ul>{sweep.data.warnings.map((w) => <li key={w}><small>{w}</small></li>)}</ul>
        <DataTable rows={sweep.data.results} getRowKey={(r) => `${r.filter}|${r.threshold}`} caption="Filter sweep" emptyTitle="Nothing to show" emptyDescription={`n = ${sweep.data.n_trades} option trades.`} columns={[
          {id: 'f', header: 'Filter', cell: (r) => `${r.filter.replaceAll('_', ' ')} ${String(r.threshold)}`},
          {id: 'k', header: 'Kept / removed', align: 'right' as const, className: 'mono', cell: (r) => `${r.n_kept} / ${r.n_removed}${r.too_few ? ' (too few)' : ''}`},
          {id: 'm', header: 'Mean kept (bps)', align: 'right' as const, className: 'mono', cell: (r) => r.mean_kept_bps === null ? '—' : formatNumber(r.mean_kept_bps, 1)},
          {id: 'd', header: 'Difference (bps)', align: 'right' as const, className: 'mono', cell: (r) => r.delta_bps === null ? '—' : formatNumber(r.delta_bps, 1)},
          {id: 'c', header: 'Holds after correction', cell: (r) => r.holds_after_correction ? 'yes' : 'no'},
        ]} />
        <small>{sweep.data.comparisons} filter settings were compared; the more that are tried, the likelier one looks good by luck.</small>
      </>}
    </TerminalPanel>}
    {view === 'Experiments' && (experiments.isLoading ? <Skeleton lines={5} /> : <TerminalPanel id="intelligence-learning-experiments" title="Try next time: measurement plans (hypotheses, nothing is changed)">
      {experiments.isError ? <ErrorState error={experiments.error} compact /> : <>
        <p><small>Each plan compares trades with and without a condition that looked relevant. A result only counts after the minimum number of matching trades, and is judged together with the other plans so luck is not mistaken for a pattern.</small></p>
        <DataTable rows={experiments.data ?? []} getRowKey={(e) => e.id} caption="Experiments" emptyTitle="No experiments yet" emptyDescription="After a closed trade, the rule findings can propose up to three measurement plans." columns={[
          {id: 'plan', header: 'Plan', cell: (e: ExperimentRow) => <div className="primary-cell"><strong>{e.title}</strong><span>{e.try}</span></div>},
          {id: 'cond', header: 'Condition', className: 'mono', cell: (e: ExperimentRow) => e.condition},
          {id: 'progress', header: 'Matching trades since', align: 'right' as const, className: 'mono', cell: (e: ExperimentRow) => `${e.progress.matching_forward_trades} of ${e.progress.needed}`},
          {id: 'history', header: 'What past trades say', cell: (e: ExperimentRow) => e.history_check?.verdict ?? 'no automatic check'},
          {id: 'status', header: 'Status', cell: (e: ExperimentRow) => <StatusBadge status={e.status} tone={e.status === 'PROPOSED' ? 'warning' : e.status === 'REJECTED' || e.status === 'EXPIRED' ? 'neutral' : 'info'} />},
          {id: 'result', header: 'Result', cell: (e: ExperimentRow) => e.outcome
            ? <span>{e.outcome.result === 'HURT' ? 'Trades with the condition did worse' : e.outcome.result === 'HELPED' ? 'Trades with the condition did better' : 'No reliable difference'} (n {e.outcome.n_condition} vs {e.outcome.n_rest}{e.outcome.delta_bps === null ? '' : `, ${formatNumber(e.outcome.delta_bps, 1)} bps`})</span>
            : '—'},
          {id: 'act', header: '', cell: (e: ExperimentRow) => e.status === 'PROPOSED'
            ? <div className="row-actions"><button className="button-secondary" onClick={() => setPendingExperiment({id: e.id, decision: 'acknowledge'})}>Acknowledge</button><button className="button-secondary" onClick={() => setPendingExperiment({id: e.id, decision: 'reject'})}>Reject</button></div> : null},
        ]} />
      </>}
    </TerminalPanel>)}
    {view === 'Edge monitor' && (changePoints.isLoading ? <Skeleton lines={5} /> : <TerminalPanel id="intelligence-learning-edge" title="Is a strategy's edge fading? (per currency, always with n)">
      {changePoints.isError ? <ErrorState error={changePoints.error} compact /> : <>
        <DataTable rows={changePoints.data ?? []} getRowKey={(c) => `${c.strategy_key}|${c.currency}|${c.metric}`} caption="Edge monitor" emptyTitle="Nothing monitored yet" emptyDescription="The monitor needs at least 15 closed trades of a strategy before it can say anything." columns={[
          {id: 's', header: 'Strategy', cell: (c: ChangePointRow) => <div className="primary-cell"><strong>{c.strategy_key}</strong><span>{c.currency} · {SERIES_LABEL[c.metric] ?? c.metric}</span></div>},
          {id: 'status', header: 'Status', cell: (c: ChangePointRow) => <StatusBadge status={c.status.replaceAll('_', ' ')} tone={CHANGE_TONE[c.status]} />},
          {id: 'n', header: 'n', align: 'right' as const, className: 'mono', cell: (c: ChangePointRow) => c.n},
          {id: 'base', header: 'Baseline', className: 'mono', cell: (c: ChangePointRow) => c.baseline_mean === null ? '—' : `${formatNumber(c.baseline_mean, 1)} bps (first ${c.reference_n} trades)`},
          {id: 'drift', header: 'Cumulative drift', align: 'right' as const, className: 'mono', cell: (c: ChangePointRow) => c.statistic === null || c.threshold === null ? '—' : `${c.statistic.toFixed(1)} of ${c.threshold.toFixed(0)}`},
          {id: 'note', header: 'What it says', cell: (c: ChangePointRow) => c.note},
        ]} />
        <small>A flag to look at a strategy, not a verdict: small samples raise false alarms (about 1 stable strategy in 20 within 50 trades), small drops need many trades to be seen, and a change in market conditions looks the same as a broken edge.</small>
      </>}
    </TerminalPanel>)}
    <ConfirmActionDialog
      open={pendingExperiment !== null}
      title={pendingExperiment?.decision === 'reject' ? 'Reject this plan' : 'Track this plan'}
      description={pendingExperiment?.decision === 'reject' ? 'The plan is closed and will not be tracked. Nothing about the strategy changes.' : 'You will keep an eye on this condition. Nothing about the strategy changes.'}
      confirmLabel={pendingExperiment?.decision === 'reject' ? 'Reject' : 'Acknowledge'}
      requireReason={pendingExperiment?.decision === 'reject'}
      danger={pendingExperiment?.decision === 'reject'}
      pending={decideExperiment.isPending}
      onClose={() => setPendingExperiment(null)}
      onConfirm={(reason) => { if (pendingExperiment) decideExperiment.mutate({id: pendingExperiment.id, decision: pendingExperiment.decision, reason}) }}
    />
    <DetailDrawer open={lessonId !== null} title="Lesson" subtitle={lesson.data?.statement} onClose={() => setLessonId(null)}>
      {lesson.isLoading && <Skeleton lines={4} />}
      {lesson.isError && <ErrorState error={lesson.error} compact />}
      {lesson.data && <>
        <dl className="detail-list">
          <div><dt>Status</dt><dd><StatusBadge status={lesson.data.status} tone={STATUS_TONE[lesson.data.status]} /></dd></div>
          <div><dt>Pattern</dt><dd className="mono">{describePredicate(lesson.data.predicate)}</dd></div>
          <div><dt>Expected</dt><dd>{lesson.data.expected_effect.outcome_metric.replace('outcome.', '')} {lesson.data.expected_effect.direction}</dd></div>
          <div><dt>Evidence</dt><dd>n = {lesson.data.n}; confidence {lesson.data.posterior_mean.toFixed(2)}</dd></div>
          {lesson.data.q_value !== null && lesson.data.q_value !== undefined && <div><dt>Chance it is luck (q)</dt><dd className="mono">{lesson.data.q_value.toFixed(3)} (corrected for the number of lessons being tested)</dd></div>}
        </dl>
        {lesson.data.note && <p><small>{lesson.data.note}</small></p>}
        <DataTable rows={lesson.data.evidence ?? []} getRowKey={(e) => e.episode_id} caption="Evidence" emptyTitle="No evidence yet" columns={[
          {id: 'ep', header: 'Trade', cell: (e) => `#${e.episode_id}${e.origin ? ' (proposed it)' : ''}`},
          {id: 'v', header: 'Verdict', cell: (e) => e.origin ? 'does not count' : e.verdict.toLowerCase()},
          {id: 'o', header: 'Outcome (bps)', align: 'right' as const, className: 'mono', cell: (e) => e.outcome_value === null ? '—' : formatNumber(e.outcome_value, 1)},
          {id: 'w', header: 'Weight', align: 'right' as const, className: 'mono', cell: (e) => e.weight},
          {id: 'f', header: 'Later trade', cell: (e) => e.forward ? 'yes' : 'no'},
        ]} />
      </>}
    </DetailDrawer>
  </>
}
