import {render, screen} from '@testing-library/react'
import {MemoryRouter} from 'react-router-dom'

import type {StrategyInstance} from '../src/api/types'
import {OptionExecutionPanel} from '../src/features/strategies/StrategyDetailPage'


test('renders the audited automatic option structure and exact IBKR legs', () => {
  const strategy = {
    option_execution: {
      model_version: 'rps-v2-daily-atr075',
      selection_provider: 'IBKR',
      status: 'ENTRY_PENDING',
      selected_strategy: 'IRON_CONDOR',
      entry_score: '78.25',
      entry_score_scale: '100',
      entry_score_weights: {distance_sr: '18', oi_structure: '17'},
      entry_score_components: {distance_sr: '100', oi_structure: null},
      observed_at: '2026-09-01T09:30:00Z',
      expiration: '2026-09-24',
      atr_reference: '22000',
      atr_value: '1000',
      atr_multiplier: '0.75',
      atr_support: '21250',
      atr_resistance: '22750',
      oi_support: '21400',
      oi_resistance: '22600',
      spot: '22000',
      candidate_rankings: [],
      legs: [{
        instrument_id: 71,
        symbol: 'NIFTY26SEP21300PE',
        conid: 930001,
        role: 'long_put',
        side: 'BUY',
        quantity: '1',
        right: 'P',
        strike: '21300',
        expiration: '2026-09-24',
        multiplier: '50',
        dhan_oi: 5000,
      }],
    },
  } as unknown as StrategyInstance

  render(<MemoryRouter><OptionExecutionPanel strategy={strategy} /></MemoryRouter>)

  expect(screen.getByText('Automatic option execution')).toBeInTheDocument()
  expect(screen.getByText('IRON CONDOR')).toBeInTheDocument()
  expect(screen.getByText('78.25 / 100')).toBeInTheDocument()
  // The boundary is the prior completed *daily* close, not a weekly one, and
  // the multiplier is read from the payload rather than written into the copy.
  expect(screen.getByText('Prior completed daily close ± 0.75 × Wilder ATR(14)')).toBeInTheDocument()
  expect(screen.getByText(/IBKR conId 930001/)).toHaveTextContent('OI 5000')
  expect(screen.getByLabelText('Selected option legs')).toHaveTextContent('long put')
  // A factor with no reading is reported as unmeasurable; scoring it zero
  // would quietly dock every candidate the same amount.
  expect(screen.getByText('RPS factors')).toBeInTheDocument()
  expect(screen.getByText('not measurable')).toBeInTheDocument()
})


test('shows which gate refused the entry and on what number', () => {
  const strategy = {
    option_execution: {
      model_version: 'rps-v2-daily-atr075',
      status: 'FLAT',
      selected_strategy: null,
      entry_score: '66.36',
      entry_score_scale: '100',
      entry_score_weights: {distance_sr: '18'},
      entry_signal: {
        entry_allowed: false,
        score: '40.99',
        iv_rank: '10.26',
        iv_percentile: '21.43',
        iv_rank_source: 'RPS_WARMUP',
        reasons: ['IV score 40.99 below 60.00'],
      },
      selection_evidence: {
        reason: 'No structure passed the approved range/IV/breach entry gate',
        unavailable_inputs: ['event_risk'],
        rankings: [
          {strategy_key: 'IRON_CONDOR', eligible: false, rejection_reason: 'IV score 40.99 below 60.00', pop: '0.72'},
          {strategy_key: 'PUT_CREDIT_SPREAD', eligible: false, rejection_reason: 'IV score 40.99 below 60.00'},
        ],
      },
      atr_multiplier: '0.75',
      candidate_rankings: [],
      legs: [],
    },
  } as unknown as StrategyInstance

  render(<MemoryRouter><OptionExecutionPanel strategy={strategy} /></MemoryRouter>)

  expect(screen.getByText('Entry gate')).toBeInTheDocument()
  expect(screen.getAllByText('IV score 40.99 below 60.00').length).toBeGreaterThan(0)
  expect(screen.getByText('Candidate structures')).toBeInTheDocument()
  expect(screen.getByText('IRON CONDOR')).toBeInTheDocument()
  expect(screen.getByText(/event_risk/)).toBeInTheDocument()
})


test('explains why nothing is trading: the refusing gate, both scores and their floors', () => {
  const blocked = (key: string, breach: number) => ({
    strategy_key: key, eligible: false,
    rejection_reason: 'Approved range/IV/breach entry blocked: IV score 42.23 below 60.00',
    entry_signal: {
      entry_allowed: false, iv_score: 42.231482, iv_rank: 10.26, iv_percentile: 21.43,
      iv_rank_source: 'RPS_WARMUP', either_breach_probability: breach,
      reasons: ['IV history 0/20 (informational; not a blocking gate)', 'IV score 42.23 below 60.00'],
    },
  })
  const evidence = {rankings: [blocked('CALL_CREDIT_SPREAD', 0.459), blocked('PUT_CREDIT_SPREAD', 0.353)]}
  const strategy = {
    attributed_quantity: 0,
    parameters: {rps_threshold: 1, rps_iv_score_threshold: 60},
    option_execution: {
      model_version: null, status: 'FLAT', selected_strategy: null,
      entry_score: '71.5', entry_score_scale: '100', entry_score_weights: {},
      entry_threshold: 1, iv_score_threshold: 60,
      last_entry_check: {
        status: 'NO_ELIGIBLE_STRUCTURE',
        reason: 'No structure passed the approved range/IV/breach entry gate or the causal bounded-loss, POP and EV checks',
        evaluated_at: '2026-09-24T05:42:00Z',
        entry_score: '71.5', entry_threshold: '1', iv_score_threshold: '60', spot: 23223.85,
        selection_evidence: evidence,
      },
      // The detail endpoint mirrors the latest check's evidence here.
      selection_evidence: evidence,
      atr_multiplier: '0.75', candidate_rankings: [], legs: [],
    },
  } as unknown as StrategyInstance

  render(<MemoryRouter><OptionExecutionPanel strategy={strategy} /></MemoryRouter>)

  const decision = screen.getByLabelText('Latest entry decision')
  expect(decision).toHaveTextContent('No trade: refused at “IV score and breach-probability gate”')
  // The reason every candidate shares is the holdup; the informational
  // history note is not a blocker and is not shown as one.
  expect(decision).toHaveTextContent('IV score 42.23 below 60.00')
  expect(decision).not.toHaveTextContent('informational')
  expect(decision).toHaveTextContent('71.5 / floor 1')
  expect(decision).toHaveTextContent('42.23 / floor 60')
  expect(screen.getByText('BELOW FLOOR')).toBeInTheDocument()
  const gates = screen.getByLabelText('Entry gates in evaluation order')
  expect(gates.querySelectorAll('.entry-gate.passed')).toHaveLength(4)
  expect(gates.querySelector('.entry-gate.refused')).toHaveTextContent('IV score and breach-probability gate')
  expect(decision.querySelector('code')).toHaveTextContent('rps_iv_score_threshold')
  expect(screen.getByText('45.9%')).toBeInTheDocument()
})


test('an earlier refusal marks the RPS and IV gates as not reached', () => {
  const strategy = {
    attributed_quantity: 0,
    parameters: {},
    option_execution: {
      model_version: null, status: 'FLAT', selected_strategy: null, entry_score: null,
      entry_score_scale: '100', entry_score_weights: {}, entry_threshold: 60, iv_score_threshold: 60,
      last_entry_check: {status: 'ENTRY_WINDOW_CLOSED', reason: 'RPS entry window has not opened', evaluated_at: '2026-09-24T03:40:00Z'},
      atr_multiplier: '0.75', candidate_rankings: [], legs: [],
    },
  } as unknown as StrategyInstance

  render(<MemoryRouter><OptionExecutionPanel strategy={strategy} /></MemoryRouter>)

  expect(screen.getByLabelText('Latest entry decision')).toHaveTextContent('RPS entry window has not opened')
  expect(screen.getAllByText('Not reached: an earlier gate refused')).toHaveLength(2)
})


test('renders the pre-entry option state without pretending boundaries exist', () => {
  const strategy = {
    option_execution: {
      model_version: null,
      status: 'WAITING_FOR_OPTION_QUOTES',
      readiness_reason: 'Selected contracts are waiting for fresh executable IBKR quotes',
      selected_strategy: null,
      entry_score: null,
      entry_score_scale: '100',
      entry_score_weights: {atr_range: '50', oi_structure: '50'},
      observed_at: null,
      expiration: null,
      atr_reference: null,
      atr_value: null,
      atr_multiplier: '0.75',
      atr_support: null,
      atr_resistance: null,
      oi_support: null,
      oi_resistance: null,
      spot: null,
      candidate_rankings: [],
      legs: [],
    },
  } as unknown as StrategyInstance

  render(<MemoryRouter><OptionExecutionPanel strategy={strategy} /></MemoryRouter>)

  expect(screen.getByText('Waiting for entry')).toBeInTheDocument()
  expect(screen.getByText('Selected contracts are waiting for fresh executable IBKR quotes')).toBeInTheDocument()
  expect(screen.getByText('No contracts selected yet')).toBeInTheDocument()
  expect(screen.getByText('Not selected')).toBeInTheDocument()
})
