# Intelligence layer: runbook

The layer is advisory only. It reads what the engine stores, writes its own `intel_*` tables, and talks to Gemini. It never
places, changes or cancels an order, never enables or activates a strategy, and its failure never affects trading or the
execution-readiness checks. Everything below is safe to do while the engine is trading.

## 1. Is it healthy?

* `GET /api/v1/intelligence/health/` (staff): enabled flag, provider, whether a model and key are configured, today's spend
  against the budget (`operations.budget.alarm`), circuit-breaker state, reviews waiting, failures in the last 24 h.
* `GET /api/v1/intelligence/metrics/`: Prometheus text. Use a staff session, or set `INTELLIGENCE_METRICS_TOKEN` and scrape
  with `Authorization: Bearer <token>`. Useful gauges: `intelligence_reviews_pending`, `intelligence_llm_failures_24h{status}`,
  `intelligence_llm_cost_usd_today`, `intelligence_budget_alarm`, `intelligence_circuit_breaker_open`, `intelligence_lessons{status}`,
  `intelligence_suggestion_reject_rate` (-1 = no decisions yet), `intelligence_calibration_brier{strategy}`, `intelligence_high_alerts`.
* The `celery-intelligence` Supervisor program consumes queue `intelligence` only. If it is down, nothing breaks: episodes are
  rebuilt by the 30-second sweep once it is back, and reviews are generated then. It is deliberately NOT a readiness role.
* Alarms worth wiring: `intelligence_budget_alarm == 1`, `intelligence_circuit_breaker_open == 1` for more than 10 minutes,
  `intelligence_reviews_pending` growing for a day, a rising `intelligence_llm_failures_24h`.

## 2. Turn it off (and on)

| Goal | Setting | Effect |
|---|---|---|
| Stop everything | `INTELLIGENCE_ENABLED=false` | The Fill hook, sweeps, live monitor, advisory and capture become no-ops. Tables and data stay. |
| Keep episodes, stop the model | `INTELLIGENCE_REVIEWS_ENABLED=false` | Episodes, live monitoring, rule-based findings and reports continue; no tokens are spent. |
| Use no key | leave `GEMINI_API_KEY` empty | Reviews fall back to rule-based output. |

Restart the web and `celery-intelligence` processes after changing settings. Beat entries are only registered when the layer is enabled.

## 3. Re-run things

* One trade's review: `POST /api/v1/intelligence/episodes/<id>/review/rerun/` (Idempotency-Key required). If a real review
  already exists for the same evidence, prompt and model it returns `UNCHANGED`; a fallback review is replaced.
* Rebuild episodes after a bug fix or a metrics version bump: `python manage.py backfill_episodes --dry-run`, then without `--dry-run`
  (idempotent; `--force` recomputes unchanged ones).
* A strategy's aggregated review: `POST /api/v1/intelligence/strategies/<KEY>/weekly-review/`.
* Statistics and lessons are recomputed from stored evidence on every closed trade; there is nothing to repair by hand.

## 4. Rotate or improve prompts

A new prompt never replaces the active one automatically. The code defines the next version as an inactive candidate; then:

```
python manage.py run_intelligence_evals --prompt-version 4 --model <model>            # score only
python manage.py run_intelligence_evals --prompt-version 4 --model <model> --activate # activate only if it wins
```

The golden set (`apps/intelligence/evals/golden/*.json`) is scored on schema validity, grounding, invented numbers, parameter
validity, agreement with the rule analyzers and advice discipline. A candidate must not regress any hard metric
(schema validity, fatal rate, invented-number rate) and must have a strictly better composite. Scores are stored on the template.
Real runs cost tokens (about 5 calls per template). Add golden cases by dropping a JSON file in that folder.

To go back: set `active=true` on the older `intel_prompt_template` row and `false` on the newer (Django shell). Old reviews keep the
prompt version that produced them.

## 5. Spend control

* `INTELLIGENCE_DAILY_BUDGET_USD` (0 = unlimited) is enforced before every call using the day's logged cost; prices come from
  `INTELLIGENCE_PRICE_INPUT_PER_MTOK_USD` / `..._OUTPUT_...`. **With prices left at 0 the guard counts nothing.**
* The alarm writes one `intelligence.budget.alarm` AuditEvent per day when spend passes `INTELLIGENCE_BUDGET_ALARM_PCT` (80).
* Over budget, reviews degrade to rule-based fallbacks; the next run after the budget resets replaces them with real ones.
* Per-call caps: `INTELLIGENCE_MAX_INPUT_TOKENS`, `INTELLIGENCE_MAX_OUTPUT_TOKENS` (thinking tokens bill as output).

## 6. Back up, and the cleanup flag

`CLEANUP_DATABASE_ON_START=true` wipes the whole database on every container start. The layer protects its data:

* On start, if any `intel_*` row exists the reset is **skipped** with a warning and the engine starts normally.
* `python manage.py flush` is refused while intelligence data exists (override: `--i-understand-intelligence-data-loss`).
* Back up before anything destructive: `python manage.py export_intelligence_dataset --out /backups/intel-$(date +%F)`
  (add `--include-evidence` for frozen evidence packs and raw LLM logs). The export is JSONL plus a manifest with SHA-256 per file.
* A flush also deletes staff users and sessions; recreate a staff user with `python manage.py createsuperuser`.
* To really start from scratch, export first, then flush with the override flag.

## 7. Gemini: network and data handling

* The container must reach `generativelanguage.googleapis.com` over HTTPS (egress). A blocked network shows up as LLM status `ERROR`/`TIMEOUT`,
  an open circuit breaker, and fallback reviews; trading is unaffected.
* **What is sent:** a whitelisted, sanitised evidence pack for one trade (parameters, indicator values, metrics, fills, sizing, risk checks, flags),
  or aggregated statistics for a weekly review. **Never sent:** broker account ids, contract ids, gateway/session ids, tokens, raw broker events,
  engine idempotency keys, execution ids, the API key. Strategy and instrument names and the operator's `intent_note` are sent as quoted data after
  sanitising (control characters, code fences and our data markers removed, length-capped).
* Trade data is financial information about your account activity. Check Google's current terms for the Gemini API on your plan (paid-tier
  and free-tier data-use terms can differ, in particular whether submitted content may be used to improve Google's products; verify this on Google's
  site, I have not confirmed it here). Do not send real trading data with a key whose terms you have not read. Decide this before enabling reviews on LIVE accounts.
* Rotate the key by changing `GEMINI_API_KEY` in the deployment environment and restarting `celery-intelligence`. A key that was ever pasted into
  chat, a ticket or a commit should be treated as leaked.

## 8. Known limits (by design)

* One trade is not evidence. Reviews, suggestions and lessons carry `n`; lessons stay hypotheses until confirmed on later trades.
  With about one options entry per expiry this takes a long time. That is correct behaviour.
* Option live IV is only known at strategy evaluations; option MAE/MFE is only as good as the live snapshots.
* Counterfactuals are hypotheses: option filters can only be tightened, the stock replay ignores sizing, risk and real commissions, and every
  extra comparison raises the chance of a lucky result (the response states the number of comparisons).
* Delayed paper data makes bar-based path metrics approximate; episodes carry `PAPER_DELAYED_DATA` and learn at half weight.
