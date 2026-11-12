# Intelligence layer: frontend integration notes (Phase 0, discovery only)

Companion to the backend's `docs/intelligence/NAME_MAP.md` and `DESIGN.md` (branch `Ai-backend`). The backend `CLAUDE.md` governs both
repos. This file records what the React app already provides and where the intelligence UI will attach. **No frontend code has been changed in Phase 0.**
Branch: `Ai-frontend` (HEAD 6312473). Node 22.15 / npm 10.9 locally.

## 1. What already exists and will be reused

| Need | Existing piece | File |
|---|---|---|
| API envelope `{ok,data,error,meta}`, `ApiError(code,status,details)` | `request<T>()`; throws on `!ok`; sends cookies (`credentials: include`) and `X-CSRFToken` on non-GET when the `csrftoken` cookie exists | src/api/client.ts |
| Idempotency-Key on state changes | `mutationOptions(method, payload, idempotent=true, key?)` | src/api/client.ts:122 |
| Query keys / polling | central `queries.*` object built with `queryOptions` (15 s default poll, 5 s for P&L) | src/api/queries.ts |
| Types | single `src/api/types.ts` (1,424 lines); `SystemStatus.is_admin` already present | src/api/types.ts:25 |
| Staff gating | `GET /system/` returns `is_admin` (user is_staff); also sets the CSRF cookie | backend apps/core/views.py:95 |
| Routes | lazy routes in one file; sidebar groups `Trading` / `Operations` | src/routes/AppRoutes.tsx, src/layouts/AppShell.tsx |
| Strategy page tabs | `Overview, Ledger, Chart, Activity, Configuration, Advanced` driven by `?tab=` | src/features/strategies/StrategyDetailPage.tsx:15 |
| UI kit | `TerminalPanel`, `DataTable`, `StatusBadge`, `DetailDrawer`, `ConfirmActionDialog`, `EmptyState`, `ErrorState`, `Freshness`, `Skeleton`, formatters | src/components/ui |
| Charts | Lightweight Charts (`TerminalChart`) and Plotly | src/components/charts |
| Tests | Vitest + Testing Library + jsdom, `tests/setup.ts` | tests/ |

## 2. Planned attachment points (built in the matching backend phase, not before)

| Backend phase | Frontend work | Where |
|---|---|---|
| 1 | `queries.intelligenceHealth`, types for health; hidden unless `is_admin` | api/ only, no screen |
| 2 | **Episodes** list + detail drawer (P&L per currency, flags, MAE/MFE, fills) | new `src/features/intelligence/`, route `/intelligence/episodes`, new nav item under `Operations`; Strategy detail gets an `Episodes` tab |
| 3 | Review panel on an episode (verdict, process grade, mistakes with evidence refs, suggestions with Accept / Reject using `ConfirmActionDialog`, always with an Idempotency-Key; `409 INSTANCE_NOT_SAFE_TO_CHANGE` shown as an explanation, not an error toast) | same feature folder |
| 4 | Live view of OPEN episodes (poll with `since` cursor / ETag), alerts list, report page with Markdown/JSON toggle | `/intelligence/live`, `/intelligence/episodes/:id/report` |
| 5 | Learning feed, lessons (status chips, n and CI always visible), performance and calibration tables | `/intelligence/learning` |
| 6 | Advisory banner on strategy create/edit/enable (additive `advisory` field; never blocks) | `CreateStrategyPage`, `EditStrategyPanel`, enable action |

## 3. UI rules that follow from CLAUDE.md

* Show sample size `n` next to every statistic; never render a bare win-rate or mean without `n` and the interval.
* Never add or average money across currencies: group by `currency` or show unitless measures (bps, return on risk).
* Label provenance: PAPER, DELAYED data, `is_fallback` reviews, LLM *commentary* vs deterministic facts, estimated option path.
* Suggestions are PROPOSED advice: the Accept button states what will happen ("creates a new version, does not activate"); disabled with the reason when the instance is enabled or in a position.
* Operator free text (`intent_note`) is plain text only, length-capped client side too; render all LLM/backend text as text, never `dangerouslySetInnerHTML`.
* Intelligence screens are staff-only and must fail soft: a failing intelligence call never blocks the trading pages (own error boundary / `ErrorState compact`).

## 4. Baseline (before any change)

* `npm ci` ok; `npx tsc -b` clean.
* `npx vitest run`: **80 passed, 12 failed** (2 test files: `tests/app.test.tsx` strategy deletion / risk requests, instrument-search and broker-session tests). Failures exist on the unmodified branch; not investigated in Phase 0 and not related to the intelligence work.
* `tsc -b` rewrites the tracked build artefacts `vite.config.js` / `vite.config.d.ts`; I restored them with `git checkout`. Avoid committing those two files by accident.

## 5. Open points for the user

1. Should the UI live under a new top-level `/intelligence/*` area (recommended) or only as tabs inside Strategies?
2. Does the operator log in as a staff user in this app today (there is no login screen in `src/`; `POST /auth/session/` exists in the backend)? Phase 2 needs a staff session to read the staff-only API; if there is no login UI, I would add a minimal one.
