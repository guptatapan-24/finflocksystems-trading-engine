
# Intelligence layer: design (Phase 0)

Status: **proposal, waiting for approval** (Phase 0 gate). No model, migration or engine file has been changed.
Names are resolved in [NAME_MAP.md](NAME_MAP.md). Section 10 lists the decisions I need from you.

## 1. Shape of the app

```
apps/intelligence/
  apps.py            IntelligenceConfig.ready(): connect Fill post_save (try/except, enqueue-on-commit only)
  models.py          intel_* tables (created phase by phase)
  services/          episodes.py, metrics.py, evidence.py, reviews.py, suggestions.py, learning.py
  analyzers/         deterministic rule analyzers (Phase 3)
  llm/               base.py, gemini.py, fake.py, budget.py, circuit.py
  tasks.py           all Celery tasks, every one routed to queue "intelligence"
  api.py / urls.py   staff-only function views, envelope from apps.core.views.response
  management/commands/   flush.py (guard), backfill_episodes.py, export_intelligence_dataset.py
```

It depends on the engine one way only (reads engine tables, never writes them). The engine never imports it, apart
from the registration lines in R13(a) and the `Fill` signal, which Django connects by itself when the app loads.

## 2. Hook points

### 2.1 Stock episode: where it opens and closes

Nothing in the engine marks a trade boundary: `apply_execution` (apps/oms/services.py:144) just updates
`PortfolioPosition`, ledgers and `StrategyAttributedPosition`. We therefore *derive* episodes from fills.

* **Trigger (fast path).** `post_save(Fill)` -> `transaction.on_commit(lambda: process_fill.delay(fill.pk))`.
  `apply_execution` is `@transaction.atomic` and creates the Fill *before* it writes ledgers/attribution (oms/services.py:151 vs 178-205),
  so the task must not run until the outer transaction commits. The handler is `try/except Exception: log`, does no query beyond
  `fill.pk`, and is skipped entirely when `INTELLIGENCE_ENABLED=false`. `created=False` saves (late commission, services.py:76-84) also
  fire it; the task treats them as "re-evaluate this fill's episode".
* **Trigger (recovery path).** Beat task `intelligence-sweep-fills` every 30 s on queue `intelligence`: reads
  `Fill.objects.filter(pk__gt=cursor).order_by("pk")[:N]`, calls the same `process_fill`, advances the cursor. This is the
  source of truth if the signal, Redis or the worker was down.
* **Resolution.** `fill.order.intent.attributions` (allocation.OrderIntentAttribution) gives the strategy instance(s). One Fill can
  belong to several instances (pro-rata). Fills of intents with no attribution (MANUAL, BROKER_IMPORT) do not create episodes but
  set `MANUAL_INTERVENTION` on any open episode that shares the instrument.
* **Build.** `services/episodes.py:build_or_update_episode(instance_id)` replays `reporting.attributed_fills(instance)` (apps/strategies/reporting.py:373)
  in `(executed_at, execution_id)` order with the same weighted-average loop as `reporting._apply`/`oms.services._apply_weighted_average_fill`.
  STOCK: one book (the instrument). An episode opens when the book leaves 0; it closes when the book returns to 0 or **flips sign** (the flip fill is split:
  the closing part ends episode N, the remainder opens N+1, both priced at the fill price, commission pro rata by quantity).
  We resume the replay from the last CLOSED episode's last fill, so cost is O(open episode).
* **Never use** `StrategyAttributedPosition.average_cost` (NAME_MAP M7) or `PortfolioPosition` (portfolio-wide, includes manual trades).
* **Reconciliation test (Phase 2):** `sum(episode.net_pnl)` per instance and currency must equal `reporting.strategy_pnl(instance)["realized_pnl"]` when flat.

### 2.2 Options episode: where it opens, closes, expires

* **Open.** First leg fill of an entry decision. Entry decisions are `StrategySignal` rows linked to a `StrategyRun` whose
  `context_snapshot["bar"]["option_context"]["selection_snapshot_id"]` names the `OptionSelectionSnapshot` (reporting.py:338-341; NAME_MAP M11).
* **Close (normal).** Exits are ordinary orders (EXIT_OPTIONS signal; plugin apps/strategies/plugins/options.py:184-250: expiry rule when
  `(nearest_expiry - today).days <= exit_dte`, strategy exit, or SOFT_EXIT). The episode closes when **all** legs of the structure are
  flat again, which is exactly `reporting.trade_points` semantics ("gross exposure leaving and returning to flat; a multi-leg structure is one entry").
  `exit_reason` comes from the exit run's `StrategySignal.reason`/`details` and `state_data["exit_reason"]`; soft-exit subtype from `option_context["status"]`.
* **Close (expiry without a fill).** Engine behaviour: any exception in `evaluate_instance` sets `StrategyInstance.state="ERROR"`
  (framework.py:644-645), and an ERROR instance is not evaluated, so the expiry exit never fires and legs can sit non-zero after expiry.
  Sweeper `intelligence-sweep-expired` (every 15 min, queue `intelligence`): for each OPEN options episode where **every non-flat leg's
  `OptionContract.expiration` < today (market zone)**, and the latest completed `BrokerPositionSnapshot` shows those contracts flat/absent, close with
  `status=EXPIRED`, `exit_reason=EXPIRED_NO_FILL`, `closed_at=expiration`. P&L basis is an **estimate** (intrinsic value from the underlying's last LIVE bar at expiry vs the structure's
  strikes; flag `PNL_ESTIMATED_AT_EXPIRY`), never mixed silently with fill-based P&L. If the snapshot says the broker still holds the contracts, do nothing and flag `EXPIRED_BROKER_STILL_HOLDS`.
  (`BrokerPositionSnapshot` fields get resolved when Phase 2 starts; it is not in MODEL_MAP yet.)
* **Side effect to document, not fix:** an expired-no-fill structure leaves a non-zero `StrategyAttributedPosition`, which blocks strategy deletion
  (`deletion._blockers` NON_ZERO_POSITIONS). Out of scope (R1/R13), reported in the runbook.

### 2.3 Apply path for suggestions (R1)

PATCH `strategy-instances/<id>/` calls `framework.update_instance` (views.py:577), which has **no** safety check: for an enabled instance a
material change retires the version, bumps `version`, and sends the instance to `WARMING_UP` (framework.py:114-150). Our accept path therefore enforces, inside one
`transaction.atomic` + `select_for_update` on the instance:
`enabled is False` and `state in {FLAT, DISABLED, PAUSED}` and no OPEN `TradeEpisode` for the instance and all `StrategyAttributedPosition.quantity == 0`
and no active order (reuse `deletion._blockers`/`ACTIVE_ORDER_STATES`) -> otherwise 409 `INSTANCE_NOT_SAFE_TO_CHANGE`. Then
`validate_configuration` (R3) -> `update_instance(instance, {"parameters": merged})` -> `AuditEvent` (key from the request's Idempotency-Key). It never calls `enable_instance`.
The request-level idempotency record is an `intel_` row (unique on key + request hash), because `StrategyAction` is per strategy-action and has a fixed meaning.

## 3. Retention and cleanup impact (R6)

* `compact_operational_records` (apps/event_bus/tasks.py:34) only deletes PUBLISHED `OutboxEvent`, COMPLETED `BrokerPositionSnapshot` and `StreamHealthMetric`
  older than 30 days. **No intel_ table and no engine table we read (except `BrokerPositionSnapshot` for the expiry sweeper) is touched.** Consequence:
  the sweeper must tolerate a missing snapshot (then it waits, never guesses); the frozen `EpisodeEvidenceSnapshot` is what survives, so it has to be complete.
* `CLEANUP_DATABASE_ON_START=true` (entrypoint.sh:15-27; baked in by `COPY .env.example .env`, Dockerfile:51) runs `manage.py flush --noinput`. `flush` truncates
  **every** table of every installed app (including intel_), resets sequences, and also deletes `auth_user` (staff accounts) and sessions. After a flush engine ids restart at 1,
  so any hard reference from an intel_ row would silently point at a different strategy. Hence R12 soft refs **plus denormalised identity** on every intel_ row.
* **Guard design (two layers):**
  1. `apps/intelligence/management/commands/flush.py` overrides Django's `flush` (an app-level command with the same name overrides the built-in one; no other app defines `flush`).
     It refuses with a non-zero exit when `INTELLIGENCE_ENABLED=true` **and** any intel_ table has a row, unless `--i-understand-intelligence-data-loss` is passed. No engine file is edited.
  2. entrypoint.sh cancels Flink jobs and flushes Kafka *before* the DB flush, so layer 1 alone aborts too late and leaves a half-reset system. Layer 2 is a 4-line pre-check at the top of the cleanup
     branch (`python manage.py check_intelligence_flush_guard`). **entrypoint.sh is not on the R13 allowed list; I need your approval** (section 10, Q3).
  3. Docs: RUNBOOK warning, `.env.example` comment, `export_intelligence_dataset` before any flush.
* Open question inside R6 as written: with the flag **off** but old rows present, the guard (as specified) allows the flush. I recommend refusing whenever any intel_ row exists (Q4).

## 4. Engine footprint (R13), planned edits

| File | Edit | Phase |
|---|---|---|
| config/settings.py | `INSTALLED_APPS += apps.intelligence`; `INTELLIGENCE_*` env settings; `INTELLIGENCE_TASK_ROUTES` merged into `CELERY_TASK_ROUTES`; beat entries `intelligence-*` | 1 |
| config/urls.py | `path("intelligence/", include(...))` inside `new_api` | 1 |
| supervisord.conf | `[program:celery-intelligence]` (prefork, `--concurrency=1`, `--queues=intelligence`, same three `--without-*` flags, priority 24) | 1 |
| requirements.txt | `google-genai` (pin after checking the current release and Python support) | 1 |
| .env.example | `INTELLIGENCE_*` block (flag false), flush warning comment | 1 |
| entrypoint.sh | guard pre-check (if approved) | 1 |
| apps/intelligence/apps.py | `Fill` post_save is registered here, **not** in the engine | 2 |
| strategies/framework.py, strategies/views.py | additive try/except hooks on activation / version creation and optional `advisory`/`intent_note` fields | 4, 6 |

Not touched, by design: `READINESS`, `REQUIRED_WORKER_ROLES`, `EXECUTION_REQUIRED_KAFKA_TOPICS`, any Flink/Kafka script, `apply_execution`.

## 5. The tables (20 + 1 proposed)

Conventions for **every** intel_ table: `id BigAutoField`, `created_at`, soft refs per R12 (`ForeignKey(..., on_delete=DO_NOTHING, db_constraint=False, null=True, related_name="+")`
or plain `BigIntegerField`; the gateway session is `UUIDField`), PROTECT only between intel_ tables, `db_table = "intel_<snake_name>"`.
`identity` below means the denormalised set: `strategy_key, instance_id, instance_name, strategy_version, instrument_symbol, asset_class, currency, execution_mode`.

**Phase 1**
1. `intel_prompt_template`: name, version, system_prompt, output_schema (JSON), active (bool), notes. Unique (name, version). Partial unique: one `active` per name.
2. `intel_llm_run_log`: scope, provider, model, prompt_template (PROTECT, null), input_tokens, output_tokens, thinking_tokens, cost_usd (Decimal 12,6), latency_ms, status (OK/SCHEMA_INVALID/GROUNDING_FAILED/ERROR/TIMEOUT/BUDGET), raw_response (Text), parsed_output (JSON), validation_errors (JSON), evidence_hash, idempotency_key **UNIQUE**, episode_id (soft). Index (scope, created_at), (status, created_at).

**Phase 2**
3. `intel_trade_episode`: identity + portfolio_id, instance/version/instrument soft FKs, asset_family (STOCK/OPTION), direction, status (OPEN/CLOSED/EXPIRED), opened_at, closed_at, entry_run (soft), option_selection_snapshot (soft), run_config_snapshot (soft, Phase 4), gross_pnl, commissions, net_pnl, pnl_basis (FILLS/ESTIMATED_AT_EXPIRY), return_on_risk, hold_seconds, exit_reason, exit_subtype, data_quality_flags (JSON list), first_fill_execution_id, last_fill_execution_id, fill_count, metrics_version_built, `idempotency_key` **UNIQUE** (`episode:{instance}:{version}:{first_fill_execution_id}`). Indexes (instance_id, opened_at), (status, closed_at), (currency, closed_at). CHECK currency non-empty; CHECK `closed_at >= opened_at`.
4. `intel_trade_episode_leg`: episode (PROTECT), instrument soft id + symbol/right/strike/expiry/multiplier, role, side, entry_qty/avg_price, exit_qty/avg_price, commission, reference_price_at_entry/exit, slippage_bps. Unique (episode, instrument_id).
5. `intel_episode_metrics`: episode (OneToOne PROTECT), metrics (JSON, flat dotted keys), metrics_version. Index (metrics_version).
6. `intel_episode_evidence_snapshot`: episode (OneToOne PROTECT), pack (JSON, whitelist output), evidence_hash, builder_version, token_estimate. Immutable (`save()` raises on update, like `StrategyVersion`).
*Proposed 21st:* `intel_cursor`: name (unique), last_id, updated_at. Needed for the 30 s fill sweep and later sweeps; the only alternative is abusing `AuditEvent`. **(Q5)**

**Phase 3**
7. `intel_trade_review`: scope (TRADE/WEEKLY/STRATEGY), episode (PROTECT null), identity, verdict, process_grade (A-F), outcome_class, summary, confidence, is_fallback, lesson_proposals (JSON raw), llm_run (PROTECT null), prompt_version, model, evidence_hash. **Unique (episode, evidence_hash, prompt_version, model)** for scope TRADE (R4).
8. `intel_mistake_finding`: review (PROTECT), category (12 enums in CLAUDE.md), severity, description, evidence_refs (JSON), source (RULE/LLM), rule_id. Index (category, created_at).
9. `intel_suggestion`: review (PROTECT), identity, kind, parameter_key, current_value/proposed_value (JSON), rationale, expected_effect, risk_of_change, confidence, evidence_needed, is_hypothesis, status (PROPOSED/ACKNOWLEDGED/ACCEPTED/REJECTED/APPLIED/EXPIRED/SUPERSEDED), decided_by, decided_at, decision_reason, applied_version_id (soft), idempotency_key UNIQUE. Index (instance_id, status).
10. `intel_review_feedback`: review (PROTECT), rating, comment (length-capped), created_by.

**Phase 4**
11. `intel_run_config_snapshot`: identity, strategy_version_id (soft) + parameter_hash, data_source_flags (JSON), regime_at_activation (JSON), advisory_shown (JSON null), intent_note (Text, capped), captured_at. Unique (instance_id, strategy_version, parameter_hash, captured_at-bucket) via idempotency_key.
12. `intel_episode_live_snapshot`: episode (PROTECT), observed_at, quote_freshness, marks (JSON), mark_pnl, mae_so_far, mfe_so_far, distances (JSON), flags. Index (episode, -observed_at). Retention: thin to 1/5 min after close.
13. `intel_live_alert`: episode (PROTECT), rule_id, severity, templated_text, dedupe_key, first_seen, last_seen, acknowledged_by/at. Unique (episode, dedupe_key).

**Phase 5**
14. `intel_lesson`: scope, statement, predicate (JSON DSL), expected_effect (JSON), status (HYPOTHESIS/SUPPORTED/ESTABLISHED/REFUTED/RETIRED), support_n, contradict_n, beta_alpha, beta_beta, effect_size, effect_ci_low/high, stream (LIVE/PAPER), predicate_hash UNIQUE, created_from_review (PROTECT null).
15. `intel_lesson_evidence`: lesson (PROTECT), episode (PROTECT), verdict (SUPPORTS/CONTRADICTS/NA), weight, forward_only (bool). Unique (lesson, episode).
16. `intel_performance_stat`: dims (strategy_key, version, instrument, regime, structure, mode, **currency**), n, wins, mean_return_bps, ci_low, ci_high, as_of. Unique over all dims + as_of bucket.
17. `intel_calibration_stat`: metric (POP/BREACH_UP/BREACH_DOWN/...), bin, n, predicted_mean, observed_rate, brier, as_of.
18. `intel_learning_event`: type (LESSON_CREATED/REINFORCED/CONTRADICTED/PROMOTED/RETIRED, STAT_SHIFT, CALIBRATION_SHIFT, SUGGESTION_RESOLVED), episode (PROTECT null), lesson (PROTECT null), payload (JSON), seq (monotonic cursor for the feed). Unique (type, episode, lesson).
19. `intel_suggestion_outcome`: suggestion (OneToOne PROTECT), result (HELPED/HURT/INCONCLUSIVE), n_before, n_after, delta, ci_low/high, evaluated_at.

**Phase 6**
20. `intel_pre_trade_advisory`: identity, trigger (CREATE/CHANGE/ACTIVATION), lessons (JSON), stats (JSON), open_suggestion_ids, calibration_warnings, compared_snapshot_ids, severity, acknowledged_by/at.

All JSON columns are plain `JSONField` (works on SQLite and PostgreSQL 16). No `ArrayField`/`JSONB`-only lookups, no partial-index syntax that SQLite lacks, except the single-active-prompt rule, which uses a Django `UniqueConstraint(condition=Q(active=True))` (supported on both).

## 6. Money, units, time (R5)

`currency` stored on every episode (from `Fill.currency`, which can differ from `Instrument.currency`; mixed currencies inside one episode -> flag `MIXED_CURRENCY`, no P&L aggregation).
Per-currency stats only; cross-currency comparisons use `return_on_risk` and `net_return_bps`. Multiplier and lot size come from `Instrument.multiplier` / `lot_size` / `OptionContract.multiplier`.
Times are stored UTC; time-of-day buckets use the zone mapped from `Instrument.trading_calendar` (XNSE -> Asia/Kolkata, XNYS -> America/New_York) via `zoneinfo` (stdlib, 3.9+).

## 7. LLM boundary (R3, R8, R11)

* Evidence builder is a **whitelist** (`evidence.py`): it constructs the pack field by field from the frozen `EpisodeEvidenceSnapshot`; there is no `model_to_dict`. Forbidden by construction:
  `BrokerAccount.account_id`, `BrokerContract.conid`, gateway session ids/`child_container_*`, `workflow_trace_id`, tokens, `raw_event`, `OrderIntent.idempotency_key`, `intent_note` outside its quoted block.
* Free text that can reach a prompt: `StrategyInstance.name`, `Instrument.symbol`, `reason` strings, `intent_note`. Names/symbols are sanitised (length cap, control chars stripped, wrapped in a data block); injection tests in Phase 3.
* Gemini: `google-genai` SDK, model ids only from env (`INTELLIGENCE_MODEL_REVIEW`, `INTELLIGENCE_MODEL_DEEP`), no code defaults. **Before Phase 1 coding I will look up the current Gemini docs** (structured output schema subset, temperature/thinking settings for the chosen model, pricing for cost accounting, stable model ids) and write the findings to this file. Not done in Phase 0 because it needs web access and a model choice from you (Q6).

## 8. Time and cost of the hot path

The signal handler adds one `transaction.on_commit` registration per Fill (no DB query). Benchmark plan for Phase 2: 1,000 `apply_execution` calls on SQLite test settings with the layer disabled / enabled-but-worker-absent;
accept if the median delta is < 2 %. The worker holds numpy/pandas/scipy lazily (the engine's own `apps/core/lazy_imports.py` pattern) to stay near the other workers' RSS.

## 9. Risks found in Phase 0

| # | Risk | Severity | Mitigation / owner |
|---|---|---|---|
| 1 | Engine does not import on Python 3.10 (`StrEnum`, `scipy 1.16`); production is 3.12 | High for your 3.10 requirement | Q1 below. |
| 2 | `CLEANUP_DATABASE_ON_START=true` is baked into every image; flush wipes intel_ data and staff users, and the Kafka/Flink wipe runs before the DB flush | High | Guard layers (section 3). |
| 3 | Strategy PATCH has no safety check and is `@csrf_exempt`; anyone reaching the API can change parameters of an enabled instance | High (engine, pre-existing) | Out of scope (R1 says we do not make it worse). Reported. |
| 4 | `.env.example` contains concrete-looking DB/Redis credentials and is copied into the image | Medium (pre-existing) | Reported only; I did not copy the values anywhere. |
| 5 | `StrategyAttributedPosition.average_cost` wrong for sells/shorts | Medium | Use `reporting._apply` logic (M7). |
| 6 | No FK from orders to `OptionSelectionSnapshot`; episode -> snapshot link is inferred through run context | Medium | `OPTION_SNAPSHOT_UNLINKED` flag; Phase 2 test on the NIFTY fixture. |
| 7 | Expired-no-fill leaves non-zero attributed positions (blocks deletion) | Low | Documented. |
| 8 | Throttle is per-process x 2 gunicorn workers | Medium | DB-backed counter for rerun/commentary endpoints. |
| 9 | No `django.contrib.admin` | Low | No admin in Phase 1 (M5). |
| 10 | 18 research tests fail on Windows checkouts (CRLF vs bundle hashes) | Low | Not ours; CI on Linux. |
| 11 | Late commission: `Fill.commission` can change after close | Medium | R4 threshold: re-review only if net P&L moves > `INTELLIGENCE_REREVIEW_PNL_DELTA_PCT`; episode metrics are still recomputed (cheap, deterministic). |
| 12 | Decision bands (TRADE_FULL/HALF/WATCH) computed at selection.py:521-523, not found elsewhere | Verify in Phase 2 | Analyzer WATCH_BAND_FULL_SIZE needs the band stored in `source_snapshot`; confirm it is. |

## 10. Decisions I need (the Phase 0 gate)

1. **Python 3.10.** The repo's own code (`enum.StrEnum`) and pins (`scipy==1.16.3`, prod image 3.12) do not run on 3.10. I made a git-ignored 3.10 venv with a venv-only shim and scipy 1.15.3; 657/678 existing tests pass (the 18 fails are the CRLF bundle issue). My new code will be 3.10-compatible. Options: (a) keep this setup for dev, prod stays 3.12 (recommended); (b) also patch `base.py` to `class X(str, Enum)` (1-line engine change, outside R13 list); (c) move your dev to 3.12.
2. **Approve NAME_MAP.md** (esp. M1, M5, M7, M9, M11).
3. **entrypoint.sh pre-check** for the flush guard (engine file outside R13 list).
4. **Guard strictness:** refuse flush when any intel_ row exists (even flag off), or exactly as written (flag on AND rows).
5. **21st table `intel_cursor`** for sweep cursors.
6. **Gemini model tier ids** you want (Flash-tier for reviews, Pro-tier for deep), and confirmation that the container may reach the Gemini endpoint.
7. **Branch:** CLAUDE.md says `feature/intelligence-layer`; the repos currently sit on `Ai-backend` / `Ai-frontend` (tracking origin). Create the feature branch from them, or work on those directly?

## 11. Phase 1 findings: Gemini (R11, looked up 2026-10-07)

* SDK: `google-genai` 2.28.0 (pinned in requirements.txt; installs and runs on Python 3.10). Call: `client.models.generate_content(model, contents, config=GenerateContentConfig(...))`.
* Structured output: `response_mime_type="application/json"` plus `response_json_schema` (a plain JSON Schema). The documented supported subset: types, title/description,
  properties/required/additionalProperties, enum, format (date, date-time, time), minimum/maximum, items/prefixItems/minItems/maxItems. Output schemas in this project must stay inside that subset, and every response is **re-validated locally** with jsonschema regardless.
* Config fields confirmed in the SDK: `system_instruction`, `temperature`, `max_output_tokens`, `thinking_config(thinking_level | thinking_budget)`, `HttpOptions(timeout=ms)`. Usage fields: `prompt_token_count`, `response_token_count`, `thoughts_token_count` (thinking bills as output).
* The docs pages I could read did not state recommended temperature/thinking values per model. They are therefore **env-controlled and unset by default** (`INTELLIGENCE_TEMPERATURE`, `INTELLIGENCE_THINKING_LEVEL` or `_BUDGET`); the model's own default applies until you choose.
* Verified live on 2026-10-07 with the project key: `gemini-3.5-flash` and `gemini-3.6-flash` returned schema-valid JSON through `GeminiClient` (thinking tokens reported separately); `gemini-3.8-flash` answered 503 high-demand/504 once (handled as LlmError, retried, circuit breaker counts it); `gemini-2.5-flash` is refused for new users (404). Google lists `gemini-3.5-flash`, `3.6-flash`, `3.8-flash`, `3.1-flash-lite` as the stable Flash tier. Recommended starting point: `INTELLIGENCE_MODEL_REVIEW=gemini-3.6-flash`.
* Pricing is not read from the web; set `INTELLIGENCE_PRICE_*_PER_MTOK_USD` from your Google AI Studio billing page. Until set, cost is recorded as 0 and `/health` shows `prices_configured=false`; the daily budget cannot protect you in that state.
* Data handling / egress: paid-tier terms and whether the container can reach `generativelanguage.googleapis.com` must be confirmed by you before enabling (RUNBOOK in Phase 6).
