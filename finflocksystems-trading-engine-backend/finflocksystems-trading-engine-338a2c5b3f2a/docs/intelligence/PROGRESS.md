# Intelligence layer: progress

Branch: `feature/intelligence-layer` (from `Ai-backend` b0e5f1e). Python for all work: **3.10** (see NAME_MAP M3).

| Phase | Status | Notes |
|---|---|---|
| 0 Discovery and name map | **Done, approved by delegation ("you do the best")** | NAME_MAP.md, DESIGN.md, verify script + test |
| 1 Skeleton, LLM client, infra tables | **Done** | see Session 2 |
| 2 Episodes and deterministic metrics | **Done, enable on PAPER only after the human check** | see Session 3 |
| 3 LLM review, mistakes, suggestions | **Done, reviews off by default** | see Session 4 |
| 4 Capture, live monitor, report | **Done, enabled in .env / .env.example** | see Session 5 |
| 5 Learning loop | **Done** | see Session 6 |
| 6 Advisory, weekly, counterfactuals, evals | **Done** | see Session 7 |

## Session 1: Phase 0 report

### 1. What changed
New files only (no engine file edited, no model, no migration):
* `docs/intelligence/NAME_MAP.md`, `DESIGN.md`, `PROGRESS.md`
* `scripts/verify_intel_names.py` (37 models, 28 functions, 8 settings)
* `tests/intelligence/__init__.py`, `tests/intelligence/test_name_map.py`
* Local only, git-ignored: `.venv` (Python 3.10.11), `.venv/Lib/site-packages/sitecustomize.py` (adds `enum.StrEnum`).

Engine files touched: **none**.

### 2. Tests added
`tests/intelligence/test_name_map.py`: 2 tests, passing. Baseline of the whole suite on Python 3.10 / Windows:
**657 passed, 3 skipped, 18 failed** (all 18: `Byte count mismatch for gics_taxonomy.json`, research bundle hashes vs CRLF checkout; unrelated).

### 3. Assumptions in CLAUDE.md that turned out wrong
See NAME_MAP.md MISMATCHES M1-M14. The ones that change the design: M1 (models live in `allocation`), M3 (Python), M5 (no Django admin),
M6 (no idempotency decorator), M7 (`average_cost` unreliable), M9 (all existing views are CSRF-exempt, CSRF cookie comes from `GET /system/`), M10/M11 (no Fill->strategy FK, no order->snapshot FK).

### 4. Risks found
DESIGN.md section 9 (12 items). The three that matter most: the engine does not run on stock Python 3.10; the baked-in `CLEANUP_DATABASE_ON_START=true` flush wipes
intel_ data and staff users after Kafka/Flink were already reset; the strategy PATCH endpoint is unauthenticated and unguarded (pre-existing).

### 5. Next
Answer the seven questions in DESIGN.md section 10, approve NAME_MAP.md, then say "Start Phase 1". Phase 1 starts with a short plan for your OK (per section 8).

## Session 2: Phase 1 report (branch `feature/intelligence-layer`, created from `Ai-backend`)

Decisions taken on the user's delegation ("you do the best, without breaking the existing structure"): keep Python 3.10 local setup (no repo change for it); strict flush guard (any intel_ row blocks a flush);
add `intel_cursor`; entrypoint pre-check added; Gemini ids left to env; work on `feature/intelligence-layer` in both repos.

### 1. What changed
New app `apps/intelligence/`: `models.py` (PromptTemplate, LlmRunLog, IntelCursor + migration 0001), `llm/` (base, gemini, fake, circuit, runner), `guard.py`,
management commands `flush` (override) and `check_intelligence_flush_guard`, `views.py` + `urls.py` (`GET /api/v1/intelligence/health/`, staff-only, not CSRF-exempt), `conf.py`, `api_utils.py`, empty `analyzers/` and `services/`.

Engine files touched (all additive, none alters existing behaviour):
| File | Change | Why |
|---|---|---|
| config/settings.py | `apps.intelligence` in INSTALLED_APPS; `INTELLIGENCE_TASK_ROUTES` merged into `CELERY_TASK_ROUTES`; `INTELLIGENCE_*` block appended at the end | R13(a) |
| config/urls.py | one `path("intelligence/", include(...))` | R13(a) |
| supervisord.conf | `[program:celery-intelligence]` (queue `intelligence` only, prefork 1, same flags); not a readiness role | R13(b) |
| requirements.txt | `google-genai==2.28.0` | R11 |
| .env.example | `INTELLIGENCE_*` block, flag false, no secrets | R7 |
| entrypoint.sh | 2 lines: run `check_intelligence_flush_guard` before the destructive cleanup branch; no-op while no intel_ row exists | R6 |

### 2. Tests added
`tests/intelligence/test_phase1.py`: 31 tests (constraints, idempotency, all Fake modes, budget/token cap, cost accounting, Gemini client with SDK stubbed incl. retry/timeout/malformed/circuit breaker, factory, flush guard incl. override, health auth/secrets/CSRF, settings defaults, not a readiness dependency).
Full suite on Python 3.10: **688 passed, 3 skipped, 18 failed** = the same 18 pre-existing CRLF research failures as the baseline; no regression. `verify_intel_names.py`: 0 problems.
Frontend: tsc clean; vitest 81 passed, 12 failed (same 12 pre-existing).

### 3. Assumptions that turned out wrong
* Docker daemon is not running here and the local PostgreSQL 17 needs credentials I do not have, so **"migrations apply on PostgreSQL 16" is NOT verified**. Run: `DATABASE_URL=postgresql://... python manage.py migrate` against a PG16 and `sqlmigrate intelligence 0001`. The only non-trivial construct is the conditional unique constraint (supported by PostgreSQL and SQLite).
* No stable Pro-tier Gemini model is listed today (DESIGN section 11).
* The hard-rule "flag off changes nothing" holds: with `INTELLIGENCE_ENABLED=false` nothing hooks into the engine yet; the only runtime effects are 3 empty tables, one URL, and a guard command that is a no-op while the tables are empty.

### 4. Risks found
* The strict guard turns `CLEANUP_DATABASE_ON_START=true` + existing intelligence data into a startup failure (container exits with a clear message) instead of a silent wipe. That is the intended R6 behaviour, but note that the shipped `.env.example` sets the flag true: set it false once intelligence data matters.
* Cost accounting is blind until prices are configured (health reports it).
* Circuit breaker state is per process; fine for a 1-process worker.

### 5. Next: Phase 2 (episodes and deterministic metrics)
Plan, for your OK: `TradeEpisode`, `TradeEpisodeLeg`, `EpisodeMetrics`, `EpisodeEvidenceSnapshot` models/migration; `services/episodes.py` built on `reporting.attributed_fills`; `Fill` post_save signal (enqueue on commit) + 30 s sweep using `intel_cursor` + expiry sweeper; `services/metrics.py` with the metric catalogue in DESIGN.md; `backfill_episodes`; read API; fixtures for AAPL SMA and the NIFTY iron condor; `apply_execution` benchmark.

## Session 3: Phase 2 report (episodes and deterministic metrics)

### 1. What changed
* Migration `0002_trade_episodes`: `intel_trade_episode`, `intel_trade_episode_leg`, `intel_episode_metrics`, `intel_episode_evidence_snapshot` (immutable). All engine references are soft (R12) with denormalised identity columns.
* `services/episodes.py`: episodes rebuilt idempotently from the engine's own `reporting.attributed_fills` with the same weighted-average arithmetic (`reporting._apply`), key `episode:{instance}:{version}:{first_fill_execution_id}`, stock sign-flip splitting, option multi-leg episodes, exit-reason and entry-snapshot resolution, data-quality flags.
* `services/metrics.py` (catalogue in METRICS.md), `services/evidence.py` (whitelist pack frozen at close, sanitised free text, no account/conid/execution ids).
* `apps.py`: `post_save(Fill)` handler that, only when `INTELLIGENCE_ENABLED=true`, registers `transaction.on_commit` -> `process_fill.apply_async(retry=False)`; any failure is swallowed (sweep recovers).
* `tasks.py`: `process_fill`, `sweep_fills` (30 s, cursor `intel_cursor`, also re-checks instances with OPEN episodes to catch late commission), `sweep_expired` (15 min). Beat entries are added only when the layer is enabled.
* Read API (staff only): `GET /api/v1/intelligence/episodes/` (filters, limit/offset, `meta.total`) and `episodes/<id>/`.
* `backfill_episodes --portfolio --instance --since --dry-run --force`.
* Frontend: `/intelligence/episodes` page (per-currency summary with n, list, detail drawer with legs, flags, frozen-evidence hash, metrics) + nav item.

Engine files touched in this session: `config/settings.py` (beat entries only when enabled; no behavioural change when off). Frontend: `AppRoutes.tsx`, `AppShell.tsx` (one route + one nav item) and `tests/app.test.tsx` (its hard-coded nav list gained "Intelligence").

### 2. Tests added
Backend `tests/intelligence/test_episodes.py` (29) + `test_hot_path_benchmark.py` (1): hand-computed AAPL (50 sh @200.05, commission 1: gross 250, net 248, slippage 2.5 bps), reconciliation with `strategy_pnl` (stock, flip, condor), scale-in/partial exit, sign flip, NIFTY iron condor (credit 24 x 65 = 1,560, exit pnl 845, net 829, TQS/POP/margins/flags), unlinked snapshot, duplicate replay, late commission with frozen evidence, immutable snapshot, expiry (estimated P&L 1,560, no snapshot -> wait, broker still holds -> flagged, no bar -> unknown), signal hook (enabled/disabled, broker down, crashing builder never reaches the engine), sweep cursor, manual fills, secrets/prompt-injection sanitising, backfill idempotency, API auth/pagination/filters, MAE/MFE by hand.
Backend suite on Python 3.10: **718 passed**, 3 skipped, 18 failed = exactly the baseline research CRLF tests (unrelated). Frontend: 84 passed, 12 failed (the same 12 as the baseline); 4 new tests.
`apply_execution` before/after (60 fills x2 rounds, SQLite): 8.07 ms disabled vs 7.82 ms enabled = no measurable overhead (noise).

### 3. Assumptions that turned out wrong
* The "stable Pro model" gap: `gemini-2.5-flash` is refused for new accounts, so older ids cannot be assumed; use ids verified against the key (DESIGN section 11).
* Option episodes have no explicit order -> snapshot link; the time fallback exists and is flagged `OPTION_SNAPSHOT_UNLINKED` (confirmed in code and covered by a test).
* `StrategyInstance.version` is not the version in force at the first fill after a PATCH; the key uses the attributed `StrategyVersion.version` instead.

### 4. Risks found
* Exit reason is inferred from the nearest earlier strategy run (the engine has no order -> run link); in a burst of runs it can mislabel an operator exit. `UNKNOWN` is used rather than guessing when nothing matches.
* The expiry sweeper trusts `BrokerPositionSnapshot.positions` rows having `conid` and `position`/`quantity`; with another shape it waits (never closes wrongly). Verify against a real snapshot on PAPER.
* A closing fill arriving after an episode was closed as EXPIRED is ignored by the builder (expiry-owned episodes are skipped). Logged as a known edge.
* Option episodes cannot have MAE/MFE until live snapshots exist (Phase 4); flagged, not guessed.
* Full-history rebuild per fill is O(fills of that instance): fine at current volumes, optimise (resume from the last closed episode) if an instance reaches thousands of fills.
* The Gemini key was pasted into chat. It is stored only in the git-ignored `Backend/trading-engine/.env`. Consider rotating it, because chat transcripts are not secret stores.

### 5. Human check before enabling (CLAUDE.md)
Verify 2-3 real PAPER trades by hand: P&L vs your broker statement, MAE/MFE vs a chart, exit reason. Then set `INTELLIGENCE_ENABLED=true` on PAPER only. Run `python manage.py backfill_episodes --dry-run` first on the real database.

### 6. Next: Phase 3 (LLM post-trade review, mistakes, suggestions)
Evidence-pack-to-prompt builder, rule analyzers, PromptTemplate v1, grounded runner with FakeLLM tests, suggestion accept/reject with the R1 safety checks. Needs your choice of `INTELLIGENCE_MODEL_REVIEW` (suggested `gemini-3.6-flash`, verified working) and price variables.

## Session 4: Phase 3 report (LLM post-trade review, mistakes, suggestions)

### 1. What changed
* Migration `0003_reviews_suggestions`: `intel_trade_review` (unique per episode + evidence hash + prompt version + model, R4), `intel_mistake_finding`, `intel_suggestion`, `intel_review_feedback`, and `intel_idempotent_request` (replay store for state-changing endpoints; an extra table beyond the 20 planned).
* `analyzers/rules.py`: the 14 deterministic rules from CLAUDE.md (+ `MANUAL_INTERVENTION_PRESENT`), thresholds overridable via `INTELLIGENCE_RULE_THRESHOLDS`. Findings cite existing fact keys and run before the model; they are the whole review when the model is unavailable.
* `services/facts.py`: flat, citable fact map built only from the frozen evidence snapshot (+ sample size, last 10 episodes, param-schema limits), trimmed to `INTELLIGENCE_MAX_INPUT_TOKENS`.
* `services/prompts.py`: `trade_review` v1 (system prompt from CLAUDE.md plus data-not-instructions rules), output schema within Gemini's supported subset; template row is created on first use.
* `services/grounding.py` (R3): cited keys must exist; every number in text must match a fact (0.1% / rounding tolerance; small ordinals 0-5 allowed); PARAMETER suggestions must exist in the strategy schema, match the real current value, differ from it, and pass the real `plugin.validate_configuration` (merges defaults, cross-field rules such as fast < slow). Item problems drop the item (recorded in `rejected_items`); a bad number in the verdict or summary is fatal: one repair retry, then the deterministic fallback review.
* R5: a parameter suggestion without a defect finding and with fewer than `INTELLIGENCE_MIN_EPISODES_FOR_SUGGESTION` (8) closed trades is forced to `is_hypothesis=true` whatever the model says.
* `services/reviews.py`: orchestration with idempotency, budget/circuit/timeout fallbacks (fail open, R2), AuditEvent on fallback, a fallback is replaced by a real review on the next run. Reviews are queued by `schedule_review` after the episode-close commit, only when `INTELLIGENCE_REVIEWS_ENABLED=true`.
* `services/suggestions.py` (R1): accept/acknowledge/reject. Accept on a PARAMETER suggestion runs `update_instance` (the PATCH service) in a locked transaction and only when the instance is disabled, in FLAT/DISABLED/PAUSED, has no OPEN episode, no non-zero attributed position and no active orders; otherwise 409 `INSTANCE_NOT_SAFE_TO_CHANGE` with reasons. It never enables or activates anything; stale suggestions are refused (`SUGGESTION_STALE`).
* API (staff-only, CSRF-protected, Idempotency-Key required on POST, audited): `GET episodes/<id>/review/`, `GET reviews/`, `POST episodes/<id>/review/rerun/` (DB-counter throttle, 202 queued / `UNCHANGED`), `POST reviews/<id>/feedback/`, `GET suggestions/`, `POST suggestions/<id>/accept|acknowledge|reject/`.
* Gemini client: the schema sent to Gemini has `maxItems/minItems/minimum/maximum` removed (the API answered 400 INVALID_ARGUMENT for the full nested schema); the full schema is still enforced locally.
* Frontend: `ReviewPanel` inside the episode drawer (grade, n, rule-based vs model findings, hypothesis badges, accept/acknowledge/reject with confirmation, plain-language refusal reasons, feedback).
Engine files touched this session: `config/settings.py` (3 appended settings), `.env.example` (comments + 3 keys). Nothing else.

### 2. Tests added
Backend `tests/intelligence/test_reviews.py` (38): valid review, idempotent re-run (one model call), all fake modes, hallucinated number (repair then fallback; item-level drop), unknown evidence key, invalid parameter / constraint violation (fast >= slow) / wrong current value / unknown key, forced hypothesis vs defect, prompt-injection and secrets in the prompt, analyzer thresholds and citations, token trimming, budget exhausted, fallback replaced by real, trigger only when enabled, task never raises, accept on a safe instance (new version, still disabled), enabled/open-trade refusal, reject never applies, idempotency (replay, conflict, missing key), stale, CSRF and staff checks, read API, re-review threshold, rerun throttle, feedback, trading tables untouched.
Backend suite on Python 3.10: **757 passed**, 3 skipped, 17 failed (research CRLF tests only; baseline range 17-18). Frontend: **89 passed**, 12 failed (the same 12 pre-existing), +5 new.

### 3. Real-model check (not part of the test suite)
With the project key, `gemini-3.1-flash-lite` reviewed the NIFTY iron-condor fixture: schema-valid on the first call (7.9k input tokens, 0.9k output, 8.5 s), grade C, 3 model findings next to 5 rule findings, and one PARAMETER suggestion (rps_threshold 60 -> 65) that passed the real plugin and was labelled hypothesis. Nothing was rejected. Model notes (2026-10-07): `gemini-3.6-flash` and `gemini-3.5-flash` returned 503/504 under load during this session, `gemini-3.8-flash` 503, and a burst of 10 calls hit 429 RESOURCE_EXHAUSTED: **the key has a small quota**. `gemini-1.5-*` no longer exists (404); `gemini-2.5-flash` is refused for new accounts. Use `INTELLIGENCE_MODEL_REVIEW=gemini-3.1-flash-lite` (stable, worked) and set `INTELLIGENCE_DAILY_BUDGET_USD` plus the price variables.

### 4. Assumptions that turned out wrong
* The documented JSON-Schema subset is not enough: `maxItems/minimum/maximum` caused HTTP 400 on a nested schema (handled, see above).
* A single-use key per request is not enough inside a transaction: a duplicate-key insert poisons an enclosing atomic block, so `run_idempotent` uses a savepoint.
* "1.5 model" requested: Gemini 1.5 is retired, so it cannot be used.

### 5. Risks found
* Quota/capacity: expect fallbacks (rule-based reviews) when Gemini is overloaded or the quota is spent; the next run replaces a fallback by a real review. Consider a retry beat for fallback reviews (not added, to avoid silent spend).
* Facts are trimmed by dropping groups when over budget; the trimmed group names are not yet stored on the review.
* Accepting a PARAMETER suggestion needs the instance disabled; on a running strategy the only action is acknowledge, by design (R1).
* The key was pasted in chat: rotate it.

### 6. Human check before relying on it (CLAUDE.md)
Read 5 real reviews on PAPER trades; reject (feedback "Wrong or misleading") any that cite wrong numbers. Enable with `INTELLIGENCE_REVIEWS_ENABLED=true`, `INTELLIGENCE_LLM_PROVIDER=gemini`, `INTELLIGENCE_MODEL_REVIEW=...`, `GEMINI_API_KEY` in the real environment.

### 7. Next: Phase 4 (parameter capture, live monitor, full report)
`RunConfigSnapshot` hook on activation/version creation (additive try/except engine edit), deterministic live monitor with alerts, `GET episodes/<id>/report/`.

## Session 5: Phase 4 report (parameter capture, live monitor, full report)

### 1. What changed
* **Configuration (done for you).** Local `.env` (git-ignored): layer, reviews, provider `gemini`, model `gemini-3.1-flash-lite`, budget 2 USD/day, list prices 0.25 / 1.50 USD per million tokens, key. `.env.example` (copied into the image): the same, enabled, with an empty key (set `GEMINI_API_KEY` in the deployment environment; without it you still get episodes, live monitoring, rule-based reviews and reports). `config/test_settings.py` pins every intelligence setting to a safe value so no developer or deployment setting can leak into tests or reach Gemini.
* **Startup reset no longer crashes the container.** `entrypoint.sh` now SKIPS the destructive `CLEANUP_DATABASE_ON_START` reset (with a warning) when intelligence data exists and starts normally; a direct `manage.py flush` is still refused. (Phase 1 aborted the start instead.)
* **A. Capture.** `RunConfigSnapshot` per (instance, version): data-source flags (remote feature DB, option chain provider, option pre-trade risk, delayed data), regime at activation (realised-vol percentile when at least 60 bars exist), parameter hash, activation log, `advisory_shown` (empty until Phase 6), and a sanitised `intent_note` (500 characters, control characters and delimiters removed; shown to a model only as quoted data and never a source of "allowed" numbers). Episodes link to the snapshot in force at entry, and the frozen evidence pack carries it.
  Engine hooks (additive, try/except, savepoint, no-ops while disabled): `framework._create_version` and `activate_instance` call `_intelligence_capture`; `views.instances` (create) and `views.action` (enable) pass the optional `intent_note`.
* **B. Live monitor** (`services/live.py`, beat every 20 s, queue `intelligence`): marks and P&L from stored reference prices only. Stale or missing quotes pause it and raise one deduplicated LOW alert. Snapshots are written at most once a minute, plus on a material change or a new alert. Stocks: distance to the exit rule (SMA gap, RSI vs exit threshold, Donchian lower). Options: per-leg marks, % of max profit/loss, underlying distance to each short strike, breakevens, DTE, and profit giveback from the running peak against the soft-exit level. Live IV is labelled `EVALUATION_TIME_ONLY`. Alerts are templated text with dedupe, cooldown and escalation: ADVERSE_EXCURSION, SHORT_STRIKE_PROXIMITY, SOFT_EXIT_PROXIMITY, IV_EXPANSION_AT_LAST_EVAL, QUOTE_STALENESS, HEDGE_LEG_UNFILLED, STAGE1_BLOCKED, UNEXPECTED_MANUAL_ORDER. Commentary is on demand only, grounded, number-checked, throttled, and labelled "not an alert and not advice". Old snapshots of closed trades are thinned after 30 days.
  API: `GET episodes/<id>/live/`, `GET live/open-episodes/` (ETag), `GET live/alerts/?since=<cursor>`, `POST episodes/<id>/live-commentary/`.
* **C. Report** (`services/report.py`, generic section/block structure plus a Markdown renderer): `GET episodes/<id>/report/` and `?format=markdown`. Sections: trade header, parameters with gate margins and neutral-substituted components, fact timeline (signal, sizing, risk, fills with reference prices, live extremes, alerts, exit), predicted vs actual, model commentary (labelled, with sample size and "insufficient evidence" below 8 trades), findings, strengths, suggestions with decision endpoints, what was learned (proposed lessons only until the learning loop exists), provenance.
* **Frontend:** Live trades page (open trades, deterministic numbers, sparkline, strike distances, alerts, on-demand commentary), full report page with Markdown download, an Intelligence tab bar, a link from the episode drawer, and an optional "Intent note" in the strategy wizard (sent only when filled in, so existing payloads are unchanged).
Engine files touched this session: `framework.py` (+14 lines), `views.py` (+11), `entrypoint.sh`, `config/settings.py` (beat entries, one setting), `config/test_settings.py`, `.env.example`. Frontend: `AppRoutes.tsx`, `AppShell.tsx`, `CreateStrategyPage.tsx`.

### 2. Tests added
Backend `tests/intelligence/test_phase4.py` (32): capture on version creation, change and activation (different hash per version); activation hook and version creation survive a capture crash and a database error inside an outer transaction; no-op when disabled; intent-note sanitising and the prompt-injection path; regime capture; a hand-computed option snapshot (mark P&L 252 = +260 unrealised - 8 commissions, 18.0% of max profit, breakevens 24,524 / 21,976); throttling and material-change snapshots; stale and missing quotes; all eight alert rules including dedupe and escalation; no gateway or network calls and no new leases; one failing episode does not stop the monitor; pruning; tasks; live API (ETag, cursor, auth); commentary gating, grounding, idempotency and throttle; NIFTY and AAPL reports (every section, Markdown, no secrets).
Backend suite on Python 3.10: **788 passed**, 3 skipped, 18 failed (the same research CRLF tests). With the layer ENABLED the rest of the engine suite also passes (643 passed; one timing-based heartbeat test is flaky and passes alone in both modes). Frontend: **93 passed**, 12 failed (the same 12 pre-existing); 9 new.

### 3. Assumptions that turned out wrong
* The Phase 1 "refuse to start" behaviour of the flush guard would have taken the container down on the next restart once data existed; it is now skip-and-warn.
* The plugin's own peak P&L is not guaranteed in `state_data`, so giveback uses the monitor's own running peak and shows the plugin value only when present.

### 4. Risks found
* The layer is now ON by default in `.env.example` (as requested). Until you have read a few real reviews, treat its output as unverified. Without `GEMINI_API_KEY` it degrades to rule-based output, never to an error.
* The live monitor needs fresh `InstrumentMarketState` prices; if the engine's leases lapse it shows "waiting for fresh prices" and a LOW alert, not numbers.
* Alert thresholds are first guesses (50% / 80% of max risk, 1.0% / 0.3% from a short strike, 80% of the soft giveback); tune with `INTELLIGENCE_LIVE_THRESHOLDS` after watching a paper trade.
* The Gemini key was pasted into chat: rotate it.

### 5. Human check (CLAUDE.md)
Watch one PAPER trade live and read its report. Compare the mark P&L with your broker's unrealised P&L and the strike distances with a chart.

### 6. Next: Phase 5 (learning loop, no model training)
Predicate DSL, lesson evaluator and lifecycle (HYPOTHESIS -> SUPPORTED -> ESTABLISHED, forward-only confirmation), per-currency performance and calibration stats, learning feed, `export_intelligence_dataset`.

## Session 6: Phase 5 report (learning loop, no model training)

### 1. What changed
* **Migration `0005_learning_loop`:** `intel_lesson`, `intel_lesson_evidence`, `intel_performance_stat`, `intel_calibration_stat`, `intel_learning_event`, `intel_suggestion_outcome`.
* **Predicate DSL** (`services/dsl.py`): `{"all"|"any": [{"metric","op","value"}]}`, up to 5 conditions, ops `< <= > >= == != in`, validated with jsonschema. Only EpisodeMetrics keys are accepted (exact keys plus the `entry.`, `entry.prev.` and `exit.` indicator patterns). Unknown metrics and operators are rejected, and so are money-valued metrics (R5: no cross-currency money; use bps and ratios). A missing metric makes a predicate "not applicable", never false. Duplicate detection without embeddings: identical normalised predicate, or the same metrics and operators with thresholds within 10%.
* **Lessons from reviews:** prompt template is now v3 and teaches the DSL with a worked example (the first real Gemini attempt proposed an invalid metric `tqs` and a malformed effect; the validator rejected it and recorded why; with the example the next real call produced a valid lesson). A proposal becomes a lesson only if it validates, matches the trade it came from, is not a duplicate, and the weekly cap (default 5) is not reached.
* **Evaluator on every closed episode** (`services/learning.py`): deterministic and idempotent, counts are recomputed from the evidence rows, never incremented. The trade that proposed a lesson is stored with weight 0 and never counts for it. PAPER and delayed-data trades count at half weight. Beta posterior over weighted evidence; effect size with a seeded bootstrap interval. Lifecycle: HYPOTHESIS -> SUPPORTED (n >= 8 + log2(1 + lessons under test), posterior >= 0.8, effect has the expected sign) -> ESTABLISHED (n >= 20 and >= 8 FORWARD trades, opened after promotion, with posterior >= 0.8; historical trades can never establish a lesson); REFUTED (posterior <= 0.3, or forward posterior <= 0.4 after 8 forward trades, including a previously supported or established lesson); RETIRED after 180 days without a matching trade.
* **Statistics:** `PerformanceStat` per currency and per slice (strategy, version, instrument, regime, structure, mode) with n, wins, mean bps and a 95% bootstrap interval (none when n < 2); `CalibrationStat` for the model's probability of profit (bins and Brier score). Shift events (`STAT_SHIFT`, `CALIBRATION_SHIFT`) fire only for well-sampled data (n >= 8, or n >= 20 overall and >= 10 in the bin). `SuggestionOutcome` (HELPED / HURT / INCONCLUSIVE with n and interval) after >= 8 trades on the new version.
* **Events and feed:** `LearningEvent` after every episode (created, reinforced, contradicted, promoted, retired or refuted, stat shift, calibration shift, suggestion resolved), deduplicated, cursor = id. Daily task retires stale lessons, resolves suggestion outcomes and logs the digest.
* **Prompts now see what was learned:** the evidence facts for the next review include the applicable lessons (with status and n) and the strategy's track record (n, mean, interval); the report's "what the system learned" section lists lessons created, supported or contradicted by that trade.
* **API (staff-only):** `GET learning-feed/?since=`, `GET lessons/`, `GET lessons/<id>/` (with evidence), `GET performance/`, `GET calibration/`, `GET digest/?days=&format=text`. **Export:** `manage.py export_intelligence_dataset --out DIR [--include-evidence]` writes JSONL per table plus a manifest with row counts and SHA-256 (backup and offline analysis; nothing trains on it).
* **Frontend:** Learning tab (digest, feed in plain language, lessons with status, n and interval, a lesson drawer that shows the evidence and says the origin trade does not count, per-currency performance, calibration).
Engine files touched this session: `config/settings.py` and `config/test_settings.py` (a daily beat entry and one threshold setting). Nothing else.

### 2. Tests added
Backend `tests/intelligence/test_learning.py` (43): DSL validation and normalisation, evaluation semantics, equivalence, effect parsing; lesson creation (matching, invalid, non-matching, duplicate, weekly cap, fallback); lifecycle on synthetic streams (supported only with enough agreeing trades, forward-only establishment, a supported lesson later refuted, refuted from the start, PAPER and delayed down-weighting, idempotent reprocessing, the multiple-testing guard, retirement); performance per currency with n and intervals; shift and calibration events never on tiny samples; suggestion outcomes (helped, hurt, inconclusive, waits for enough trades); prompt upgrade; end-to-end review -> lesson -> report -> next prompt; tasks; API; digest; export (checksums, no secrets); flush guard.
Backend suite on Python 3.10: **831 passed**, 3 skipped, 18 failed (the same research CRLF tests). Frontend: **97 passed**, 12 failed (the same 12 pre-existing); 4 new.

### 3. Real-model check
`gemini-3.1-flash-lite` with prompt v3 on the NIFTY condor fixture: valid review, one parameter hypothesis, and one lesson (`opt.tqs < 60 AND opt.decision_band == WATCH`) accepted into the lesson table as a HYPOTHESIS. The model chose `direction: positive` for a statement that says "avoid", i.e. it contradicted itself. The prompt now says what `direction` means, but this shows why lessons are only hypotheses: the evaluator decides from the data, and a lesson whose direction is wrong will simply collect contradicting evidence. Read new lessons before trusting them.

### 4. Assumptions that turned out wrong
* The documented DSL was not enough for the model; a worked example and an explanation of `direction` were needed (prompt v3).
* "ESTABLISHED needs forward trades" cannot be judged on history: promotion time is the clock, so a lesson that is promoted from a large back-catalogue is deliberately not established until later trades confirm it.

### 5. Risks found
* **Expectation (by design):** with roughly one entry per expiry per instance, lessons stay HYPOTHESIS or SUPPORTED for a long time. That is correct behaviour, not a bug. Performance intervals will be wide; the digest says so.
* Bootstrap intervals on 8 to 20 trades are rough guides only.
* Lesson statements come from a model and are put back into later prompts after sanitising, labelled by status and n; a poor lesson that reaches SUPPORTED by chance is possible at small n, which is why the bar rises with the number of lessons under test.
* All current trades are PAPER, so evidence counts at half weight until live trades exist.

### 6. Human check (CLAUDE.md)
Replay history (`backfill_episodes`, then let reviews run) and confirm lessons promote and refute sensibly; read the first dozen lesson statements against their predicates.

### 7. Next: Phase 6 (pre-trade advisory, weekly review, counterfactuals, evals, operations)
`PreTradeAdvisory` on create / change / activation (never blocks), weekly deep review, counterfactual sweeps, `run_intelligence_evals` (a new prompt must beat the active one), Prometheus gauges, `RUNBOOK.md`, security and redaction tests.

## Session 7: Phase 6 report (advisory, weekly review, counterfactuals, evals, operations, security)

### 1. What changed
* **Migration `0006_advisory_evals`:** `intel_pre_trade_advisory` (the 20th planned table) and `eval_scores` on `intel_prompt_template`.
* **Pre-trade advisory** (`services/advisory.py`). Built on create, parameter change and activation, and by `POST advisories/preview/` for unsaved settings (no writes). It contains: lessons that held up and can apply (a lesson whose pattern is ruled out by the proposed parameters is excluded; hypotheses appear only when they clearly apply, labelled "not evidence yet"), the strategy's per-currency track record with n and intervals, open suggestions, a calibration warning (only with n >= 20), and a comparison with past runs of the same strategy (what changed in the parameters, and how that run did, with n and interval; HIGH if the very same settings lost over >= 8 trades). It is returned as an additive `advisory` field on the strategy create, PATCH (parameter changes only) and enable responses, recorded once per instance, parameters, trigger and day, and remembered as `advisory_shown` on the run-config snapshot. It never blocks and never raises into the caller (tested with a crashing builder). The optional acknowledgement for HIGH is off (`INTELLIGENCE_ADVISORY_REQUIRE_ACK=false`) and informational even when on.
* **Weekly / per-strategy review** (`services/weekly.py`, daily sweep, `POST strategies/<KEY>/weekly-review/`). Aggregated facts only (counts, intervals, lesson states, finding frequencies, calibration). Below 8 closed trades no model is called and the review says "Insufficient data". Otherwise the model must cite keys, quote only numbers from the facts, and give each hypothesis a test plan; ungrounded items are dropped; a bad headline falls back to a deterministic summary. Uses `INTELLIGENCE_MODEL_DEEP`, or the review model when that is unset.
* **Counterfactuals** (`services/counterfactuals.py`, hypotheses only). (a) Option filter sweep on stored evidence: RPS, IV score, TQS cutoff, breach ceiling, WATCH exclusion; reports the number of comparisons and an interval corrected for them (`holds_after_correction`), and states that filters can only be tightened. (b) Stock replay: runs the real plugin `evaluate()` over persisted bars using the engine's own indicator code (`streaming.flink.jobs.processing.compute_indicator`, so the Cutler RSI and the Donchian channel excluding the current bar match live trading) with a flat cost model; its `fidelity` list says what is not modelled (sizing, risk, order types, real commissions, delayed data, fills at signal close). Option strategies are refused.
* **Evals** (`services/evals.py`, `run_intelligence_evals`). Five golden cases scored on schema validity, grounded-item rate, invented-number rate, parameter validity, agreement with the analyzers, and advice discipline (a one-trade parameter change presented as fact fails). A candidate prompt must not regress any hard metric and must have a strictly better composite. **A newer prompt defined in code is now only an inactive candidate**; it replaces the active one solely through `run_intelligence_evals --prompt-version N --activate`. Scores are stored on the templates.
* **Operations.** `GET metrics/` (Prometheus text; staff session or bearer `INTELLIGENCE_METRICS_TOKEN`): reviews pending, failures by status, spend, budget and alarm, circuit breaker, lessons by status, suggestion reject rate, Brier score, open episodes, HIGH alerts. Health now includes `operations` (pending reviews, failures, budget alarm, last model review). A budget alarm AuditEvent is written once a day when spend passes 80% (`INTELLIGENCE_BUDGET_ALARM_PCT`). `docs/intelligence/RUNBOOK.md`: health, switching off, re-running, rotating prompts, spend control, backup and the cleanup flag, Gemini egress and data-handling, known limits.
* **Security.** Every string in a frozen evidence pack (names, symbols, parameters, signal and risk reasons, exit subtype, engine JSON) is now sanitised recursively, and so are the commentary, weekly and advisory payloads; the advisory also echoes sanitised parameters. Redaction tests prove that no account id, contract id, session token, execution id, idempotency key or API key reaches any prompt; injection tests put hostile text into every free-text field that can reach a prompt and check the data block, markers, control characters and lengths. All new POST endpoints are staff-only, CSRF-protected, idempotent where state changes, and rate-limited with database counters.
* **Frontend:** advice banner in the strategy wizard review step and in the edit panel (renders nothing if the API errors or returns something unexpected); Learning tab gained Weekly (reviews, "review now") and What-if (filter sweep with the number of comparisons and the warnings).
Engine files touched this session: `apps/strategies/views.py` (+17 lines, additive advisory field via a try/except helper), `config/settings.py` and `config/test_settings.py` (settings, one beat entry). Frontend: `CreateStrategyPage.tsx`, `EditStrategyPanel.tsx` (one banner line each).

### 2. Tests added
Backend `tests/intelligence/test_phase6.py` (28): advisory content rules, API hooks on create/PATCH, failure isolation, disabled no-op, dedupe, preview/list/acknowledge; weekly review (insufficient data without a model call, grounding, idempotency, deep model, fallbacks, sweep, API); option sweep and stock replay (engine indicator code used, costs, fidelity notes, option strategies refused); evals (scoring, reckless advice punished, promotion rules, hard-metric regression, command); metrics endpoint and token, budget alarm, health; redaction and injection across every prompt; staff-only and CSRF on all new endpoints.
Backend suite on Python 3.10: **860 passed**, 3 skipped, 17 failed (the same research CRLF tests). With the layer enabled the other 644 engine tests also pass. Frontend: **101 passed**, 12 failed (the same 12 pre-existing); 4 new.

### 3. Real-model check
Weekly review of 12 synthetic trades with `gemini-3.1-flash-lite`: valid, labelled "insufficient data" by the model itself, with one finding accepted and two ungrounded items dropped by the validator. `gemini-2.5-pro` no longer exists, and `gemini-pro-latest` / `gemini-3.1-pro-preview` answer 429 on this key, so `INTELLIGENCE_MODEL_DEEP` stays empty and weekly reviews use the review model.

### 4. Assumptions that turned out wrong
* The first advisory banner crashed existing wizard tests when the API returned an unexpected payload; advice components must tolerate any response, now enforced.
* The budget guard silently did nothing for a while in testing because a Decimal times float error was swallowed by the "never raise" wrapper: found by test, fixed; the lesson is that fail-open code needs tests that assert the positive effect.
* The stock replay first found no trades because indicator roles live in the input parameters, not in the role field; replay now reads them the way the engine does.

### 5. Risks found
* Deep-model reviews need a Pro-tier model your key cannot use today; the weekly review is therefore as good as the Flash-Lite model.
* Counterfactuals are easy to over-read. The UI and API repeat the comparison count and the "hypotheses only" warnings; keep it that way.
* Prompts are now frozen to the active version until a candidate wins the golden evals; the golden set is small (5 cases) and synthetic. Add real anonymised cases as trades accumulate.
* Free-tier Gemini quotas are tiny (429 after a handful of calls). Real use needs a paid key with data-handling terms you have read (RUNBOOK section 7).

### 6. Status
All six phases are implemented and committed on `feature/intelligence-layer` in both repos. Remaining work is operational: provide a Gemini key with adequate quota in the deployment environment, run `backfill_episodes --dry-run` on the real database, enable on PAPER, read the first reviews, and merge after your review.
