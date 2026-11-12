import {readFileSync, readdirSync} from 'node:fs'
import {join} from 'node:path'

const base = '/qfsplatform/trading_engine_frontend/'
const index = readFileSync('dist/index.html', 'utf8')
const runtime = readFileSync('dist/runtime-config.js', 'utf8')

if (!index.includes(`${base}runtime-config.js`)) throw new Error('runtime config is not below the production base path')
if (!index.includes(`${base}assets/`)) throw new Error('entry assets are not below the production base path')
if (/\b(?:src|href)="\/assets\//.test(index)) throw new Error('an entry asset uses the domain root')
if (!runtime.includes('https://finflockqfsplatform.cloud/qfsplatform/ai_trading_engine_backend1/api/v1')) {
  throw new Error('runtime config does not contain the QFS backend default')
}
if (!readdirSync(join('dist', 'assets')).some((name) => name.endsWith('.js'))) {
  throw new Error('production build contains no JavaScript chunks')
}

console.log('production base path and runtime API configuration verified')
