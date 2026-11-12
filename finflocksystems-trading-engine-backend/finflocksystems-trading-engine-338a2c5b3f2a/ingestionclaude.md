# ingestionclaude.md — Nightly NFO ingestion on Kestra

> Brief for Claude Code. Analysis and plan are **done and approved**; the build is **done except the
> items in section 13** (they need the user: Dropbox + SMTP credentials, the trading stack running, the
> manual backlog finished). Sections 1-12 are the original decisions and findings, section 13 is the
> as-built state and what deviated from them, section 14 is the frontend requirement. **Section 16 (2026-11-03) is the
> authoritative decision on how the ingestion reaches IBKR (via the Backend relay) and replaces the conflicting parts of 1-15,
> which are marked `[SUPERSEDED]`.** `kestra/README.md` and `kestra/prod/DEPLOY.md` are the operating manuals. Re-read section 2 before touching anything. Last updated 2026-11-03.

> **FINAL (2026-11-03 evening): section 17 supersedes the separate-container / Kestra / QCH design in sections 4-16.** The ingestion runs INSIDE the hosted Backend, using only this repo. Read section 17 first.

## 0. Goal

Every trading day the data provider (Global Data Feeds / GDF) uploads
`GFDLNFO_CONTRACT_DDMMYYYY.zip` to a Dropbox shared folder, normally **20:00–21:00 IST**
(not guaranteed). Until now the user downloaded it and ran this by hand:

```bat
cd /d "C:\Users\Shree Harish V\Desktop\Options terminal finflock\backend\options-dashboard"
.venv\Scripts\python.exe manage.py ingest_nfo_index_options ^
  --input-dir "pipeline\NFO(Contractwise)_03.09.2025 to 03.09.2027_1 Min Data" ^
  --resume --copy-workers 4 ^
  --scratch-dir "D:\nfo_ingestion_scratch" --max-local-scratch-gb 2
```

Automate it with a self-hosted **Kestra** that runs **Mon–Fri at 22:00 IST, unattended, forever**:

```
download new zip(s) from Dropbox -> ingest (IBKR spot enrich, load to remote TimescaleDB) -> verify -> email on failure
```

Kestra is only scheduler and orchestrator. All ingestion logic is the existing `nfo_pipeline` code.

## 1. Decisions already made (do not re-ask)

| Topic | Decision |
|---|---|
| Start | ~~First scheduled run Wed 2026-11-04; sessions >= 2026-11-04~~ **[SUPERSEDED by 16.10]** `MIN_SESSION_DATE=2026-11-03`: the backlog is loaded through 2 Nov and the 3 Nov file is the first one the nightly run waits for. Earlier history stays manual (see 10.3). The 22:00 schedule is OFF (`SCHEDULE_ENABLED=false`) until gate G5. |
| Schedule | cron `0 22 * * 1-5`, timezone `Asia/Kolkata`. Poll Dropbox until **01:30 IST** if the file is late. |
| Hosting | Kestra runs as its **own Docker Compose project `finflock-ingestion`** (Kestra + its Postgres), `restart: unless-stopped`, pinned image version. It is NOT added to the trading stack's compose file. |
| Worker | Each run starts a short-lived container `nfo-ingest:local` through Kestra's Docker task runner, attached to the trading stack's network so it can reach the IBKR gateway. |
| NetBird | **Option A** (use the host's NetBird). Verified: a Docker Desktop container reaches the DB through the host's NetBird. No NetBird sidecar. `skip_netbird_check=True` in the worker (the `netbird` binary isn't in the container); the pipeline's own TCP+auth preflight to the DB still runs. |
| Notifications | **Email** on failure. The SMTP sender/credentials will be supplied by the user later; build it against Kestra secrets (section 8.4). |
| Session cap | ~~Max 5 sessions per run~~ **[SUPERSEDED by 16.4]** hard cap **2 per night** in gateway mode (5 history requests each, on the strategy's connection). |
| Old ingestion code | The previous runner/API/UI design was **deleted on 2026-11-03** (section 3). Don't recreate it. |

## 2. Hard constraints (do not break)

- **Do not modify** `nfo_pipeline/*`, the options-dashboard Django app, the strategy code, the IBKR gateway
  (`gateway/IB_gateway`) or `docker-compose.options.yml`. No refactors or renames. If the pipeline needs a change, stop and ask.
- **Do not touch the running IBKR session.** No restart, no new login, no settings change from code.
  The ingestion is one extra **read-only** API client (`ReadOnlyIBKRClient`: only `reqHistoricalData` /
  `cancelHistoricalData`, API surface audited at preflight). Keep that.
- **Do not weaken DB safety:** batch ledger `nfo_ingestion_batch`, staging, transactional promotion,
  post-promotion verification stay as they are.
- **No secrets in git, flow YAML, image, or logs.** Kestra secrets / env vars only.
- **Never delete data, ledger rows, or scratch files** to "fix" a failed night. Retry or resume only.
- The manual command in section 0 must keep working unchanged.
- Additive only: new files under `trading-engine/kestra/`. Don't commit unless the user asks.

## 3. What was removed (2026-11-03, uncommitted at time of writing)

The old design (a long-running "ingestion runner" agent polling a backend API, plus a UI card) was deleted:

- Backend `trading-engine`: `ingestion_runner/` (incl. its vendored copy of `nfo_pipeline`), `apps/ingestion/`,
  `tests/ingestion/`, `docs/ingestion_agent_contract.md`, `apps.ingestion` in `INSTALLED_APPS`,
  the `ingestion/` URL, the `INGESTION_*` settings and `.env.example` block, and the `.gitignore`/`.dockerignore` lines.
- Frontend `trading-engine`: `src/features/system/IngestionCard.tsx`, its test, its queries and types, and its use in `SystemPage.tsx`.
- After removal `manage.py check` is clean and `tsc` passes. The DB table from `apps/ingestion` migration 0001 is left
  orphaned in the engine Postgres (harmless; don't drop it without asking).
- **Test failures you will see that aren't caused by this:** frontend 14 failing tests (`app.test.tsx`,
  `BrokerInstrumentSearch.test.tsx`, `intelligence*.test.tsx`) also fail on the original code; backend 20 failing tests in
  `test_full_recommendation_system.py`, `test_research.py`, `test_broker_sessions.py`, `test_finnhub_market_data.py`
  are unrelated to ingestion. Don't try to fix them as part of this work.

## 4. Topology (verified 2026-11-03)  [SUPERSEDED for production: the worker has no IBKR connection or clientId 9241; see 16.3]

```
Windows 11 laptop, Docker Desktop (Linux engine), NetBird 0.78.1 on the host (Connected)
│
├── compose project finflock-options-validation  (trading stack, docker-compose.options.yml — DON'T EDIT)
│     network: finflock-options-validation_default
│     service paper-ibkr-gateway  (container finflock-options-validation-paper-ibkr-gateway-1)
│        IB Gateway + IBC + broker worker; IBC_TRADING_MODE=paper -> API port 4002 (live would be 4001)
│        strategy's clientId = 91 (code default 17).  Ingestion uses 9241 -> no clash.
│        IBC: AcceptIncomingConnectionAction=accept (no popup), ExistingSessionDetectedAction=primary
│        IBC AutoRestartTime=11:45 PM container time; container TZ is unset = UTC -> 05:15 IST. Outside our window.
│        only 127.0.0.1:18082->8080 (HTTP) is published; 4002 is NOT published to the host.
│     backend, postgres, redis, kafka, flink...
│
├── compose project finflock-ingestion  (NEW — this work)
│     kestra + kestra-postgres, restart: unless-stopped
│     each run: container nfo-ingest:local on network finflock-options-validation_default
│              -> paper-ibkr-gateway:4002 (clientId 9241)  and  -> TimescaleDB via host NetBird
│
└── old, stopped: compose project "kestra" (kestra/kestra:latest, volumes kestra_kestra-data, kestra_postgres-data).
      Leave it and its volumes alone; don't reuse or delete them.
```

- **Remote DB:** `TIMESCALE_DB_URL` (in options-dashboard `.env`) points at `deploydatahub1.reminternals.com:30017`,
  which resolves to `100.92.3.152` (NetBird range). Public DNS returns NXDOMAIN: **the DB is NetBird-only.**
  From a container both the IP and the hostname reach port 30017 (tested with `nc` from `postgres:16-alpine`).
- **At the time of writing the trading stack is stopped** (gateway exited 24 days ago). It must be up with IB
  Gateway logged in before the first run, otherwise the run fails closed (exit 20) and emails.

## 5. The code being orchestrated

Repo: `C:\Users\Shree Harish V\Desktop\Options terminal finflock\backend\options-dashboard` (has `.git`, `.venv` Python 3.10.11).

- `nfo_pipeline/service.py` (no Django needed): `precheck_database`, `latest_db_session`, `local_sessions`,
  `plan_missing`, `validate_session`, `check_disk_space`, `build_options`, `run_ingestion`, `verify_session`,
  `IngestError(code)` with codes `db_unreachable, no_data, download_failed, validation_failed, disk_space,
  ingestion_failed, verification_failed`.
- `nfo_pipeline/acquire.py`: `get_acquirer("dropbox_api").acquire(input_dir, latest_db, have)` downloads files whose
  session is `> latest_db` and not in `have`. `.part` + atomic rename, size check, restores provider mtime
  (so the runner's 120 s stability window passes). Env: `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`,
  `DROPBOX_REFRESH_TOKEN`, `GDF_DROPBOX_SHARED_LINK`.
- `nfo_pipeline/config.py`: `IngestionOptions` already has `tws_host/tws_port/tws_client_id/skip_netbird_check/
  scratch_dir/copy_workers/max_local_scratch_gb/resume`. `database_url()` calls `load_dotenv(ENV_FILE or ROOT/.env,
  override=False)`. **Set `ENV_FILE=/nonexistent` in the worker** so the repo's `.env` is never read inside the container
  and only Kestra-provided env vars are used.
- `runner.py`: `_code_revision()` runs `git rev-parse HEAD` (falls back to `UNVERSIONED`; safe because the ledger's
  same-source check skips COMPLETE sessions). Install `git` in the image and set `safe.directory=/app` so it resolves.
  `_ensure_single_scratch` raises if a scratch Parquet from another batch exists → dedicated scratch volume.
- Existing options-dashboard entry points `manage.py ingest_latest_nfo`, `manage.py nfo_ingest_scheduler`
  and the "Ingest Latest Data" button use `trading/nfo_ingest_jobs.py`. **Not reused**: they can't pass
  tws host/port/clientId or `skip_netbird_check`, have no session cap, no wait loop, only exit 0/1, and need Django.
  The new worker mirrors `nfo_ingest_jobs._execute` but calls `service` directly. Leave those files untouched.
- Pinned versions from the working venv: `ibapi==9.81.1.post1 pandas==2.3.3 numpy==2.2.6 pyarrow==25.0.1
  scipy==1.15.3 psycopg[binary]==3.3.6 python-dotenv==1.2.4 requests==2.34.2`. Base image `python:3.10-slim`.
- Data: daily zips are ~10–13 MB. Local data folder holds 41 zips, newest `15-10-2026`.

## 6. IBKR coexistence rules  [SUPERSEDED for production by 16.2-16.4: the history calls ride the strategy's own connection (clientId 91) through the gateway's HTTP endpoint, only 16:30-08:30 IST. The clientId 9241 rules below apply to the local `SPOT_SOURCE=tws` test mode and to the manual command.]

- clientId **9241**, documented, never reused. Never run the manual command, `ingest_latest_nfo`,
  `nfo_ingest_scheduler` or the UI button while Kestra is enabled: they also use 9241 and would kick each other off.
- Connect to the already-running gateway. Never start another gateway or log in a second session.
- Pacing: 5 `reqHistoricalData` per session, cap 5 sessions/run.
- Fail closed: gateway unreachable or handshake timeout → exit 20, email, no retry loop against the gateway.
- **Risk to verify first (acceptance step 1):** the worker reaches the gateway at `paper-ibkr-gateway:4002` from another
  container. If IB Gateway only accepts localhost API connections, the handshake will be refused. **Fallback:** run the
  task container with `networkMode: container:finflock-options-validation-paper-ibkr-gateway-1` and `TWS_HOST=127.0.0.1`
  (shares the gateway's network namespace only; changes nothing in the gateway). Pick whichever passes step 1 and document it.

## 7. Worker: `kestra/run_nightly.py`  [SUPERSEDED in part: 16.3 gives the production order (spot validated before download); exit code 50 added; cap 2; gateway probe replaced by the relay]

COPY it into the image at `/opt/worker/run_nightly.py`; the options-dashboard repo is mounted read-only at `/app`
(`PYTHONPATH=/app`). Flow:

```
FLOOR = MIN_SESSION_DATE (2026-11-04)
1. service.precheck_database()                               -> IngestError -> exit 20
2. TCP probe TWS_HOST:TWS_PORT (open only, no API login)      -> fail -> exit 20
3. latest = service.latest_db_session()
4. acquire_floor = max(latest, FLOOR - 1 day)                 # never download/ingest the pre-floor backlog
5. loop until RUN_DEADLINE_IST (01:30, next day):
       get_acquirer("dropbox_api").acquire(INPUT_DIR, acquire_floor, set(local_sessions(INPUT_DIR)))
       candidates = [d for d in plan_missing(local_sessions(INPUT_DIR), latest) if d >= FLOOR]
       if candidates: break
       sleep POLL_MINUTES (20)
   at deadline with nothing new -> log WARN "no new file" -> exit 10
6. sessions = candidates[:MAX_SESSIONS_PER_RUN]
7. for each: service.validate_session(local[d])               -> exit 30
   service.check_disk_space(SCRATCH_DIR, MAX_SCRATCH_GB)       -> exit 20
8. for each session d (oldest first, one at a time, like nfo_ingest_jobs):
       service.run_ingestion(service.build_options(
           input_dir=INPUT_DIR, scratch_dir=SCRATCH_DIR, session=str(d),
           resume=True, copy_workers=4, max_local_scratch_gb=2,
           tws_host=TWS_HOST, tws_port=TWS_PORT, tws_client_id=9241,
           skip_netbird_check=True))
       service.verify_session(d)                                -> exit 40
       stop at the first failure; report done / failed / not attempted
9. print ONE JSON summary line: sessions done, rows per session, durations, error code if any
```

- `--session YYYY-MM-DD` (manual, bypasses the floor and the wait loop; one session), `--dry-run`
  (steps 1, 3, Dropbox listing, plan; **no downloads, no DB writes, no IBKR connection**), `--max-sessions N`.
- Exit codes: `0` done or nothing to do, `10` no new file by deadline, `20` infra (DB/NetBird, gateway, Dropbox,
  disk, `no_data`, or `ingestion_failed` caused by a non-`ValueError`), `30` data quality (`validation_failed`, or
  ingestion failed with `ValueError`/`AssertionError` = ledger `FAILED_DATA_QUALITY`), `40` `verification_failed`.
  Always log `IngestError.code`. Never print secrets or the DB URL.
- Config from env: `INPUT_DIR=/data/pipeline/NFO(Contractwise)_03.09.2025 to 03.09.2027_1 Min Data`,
  `SCRATCH_DIR=/data/scratch`, `TWS_HOST`, `TWS_PORT=4002`, `TWS_CLIENT_ID=9241`, `MIN_SESSION_DATE=2026-11-04`,
  `MAX_SESSIONS_PER_RUN=5`, `RUN_DEADLINE_IST=01:30`, `POLL_MINUTES=20`, `ENV_FILE=/nonexistent`, plus secrets.

## 8. Files to add (all under `trading-engine/kestra/`)  [the laptop compose parts are a test harness only; production is kestra/prod/, see 15 and 16]

```
kestra/
  docker-compose.yml          # project name finflock-ingestion: kestra (pinned) + postgres, restart: unless-stopped
  .env.example                # non-secret settings + names of the secrets; real .env gitignored
  Dockerfile                  # nfo-ingest:local — python:3.10-slim, git, pinned deps, COPY run_nightly.py
  requirements.txt            # the pins from section 5
  run_nightly.py              # section 7
  flows/nfo_nightly_ingest.yml
  tests/test_run_nightly.py   # mock service.* and the acquirer; cover floor, cap, wait loop, exit codes
  README.md                   # setup, secrets, manual trigger, disabling, rollback, what NOT to run concurrently
```

Add `kestra/.env` and any secrets file to `.gitignore`.

### 8.1 Kestra compose

- Pin a Kestra version (check what `kestra/kestra:latest` locally is, then pin a specific tag; verify config keys against
  that version's docs). Kestra UI bound to `127.0.0.1` only, on a free port (18090 suggested; 18000/18081/18082 are taken).
- Mount `/var/run/docker.sock` and a shared working dir (`/tmp/kestra-wd`) as Kestra's Docker runner docs require.
- Enable volumes for the Docker task runner (in `KESTRA_CONFIGURATION`: plugin configuration for
  `io.kestra.plugin.scripts.runner.docker.Docker` with `volume-enabled: true`). Enable basic auth if the version requires it.
- New named volumes (don't reuse the old `kestra_*` ones). New volume `nfo_scratch` for the worker.
- Secrets: Kestra OSS reads `SECRET_<NAME>` env vars, **base64-encoded**, via an `env_file` that's gitignored. Document how
  to generate it. Host paths for the flow come in as `ENV_*` env vars on the Kestra container (`{{ envs.* }}` in the flow):
  `ENV_NFO_REPO_HOST_PATH` (options-dashboard repo) and `ENV_NFO_PIPELINE_HOST_PATH` (its `pipeline` dir). Paths contain
  spaces; quote and test them.

### 8.2 Flow `finflock.ingestion / nfo_nightly_ingest`

- Trigger `io.kestra.plugin.core.trigger.Schedule`, `cron: "0 22 * * 1-5"`, `timezone: Asia/Kolkata`.
  Make sure it can't fire before 2026-11-04 (enable it on 2026-11-04, or use the trigger's start-date/condition support if the version has one).
- `concurrency: {limit: 1, behavior: FAIL}`.
- Inputs: `session` (STRING, optional), `dry_run` (BOOLEAN, default false), `max_sessions` (INT, default 5).
- One task: `io.kestra.plugin.scripts.shell.Commands` with `taskRunner` type `...runner.docker.Docker`,
  `image: nfo-ingest:local`, `pullPolicy: NEVER` (or IF_NOT_PRESENT), `networkMode` per section 6, volumes:
  `<repo>:/app:ro`, `<pipeline-dir>:/data/pipeline`, `nfo_scratch:/data/scratch`.
  Task timeout ~5 h (22:00 → deadline + ingest). Retries: 0.
- Exit 10 ("no new file", e.g. exchange holiday) should end the execution as **WARNING, not FAILED**, and not send the failure
  email. Verify the mechanism against the installed Kestra version; if it's not available, map 10 → success and send a separate
  low-priority "no file today" email.

### 8.3 Env passed to the task

Secrets: `TIMESCALE_DB_URL`, `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`, `DROPBOX_REFRESH_TOKEN`, `GDF_DROPBOX_SHARED_LINK`
(values currently in the options-dashboard `.env`; the user copies them in, you never print them).
Non-secret: everything else in section 7.

### 8.4 Email

Flow-level `errors:` block with Kestra's mail task (`io.kestra.plugin.notifications.mail.MailSend`; confirm the type name
for the pinned version). Subject `NFO ingest FAILED <date> exit=<code>`; body: failing step, session date(s),
`IngestError.code`, a link to the execution. SMTP host/port/username/password/from/to come from secrets `SMTP_*`.
**The user will provide the sender account later**: build and document it, leave the secrets empty, and tell the user exactly
which secrets to add. The ingestion must still run without them (only the notification would fail).

## 9. Implementation order

1. `kestra/Dockerfile` + `requirements.txt` → build `nfo-ingest:local`.
2. `run_nightly.py` + `tests/test_run_nightly.py` → tests pass without network, ibapi or Kestra.
3. `docker-compose.yml`, `.env.example`, flow, README → `docker compose -p finflock-ingestion up -d`.
4. Run the section 10 acceptance in order. Stop and report after each failure; never work around a safety check.

## 10. Acceptance (do not skip)  [SUPERSEDED by the gates G0-G5 in 16.9]

1. **Container checks** (from a container on the chosen network): DB TCP reachable; gateway TCP port reachable;
   `python -c "import ibapi, pandas, pyarrow, psycopg, nfo_pipeline.service"` works; `git rev-parse HEAD` works in `/app`.
   Decide `paper-ibkr-gateway` vs `container:` networking here (section 6). Requires the trading stack running.
2. **Dry run from Kestra** (`dry_run=true`): shows latest DB session, Dropbox listing, planned sessions (none < 2026-11-04).
   No DB writes, no IBKR connection.
3. **First real run = the scheduled 2026-11-04 22:00 IST run**, with the user watching the strategy's IBKR connection
   (clientId 91) stay up. Confirm `verify_session` passed and the ledger row for 2026-11-04 is COMPLETE.
4. Manually re-run with `session=2026-11-04` → skipped by the ledger, no new rows.
5. Check the first two nights' outcomes and emails before relying on it.

### 10.3 Backlog note for the user (tell them; don't act on it)

Sessions between the DB's latest COMPLETE session and 2026-11-03 (local files stop at 15-10-2026) aren't Kestra's job.
The rolling features (`iv_rank_252`, `iv_percentile_252`, `realized_volatility_20`) for each session are computed from the
sessions already in the DB **at ingest time**. If the backlog is loaded after November sessions, those November rows keep
features computed without the gap. Best: the user runs the backlog manually **before 2026-11-04 22:00**, or accepts slightly
degraded rolling features on the first November sessions. Manual runs must never overlap Kestra's window (22:00–01:30).

## 11. Failure modes

| Situation | Behavior |
|---|---|
| No file by 01:30 (holiday or late upload) | exit 10, WARNING; the next run picks it up |
| DB unreachable / NetBird down | exit 20, email, nothing written (preflight first) |
| Trading stack down / gateway handshake fails | exit 20, email, no retries; strategy untouched |
| Bad zip, header or date mismatch | exit 30 from `validate_session`, ledger untouched, email |
| Crash mid-session | ledger `FAILED_RETRYABLE`; next run resumes from retained scratch (`resume=True`) |
| Leftover scratch from a different batch | `_ensure_single_scratch` raises → email; never auto-delete |
| Overlap with another run | Kestra concurrency limit; don't run manual/other ingestion paths during the window |
| Laptop asleep/off at 22:00 | run is missed; the next run catches up (still capped at 5) |
| Disk low | `check_disk_space` fails before ingest → exit 20, email |

Rollback: disable the trigger in Kestra UI (or `docker compose -p finflock-ingestion down`). The manual command keeps working.

## 12. Still open

- SMTP sender and recipient for alerts (user will provide).
- Moving to a server later: copy `kestra/` and the trading stack; the server needs its own NetBird peer (DB is NetBird-only),
  and the host paths in `kestra/.env` change.
- Security: `trading-engine/.env.example` is committed with what look like real credentials (Timescale DB URL with password,
  IB Gateway Postgres password). Recommend rotating and replacing them with placeholders. Ask before changing.

---

## 13. As built (2026-11-03) and what remains  [partly SUPERSEDED: items about starting a local gateway and the 4 Nov start no longer apply; see 15 and 16]

### Done and verified
- `kestra/` (all new files): `Dockerfile`, `requirements.txt` (pins from section 5), `run_nightly.py`, `docker-compose.yml`
  (project `finflock-ingestion`, Kestra pinned to **v2.0.8**, UI `127.0.0.1:18090`, `restart: unless-stopped`),
  `flows/nfo_nightly_ingest.yml`, `.env.example`, `secrets.plain.env.example`, `make_secrets.py`,
  `dropbox_refresh_token.py`, `README.md`, `tests/test_run_nightly.py` (33 tests, all pass; mutation-checked).
  `.gitignore` ignores `kestra/.env`, `kestra/.env.secrets`, `kestra/secrets.plain.env`; `.dockerignore` excludes `kestra/`.
- Running on this machine: Kestra + its Postgres (healthy), image `nfo-ingest:local` built, flow **imported**, trigger
  `0 22 * * 1-5` Asia/Kolkata active (next evaluation 2026-11-03 22:00 IST = a deliberate no-op, see below).
- Verified through Kestra itself: Docker task runner with the Windows host paths, `/app` read-only mount, network
  `finflock-options-validation_default`, secrets, `--dry-run` reads the live ledger over NetBird, `git rev-parse HEAD` works in the
  mount, a forced failure ends `FAILED` with exit 20 `gateway_unreachable` and the failure e-mail renders (error code, exit code,
  session, step, reason, execution link) - tested against a throwaway local SMTP catcher, since removed.
- Restart persistence: the flow and trigger survive `docker compose restart kestra`.

### Deviations from sections 7-8 (the as-built behaviour wins)
- Flow input type is `BOOL` (Kestra 2.x rejects `BOOLEAN`). Mail task is `io.kestra.plugin.email.MailSend` (the
  `notifications.mail` one is deprecated in 2.x). Execution link uses `{{ kestra.url }}` (`kestraUrl()` does not exist in 2.0.8).
  The task uses `containerImage:` plus `taskRunner: ...runner.docker.Docker` (`pullPolicy: NEVER`).
- Worker additions beyond section 7: (1) **start-date guard** - a scheduled/real run before `MIN_SESSION_DATE` exits 0 without
  touching the DB (so the 2026-11-03 22:00 run is a no-op); (2) **daytime rule** - a run started between 01:30 and 19:00 IST
  checks once and never waits, so a catch-up run after the laptop wakes cannot hold the lock into the 22:00 run; (3) **gap guard** -
  first new session more than `MAX_GAP_DAYS` (5) after the DB's latest -> exit 20 `backlog_gap`, nothing ingested; weekdays with no data
  inside the allowed gap are listed in the summary/log (`weekdays_without_data`) as holiday-or-never-loaded; (4) up to 3 consecutive
  Dropbox failures are tolerated while waiting; (5) `--no-file-exit-code` (flow passes 0, so "no file by 01:30" is a logged WARNING
  and no e-mail; the app panel shows Behind); (6) summary line `NFO_INGEST_SUMMARY {json}` and a Kestra `::{"outputs":...}::` line
  (state, code, exit_code, failed_session, message) used by the e-mail.
- `schedule recoverMissedSchedules: LAST` (after downtime run once, don't replay every missed night).
- `ENV_FILE=/nonexistent` is baked into the image so the repo's `.env` is never read in the container.

### Still needed from the user (nothing below can be done without them)
1. **Dropbox**: the options-dashboard `.env` holds only `TIMESCALE_DB_URL`; there are no Dropbox credentials anywhere on this
   machine usable by `DropboxAcquirer` (needs app key, app secret, a **refresh token**, shared-folder link; the old Kestra container
   only had a short-lived `sl.` access token). `kestra/.env.secrets` currently holds clearly fake `PLACEHOLDER-NOT-SET` values so tonight's
   no-op renders; replace them via `secrets.plain.env` + `make_secrets.py`, then `docker compose up -d --force-recreate kestra`.
   **Until then the 2026-11-04 run fails with `download_failed`.**
2. **SMTP** sender + recipient (`SMTP_*`, `ALERT_TO`). Until set, a failed run cannot send its alert (the run still fails correctly
   and is visible in the Kestra UI and the app panel).
3. **Start the trading stack** (IB Gateway logged in). Deliberately not started by Claude: IBC has `ExistingSessionDetectedAction=primary`,
   so a login could take over an IBKR session running elsewhere. Acceptance step 1 for the gateway part is pending: confirm
   `paper-ibkr-gateway:4002` accepts the API handshake from the worker container (the worker's port probe is only a TCP check). If the
   gateway only accepts localhost API connections, use the fallback in section 6 (`networkMode: container:<gateway>` and `TWS_HOST=127.0.0.1`).
4. The user's **manual backlog load** (in progress; DB latest was 2026-10-26 at last check) must reach **2026-11-03** before the
   2026-11-04 22:00 run, otherwise the run refuses with `backlog_gap`.
5. Acceptance steps 3-5 of section 10 (first real run watched, ledger re-run skip, two nights of review).

### Operational facts worth remembering
- Do not run `manage.py ingest_nfo_index_options`, `ingest_latest_nfo`, `nfo_ingest_scheduler` or the UI button 22:00-01:30 IST: same clientId 9241.
- The worker executes the `options-dashboard` working tree, including uncommitted changes (it had uncommitted edits to
  `ingest_nfo_index_options.py`, `settings.py`, `models.py` on 2026-11-03).
- The laptop Kestra (compose project `finflock-ingestion`, `kestra/docker-compose.yml`) was only a local test harness and is **stopped**.
  Production is the self-contained container in `kestra/prod/` (section 15). Nothing may depend on a laptop being on.
- Secrets leaked into this session's tool output while inspecting the old stopped `kestra-kestra-1` container (its env contained a Dropbox
  app secret, a short-lived Dropbox token and a Marketaux key). Recommend rotating those; nothing was copied or reused.

## 14. Frontend: show that data was ingested (done)

Requirement: the app must show whether the nightly ingestion happened.

- Source of truth is the ingestion's own ledger `nfo_ingestion_batch` in the remote DB; Kestra does not call the backend and there is no
  runner API (the old design stays deleted). The backend only **reads** it, with the same read-only/autocommit connection style as
  `apps/market_data/remote_features.py`, using the existing `REMOTE_FEATURE_DB_URL` (in production the backend host therefore needs NetBird
  to the DB, as it already does for RPS features).
- Backend: `apps/market_data/ingestion_status.py`, view `ingestion_status` in `apps/market_data/views.py`, route
  `GET /api/v1/data-providers/nfo-ingestion/`, setting `NFO_LEDGER_PROFILE` (default = `nfo_pipeline.runner.CALCULATION_PROFILE`; keep in sync if
  that constant changes). Returns `state` = `UP_TO_DATE | BEHIND | FAILED | EMPTY | UNAVAILABLE`, latest session, rows, completed_at,
  newer failure (session, error category), and the last 10 sessions. `expected_session` = newest weekday whose run window (22:00 + 3.5 h) has
  closed; there is no exchange calendar, so a holiday shows "Behind" with wording that says to check for a holiday. An unreadable DB returns
  200 with `available: false` and a message that never contains the URL or credentials. Tests: `tests/test_nfo_ingestion_status.py` (9).
- Frontend: `src/features/system/NfoIngestionPanel.tsx` ("Options data ingestion" panel on the System page, polls every 60 s), types
  `NfoIngestionStatus`, query `queries.nfoIngestion()`. Tests: `tests/nfoIngestionPanel.test.tsx` (4). The component tolerates malformed responses.
- Verified against the real ledger (read-only) over NetBird. Known unrelated failing tests: frontend 14 (also fail on the original code),
  backend 20 (research/broker/recommendation/finnhub tests).

## 15. Production hosting - findings 2026-11-03  [SUPERSEDED by section 17: no separate container, no Docker Hub, no QCH container]

The user requires everything in production, with Kestra hosted as a sub-container, nothing on a laptop. How production works:
the Backend is one container on the QFS platform; IB Gateway runs as **child containers created through QCH**
(`apps/broker_gateway/qch.py`: `POST {QCH_API_HOST}/api/apps/{QCH_APP_ID}/containers`, bearer `QCH_SERVICE_TOKEN`, body
`{name, image, command?, env?, network?}`; images only from `docker.io`, production by `@sha256:` digest; the Backend itself has no Docker
socket). **A QCH child has no volumes, ports or Docker socket**, so the laptop design (Docker-socket task runner, host mounts, compose) cannot be
used there.

### Built (verified locally by running it the way QCH would: env vars only, no mounts, no socket)
- `kestra/prod/`: `Dockerfile` (Kestra v2.0.8 slim base + only the shell and email plugins + standalone Python 3.10 venv with the pinned
  deps + `nfo_pipeline` copied unchanged + worker + flow; 1.5 GB image, ~0.8-1.3 GB RAM), `start.sh` (renders Kestra config from env,
  `server local` = embedded DB, flows from the image), `git-shim.sh` (answers only `git rev-parse HEAD` from build arg `NFO_CODE_REVISION`, so
  the ledger's code_revision stays meaningful without a repo), `flows/nfo_nightly_ingest.yml` (Process runner, no containers), `build_image.py`
  (assembles the context from the options-dashboard working tree, tags `-dirty` and warns if `nfo_pipeline` is uncommitted, `--push` only on request),
  `DEPLOY.md` (exact QCH payload, secrets, verify, update/rollback).
- Proven in the QCH-style run: boots in ~15 s, flow + 22:00 IST trigger load from the image, a dry run reads the live ledger over NetBird,
  a forced failure exits 20 `gateway_unreachable` and the alert e-mail renders (code, exit code, session, step, reason). The worker also creates
  its own work folders (fresh container). `make_secrets.py --qch-json` emits the QCH `env` entries.
- State is ephemeral by design (the ledger is the record; the app panel reads it).

### ~~BLOCKER~~ RESOLVED 2026-11-03: the worker cannot reach the IBKR API from a sibling container. The user chose Option 2 (see section 16)
The gateway image publishes only HTTP 8080 (nginx), and IBC's generated config leaves `TrustedTwsApiClientIPs` empty, so IB Gateway accepts API clients
from localhost only. `nfo_pipeline` talks to the TWS API directly (`ReadOnlyIBKRClient`). Options:
1. **Gateway change (recommended, cleanest):** the gateway owner adds an internal listener/trusted client (set `TrustedTwsApiClientIPs` or run a
   socat forwarder on the private network, and publish the API port only to the platform network). Pipeline untouched; read-only clientId 9241 on its own
   connection. Needs a new gateway image digest and a restart of the session, so do it after the strategy week.
2. **HTTP spot adapter, no gateway change:** the gateway has `POST market-data/history/` (queued command, bearer = per-session `GATEWAY_SERVICE_TOKEN`).
   An adapter injected by the worker (pipeline files untouched) could replace `ReadOnlyIBKRClient`. Costs: the 5 requests/night run on the strategy's own
   IBKR connection and command queue (contention), the per-session token and dynamic child name must reach the worker (needs a Backend endpoint or secret
   plumbing), and the gateway's `end_time` handling for IND 1-minute bars would need testing. Needs explicit approval because it changes how spot is fetched.
3. A separate paper IBKR user with its own gateway: the image has the same localhost-only limitation, so it does not solve it alone.
Until one is chosen the nightly run stops at `gateway_unreachable` (exit 20, e-mail) before any download or write. Do not decide this unilaterally.

### Also needed before go-live
Commit `nfo_pipeline/acquire.py` and `service.py` in options-dashboard (they are untracked); Docker Hub repo + push; QCH access to create the child (the
Backend holds the token; Claude has none); Dropbox and SMTP secrets; confirm QCH resource limits (~1 GB RAM) and that a child can reach the NetBird DB and Dropbox.

---

## 16. DECISION (2026-11-03): spot bars via the gateway HTTP history endpoint, "Option 2"  [still valid for the spot path and guardrails; the relay/Kestra/container parts are SUPERSEDED by section 17]

This section supersedes every earlier statement about how the ingestion reaches IBKR in production (sections 1, 4, 6, 7, 8,
10, 13 and the blocker in 15 carry a `[SUPERSEDED]` note pointing here). Everything else in 1-15 still holds.

### 16.1 Decision and why
- **Option 1 (expose the TWS API from the gateway image) is out for now**: it needs a new gateway image and a restart of the live
  gateway session, and the gateway must not restart or disconnect while the strategy runs. Revisit later (16.8).
- **Option 2**: the nightly worker never opens an IBKR API connection. It asks the Backend's read-only relay, which asks the gateway child's
  HTTP `market-data/history/` endpoint. No gateway change, no restart, `nfo_pipeline` untouched.
- The strategy trades 09:00-16:00 IST; the ingestion runs 22:00 IST, so they do not overlap. The relay and the worker both refuse to run
  outside the **idle window 16:30-08:30 IST**.

### 16.2 Verified facts behind it (read-only checks, 2026-11-03)
- **Auth:** `POST /api/v1/market-data/history/` with `Authorization: Bearer <GATEWAY_SERVICE_TOKEN>` and an `Idempotency-Key`; returns a queued command,
  poll `GET /api/v1/commands/<id>/`. Body: `conid, symbol, exchange, currency, bar_size, duration, what_to_show, use_rth, end_time`.
- **Token:** the Backend creates it (`secrets.token_urlsafe(48)`) when a broker session is created, stores it encrypted, and injects it once into the child's
  env. It does **not** change on gateway re-login, the daily IBC restart or a re-provision of the same session; it changes only when a NEW session is created
  (which also changes the child's name). The local static paper gateway uses the fixed `LOCAL_PAPER_GATEWAY_SERVICE_TOKEN`.
- **Parameters match the pipeline:** conId, `1 min`, `TRADES`, `use_rth=false`, `1 D`, and the pipeline's exact UTC end time `YYYYMMDD-HH:MM:SS` (the gateway's
  validator accepts it and passes it to TWS untouched; verified offline in the gateway image). Replies carry ISO UTC timestamps (convert to IST, floor to the minute)
  and volume as `max(0, volume)`; the database stores volume 0 for every spot bar, so they agree. Every stored session has exactly **375 bars per index
  (09:15-15:29 IST), source IBKR_TRADES**.
- **Pluggable without editing `nfo_pipeline`:** `runner.py` binds `ReadOnlyIBKRClient` and `audit_api_surface` in its own namespace, so the worker swaps them at runtime
  (`install_spot`). The pipeline only calls `connect_ready`, `resolve_index`, `historical_index`, `close`. It calls `historical_index` from five threads at once, so the
  adapter serializes them.
- **Shared connection (the real risk):** history commands run on the strategy's own IBKR connection (clientId 91) in the gateway's single sequential command loop.
  The gateway's `historical_bars` calls `qualifyContracts` first (a `reqContractDetails`, which the pipeline's own comments say TWS can delay "indefinitely" for these
  NSE indexes) with **no timeout** (`RequestTimeout` defaults to 0), so a stuck call can stall that loop. At 22:00 the strategy is idle and the daily gateway restart
  (05:15 IST) clears a stall before 09:00, but the design treats it as a hazard: NIFTY is a canary, any failure halts further requests, requests are spaced, and G1
  watches for it. The endpoint itself has no rate limit; IBKR pacing applies.
- **Gateway restart / re-login:** IBC `AutoRestartTime` default 11:45 PM container time (UTC container) = **05:15 IST**, outside our window. A weekly IBKR re-login is
  needed; paper re-logs in unattended from stored credentials, live waits at `WAITING_FOR_2FA` for a human via noVNC. The Backend only allows command traffic while the
  session is `CONNECTED` with commands enabled; otherwise the relay answers `GATEWAY_UNAVAILABLE`.

### 16.3 Architecture
```
Kestra container (22:00 IST) --HTTPS, service token--> Backend relay --gateway token (internal)--> paper IB Gateway child --> IBKR
        |                                                      POST /api/v1/data-providers/nfo-ingestion/spot-bars/
        +--> Dropbox (listing + download), remote TimescaleDB (ledger, load)
```
- **Relay** (`apps/market_data/nfo_spot.py`, view `nfo_spot_bars`): the caller sends only `{underlying, session_date, run_id, attempt}`; the Backend builds the exact
  request for the five pinned contracts (kept identical to `nfo_pipeline.ibkr.INDEX_REQUESTS`; the worker re-checks every reply). Disabled unless `NFO_SPOT_PROXY_TOKEN` is set.
- **Adapter** (`kestra/spot_gateway.py`, `GatewaySpotClient`): drop-in for `ReadOnlyIBKRClient`, installed at runtime by `run_nightly.py`.
- **Worker order per session (gateway mode):** window check -> **fetch + validate all five spot series** -> only then **download that one session** (acquire with
  `latest=d-1` and every other known session as `have`) -> validate zip -> ingest (pipeline uses the cached bars; no second request) -> verify. A bad or missing spot
  reply stops the night before anything is downloaded or written.

### 16.4 Guardrails (enforced in code; each has tests, and the key ones were mutation-checked)
| Guardrail | Enforced in |
|---|---|
| Only 16:30-08:30 IST; outside it exit **50** `outside_window`, nothing contacted | relay (403), adapter (every request), worker (start, before the DB), G1 tool |
| Five requests strictly one at a time | adapter lock (the pipeline's 5 threads are serialized) + relay Redis lock (409 `BUSY`) |
| 5-10 s apart | adapter 7 s (`SPOT_SPACING_SECONDS`) + relay minimum gap 5 s (429 `TOO_SOON`) |
| Per-request timeout | adapter (10 s connect, 75 s read) + relay waits at most 45 s for the gateway command (504 `GATEWAY_TIMEOUT`) |
| One retry after 60 s, then stop | adapter retries transient errors once; a second failure halts the adapter, no further request is sent in the run |
| Max 2 sessions a night | worker clamps `MAX_SESSIONS_PER_RUN` to 2 in gateway mode (code, not convention) + relay daily budget of 20 requests |
| Fail closed on empty / wrong-date / wrong-contract bars, before any download or DB write | `validate_bars` (right index and conId, session date, every bar on that IST date, >= 300 bars, sane prices, UTC timestamps with zone) run in `prefetch`; relay returns 502 `WRONG_CONTRACT` |
| Never log the token | header only; scrubbed from messages and logs; tests assert it |
| Also | paper session by default (`NFO_SPOT_GATEWAY_MODE`), optional pin `NFO_SPOT_GATEWAY_SESSION_ID`, mode-mismatch refusal, Redis down = refuse, NIFTY first as canary, no DB writes in the relay, `SCHEDULE_ENABLED` defaults to false |

### 16.5 Runtime token refresh (no human, no rotation problem)
The worker holds **no gateway token**. The relay looks up the current `BrokerGatewaySession` on every request (pinned id, or the single connected PAPER session) and uses
its stored token, so a re-provisioned or re-created session is picked up automatically. The only static secret is the relay token `NFO_SPOT_PROXY_TOKEN`, shared by the
Backend env and Kestra's secret (`python kestra/make_secrets.py --new-spot-token`). To rotate it: change both, then recreate the ingestion container.

### 16.6 Daily operation, late files and the missed-night alert
- Mon-Fri 22:00 IST; the worker waits for the provider's file (Dropbox listing every 20 min) until **01:30 IST**, then ends with a logged warning (exit 10 mapped to 0).
  A file that is still missing is simply picked up by the next night's run, which catches up (**at most 2 sessions a night**, so a backlog of N sessions takes N/2 nights).
- **Missed-night alert** (`apps/market_data/ingestion_alert.py`, Celery beat 08:00 and 12:00 IST): e-mails `NFO_FRESHNESS_ALERT_TO` when a trading day passes with no
  COMPLETE session, once per missed day (deduplicated through the audit table, no migration). It runs in the Backend **on purpose, outside the ingestion container**: a check
  inside Kestra would die with Kestra. Exchange holidays go in `NFO_HOLIDAYS`. It also alerts once a day if the ledger cannot be read. Needs `EMAIL_*` settings. The System-page
  panel shows the same state (it now honours `NFO_HOLIDAYS`).
- Failure e-mails from Kestra itself (exit 20/30/40/50) need the SMTP secrets.

### 16.7 Weekly gateway re-login
Paper: automatic. Live: a human completes 2FA through noVNC. While the session is not `CONNECTED` the relay answers `GATEWAY_UNAVAILABLE`, the worker ends with exit 20
`spot_unavailable` (nothing downloaded or written), the failure e-mail goes out, and the next night retries. The daily 05:15 IST restart is outside the window and does
not change the token. If the weekly login lapses over a weekend, Monday's run catches up.

### 16.8 Option 1 later
Do it in a quiet window such as a weekend, after the Option 2 path has run cleanly for a while: have the gateway owner allow an internal API client (`TrustedTwsApiClientIPs`
or a private forwarder), publish the API port to the platform network only, roll a new gateway digest and restart the session. Then the worker can use `SPOT_SOURCE=tws`
(its own read-only clientId, no shared command queue, no relay). Keep the relay as a fallback or remove it. Not needed for go-live.

### 16.9 Gates (each must pass before the next)
| Gate | What | Status 2026-11-03 |
|---|---|---|
| G0 | commit `nfo_pipeline/acquire.py` and `service.py` (options-dashboard) and this work | committed **locally only**; nothing pushed (a push to the Backend branch may trigger a production deploy: the owner decides) |
| G1 | equivalence, no writes: 2 Nov + one more past session, all 5 indexes, bar by bar vs `index_underlying_candle` (count, timestamps, OHLC within 1e-6, volume). **After 16:30 IST, paper gateway logged in, never in strategy hours.** `python kestra/tools/equivalence_check.py --sessions 2026-11-02,2026-10-30` | **pending**: tool built and tested (refuses before 16:30), gateway not running when checked |
| G2 | dry run in the production container (`dry_run=true`) | rehearsed locally in the QCH-style container; repeat after deploy |
| G3 | one real session by hand in the evening idle window (`session=YYYY-MM-DD`, with `SCHEDULE_ENABLED=false`) | pending |
| G4 | re-run that session; the ledger must skip it | pending |
| G5 | set `ENV_SCHEDULE_ENABLED=true` (recreate the container) and check the first two nights | pending |
A mismatch or any stall at G1 stops everything: report, do not work around it.

### 16.10 Current backlog state
Ledger (read-only check, 2026-11-03 ~14:06 IST): newest COMPLETE session **2026-11-02**; the manual backlog load is finished through it. The **3 Nov zip has not arrived**.
`MIN_SESSION_DATE` is **2026-11-03** (it was 2026-11-04), so the first nightly run waits for the 3 Nov file until 01:30 IST and, if it is still missing, the following run catches up.
The gap guard is unaffected (latest 2 Nov to first new 3 Nov = 1 day).

### 16.11 Automatic hosting as a sub-container, and the Dropbox findings (2026-11-03, afternoon)  [container part SUPERSEDED by 17; Dropbox findings still valid]
- **Automatic creation** (`apps/market_data/nfo_container.py`, command `manage.py nfo_ingestion_container status|ensure|recreate|delete`, beat task every 10 min):
  the Backend, which already holds the QCH credentials, creates `finflock-nfo-ingestion` from `NFO_INGEST_CONTAINER_IMAGE` with the env in `NFO_INGEST_CHILD_ENV_JSON`.
  Creates only when absent; never replaces a healthy container; a dead one is recreated at most once an hour; unsafe image references and an env without the relay
  token / DB URL / UI password are refused; a QCH outage changes nothing. The schedule gate G5 is `NFO_INGEST_SCHEDULE_ENABLED` (then `recreate --yes`). No-op while the image is empty.
- **E-mail is on hold** by the user: no SMTP secrets are set, so Kestra's failure e-mail and the Backend missed-night alert are inactive. Failures still show in the Kestra
  execution log, the exit codes and the System-page panel. Turn on later with `SMTP_*` secrets and `NFO_FRESHNESS_ALERT_TO` + `EMAIL_*`.
- **Dropbox credentials** (given by the user from the old Kestra container's environment) are in the git-ignored `kestra/secrets.plain.env`: app key, app secret, refresh token. The refresh
  token works (token exchange 200). **Two things are still missing:** (1) the Dropbox app (ID 8709329) does **not** have the `sharing.read` scope the pipeline's acquirer needs to list a shared
  folder (Dropbox answered "not permitted ... required scope 'sharing.read'"): enable it in the app console (Permissions), then re-authorize with `kestra/dropbox_refresh_token.py` for a new
  refresh token, because scopes are fixed when a token is issued; (2) the provider's **shared-folder link** (`GDF_DROPBOX_SHARED_LINK`), which is not in any file on this machine.
  The user's own Dropbox holds a stale copy of the data (`/03.09.2027_Kalastri Jagannathan_NFO(Contractwise)_1 Min Data/...`, 29 daily zips, newest 2026-10-13, uploaded in one batch on 14 Oct) and
  the `Apps/Global data feed Ingester` folder is empty, so new daily files come only through the provider's link. The old container's `sl.` access token is short-lived and not usable.
- **IBKR check not done:** the IBKR process running locally is **TWS desktop (tws.exe, port 7497)**, not the gateway container, so the Option 2 HTTP path cannot be exercised against it, and at
  15:11 IST it was strategy hours. G1 needs the local paper gateway container logged in via noVNC after 16:30 IST. Starting it with the same paper credentials would log TWS desktop out
  (one session per IBKR login), so close TWS desktop first or use a different paper login.

### 16.12 Dropbox RESOLVED (2026-11-03, 15:35 IST) - supersedes the Dropbox bullets in 16.11
- New refresh token (scopes account_info.read, files.metadata.read, files.content.read, sharing.read) and the provider's shared-folder link are in the git-ignored
  `kestra/secrets.plain.env` / `.env.secrets` / `qch-env.json`. All five ingestion secrets are now set; only SMTP (on hold) is missing.
- **The pipeline's own `DropboxAcquirer` cannot work against the real folder** (found by running it): Dropbox answers HTTP 400 "Recursive list folder is not supported for shared
  link" to its listing, and entries listed through a shared link have no `path_display`, so its downloads would use the wrong path for nested files. `nfo_pipeline` is NOT edited
  (constraint 2). `kestra/dropbox_shared.py` (`SharedFolderAcquirer`, `GDF_ACQUIRER=dropbox_shared`, now the default) walks the folder level by level and downloads with the right
  paths, keeping the pipeline's rules (only sessions newer than the ledger, `.part` + atomic rename, size check, provider mtime restored, nothing outside input_dir, secrets never in
  errors). 11 tests. Worth reporting to the pipeline owner: the "Ingest Latest Data" feature uses the broken class.
- **Verified live against the real folder:** 42 daily zips, 2026-09-01 .. 2026-11-02; one file downloaded to a temp folder and accepted by the pipeline's own `validate_session`,
  then deleted; the production container's dry run lists Dropbox successfully (nothing newer than the ledger's 2026-11-02 yet).
- **Upload timing:** recent daily files were uploaded about 11:15-11:58 UTC (~16:45-17:30 IST) on the day itself, earlier than the 20:00-21:00 IST assumed; the 3 Nov file had not
  arrived at 15:35 IST. The 22:00 start with polling to 01:30 IST has ample margin.

---

## 17. FINAL ARCHITECTURE (2026-11-03 evening): the ingestion runs INSIDE the hosted Backend - AUTHORITATIVE

**Why:** the user cannot use Docker Hub or a separate container, and wants everything to run once the Backend is hosted, from this repo only (`options-dashboard`
is reference code and is not to be considered). So there is no Kestra, no separate image, no QCH container, no HTTP relay and no relay token.

### 17.1 What runs where
```
Hosted Backend container (built by QFS from this repo, supervisor)
 ├─ program nfo-ingest-scheduler   manage.py nfo_ingest_scheduler: idles unless NFO_INGEST_SCHEDULE_ENABLED=true
 │     Mon-Fri 22:00 IST (or back up before 01:30) -> starts, as a SEPARATE low-priority process:
 └─ python -m nfo_ingestion.run_nightly     (nice 10, own process: a crash/hang/memory spike cannot take the Backend down)
       Dropbox listing -> for each session (max 2/night): fetch+validate 5 spot series FIRST -> download -> validate -> ingest -> verify
       spot bars: nfo_ingestion/spot.py -> apps.market_data.nfo_spot (in-process) -> paper IB Gateway child's HTTP history endpoint
       ingestion code: nfo_pipeline/ (vendored, UNCHANGED, restored from this repo's own history)
       state: remote TimescaleDB ledger (nfo_ingestion_batch); scheduler state/lock: the Backend's Redis
```
- New code (all in this repo): `nfo_ingestion/` (worker `run_nightly.py`, `spot.py`, `dropbox_shared.py`, `scheduler.py`), `nfo_pipeline/` (vendored pipeline),
  `apps/market_data/{nfo_spot,ingestion_status,ingestion_alert}.py`, management commands `nfo_ingest_scheduler` and `run_nfo_ingestion`, `scripts/nfo_equivalence_check.py`,
  `scripts/dropbox_refresh_token.py`, a supervisor program, and `ibapi==9.81.1.post1` in `requirements.txt` (the vendored `runner.py` imports it at load time; no IBKR API
  connection is ever opened from the Backend).
- Removed: `kestra/` (compose, flows, prod image), `apps/market_data/nfo_container.py` and its command/task/tests, the HTTP relay view/route/`NFO_SPOT_PROXY_TOKEN`.
- Everything in section 16.4 (guardrails) still applies: idle window 16:30-08:30 IST only (exit 50), one request at a time 7 s apart, one retry after 60 s then halt, max 2
  sessions a night, strict bar validation before any download/DB write, NIFTY canary first. `nfo_spot` additionally enforces the window, a Redis one-at-a-time lock, a 5 s gap and a
  daily budget of 20 requests on its side, and prefers the single connected PAPER session.
- Scheduler policy: Mon-Fri evenings only; an infrastructure failure (exit 20) is retried after 30 min, at most 3 times a night; data-quality / verification / outside-window
  failures (exit 30/40/50) are not retried; the run belongs to the evening it started (00:00-01:30 counts for the previous weekday); Redis down = it does nothing.
  Every run leaves a secret-free `AuditEvent` (NFO_INGEST_RUN) that the System page shows as "last nightly run".
- Visibility without e-mail (e-mail is on hold): worker output goes to the Backend log with prefix `[nfo-ingest]`; the System-page panel shows the ledger state and the last run;
  the missed-night alert (Backend beat 08:00/12:00 IST) is off until `NFO_FRESHNESS_ALERT_TO` + `EMAIL_*` are set.

### 17.2 Verified (2026-11-03)
- Pipeline numerics in the Backend's own environment: in `python:3.12-slim-bookworm` with the Backend's exact pins (numpy 2.2.3, pandas 2.2.3, scipy 1.16.3, pyarrow 19.0.1,
  psycopg 3.2.6), the vendored pipeline's transform (select, identity, spot match, IV and Greeks) was run on the real 2026-11-02 session with the spot bars from the DB and compared with
  the production rows: **250,533 of 250,533 rows matched, every IV/Greek within 3.3e-10, 0 quality-flag and 0 spot-match mismatches**. Peak memory of that step 770 MB (the chain-feature
  step comes on top; the worker is a separate process, and the Backend container's memory limit is not known to us).
- Real Dropbox listing/download with `nfo_ingestion/dropbox_shared.py` (see 16.12). Tests: 119 worker/spot/dropbox/equivalence + 24 scheduler tests + relay/status/alert tests;
  critical guards were mutation-checked.

### 17.3 What the owner must do (nothing needs Docker Hub or QCH access)
1. Set these on the hosted Backend (the real values are in the git-ignored `.nfo-ingest.env` at the repo root; never commit them): `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`,
   `DROPBOX_REFRESH_TOKEN`, `GDF_DROPBOX_SHARED_LINK`. `TIMESCALE_DB_URL` is optional (defaults to `REMOTE_FEATURE_DB_URL`). Leave `NFO_INGEST_SCHEDULE_ENABLED=false`.
2. Deploy as usual. The `nfo-ingest-scheduler` program starts with the Backend and idles.
3. Gates, in order (each must pass before the next): G1 equivalence (`python scripts/nfo_equivalence_check.py --sessions 2026-11-02,2026-10-30`, after 16:30 IST, paper gateway logged in,
   read-only); G2 `python manage.py run_nfo_ingestion --dry-run`; G3 one real session by hand in the evening (`run_nfo_ingestion --session YYYY-MM-DD`); G4 run it again, the ledger
   must skip it; G5 set `NFO_INGEST_SCHEDULE_ENABLED=true` and watch two nights.
4. Optional later: SMTP + `NFO_FRESHNESS_ALERT_TO` for e-mails; `NFO_HOLIDAYS` for exchange holidays.

### 17.4 Known limits
- The Backend container's disk is ephemeral: downloaded zips and scratch files are re-created as needed; the DB ledger is the record. A restart mid-night resumes from the ledger.
- The Backend container's memory limit is unknown; the ingestion is a separate low-priority process and the first real run (G3) should be watched for memory.
- History calls still share the strategy's gateway connection and command loop (section 16.2); G1 and the canary exist for that reason. Option 1 (exposing the TWS API) remains a later,
  quiet-window improvement.
- The options-dashboard repo got one local-only commit earlier (`459c064`, `acquire.py` + `service.py`); nothing in this repo depends on it any more.

---

## 18. Production logs and the sub-container question (2026-11-03, evening)

### 18.1 What the hosted deployment's logs show
- Platform facts: QFS builds the Backend image itself from the app source (`trading_engine_20:v4`), attaches Postgres and Redis as resources (autowired `POSTGRES_*`, `REDIS_*`),
  routes `https://finflockqfsplatform.cloud/qfsplatform/ai_trading_engine_backend1/`, no extra volumes, and gives the Backend container **4 CPU cores and 8192 MB RAM**
  (so the ingestion's ~0.8-2 GB, run as a separate low-priority process, fits). The platform UI shows "Sub-containers: 0 of 10 in use, started by the app through QFS".
- **Defect found and fixed (commit f9926bc): Celery was crash-looping.** `kombu.exceptions.VersionMismatch: Redis transport requires redis-py versions 6.1.0 or later. You have 5.3.1`;
  `beat` and `celery-research` exited and restarted repeatedly, and the other Celery programs use the same broker, so they cannot work either. Cause: neither kombu nor redis was pinned,
  so the new server's fresh build resolved kombu 5.7.0 beside redis-py 5.3.1 (`pip check` does not catch it, it is a runtime check). Fix: `kombu==5.6.2`, `redis==5.3.1`, verified in a clean
  python:3.12 with a fresh `pip install -r requirements.txt` and a real Redis round trip (kombu 5.5.4 also works, 5.7.0 does not). Until this is deployed, periodic tasks (including the
  missed-night alert) do not run. Minor: the workers run as root (`SecurityWarning`); `ENV_FILE` is read without a default (harmless).
- The ingestion scheduler (supervisor program `nfo-ingest-scheduler`) does not use Celery, so it is independent of that defect; it uses Redis through redis-py directly.

### 18.2 Running services as sub-containers (the senior's direction)
Facts from the code: the Backend creates IB Gateway children through `QCHBrokerClient.create_container(name, image, command=None, env=None, network=None)`. The Backend's own validator only
accepts Docker Hub references for the gateway image; whether QCH itself accepts the app's own QFS-built image, a custom `command`, and how many slots gateways consume is NOT known from the code.
Considerations: 10 slots are shared with IB Gateway sessions (one per session); the Backend has ~10 supervisor programs, so it cannot be one container per program; sub-containers get no autowired
Postgres/Redis, so connection settings must be passed explicitly in the create call (QCH may echo a container's env when listing, so treat the QCH API as a secret store); nothing restarts a dead
sub-container unless the app does it; a split moves risk from "one process crashes" to "a platform feature we cannot test from here".
Recommendation (phased, reversible): (0) deploy the Celery fix first; (1) leave everything trading-critical where it is (web, beat, celery-trading, celery-broker-commands, celery-core, celery-data);
(2) only if isolation is wanted, move the heavy non-trading services (celery-research, celery-intelligence, the NFO ingestion) into sub-containers started from the app's own image with a different
command, one at a time, each behind an env switch and with the old supervisor program kept as the fallback; (3) do not use Kestra (18.3).
**Measure before building:** `python manage.py qch_probe list | create --image <app image> --command "sleep 120" --yes | delete --name probe-sleep --yes` (apps/broker_gateway/management/commands/qch_probe.py)
creates a harmless `probe-*` container with NO environment variables and tells us what QCH accepts. Questions for the senior: (1) can a sub-container use the app's own image (`trading_engine_20:v4`)
with a custom command? (2) do gateway sessions and service sub-containers share the 10 slots? (3) per-container CPU/memory limits and restart policy? (4) do sub-containers reach Postgres/Redis
(network `qch-app-resources`) and how are credentials passed?

### 18.3 Kestra
Not recommended even as a sub-container: QCH could pull the public `docker.io/kestra/kestra` image, but our worker code cannot be injected into it without an image of our own; Kestra would need
~1 GB RAM plus a slot and a network hop, and the Backend scheduler (section 17) already provides the schedule, retries, run history and the System-page panel. Reconsider only if a separate UI is required.

---

## 19. Hosting the whole Backend properly on the platform (2026-11-03, late evening)

### 19.1 Root causes found from the hosted logs and the System page
1. **Celery crash loop = the CPU spike (240%, 97% throttled).** kombu 5.7.0 + redis-py 5.3.1 -> `VersionMismatch` (section 18.1). Seven Celery programs (beat, core, data, research,
   trading, broker-commands, intelligence) each take about 1.5 s to import Django/Celery and then exit. That is just over supervisord's default `startsecs` of 1 s, so each start counts as
   "successful" (the retry counter resets) and `autorestart=true` restarts it forever: seven imports every couple of seconds saturate the 4-core limit and the workers never serve a task
   (System page: strategy / target / intent / broker workers MISSING). **Fixed and pushed: remote `Ai-backend` a2ccbe0 (kombu==5.6.2, redis==5.3.1).** The platform reused the old image for
   v5 and v6 ("commit and dependencies unchanged") because the fix had not been pushed; a rebuild happens only once requirements change.
2. **Kafka and Flink do not exist on the platform.** docker-compose.options.yml runs `apache/kafka:3.9.0` (single KRaft broker) and `flink:1.20.1-scala_2.12-java17` as JobManager + TaskManager as
   separate services; QFS attaches only Postgres and Redis ("3 attached" in the deploy log). The Backend therefore reports KAFKA_UNAVAILABLE, OUTBOX_PUBLISHER_UNHEALTHY and
   EXECUTION_WORKERS_ABSENT and automatic execution stays NOT READY. The celery fix does not change this.
3. Minor: Celery workers run as root (SecurityWarning); `.env.example` is baked into the image as `.env` (it contains real credentials and `kafka:9092`).

### 19.2 Kafka + Flink as sub-containers (built, OFF by default, not yet verified on the platform)
`apps/broker_gateway/platform_services.py` creates three QCH sub-containers from PUBLIC Docker Hub images with the compose file's settings: `finflock-kafka` (docker.io/apache/kafka:3.9.0),
`finflock-flink-jobmanager` and `finflock-flink-taskmanager` (docker.io/library/flink:1.20.1-scala_2.12-java17, command `jobmanager` / `taskmanager`, FLINK_PROPERTIES as in compose).
Rules: only when `PLATFORM_SERVICES_ENABLED=true`; creates a service only when QCH lists it absent (create_container resolves name conflicts, so no duplicates); never deletes or replaces anything; a QCH
refusal changes nothing and never blocks the Backend. Run by `scripts/ensure_service_containers.py` from entrypoint.sh BEFORE `ensure_kafka_topics.py` (which already waits ~8 minutes for Kafka and
Flink), and every 5 minutes by the beat task `ensure-platform-services`. Uses 3 of the 10 slots (plus one per IB Gateway session). 9 tests.
To turn on (after the Celery fix is healthy): set on the Backend `PLATFORM_SERVICES_ENABLED=true`, `KAFKA_ENABLED=true`, `KAFKA_BOOTSTRAP_SERVERS=finflock-kafka:9092`,
`FLINK_REST_URL=http://finflock-flink-jobmanager:8081`; optional `PLATFORM_SERVICE_PREFIX`, `PLATFORM_KAFKA_IMAGE`, `PLATFORM_FLINK_IMAGE`.
**Run `manage.py qch_probe` first** (create a `probe-*` container from `docker.io/apache/kafka:3.9.0`, then delete it) to confirm QCH pulls public images and accepts a command.
Open questions for the platform owner: per-sub-container CPU/memory limits (the compose file sizes the Flink TaskManager at 3 GB, JobManager 1 GB, Kafka heap 512 MB); sub-container DNS and ports
(the gateway children are reached by name, so Kafka 9092 and Flink 8081 should be too); restart policy; and storage.
Known limits: no volumes, so Kafka data is ephemeral (the compacted `instrument.registry.v1` topic is republished by the Backend's 60 s beat task `publish_instrument_registry_snapshot`) and Flink
checkpoints are lost on a restart (jobs are resubmitted when the Backend starts; a Flink restart while the Backend runs needs the Backend restarted or the jobs resubmitted).

### 19.3 What to do, in order
1. Redeploy now (QFS rebuilds because requirements changed). Verify: no `VersionMismatch`, no `exited ... not expected`, CPU well below the limit, the four worker rows leave MISSING.
2. Only then deploy the rest of the local work (ingestion layer, qch_probe, platform services): rebase the local `Ai-backend` onto `origin/Ai-backend` first (the pin commit exists twice with different hashes).
3. Run `qch_probe`, then enable `PLATFORM_SERVICES_ENABLED` and re-check the System page diagnostics (Kafka, outbox, execution workers).

### 19.4 Platform rules learned on deploy (2026-11-03, night)
- The platform builds `.env` from `.env.example` and treats every BLANK `KEY=` line there as a secret it must attach; one it cannot attach FAILS the deploy
  (it failed on EMAIL_HOST, EMAIL_HOST_USER, EMAIL_HOST_PASSWORD, DEFAULT_FROM_EMAIL, NFO_FRESHNESS_ALERT_TO, NFO_HOLIDAYS). Only REDIS_PASSWORD, REDIS_USERNAME,
  QCH_APP_ID, QCH_API_HOST, QCH_SERVICE_TOKEN may be blank (the platform fills them). Leave optional settings commented. Guarded by tests/test_env_example_platform.py.
- E-mail is ON HOLD and removed: no EMAIL_* settings and no beat entry; the missed-night alert code stays dormant. The System page still shows missed nights.
  The platform offers a "Mail sender" resource (gives QCH_MAIL_URL, QCH_MAIL_FROM_NAME): use that when e-mail is wanted.
- The Dropbox credentials are baked into `.env.example` by the owner's decision (a473f23). There is no shell inside the hosted container.

