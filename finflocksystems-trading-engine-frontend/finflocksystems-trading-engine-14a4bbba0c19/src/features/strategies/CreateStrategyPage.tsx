import {useState} from 'react'
import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query'
import {ArrowLeft, ArrowRight, Check, CircleCheck, ShieldCheck} from 'lucide-react'
import {Link, useNavigate} from 'react-router-dom'
import {mutationOptions, request} from '../../api/client'
import {AdvisoryBanner} from '../intelligence/AdvisoryBanner'
import {queries} from '../../api/queries'
import type {ExecutionMode, InstrumentResolution, RiskProfileCatalogue, Scalar, StrategyDefinition, StrategyInstance} from '../../api/types'
import {BrokerInstrumentSearch} from '../../components/BrokerInstrumentSearch'
import {RiskProfileSelector, profileLabel} from '../../components/RiskProfileSelector'
import {SchemaParameterForm} from '../../components/SchemaParameterForm'
import {ErrorState, PageHeader, Skeleton, StatusBadge, TerminalPanel, formatCompact} from '../../components/ui'
import {executionModeForSession, executionModeLabel} from '../../executionMode'
import {useSelection} from '../../stores/useSelection'

const steps = ['Instrument', 'Definition', 'Parameters', 'Risk & execution', 'Review']

interface Draft {
  ticker: string
  exchange: string
  name: string
  definitionKey: string
  timeframe: string
  parameters: Record<string, Scalar>
  riskProfile: string
  targetWeight: string
  capitalShare: string
  priority: string
  riskPolicyId: string
  orderPolicyId: string
  intentNote: string
}

const initialDraft: Draft = {
  ticker: '', exchange: 'SMART', name: '', definitionKey: '', timeframe: '', parameters: {}, riskProfile: '',
  targetWeight: '0.05', capitalShare: '1', priority: '100', riskPolicyId: '', orderPolicyId: '', intentNote: '',
}

export function CreateStrategyPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const {selectedPortfolioId, portfolio, sessions} = useSelection()
  const executionSession = sessions.find(
    (candidate) => candidate.id === portfolio?.gateway_session_id,
  ) || null
  const executionMode = executionModeForSession(executionSession)
  const definitions = useQuery(queries.strategyDefinitions())
  const instruments = useQuery(queries.instruments())
  const policies = useQuery(queries.strategyPolicies())
  const riskProfiles = useQuery(queries.riskProfiles())
  const [step, setStep] = useState(0)
  const [draft, setDraft] = useState<Draft>(initialDraft)
  const [validation, setValidation] = useState<string | null>(null)
  const [resolution, setResolution] = useState<InstrumentResolution | null>(null)
  const definition = definitions.data?.find((item) => item.key === draft.definitionKey) || null
  const isOptionDefinition = definition?.strategy_kind === 'OPTIONS'
  // Risk-profile mode needs the Backend flag on and a definition it covers; otherwise the schema form below is unchanged.
  const catalogue = riskProfiles.data?.enabled && definition && riskProfiles.data.definition_keys.includes(definition.key) ? riskProfiles.data : null
  const selectedProfile = catalogue ? (draft.riskProfile || catalogue.default_profile) : ''
  const effectiveParameters = catalogue ? catalogue.profiles[selectedProfile]?.parameters ?? draft.parameters : draft.parameters
  const create = useMutation<StrategyInstance, Error, boolean | void>({
    mutationFn: (activateRequested) => {
      const activate = activateRequested === true
      if (!executionMode || !executionSession) throw new Error('The selected portfolio must have an assigned Paper or Live Gateway session')
      return request<StrategyInstance>('strategy-instances/', mutationOptions('POST', {
        name: draft.name.trim(), definition_key: draft.definitionKey, instrument_id: resolution?.instrument_id,
        portfolio_id: selectedPortfolioId, timeframe: draft.timeframe,
        ...(catalogue ? {risk_profile: selectedProfile} : {parameters: draft.parameters}),
        target_configuration: {target_weight: isOptionDefinition ? 0 : Number(draft.targetWeight), capital_share: Number(draft.capitalShare), priority: Number(draft.priority)},
        risk_policy_id: draft.riskPolicyId ? Number(draft.riskPolicyId) : null,
        order_policy_id: draft.orderPolicyId ? Number(draft.orderPolicyId) : null,
        execution_mode: executionMode, qualify: false, activate,
        ...(draft.intentNote.trim() ? {intent_note: draft.intentNote.trim().slice(0, 500)} : {}),
      }, true))
    },
    onSuccess: async (data) => {
      await queryClient.invalidateQueries({queryKey: ['strategy-instances']})
      navigate(`/strategies/${data.id}`)
    },
  })

  const selectDefinition = (key: string) => {
    const selected = definitions.data?.find((item) => item.key === key)
    setDraft((current) => ({...current, definitionKey: key, timeframe: selected?.supported_timeframes[0] || '', parameters: {...(selected?.default_parameters || {})}}))
  }
  const validateStep = () => {
    let error = ''
    if (step === 0 && !draft.ticker.trim()) error = 'Enter a ticker before continuing.'
    else if (step === 0 && !resolution?.conid) error = 'Select and qualify an exact IBKR contract before continuing.'
    if (step === 1 && (!draft.name.trim() || !draft.definitionKey || !draft.timeframe)) error = 'Choose a definition and timeframe, and name this instance.'
    if (step === 2 && definition && !catalogue) {
      const missing = (definition.parameter_schema.required || []).filter((key) => draft.parameters[key] === undefined || draft.parameters[key] === null || draft.parameters[key] === '')
      if (missing.length) error = `Complete required parameters: ${missing.join(', ')}.`
    }
    if (step === 3 && ((!isOptionDefinition && !Number.isFinite(Number(draft.targetWeight))) || Number(draft.capitalShare) < 0 || Number(draft.capitalShare) > 1 || Number(draft.priority) < 1)) error = 'Review target, capital share, and priority values.'
    setValidation(error || null)
    return !error
  }
  const next = () => { if (validateStep()) setStep((value) => Math.min(4, value + 1)) }

  if (definitions.isLoading || policies.isLoading) return <><PageHeader title="Create strategy" description="Configure a strategy through a safe, schema-driven workflow." /><Skeleton lines={7} height={480} /></>
  if (definitions.isError) return <ErrorState title="Strategy definitions are unavailable" error={definitions.error} onRetry={() => void definitions.refetch()} />
  if (!definitions.data?.length) return <ErrorState title="No strategy definitions are enabled" error={new Error('The Backend strategy catalog is empty. The page will retry automatically; verify Backend migrations if this continues.')} onRetry={() => void definitions.refetch()} />

  return <div className="page-stack create-strategy-page">
    <PageHeader eyebrow="Strategies / New" title="Create a strategy" description={`Build for ${portfolio?.name || 'the selected portfolio'}. Execution mode comes from its assigned Gateway session${executionMode ? ` (${executionModeLabel(executionMode)})` : ''}.`} actions={<Link className="button-secondary" to="/strategies"><ArrowLeft />Back to strategies</Link>} />
    <ol className="wizard-steps" aria-label="Create strategy progress">{steps.map((label, index) => <li key={label} className={index === step ? 'active' : index < step ? 'complete' : ''}><span>{index < step ? <Check /> : index + 1}</span><strong>{label}</strong></li>)}</ol>
    <TerminalPanel id="create-strategy-wizard" className="wizard-panel" collapsible={false}>
      {step === 0 && <div className="wizard-content"><div className="wizard-heading"><span>1</span><div><h2>Choose the instrument or option underlying</h2><p>Search IBKR by ticker or company name, then select and qualify the exact stock or index underlying through this portfolio&apos;s assigned Gateway.</p></div></div><BrokerInstrumentSearch value={draft.ticker} suggestions={instruments.data || []} autoFocus includeIndices portfolioId={selectedPortfolioId} gatewaySessionId={executionSession?.id} onValueChange={(ticker) => setDraft((current) => ({...current, ticker}))} onContractSelected={(contract) => setDraft((current) => ({...current, exchange: contract.exchange}))} onResolved={(value) => {setResolution(value); setValidation(null)}} /></div>}
      {step === 1 && <WizardDefinition draft={draft} setDraft={setDraft} definitions={definitions.data || []} definition={definition} onDefinition={selectDefinition} />}
      {step === 2 && (catalogue
        ? <WizardRiskProfile catalogue={catalogue} value={selectedProfile} onChange={(riskProfile) => setDraft((current) => ({...current, riskProfile}))} />
        : <WizardParameters draft={draft} setDraft={setDraft} definition={definition} />)}
      {step === 3 && <WizardExecution draft={draft} setDraft={setDraft} policies={policies.data} executionMode={executionMode} gatewayName={executionSession?.display_name} optionStrategy={isOptionDefinition} />}
      {step === 4 && <WizardReview draft={draft} definition={definition} resolution={resolution} portfolioName={portfolio?.name} executionMode={executionMode} gatewayName={executionSession?.display_name} catalogue={catalogue} profile={selectedProfile} />}
      {step === 4 && draft.definitionKey && <AdvisoryBanner strategyKey={draft.definitionKey} parameters={effectiveParameters} instrument={draft.ticker.trim().toUpperCase()} />}
      {step === 4 && <label className="field"><span>Intent note (optional)</span><textarea aria-label="Intent note" maxLength={500} value={draft.intentNote} onChange={(event) => setDraft({...draft, intentNote: event.target.value})} placeholder="Why are you running this? The intelligence layer keeps it with the run (plain text, never an instruction)." /></label>}
      {(validation || create.isError) && <ErrorState title={validation ? 'Complete this step' : 'Strategy validation failed'} error={validation ? new Error(validation) : create.error} compact />}
      <div className="wizard-footer">
        <button className="button-secondary" disabled={step === 0 || create.isPending} onClick={() => {setValidation(null); setStep((value) => Math.max(0, value - 1))}}><ArrowLeft />Back</button>
        <span>{step < 4 ? `Step ${step + 1} of ${steps.length}` : 'Choose the initial strategy state'}</span>
        {step < 4
          ? <button className="button-primary" onClick={next}>Continue<ArrowRight /></button>
          : <div className="wizard-create-actions">
              <button className="button-secondary" disabled={create.isPending || !selectedPortfolioId || !executionMode} onClick={() => {if (validateStep()) create.mutate(false)}}><Check />{create.isPending && create.variables === false ? 'Creating…' : 'Create as disabled'}</button>
              <button className="button-primary" disabled={create.isPending || !selectedPortfolioId || !executionMode} onClick={() => {if (validateStep()) create.mutate(true)}}><ShieldCheck />{create.isPending && create.variables === true ? 'Creating & queuing…' : 'Create and activate'}</button>
            </div>}
      </div>
    </TerminalPanel>
  </div>
}

function WizardDefinition({draft, setDraft, definitions, definition, onDefinition}: {draft: Draft; setDraft: React.Dispatch<React.SetStateAction<Draft>>; definitions: StrategyDefinition[]; definition: StrategyDefinition | null; onDefinition: (key: string) => void}) {
  return <div className="wizard-content"><div className="wizard-heading"><span>2</span><div><h2>Select a strategy definition</h2><p>Definitions and allowed timeframes come directly from the Backend plugin catalog.</p></div></div><div className="form-grid two-columns"><label>Instance name<input aria-label="Instance name" value={draft.name} placeholder="A descriptive portfolio-unique name" onChange={(event) => setDraft((current) => ({...current, name: event.target.value}))} /></label><label>Definition<select aria-label="Strategy definition" value={draft.definitionKey} onChange={(event) => onDefinition(event.target.value)}><option value="">Choose a backend definition</option>{definitions.map((item) => <option key={item.key} value={item.key}>{item.name}</option>)}</select></label><label>Timeframe<select aria-label="Timeframe" value={draft.timeframe} disabled={!definition} onChange={(event) => setDraft((current) => ({...current, timeframe: event.target.value}))}><option value="">Choose a timeframe</option>{(definition?.supported_timeframes || []).map((value) => <option key={value}>{value}</option>)}</select></label></div>{definition && <div className="definition-card"><CircleCheck /><div><strong>{definition.name}</strong><p>{definition.description}</p><div>{definition.supported_directions.map((direction) => <StatusBadge key={direction} status={direction} />)}</div></div></div>}</div>
}

function WizardParameters({draft, setDraft, definition}: {draft: Draft; setDraft: React.Dispatch<React.SetStateAction<Draft>>; definition: StrategyDefinition | null}) {
  return <div className="wizard-content"><div className="wizard-heading"><span>3</span><div><h2>Configure parameters</h2><p>Fields, defaults, ranges, and choices are generated from this definition’s parameter schema.</p></div></div>{definition ? <SchemaParameterForm schema={definition.parameter_schema} values={draft.parameters} onChange={(parameters) => setDraft((current) => ({...current, parameters}))} /> : <p>Select a definition first.</p>}</div>
}

function WizardRiskProfile({catalogue, value, onChange}: {catalogue: RiskProfileCatalogue; value: string; onChange: (profile: string) => void}) {
  return <div className="wizard-content"><div className="wizard-heading"><span>3</span><div><h2>Choose a risk profile</h2><p>The platform sets every strategy parameter from the profile you pick. The values below are what this profile changes.</p></div></div><RiskProfileSelector catalogue={catalogue} value={value} onChange={onChange} /></div>
}

function WizardExecution({draft, setDraft, policies, executionMode, gatewayName, optionStrategy}: {draft: Draft; setDraft: React.Dispatch<React.SetStateAction<Draft>>; policies?: {risk_policies: {id: number; name: string}[]; order_policies: {id: number; name: string}[]}; executionMode: ExecutionMode | null; gatewayName?: string; optionStrategy: boolean}) {
  return <div className="wizard-content"><div className="wizard-heading"><span>4</span><div><h2>Set capital, risk, and execution</h2><p>Execution uses the complete sizing, risk, OMS, and matching Gateway pipeline. Use rebalance preview when no orders should be created.</p></div></div>{optionStrategy && <div className="safety-path"><ShieldCheck /><div><strong>Exact multi-leg quantity execution</strong><p>Lots determine the contract targets. Long protection is filled before short entry legs; capital share remains the strategy risk-allocation boundary.</p></div></div>}<div className="form-grid three-columns">{!optionStrategy && <label>Target weight<input aria-label="Target weight" type="number" min="-1" max="1" step="0.0001" value={draft.targetWeight} onChange={(event) => setDraft((current) => ({...current, targetWeight: event.target.value}))} /></label>}<label>Capital share<input aria-label="Capital share" type="number" min="0" max="1" step="0.01" value={draft.capitalShare} onChange={(event) => setDraft((current) => ({...current, capitalShare: event.target.value}))} /></label><label>Priority<input aria-label="Priority" type="number" min="1" step="1" value={draft.priority} onChange={(event) => setDraft((current) => ({...current, priority: event.target.value}))} /></label><div className="mode-derived-field" aria-label="Execution mode"><span>Execution mode</span><strong><StatusBadge status={executionMode || 'UNAVAILABLE'} /></strong><small>{gatewayName ? `Derived from ${gatewayName}` : 'Assign the portfolio to a Gateway session.'}</small></div></div><TerminalPanel id="advanced-policy-settings" title="Advanced policy settings" description="Use platform defaults unless this strategy needs an approved policy override." defaultOpen={false}><div className="form-grid two-columns"><label>Risk policy<select aria-label="Risk policy" value={draft.riskPolicyId} onChange={(event) => setDraft((current) => ({...current, riskPolicyId: event.target.value}))}><option value="">Platform default</option>{(policies?.risk_policies || []).map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><label>Order policy<select aria-label="Order policy" value={draft.orderPolicyId} onChange={(event) => setDraft((current) => ({...current, orderPolicyId: event.target.value}))}><option value="">Platform default</option>{(policies?.order_policies || []).map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label></div></TerminalPanel></div>
}

function WizardReview({draft, definition, resolution, portfolioName, executionMode, gatewayName, catalogue, profile}: {draft: Draft; definition: StrategyDefinition | null; resolution: InstrumentResolution | null; portfolioName?: string; executionMode: ExecutionMode | null; gatewayName?: string; catalogue: RiskProfileCatalogue | null; profile: string}) {
  const optionStrategy = definition?.strategy_kind === 'OPTIONS'
  const review = [
    ['Portfolio', portfolioName || 'Selected portfolio'], ['Instrument', `${draft.ticker.toUpperCase()} · ${draft.exchange}`], ['Contract', resolution?.conid ? `Qualified · conId ${resolution.conid}` : 'Pending / requested on create'],
    ['Definition', definition?.name || '—'], ['Timeframe', draft.timeframe], ['Execution mode', executionMode || 'UNAVAILABLE'], ['Gateway', gatewayName || 'Not assigned'], ...(optionStrategy ? [['Sizing', 'Exact leg quantities from lots']] : [['Target weight', draft.targetWeight]]), ['Capital share', draft.capitalShare], ['Priority', draft.priority],
    ...(catalogue ? [['Risk profile', profileLabel(profile)]] : []),
  ]
  return <div className="wizard-content"><div className="wizard-heading"><span>5</span><div><h2>Review and validate</h2><p>The Backend performs final schema, contract, session-mode, and policy validation before creating immutable version 1.</p></div></div>{optionStrategy && <div className="safety-path"><ShieldCheck /><div><strong>Automatic RPS lifecycle</strong><p>Entry requires an RPS score over its floor, then the IV, breach, POP and EV gates, then the 14-factor TQS picks the structure. Open interest scores a factor and confirms a level; it never selects a strike. Positions are held to expiry unless a soft profit or volatility exit fires.</p></div></div>}<div className="review-grid">{review.map(([label, value]) => <div key={label}><span>{label}</span>{label === 'Execution mode' ? <StatusBadge status={value} /> : <strong>{value}</strong>}</div>)}</div>{catalogue
    ? <div className="review-parameters"><h3>Risk profile · {profileLabel(profile)}</h3>{catalogue.summary_fields.map((field) => <div key={field.key}><span>{field.label}</span><code>{field.type === 'boolean' ? (catalogue.profiles[profile]?.summary[field.key] ? 'Yes' : 'No') : formatCompact(catalogue.profiles[profile]?.summary[field.key] ?? null)}</code></div>)}</div>
    : <div className="review-parameters"><h3>Parameters</h3>{Object.entries(draft.parameters).map(([key, value]) => <div key={key}><span>{key.replaceAll('_', ' ')}</span><code>{formatCompact(value)}</code></div>)}</div>}<div className="safety-path"><ShieldCheck /><div><strong>Execution boundary preserved</strong><p>Paper and Live both pass through allocation, sizing, risk, OMS, the matching Gateway, ledger, and reconciliation. Preview rebalance creates no orders.</p></div></div></div>
}
