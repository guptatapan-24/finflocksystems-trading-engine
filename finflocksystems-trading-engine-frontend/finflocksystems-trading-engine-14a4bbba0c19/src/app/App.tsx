import {QueryClientProvider} from '@tanstack/react-query'
import {BrowserRouter} from 'react-router-dom'
import {queryClient} from './queryClient'
import {AppRoutes} from '../routes/AppRoutes'

export function normalizeBasename(configured: string) {
  const normalized = configured.startsWith('/') ? configured : `/${configured}`
  return normalized === '/' ? '/' : normalized.replace(/\/$/, '')
}

export function appBasename() {
  if (import.meta.env.MODE === 'test') return '/'
  return normalizeBasename(import.meta.env.BASE_URL || '/')
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter basename={appBasename()}>
        <AppRoutes />
      </BrowserRouter>
    </QueryClientProvider>
  )
}
