# Flink state: bounded in Flink, durable in PostgreSQL, bridged by the outbox

## The problem

Flink keyed state was the only long-term memory for data PostgreSQL already
holds durably, and it was unbounded and not durable.

| Where | What was kept | Growth |
|---|---|---|
| `bar_aggregation.RegistryBars` (`dynamic-bar-buckets-v1`) | One JSON `ValueState` per instrument holding **every bucket ever created**, with all raw ticks, per BAR requirement | Never evicted (doc §15.2 item 5). Each tick deserialised and re-serialised the whole blob, so CPU and Python-harness memory grew with job age |
| `indicator_computation.RegistryIndicators` (`rolling-final-bars-v2`) | Up to 10,000 full bar payloads per (instrument, interval) **per scope** (LIVE, REPLAY, BACKFILL), one JSON blob | Capped per scope, but no TTL. The whole blob was parsed and sorted for every bar |
| Broadcast registries (`strategy.inputs.v1`) | Requirement registry, rebuilt only by replaying the topic from `earliest` | The topic has no compaction and broker-default retention (7 days). An activation older than retention cannot be replayed |

Runtime: the default HashMap backend keeps all of this on the TaskManager JVM
heap (3 GB process). Checkpoints sit in JobManager memory because there is no
`state.checkpoints.dir`, so `ensure_flink_jobs.py` sees
`<checkpoint-not-externally-addressable>` and can never restore a resubmitted
job. The failure chain is: state grows, TaskManager is OOM-killed, jobs go to
`RESTARTING`, readiness reports `REQUIRED_FLINK_JOBS_NOT_RUNNING`, and trading
blocks. If the job is resubmitted, all state is lost:

* Requirements older than Kafka retention are gone, so their strategies get no bars or indicators, with no error raised.
* Indicator history is empty, so indicators return `null` until enough new bars arrive (days for daily strategies).
* Bar version fingerprints reset, so a re-sent bar becomes "version 1" with different content, which the backend dead-letters as a conflicting fact. The workaround in `_backfill_stored_windows` exists because of this.

## The fix

PostgreSQL is the system of record. Flink state is a bounded cache. The
transactional outbox feeds the cache from the database.

### Flink: bounded state

* **Bar aggregation**: per-bucket `MapState` (`open-bar-buckets-v1`). A bucket is
  evicted `BAR_BUCKET_RETENTION_SECONDS` (processing time) after its window
  last fired. A small `{version, fingerprint}` ledger with a state TTL
  (`BAR_VERSION_LEDGER_TTL_SECONDS`) keeps versioning stable. A tick for a window
  that was already evicted goes to `dead-letter.v1` with reason
  `LATE_TICK_AFTER_BAR_EVICTION`; it is no longer turned into a partial bar.
  Timer indexes mean a timer reads only its own buckets.
* **Indicators**: the history is capped at `INDICATOR_HISTORY_MAX_BARS`, but never
  below the longest registered warm-up. It keeps only the fields the indicator
  maths reads and carries a TTL (`INDICATOR_HISTORY_TTL_SECONDS`).
* **Registries** accept `strategy.inputs.snapshot`, which *replaces* a stage's
  requirements for one (instrument, timeframe). A snapshot older than an
  incremental event already applied is ignored.
* New operator uids (`registry-bars-v3`, `registry-indicators-v3`).
  `ensure_flink_jobs.py` passes `--allowNonRestoredState` when it restores, so
  the old unbounded state is dropped on purpose.

### Backend: outbox rehydration (`apps/market_streams/rehydration.py`)

| Producer | Topic | When |
|---|---|---|
| `publish_strategy_inputs_snapshot` | `strategy.inputs.v1` (`strategy.inputs.snapshot`) | Beat every `STRATEGY_INPUTS_SNAPSHOT_INTERVAL_SECONDS` (900 s), and on rehydration. One outbox row per (instrument, timeframe), re-queued in place, so the table does not grow |
| `publish_bar_history_seed` | `market.bars.seed.v1` (`market.bar.seed`) | On rehydration. The latest `FLINK_REHYDRATION_SEED_BARS` final bars per active indicator pair, in chunks of `FLINK_REHYDRATION_SEED_CHUNK_BARS` |
| `rehydrate_flink_state_if_needed` | both | Beat every 30 s. Runs once for each required Flink job that is RUNNING with **no restored checkpoint** and that it has not seen before. Fails open if Flink is unreachable |

The outbox is the right transport because it already guarantees what this
needs. Each message is written in a database transaction and read from the
same database that owns the data. It is published at least once with an
idempotent producer and retried with backoff. Every consumer is idempotent:
registry puts and seed merges keyed by `bar_id`/version are order-independent.
Readiness already monitors the outbox. No component talks to Kafka directly,
and Flink never needs database access.

Manual trigger:

```bash
python manage.py rehydrate_flink_state --force
```

Other options: `--snapshot-only`, `--seed-only`, `--instrument-id N`, `--timeframe 5m`.

## Configuration

| Variable | Side | Default |
|---|---|---|
| `BAR_BUCKET_RETENTION_SECONDS` | Flink (read at submit) | 900 |
| `BAR_VERSION_LEDGER_TTL_SECONDS` | Flink (read at submit) | 604800 |
| `INDICATOR_HISTORY_MAX_BARS` | Flink (read at submit) | 1000 |
| `INDICATOR_HISTORY_TTL_SECONDS` | Flink (read at submit) | 1209600 |
| `FLINK_REHYDRATION_ENABLED` | backend | true |
| `FLINK_REHYDRATION_CHECK_INTERVAL_SECONDS` | backend | 30 |
| `FLINK_REHYDRATION_SEED_BARS` (keep at or below `INDICATOR_HISTORY_MAX_BARS`) | backend | 500 |
| `FLINK_REHYDRATION_SEED_CHUNK_BARS` | backend | 100 |
| `STRATEGY_INPUTS_SNAPSHOT_INTERVAL_SECONDS` | backend | 900 |

"Read at submit" means the value is read where `ensure_flink_jobs.py` submits
the job (the backend container) and is pickled into the operator. The
TaskManager does not need these variables.

## Known residuals

* Between a fresh job starting and the seed arriving (one beat interval plus
  one outbox cycle, about 35 s), a live bar can be computed on a short history.
* `weekly_atr` is an EWM over all retained weeks. With a fixed cap, its value
  depends on the last `INDICATOR_HISTORY_MAX_BARS` bars, not on how long the job
  has been running.
* Bar version fingerprints are not seeded from the database. After a fresh
  start, a re-sent bar whose content differs from the stored row is still
  dead-lettered, as before.
* Durable checkpoints (`state.backend.type: rocksdb`, `state.checkpoints.dir`
  on a shared volume) are still recommended. They are no longer needed for
  correctness.
