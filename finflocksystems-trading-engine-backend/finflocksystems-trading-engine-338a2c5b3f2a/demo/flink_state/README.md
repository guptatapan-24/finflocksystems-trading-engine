# Flink state demo

This demo runs the real `market-pipeline-v1` Flink job twice under the same load
and the same simulated incident:

* **baseline**: the operators exactly as they were before the fix (`baseline_jobs/`,
  copied verbatim from commit `13be80a`; at run time only an operator `.name()` is
  added so the Flink REST API can report that operator's state size). The backend
  does no outbox rehydration.
* **fixed**: the current operators and the backend outbox rehydration.

The stack (`docker-compose.flink-demo.yml`, project `finflock-flink-demo`) contains
only PostgreSQL, Kafka, a Flink JobManager, one single-slot TaskManager and a
runner built from the backend image. It has no Redis, Celery, IBKR gateway or
frontend, and no named volumes.

## Memory

Give Docker about **5 GB**. The containers' memory limits are:

| Container | Limit |
|---|---|
| TaskManager | 2.5 GB |
| JobManager | 1 GB |
| Kafka | 768 MB |
| Runner | 1.5 GB |
| PostgreSQL | 256 MB |

## Run it

From the backend folder:

```powershell
powershell -ExecutionPolicy Bypass -File demo\flink_state\run-demo.ps1
```

Or step by step:

```powershell
docker compose -f docker-compose.flink-demo.yml build demo
docker compose -f docker-compose.flink-demo.yml run --rm demo baseline
docker compose -f docker-compose.flink-demo.yml down
docker compose -f docker-compose.flink-demo.yml run --rm demo fixed
docker compose -f docker-compose.flink-demo.yml down
docker compose -f docker-compose.flink-demo.yml run --rm --no-deps demo compare
```

The first build installs PyFlink and the Flink worker runtime, so it takes a few
minutes. After that, the working tree is mounted into the runner, so code changes
need no rebuild. Each variant takes about 12–15 minutes. While a variant runs, the
Flink UI is at http://localhost:28081, and `docker stats` shows the TaskManager's
memory, including the PyFlink worker.

## What one run does

1. **Setup.** Creates SMA_CROSSOVER(5/20) strategies on 1m bars in PostgreSQL. Their
   instrument registry and `strategy.inputs.v1` events go out through the real outbox.
2. **Submit.** Submits the job with the production state backend: heap state, and no
   checkpoint that a resubmitted job can restore.
3. **Load.** Replays 5-second ticks about 50 times faster than real time, so one 1m bar
   per instrument arrives every 1.2 s. Every 15 s it prints:
   * checkpointed state of the bar-aggregation and indicator operators;
   * total checkpointed state;
   * checkpoint duration;
   * TaskManager heap;
   * Kafka lag.

   The backend market consumer persists bars and indicators.
4. **Time passes.** `strategy.inputs.v1` retention is cut to 60 s, so the original
   activation events are deleted. This stands in for "activated more than a week
   ago". Half the strategies (the `recent` cohort) are then re-registered.
5. **Restart.** Cancels the job and resubmits it, as a TaskManager crash or a
   redeploy would. The new job starts with empty state.
6. **After restart.** The market resumes. PostgreSQL shows what the strategies get:
   bars and indicators after the restart, and how many indicator values are null.
   In the fixed variant, the first indicator after the restart is also compared
   with a recomputation from `MarketBar`.

Results are written to `demo/flink_state/results/{baseline,fixed}.json`.

## Expected comparison

| # | Check | baseline | fixed |
|---|---|---|---|
| 1 | Bar-aggregation state from mid-run to end | grows (x1.5–2+) | flat (≤ x1.3) |
| 2 | Kafka backlog at the end of the load | falls behind as state grows | ≤ baseline |
| 3 | `old` cohort: bars after restart | 0 (strategies silently stop) | > 0 (registry rebuilt from the PostgreSQL snapshot) |
| 4 | `recent` cohort: null indicators after restart | > 0 (rolling history lost) | 0 (history re-seeded from `MarketBar`) |
| 5 | First indicator after restart equals the DB recomputation | n/a | all match |
| 6 | Old activations really deleted from Kafka | yes | yes |

## Demo-only settings

Each setting shrinks to match the ~50x time compression, so the plateaus show
within minutes:

| Setting | Demo | Production default |
|---|---|---|
| `BAR_BUCKET_RETENTION_SECONDS` | 20 | 900 |
| `BAR_VERSION_LEDGER_TTL_SECONDS` | 120 | 604800 |
| `INDICATOR_HISTORY_MAX_BARS` | 200 | 1000 |
| `FLINK_REHYDRATION_SEED_BARS` | 200 | 500 |
| `FLINK_CHECKPOINT_INTERVAL_MS` | 10000 | 30000 |
| `strategy.inputs.v1` retention | 60 s | broker default, 7 days |

The restart happens while the simulated market is paused, as a deploy before the
open would. The market resumes 20 s after the new job is running. In real time
that 20 s is less than one 1m bar; at 50x compression it would be about 16 bars,
so pausing keeps the race between the restart and the seed at its real size.

## Tuning

Tuning flags go after the variant name:

| Flag | Default |
|---|---|
| `--instruments` | 4 |
| `--steps-per-second` | 10 |
| `--load-seconds` | 300 |
| `--after-seconds` | 90 |
| `--drain-seconds` | 300 |

Example:

```powershell
docker compose -f docker-compose.flink-demo.yml run --rm demo baseline --load-seconds 600
```

A longer load makes the baseline growth larger; the fixed variant stays flat.

If the compare step prints a NOTE that the baseline still had a backlog at the
restart, re-run `baseline` with a lower `--steps-per-second` or a higher
`--drain-seconds`. A replayed backlog rebuilds history and hides check 4.
