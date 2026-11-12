# Finflock terminal frontend

The React/TypeScript operator application is a dark, information-dense trading terminal organized around operator workflows. It displays only Backend API data: the browser never connects directly to IBKR/TWS and does not synthesize market or portfolio values.

## Routes

- `/dashboard` — selected account and portfolio summary, real NAV/P&L and exposure charts, holdings, open orders, activity, and attention items.
- `/strategies` — filterable strategy inventory with safe enable, pause, flatten, and delete controls.
- `/strategies/new` — schema-driven strategy wizard; execution mode is derived from the selected portfolio's assigned Paper or Live Gateway session.
- `/strategies/:id` — strategy overview, price/indicator chart, execution activity, configuration, versions, and inputs.
- `/portfolio-builder` — goal construction, instrument qualification, strategy assignment, combined preview, and guarded apply workflow.
- `/portfolio` — holdings, cash, allocation, concentration, drift, and advanced flow/rebalance/optimization tools.
- `/activity` — order blotter, responsive order inspector, executions, operational activity, and advanced manual ticket.
- `/ibkr-sessions` — concurrent paper/live Gateway provisioning, login/2FA status, account selection, noVNC, reconnect, credentials, and deletion.
- `/system` — aggregate gateway, streaming, reconciliation, provider status, risk/kill switch, and audit controls.

All feature routes are lazy-loaded while the terminal shell remains mounted.

## Terminal shell and preferences

The desktop sidebar has expanded and compact modes. Below the mobile breakpoint it becomes an independent navigation drawer, so opening the drawer never changes the saved desktop mode. The top bar preserves global account and portfolio selection, health/freshness status, and the real-data ticker.

Workspace preferences are persisted by `src/stores/workspacePreferences.ts` under `finflock-workspace-v1`. They include sidebar mode, density, panel collapse state, right-rail preference, and per-chart controls. Mobile-drawer and fullscreen state are deliberately session-only. Invalid persisted values fall back to safe defaults.

`src/stores/preferences.ts` persists only selected session/account/default-portfolio IDs, while `src/stores/useSelection.ts` derives valid choices from server state. IBKR passwords remain component state and are cleared after successful submission. TanStack Query remains the owner of every server value, polling lifecycle, stale/error state, retry, and mutation invalidation; Zustand is not used as a server-data cache.

## Panels and charts

`TerminalPanel` is the single normal information-panel primitive. A route-scoped `id` gives each panel persisted collapse state. Collapsing uses the HTML `hidden` state rather than unmounting content, preserving form and query component state. Panels expose accessible keyboard toggles and can opt into fullscreen mode, which exits with Escape.

`TerminalChart` keeps one Lightweight Charts instance for its lifetime and updates retained series as API data changes. Depending on available data it supports candlesticks, line/area views, volume, indicators, execution markers, crosshair legends, percentage scale, ranges, intervals, fit/reset, auto-scroll, and fullscreen. Chart input is sorted, deduplicated, bounded, and validated; empty or invalid API data produces an explicit empty state rather than a fabricated series.

Theme and chart colors come from the semantic variables in `src/styles/tokens.css`.

## Safety and data flow

Strategies still emit signals and targets. Executable Paper and Live actions continue through allocation, sizing, pre-trade risk, OMS, the matching Gateway, append-only ledgers, and reconciliation. Preview is explicitly non-executing and creates no order intents or broker commands. Existing confirmation, reason, eligibility, Live safety-gate, kill-switch, and idempotency controls remain enforced.

The mode integration consumes the Backend's session-derived Paper/Live contracts and explicit preview run type. Existing chart endpoints may accept optional `range` and `interval` query parameters; omitting them preserves the established response and query-cache contracts.

## Development

Use Node.js and run from this directory:

```bash
npm install
npm run dev
npm test
npm run build
```

`npm test` runs Vitest and React Testing Library coverage for routes, workflows, responsive shell state, persisted panels, chart normalization, and safety controls. `npm run build` runs TypeScript project compilation before creating the Vite production bundle.

The production Docker build defaults to the normalized Vite base `/ai_trading_engine_frontend/`; React Router reads Vite's resulting `BASE_URL`, so router and asset paths cannot diverge. A real process-level `VITE_APP_BASE_PATH` build override is supported for non-QFS builds. Local Vite uses the relative `/api/v1` client path and proxies it to `http://localhost:8000`.

Run `docker build -t trading-engine-frontend .` from this directory. The image builds from `Frontend` alone and contains no `.env`. At container start, the validated single-line HTTP(S) `BACKEND_API_URL` generates uncached `runtime-config.js`; production uses `https://qfsplatform.com/ai_trading_engine_backend/api/v1`. Values containing whitespace, line breaks, quotes, or other unsafe JavaScript characters stop container startup.

Nginx listens on `${PORT:-5173}`, permanently redirects the exact `/ai_trading_engine_frontend` path to `/ai_trading_engine_frontend/`, and serves assets, lazy chunks, health, runtime configuration, and SPA deep links whether QFS preserves the prefix or strips it. It does not proxy an API path or use Docker DNS. Managed noVNC links always come from the Backend session API.

The Frontend and Backend are the only public QFS applications. The Gateway source directory builds a private child image and supplies no Frontend deployment variables.
