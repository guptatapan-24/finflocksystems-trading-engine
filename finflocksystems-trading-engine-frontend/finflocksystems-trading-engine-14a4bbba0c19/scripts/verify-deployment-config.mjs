import {readFileSync} from 'node:fs'

const dockerfile = readFileSync('Dockerfile', 'utf8')
const nginx = readFileSync('nginx.conf.template', 'utf8')
const entrypoint = readFileSync('docker-entrypoint.d/40-runtime-config.sh', 'utf8')
const environmentExample = readFileSync('.env.example', 'utf8')
const viteConfig = readFileSync('vite.config.ts', 'utf8')
const dockerignore = readFileSync('.dockerignore', 'utf8')

if (dockerfile.includes('COPY .env.example') || dockerfile.includes('COPY .env ')) {
  throw new Error('Frontend image must not contain an environment file')
}
if (!dockerignore.split(/\r?\n/).includes('.env.example')) {
  throw new Error('Frontend example configuration must be excluded from the build context')
}
const obsoleteApiVariable = 'VITE_API_' + 'BASE_URL'
if ((dockerfile + environmentExample + viteConfig).includes(obsoleteApiVariable)) {
  throw new Error('Frontend must use runtime BACKEND_API_URL and the relative development API path')
}
if (dockerfile.includes('http://backend:8000') || environmentExample.includes('http://backend:8000')) {
  throw new Error('Frontend production configuration uses Docker DNS')
}
if (!nginx.includes('location = /qfsplatform/trading_engine_frontend { return 301 /qfsplatform/trading_engine_frontend/; }')) {
  throw new Error('Exact Frontend base-path redirect is missing')
}
if (!nginx.includes('absolute_redirect off;')) {
  throw new Error('Frontend redirects must not expose the container port')
}
if (!nginx.includes('^/qfsplatform/trading_engine_frontend/(.*)$') || !nginx.includes('location / { try_files')) {
  throw new Error('Prefix-preserved and prefix-stripped SPA fallbacks are required')
}
if (!nginx.includes('Cache-Control "no-store"')) {
  throw new Error('runtime-config.js must not be cached')
}
if (!entrypoint.includes("^https?://") || !entrypoint.includes('single-line HTTP or HTTPS URL')) {
  throw new Error('Runtime Backend URL validation is missing')
}
if (!environmentExample.includes('BACKEND_API_URL=https://finflockqfsplatform.cloud/qfsplatform/ai_trading_engine_backend1/api/v1')) {
  throw new Error('Frontend QFS Backend URL is missing')
}
if (!viteConfig.includes('process.env.VITE_APP_BASE_PATH')) {
  throw new Error('Vite base override does not read the build process environment')
}
if (!viteConfig.includes("'/api/v1'") || !viteConfig.includes("target: 'http://localhost:8000'")) {
  throw new Error('Local Vite API proxy is missing')
}
if (/PUBLIC_BASE_URL|GATEWAY_SERVICE_TOKEN|IBKR_GATEWAY_IMAGE/.test(environmentExample + dockerfile)) {
  throw new Error('Frontend contains Backend/Gateway deployment variables')
}
