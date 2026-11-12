import { loadEnv } from 'vite'
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

function normalizeBase(value: string) {
  const leading = value.startsWith('/') ? value : `/${value}`
  return `${leading.replace(/\/+$/, '')}/`
}

export default defineConfig(({mode}) => {
  const env = loadEnv(mode, process.cwd(), '')
  const defaultBase = mode === 'production' ? '/qfsplatform/trading_engine_frontend/' : '/'
  const configuredBase = process.env.VITE_APP_BASE_PATH || env.VITE_APP_BASE_PATH || defaultBase
  const base = mode === 'test' ? '/' : normalizeBase(configuredBase)
  return {
    base,
    plugins: [react()],
    server: {
      proxy: {
        '/api/v1': {
          target: 'http://localhost:8000',
          changeOrigin: true,
        },
      },
    },
    test: {environment: 'jsdom', globals: true, setupFiles: './tests/setup.ts'},
  }
})
