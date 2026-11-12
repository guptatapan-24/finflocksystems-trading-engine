import {useState} from 'react'
import {render, screen, within} from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type {RiskProfileCatalogue} from '../src/api/types'
import {RiskProfileSelector} from '../src/components/RiskProfileSelector'
import createPage from '../src/features/strategies/CreateStrategyPage.tsx?raw'
import editPanel from '../src/features/strategies/EditStrategyPanel.tsx?raw'

// The numbers are test data standing in for the Backend response; the component must show whatever it is given.
const catalogue: RiskProfileCatalogue = {
  enabled: true,
  default_profile: 'NEUTRAL',
  profile_names: ['CONSERVATIVE', 'NEUTRAL', 'AGGRESSIVE'],
  definition_keys: ['RPS_OPTIONS_AUTO_SELECTOR'],
  summary_fields: [
    {key: 'rps_threshold', label: 'RPS Entry Floor', type: 'number'},
    {key: 'rps_iv_score_threshold', label: 'RPS IV Score Threshold', type: 'number'},
    {key: 'rps_tqs_threshold', label: 'RPS TQS Threshold', type: 'number'},
    {key: 'one_entry_per_expiry', label: 'One Entry Per Expiry', type: 'boolean'},
  ],
  profiles: {
    CONSERVATIVE: {summary: {rps_threshold: 101, rps_iv_score_threshold: 102, rps_tqs_threshold: 103, one_entry_per_expiry: false}, parameters: {}},
    NEUTRAL: {summary: {rps_threshold: 201, rps_iv_score_threshold: 202, rps_tqs_threshold: 203, one_entry_per_expiry: false}, parameters: {}},
    AGGRESSIVE: {summary: {rps_threshold: 301, rps_iv_score_threshold: 302, rps_tqs_threshold: 303, one_entry_per_expiry: false}, parameters: {}},
  },
}

function Harness({onChange = () => undefined}: {onChange?: (profile: string) => void}) {
  const [value, setValue] = useState(catalogue.default_profile)
  return <RiskProfileSelector catalogue={catalogue} value={value} onChange={(profile) => {setValue(profile); onChange(profile)}} />
}

test('shows the three profiles with the default selected and its read-only summary', () => {
  render(<Harness />)
  const group = screen.getByRole('radiogroup', {name: 'Risk profile'})
  expect(within(group).getAllByRole('radio').map((item) => item.textContent)).toEqual(['Conservative', 'Neutral', 'Aggressive'])
  expect(screen.getByRole('radio', {name: 'Neutral'})).toHaveAttribute('aria-checked', 'true')
  const summary = screen.getByLabelText('Risk profile summary')
  expect(within(summary).getByText('RPS Entry Floor').nextSibling).toHaveTextContent('201')
  expect(within(summary).getByText('One Entry Per Expiry').nextSibling).toHaveTextContent('No')
})

test('switching profiles updates the summary immediately', async () => {
  const changed = vi.fn()
  render(<Harness onChange={changed} />)
  await userEvent.click(screen.getByRole('radio', {name: 'Aggressive'}))
  const summary = screen.getByLabelText('Risk profile summary')
  expect(within(summary).getByText('RPS IV Score Threshold').nextSibling).toHaveTextContent('302')
  expect(changed).toHaveBeenCalledWith('AGGRESSIVE')
  await userEvent.click(screen.getByRole('radio', {name: 'Conservative'}))
  expect(within(summary).getByText('RPS TQS Threshold').nextSibling).toHaveTextContent('103')
})

test('renders no editable parameter fields', () => {
  render(<Harness />)
  expect(screen.queryAllByRole('textbox')).toHaveLength(0)
  expect(screen.queryAllByRole('spinbutton')).toHaveLength(0)
  expect(screen.queryAllByRole('combobox')).toHaveLength(0)
})

test('the pages send only risk_profile in profile mode and keep the schema form otherwise', () => {
  expect(createPage).toContain('...(catalogue ? {risk_profile: selectedProfile} : {parameters: draft.parameters})')
  expect(createPage).toContain('<SchemaParameterForm')
  expect(editPanel).toContain("risk_profile: riskProfile")
  expect(editPanel).toContain('Strategy parameters')
})
