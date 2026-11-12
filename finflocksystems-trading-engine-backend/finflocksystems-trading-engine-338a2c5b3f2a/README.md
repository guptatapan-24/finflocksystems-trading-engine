# Backend

The Backend is the Django/ASGI execution core. One container runs Gunicorn with
an ASGI worker, Celery Beat, the market and Finnhub consumers, and dedicated
Celery workers for `strategy_evaluation`, `target_coordination`,
`intent_execution`, and `broker_commands` under Supervisor. Intent and broker
dispatch use separate worker processes so readiness can prove each financial
handoff is staffed. Startup applies Django migrations before Supervisor starts.

## Run and test

Create a local ignored `.env` only when running outside Compose. Production images do not contain an environment file.

```bash
pip install -r requirements.txt
python manage.py migrate --noinput
python manage.py runserver 8000
pytest
docker build -t trading-engine-backend .
```

The production image builds from `Backend` alone. Local Compose deliberately
mounts the trusted root `Trading_Engine_Stock_Strategy_Universe_JSON` bundle
read-only at `/app/research_bundle`, replacing the stale image-baked sample
without copying mutable research files into the container.

## Public routing and health

- `GET /healthz` is process liveness and is the QFS health check.
- `GET /readyz` checks database and recommendation-cache readiness.
- `GET /api/v1/execution/readiness/` reports fail-closed automatic PAPER
  readiness and returns 503 with named blockers when execution must stop.
- `GET /api/v1/system/` reports non-secret deployment status.
- The exact configured base path returns service, health, and System route metadata.
- Both prefix-preserved requests and prefix-stripped requests with `X-Forwarded-Prefix` are supported.

Set `APP_BASE_PATH=/ai_trading_engine_backend` and `PUBLIC_BASE_URL=https://qfsplatform.com/ai_trading_engine_backend` on QFS. APIs use the `{ok,data,error,meta}` envelope. Missing QCH or child-image configuration disables managed-session operations without failing `/healthz` or otherwise disabling the Backend.

## Managed IBKR sessions

Every broker operation resolves a real `BrokerGatewaySession`. A portfolio without a valid session raises `GatewaySessionUnavailable`; there is no global or local static route.

The Backend calls QCH to list, create, and delete app-scoped child containers. New names contain the complete session UUID and resolve internally as `http://<child-container-name>:8080/api/v1`. QCH receives the validated `IBKR_GATEWAY_IMAGE`, child name, permitted child environment, and an optional network. Blank `QCH_SUBCONTAINER_NETWORK` is omitted so QCH can select its platform default.

`IBKR_GATEWAY_IMAGE` accepts explicit Docker Hub references only:

- Production: `docker.io/<username>/<repository>@sha256:<64-hex-digest>`
- Controlled testing: `docker.io/<username>/<repository>:<fixed-non-latest-tag>`

The browser cannot override the image or provide registry authentication. The Backend has no Docker SDK/CLI/socket/SSH/Compose path and never pulls the image itself. Registry access, if needed for a private repository, is configured on the QCH host outside this application.

Managed noVNC HTTP and WebSocket traffic is proxied through `/api/v1/broker-sessions/<uuid>/novnc/...`. The QFS outer proxy must preserve WebSocket `Upgrade` and `Connection` headers.

## External services and safety

PostgreSQL, Redis, Celery broker/result storage, Kafka, Flink, and Finnhub configuration belong only to this application. Managed live sessions still require the independent `ALLOW_LIVE_TRADING` gate and all kill-switch, sizing, risk, OMS, ledger, and reconciliation controls.

## Automatic option strategies

The built-in strategy seed now includes Cash-Secured Put, Put Credit Spread,
Call Credit Spread, Iron Condor, Iron Butterfly, Delta Put Credit Spread, Long
ATM Straddle, Calendar Call, and the RPS Options Auto Selector. Run the
idempotent seed explicitly with:

```bash
python manage.py seed_strategy_definitions
```

Automatic entry freezes the prior completed weekly close plus/minus exactly
`0.75 * Wilder ATR(14)` and requires confirmation from the OI-weighted top-five
put and call walls. Dhan supplies the live option-chain OI, volume, IV, and
Greeks used for selection; set `DHAN_OPTIONS_ENABLED=true`, `DHAN_CLIENT_ID`,
and `DHAN_ACCESS_TOKEN`. The chosen legs are then independently matched and
qualified through the portfolio's IBKR Gateway. Dhan is never an order route.

Each decision and exact leg set is retained in `OptionSelectionSnapshot`. The
selected structure stays locked until its configured hold-period/expiry exit.
When newly qualified legs are still awaiting IBKR prices, the evaluation job
waits durably; fresh `market.quality` events promote that same bar/version job
as soon as every leg has an executable quote, without waiting for another daily
underlying bar.
Execution uses exact contract quantities and multipliers, fills long protection
before short entry legs, and covers short legs before selling long protection
on exit. Automatic short legs pass only with an already-filled matching hedge
or a cash-secured-put assignment reserve; manual naked-short behavior remains
unchanged.

Database startup uses normal Django migrations. See [database upgrades](../docs/DATABASE_UPGRADES.md), [async operations](../docs/ASYNC_OPERATIONS.md), and [QFS deployment](../docs/QFS_DEPLOYMENT.md).

From the repository root, the complete automatic path can be checked with:

```powershell
powershell -NoProfile -File docs\automatic_execution_smoke.ps1
```
