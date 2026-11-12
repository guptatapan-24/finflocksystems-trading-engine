import {useMemo, useState} from 'react'
import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query'
import {RotateCcw, Save} from 'lucide-react'
import {mutationOptions, request} from '../../api/client'
import {queries} from '../../api/queries'
import type {JsonRecord, StrategyInstance} from '../../api/types'
import {ErrorState, TerminalPanel} from '../../components/ui'
import {RiskProfileSelector} from '../../components/RiskProfileSelector'
import {AdvisoryBanner} from '../intelligence/AdvisoryBanner'

/** Fields the Backend PATCH accepts that also retire the running version.
 *
 * `update_instance` treats a change to any of these as material: it retires the
 * current immutable version, increments to the next one, and resets warm-up to
 * zero. A name change alone does not. The form says so before saving rather
 * than letting an operator discover it when a live strategy restarts. */
const MATERIAL_FIELDS = ['timeframe', 'parameters', 'risk_policy_id', 'order_policy_id'] as const

type Draft = {name: string; timeframe: string; parameters: string; riskPolicyId: string; orderPolicyId: string}

function toDraft(strategy: StrategyInstance): Draft {
  return {
    name: strategy.name,
    timeframe: strategy.timeframe,
    parameters: JSON.stringify(strategy.parameters, null, 2),
    riskPolicyId: strategy.risk_policy_id ? String(strategy.risk_policy_id) : '',
    orderPolicyId: strategy.order_policy_id ? String(strategy.order_policy_id) : '',
  }
}

/** The changed fields only, so an unchanged edit never retires a version. */
export function buildStrategyPatch(strategy: StrategyInstance, draft: Draft): {payload: JsonRecord; material: string[]} | {error: string} {
  let parameters: JsonRecord
  try {
    parameters = JSON.parse(draft.parameters) as JsonRecord
  } catch {
    return {error: 'Parameters must be valid JSON.'}
  }
  if (parameters === null || typeof parameters !== 'object' || Array.isArray(parameters)) {
    return {error: 'Parameters must be a JSON object.'}
  }
  const name = draft.name.trim()
  if (!name) return {error: 'A strategy needs a name.'}

  const payload: JsonRecord = {}
  if (name !== strategy.name) payload.name = name
  if (draft.timeframe && draft.timeframe !== strategy.timeframe) payload.timeframe = draft.timeframe
  if (JSON.stringify(parameters) !== JSON.stringify(strategy.parameters)) payload.parameters = parameters
  const risk = draft.riskPolicyId ? Number(draft.riskPolicyId) : null
  const order = draft.orderPolicyId ? Number(draft.orderPolicyId) : null
  if (risk !== (strategy.risk_policy_id ?? null)) payload.risk_policy_id = risk
  if (order !== (strategy.order_policy_id ?? null)) payload.order_policy_id = order

  const material = MATERIAL_FIELDS.filter((field) => field in payload)
  return {payload, material}
}

export function EditStrategyPanel({strategy}: {strategy: StrategyInstance}) {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<Draft>(() => toDraft(strategy))
  const [invalid, setInvalid] = useState<string | null>(null)
  const definitions = useQuery(queries.strategyDefinitions())
  const policies = useQuery(queries.strategyPolicies())
  const riskProfiles = useQuery(queries.riskProfiles())
  const definition = (definitions.data || []).find((item) => item.key === strategy.definition_key)
  const timeframes = definition?.supported_timeframes || [strategy.timeframe]

  // Only a strategy saved with a risk profile edits it here; every other strategy keeps the JSON editor.
  const currentProfile = typeof strategy.target_configuration?.risk_profile === 'string' ? strategy.target_configuration.risk_profile : ''
  const catalogue = currentProfile && riskProfiles.data?.enabled && riskProfiles.data.definition_keys.includes(strategy.definition_key) ? riskProfiles.data : null
  const [riskProfile, setRiskProfile] = useState(currentProfile)
  const profileChanged = Boolean(catalogue) && riskProfile !== currentProfile

  const built = useMemo(() => buildStrategyPatch(strategy, draft), [strategy, draft])
  const error = 'error' in built ? built.error : null
  const payload = 'error' in built ? null : profileChanged ? {...built.payload, risk_profile: riskProfile} : built.payload
  const material = 'error' in built ? [] : profileChanged ? [...built.material, 'risk_profile'] : built.material
  const changed = payload !== null && Object.keys(payload).length > 0

  const update = useMutation({
    mutationFn: (body: JsonRecord) => request<StrategyInstance>(`strategy-instances/${strategy.id}/`, mutationOptions('PATCH', body, true)),
    onSuccess: async () => {
      setInvalid(null)
      await Promise.all([
        queryClient.invalidateQueries({queryKey: ['strategy-instance', strategy.id]}),
        queryClient.invalidateQueries({queryKey: ['strategy-instances']}),
        queryClient.invalidateQueries({queryKey: ['strategy-timeline', strategy.id]}),
      ])
    },
  })

  return <TerminalPanel
    id="edit-strategy"
    title="Edit strategy"
    description="Changes are validated by the Backend against the plugin schema before they are accepted."
    className="detail-wide"
    defaultOpen={false}
  >
    <div className="form-grid two-columns">
      <label>Name<input aria-label="Strategy name" value={draft.name} onChange={(event) => setDraft((current) => ({...current, name: event.target.value}))} /></label>
      <label>Timeframe<select aria-label="Strategy timeframe" value={draft.timeframe} onChange={(event) => setDraft((current) => ({...current, timeframe: event.target.value}))}>{timeframes.map((value) => <option key={value}>{value}</option>)}</select></label>
      <label>Risk policy<select aria-label="Strategy risk policy" value={draft.riskPolicyId} onChange={(event) => setDraft((current) => ({...current, riskPolicyId: event.target.value}))}><option value="">Platform default</option>{(policies.data?.risk_policies || []).map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
      <label>Order policy<select aria-label="Strategy order policy" value={draft.orderPolicyId} onChange={(event) => setDraft((current) => ({...current, orderPolicyId: event.target.value}))}><option value="">Platform default</option>{(policies.data?.order_policies || []).map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
    </div>
    {catalogue
      ? <RiskProfileSelector catalogue={catalogue} value={riskProfile} onChange={setRiskProfile} disabled={update.isPending} />
      : <div className="form-grid"><label>Parameters (JSON)<textarea className="parameter-editor" aria-label="Strategy parameters" spellCheck={false} rows={14} value={draft.parameters} onChange={(event) => setDraft((current) => ({...current, parameters: event.target.value}))} /></label></div>}
    {Boolean(payload?.parameters) && !error && <AdvisoryBanner strategyKey={strategy.definition_key} parameters={(payload?.parameters ?? {}) as Record<string, unknown>} instrument={strategy.symbol} />}
    {error && <div className="inline-warning"><div><strong>Cannot save yet</strong><p>{error}</p></div></div>}
    {material.length > 0 && <div className="inline-warning"><div><strong>This creates a new immutable version</strong><p>Changing {material.join(', ')} retires version {strategy.version} and resets warm-up to zero. {strategy.enabled ? 'This strategy is enabled, so it stops trading until warm-up completes again.' : 'The strategy is disabled, so it stays disabled.'}</p></div></div>}
    {invalid && <div className="inline-warning"><div><strong>Nothing to save</strong><p>{invalid}</p></div></div>}
    {update.isError && <ErrorState title="Update rejected by the Backend" error={update.error} compact />}
    {update.isSuccess && !changed && <div className="inline-success"><strong>Saved</strong><p>The Backend accepted the change and returned version {strategy.version}.</p></div>}
    <div className="system-actions">
      <button className="button-secondary" type="button" disabled={update.isPending} onClick={() => {setDraft(toDraft(strategy)); setRiskProfile(currentProfile); setInvalid(null)}}><RotateCcw />Reset</button>
      <button
        className="button-primary"
        type="button"
        disabled={Boolean(error) || update.isPending}
        onClick={() => {
          if (!payload || !changed) {setInvalid('Edit a field before saving.'); return}
          setInvalid(null)
          update.mutate(payload)
        }}
      ><Save />{update.isPending ? 'Saving…' : 'Save changes'}</button>
    </div>
  </TerminalPanel>
}
