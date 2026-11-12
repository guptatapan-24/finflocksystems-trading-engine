import {render, screen} from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import {MemoryRouter} from 'react-router-dom'
import {EmptyState, ErrorState, Freshness, Skeleton, TerminalPanel} from '../src/components/ui'
import {useWorkspacePreferences} from '../src/stores/workspacePreferences'

test('renders loading empty stale error and collapsed advanced states', async () => {
  useWorkspacePreferences.getState().resetWorkspace()
  const user = userEvent.setup()
  render(<MemoryRouter initialEntries={['/states']}><Skeleton /><EmptyState title="No holdings" /><Freshness updatedAt={Date.now()} stale /><ErrorState error={new Error('Partial failure')} /><TerminalPanel id="advanced" title="Advanced controls" defaultOpen={false}><label>Retained value<input aria-label="Retained value" /></label></TerminalPanel></MemoryRouter>)
  expect(screen.getByLabelText('Loading')).toBeInTheDocument()
  expect(screen.getByText('No holdings')).toBeInTheDocument()
  expect(screen.getByText(/Stale/)).toBeInTheDocument()
  expect(screen.getByText('Partial failure')).toBeInTheDocument()
  const toggle = screen.getByRole('button', {name: /Advanced controls/})
  expect(toggle).toHaveAttribute('aria-expanded', 'false')
  const retained = screen.getByLabelText('Retained value')
  expect(retained.closest('.panel-content')).toHaveAttribute('hidden')
  toggle.focus()
  await user.keyboard('{Enter}')
  expect(toggle).toHaveAttribute('aria-expanded', 'true')
  await user.type(retained, 'operator context')
  await user.click(toggle)
  expect(retained.closest('.panel-content')).toHaveAttribute('hidden')
  expect(retained).toHaveValue('operator context')
  toggle.focus()
  await user.keyboard('{Enter}')
  expect(retained).toHaveValue('operator context')
  expect(localStorage.getItem('finflock-workspace-v1')).toContain('/states:advanced')
})
