import type {ReactNode} from 'react'
import {ThemeProvider, createTheme} from '@mui/material/styles'
import type {SxProps, Theme} from '@mui/material/styles'

/** MUI theme mapped onto the terminal's design tokens (see styles/tokens.css),
 * so MUI surfaces read as part of the same dark terminal. */
export const terminalMuiTheme = createTheme({
  palette: {
    mode: 'dark',
    primary: {main: '#4f8cff'},
    success: {main: '#28b781'},
    error: {main: '#ef5f67'},
    warning: {main: '#d9a441'},
    info: {main: '#49a8c7'},
    background: {default: '#0b0f16', paper: '#121822'},
    text: {primary: '#d7dee9', secondary: '#9aa7b8'},
    divider: '#263244',
  },
  typography: {
    fontFamily: 'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
    fontSize: 13,
  },
  shape: {borderRadius: 6},
})

export function TerminalMuiProvider({children}: {children: ReactNode}) {
  return <ThemeProvider theme={terminalMuiTheme}>{children}</ThemeProvider>
}

/** Shared DataGrid styling: readable 13px body, tabular numerals, wrapped text. */
export const terminalGridSx: SxProps<Theme> = {
  border: '1px solid var(--border)',
  borderRadius: 'var(--radius)',
  bgcolor: 'var(--surface)',
  color: 'var(--text)',
  fontSize: 13,
  '--DataGrid-containerBackground': 'var(--surface-raised)',
  '--DataGrid-rowBorderColor': 'var(--border)',
  '& .MuiDataGrid-columnHeaders': {borderBottom: '1px solid var(--border-strong)'},
  '& .MuiDataGrid-columnHeaderTitle': {
    fontWeight: 650, fontSize: 11.5, letterSpacing: '.04em', textTransform: 'uppercase', color: 'var(--text-soft)',
  },
  '& .MuiDataGrid-cell': {
    display: 'flex', alignItems: 'center', py: '8px', lineHeight: 1.45,
    fontVariantNumeric: 'tabular-nums', borderColor: 'var(--border)',
  },
  '& .MuiDataGrid-cell:focus, & .MuiDataGrid-cell:focus-within, & .MuiDataGrid-columnHeader:focus': {outline: 'none'},
  '& .MuiDataGrid-row:hover': {bgcolor: 'var(--surface-hover)', cursor: 'pointer'},
  '& .MuiDataGrid-footerContainer': {borderTop: '1px solid var(--border)'},
  '& .MuiDataGrid-toolbar': {borderBottom: '1px solid var(--border)', minHeight: 44},
  '& .grid-row-entered': {boxShadow: 'inset 3px 0 0 var(--positive)', bgcolor: 'var(--positive-soft)'},
  '& .grid-row-exited': {boxShadow: 'inset 3px 0 0 var(--critical)', bgcolor: 'var(--critical-soft)'},
  '& .grid-row-error': {boxShadow: 'inset 3px 0 0 var(--warning)'},
}
