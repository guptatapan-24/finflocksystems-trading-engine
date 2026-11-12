# Automatic options through IBKR

Create an options strategy with `POST /api/v1/options/strategy-instances/`:

```json
{
  "name": "NIFTY automatic options",
  "portfolio_id": 1,
  "ticker": "NIFTY",
  "parameters": {
    "lots": 1,
    "round_trip_cost_per_lot": 40
  },
  "activate": false
}
```

The endpoint defaults to `RPS_OPTIONS_AUTO_SELECTOR` and daily bars. NSE/INR
`NIFTY` resolves as IBKR's `NIFTY50` index; it does not select the SGX/USD NIFTY
product. For other underlyings, supply a qualified `instrument_id`, or specify
the ticker, asset class, exchange and currency. `GET` on this collection lists
only options strategies. The corresponding `/<id>/` route supports detail,
updates and deletion through the existing lifecycle rules.

Set `activate: true` to start the standard qualification, subscription, warmup,
evaluation and execution lifecycle. Alternatively, use the existing
`POST /api/v1/strategy-instances/<id>/enable/` route with an `Idempotency-Key`.
The existing pause, flatten, state and execution-timeline endpoints also apply.
Creation itself is not idempotent: retain the returned strategy ID and retry
activation on that resource rather than creating another strategy.

The portfolio's gateway session determines PAPER or LIVE. Do not send an
`execution_mode` to the options endpoint. Each session has its own authenticated
gateway URL. Within that gateway, `IBC_TRADING_MODE` selects IBKR API port 4002
for paper or 4001 for live. Quote requests carry that mode and reject mismatches;
orders retain the existing intent/command/session/health mode checks. Paper
orders go to the connected IBKR paper account.

### The trading mode decides the market data

`IBC_TRADING_MODE` selects the feed, for options exactly as it already did for
stocks. A **paper** session reads IBKR's **delayed** data; a **live** session
reads **real time**. Nothing else has to be configured, and the options path no
longer pins itself to real time independently of the mode.

`IBKR_MARKET_DATA_TYPE` remains an override with one direction: it defaults from
the mode, may narrow a paper session to `LIVE` when that account carries the
entitlement, and is refused outright for a live session, which may never read
delayed data. The local validation stack sets nothing and so runs delayed.

Provenance is checked on both sides of the gateway and must match exactly what
the mode entitled. The Gateway calls `reqMarketDataType` with 1 or 3 and accepts
only a ticker stamped with that same type; the backend then accepts only tick
type 3 for a paper session and 1 for a live one. IBKR's frozen (2) and
delayed-frozen (4) fallbacks are a closed book and are refused in either mode,
as is real-time data arriving on a session that asked for delayed.

Because a delayed tick is stamped about fifteen minutes behind the book, a paper
session is bounded by `OPTION_QUOTE_DELAYED_MAX_AGE_SECONDS` (that delay plus the
real-time window) instead of the 30-second `OPTION_QUOTE_MAX_AGE_SECONDS`. The
allowance bounds staleness; it does not waive it.

A paper account therefore needs no real-time entitlement to run automatic
options. It does trade on quotes that are a quarter of an hour old, which is
sound for validating the pipeline and for paper fills, and is the reason a live
session is never allowed the same feed.

### Timeframes and the entry window

The source's RPS is timeframe-independent by construction: one observation per
session, every derived input cut strictly before that session, and the session
scored against its **opening** print rather than a snapshot close, precisely so
a 5m and a 15m replay produce the same `rps_base` (`trading/rps.py`,
`build_rps_observations`). The execution timeframe therefore decides only how
often the entry window is sampled and what price a fill happens at.

This runtime matches that: `1m`, `5m`, `15m`, `1h` and `1d` are all accepted.
`rps_market_open_time` (default `09:15`) and `rps_entry_delay_minutes`
(default `0`) reproduce the source's gate -- a decision is refused with
`ENTRY_WINDOW_CLOSED` until the session has been open that long. The check runs
before the chain is fetched, so an intraday timeframe spends no IBKR request on
bars that precede the window. Because nothing here records IBKR's `timeZoneId`,
the comparison zone is `rps_market_timezone`, defaulting to the source's own
`Asia/Kolkata`.

`weekly_atr` and `weekly_close` are no longer declared as stream inputs. They
had no counterpart in the source and already decided nothing, but a declared
INDICATOR must carry a non-null value on a warm-up bar before the strategy is
released, and a 14-period weekly figure is null on every intraday bar -- it
needs about fourteen weeks of history, beyond IBKR's ninety-day intraday cap.
Declaring them made every timeframe below `1d` impossible to activate.

## The decision rules

Entry, strategy choice and exit are ports of the `Aryan_Options` backend. The
first four modules below are the source's own code; the last three are adapted
to this runtime's inputs, with every threshold, weight and rule unchanged:

| This runtime | Source |
| --- | --- |
| `option_analytics/payoff.py` | `trading/pop_engine.py` (byte-identical) |
| `option_analytics/selection.py` | `trading/decision_engines/strategy_selection.py` (byte-identical) |
| `option_analytics/range_entry.py` | `trading/decision_engines/range_entry.py` (verbatim) |
| `option_analytics/exit_policy.py` | `trading/decision_engines/dynamic_exit_policy.py` (verbatim, one docstring cross-reference retargeted) |
| `option_analytics/rps.py` | the RPS V2 heuristic in `trading/rps.py` |
| `option_analytics/entry_signal.py` | `trading/entry_signal.py` |
| `option_analytics/dynamic_exit.py` | `trading/dynamic_exit.py` |

A decision runs in the source's order, and every stage must pass:

1. **RPS V2.** Eight frozen-weight factors — `distance_sr` 18, `oi_structure` 17,
   `expected_move` 15, `volume_pressure` 10, `atr_realized_vol` 10, `trend_adx` 7,
   `bollinger_compression` 4, `mean_reversion` 4, renormalised to 100. The range
   is a **0.75 × 14-day Wilder ATR** envelope around the **prior completed daily**
   session's close. A factor whose input cannot be observed is excluded and the
   remaining weights renormalise; it is never scored as a fabricated neutral.
   The score must clear `rps_threshold`.
2. **The approved range/IV/breach entry gate**, run per candidate against that
   candidate's **own** boundaries. It blends IV rank, IV percentile, IV/RV, term
   structure and 25-delta skew onto a normalised 0-100 scale, requires
   `FAVOURABLE`, and requires a deterministic-GBM **settlement** breach
   probability inside the configured ceiling. A two-sided structure is judged on
   the probability of finishing outside the range *at all* — the two sides are
   mutually exclusive at expiry, so their union is their sum — while a
   single-sided spread is judged on its own side against its own ceiling.
3. **Causal bounded-loss, POP and EV pre-filters** (`rps_max_entry_loss`,
   `rps_min_pop` clamped to the family floor, `rps_require_positive_ev`).
4. **Approved market-state routing**, then the hard gates, the 14-factor TQS and
   the risk-adjusted winner.

Open interest is an RPS factor and an independent confirmation level. **It never
selects a strike.** Strikes come from each template's ATM offsets, which are the
source's: an iron condor sells ±2 strikes and buys ±3, an iron butterfly sells
the ATM strike on both sides and buys ±2, a put credit spread buys −2 and sells
−1, a call credit spread sells +1 and buys +2.

### Exits

Positions are **held to expiry**. There is no hard exit and no range-breach
exit: the worst case of a defined-risk credit structure is already bounded at
entry, and closing early on a transient breach realises that worst case instead
of letting it resolve. Three discretionary exits survive, and all three bank an
edge that already exists:

- `SOFT_PROFIT_GIVEBACK_RISK` — most of the profit is captured and a material
  giveback is likely.
- `SOFT_PROFIT_CAPTURE_RISK_REWARD` — most of the profit is captured and what is
  left does not pay for continuing to hold.
- `SOFT_VOLUME_IV_EXPANSION` — volume *and* IV expanded together.

The range is still measured and reported on every evaluation; it closes nothing.
The contract close at `exit_dte` is the settlement analogue, and `hold_days`
remains available.

### The remote feature source

`REMOTE_FEATURE_DB_URL` points at the same per-session feature database the
source reads through `RemoteDBProvider`, and supplies the two inputs the live
IBKR path cannot:

- **`index_underlying_candle`** is the source's `feature_candles`. The source
  fetches it at a fixed granularity "whatever the replay timeframe" and
  resamples to sessions in `_daily_frame`; `daily_features` groups by session
  the same way, so the intraday rows are handed to it unresampled. Reading the
  daily features here rather than from the strategy's own subscription is what
  makes the RPS score independent of the execution timeframe, and it is what
  lets an intraday strategy score RPS at all.
- **`index_chain_feature`** supplies the precomputed `iv_rank_252` and
  `iv_percentile_252` (with ATM IV, realized vol, IV/RV, term structure and
  25-delta skew), T-1 aligned like every other daily factor. The front expiry
  -- the nearest not before the session -- is the one read, which is what an
  ATM IV rank describes.

With it configured the IV blend scores a measured rank instead of the neutral
50.0, `C1_IVRANK` keeps its TQS weight, and `iv_rank` counts towards the
quality test as the source requires. Without it the runtime keeps the
fallbacks described below.

A configured source that cannot be read **raises and refuses the entry**; it
never silently reverts to the neutral score, because entering a real position
on a materially different score is the divergence this port exists to remove.

### What this runtime does not measure

- **IV rank / IV percentile, without a remote feature source.** No daily
  ATM-IV history is kept locally, so both are unavailable. The IV blend then
  scores them a neutral 50.0 (the source's short-history fallback) and
  `C1_IVRANK` is dropped from TQS rather than scored zero, so
  `tqs_factor_coverage` reports what the score actually rests on.
- **Event risk / PIR / event-expiry gap.** Nothing observes an event calendar
  with a genuine per-event `known_at`, so the whole B block drops out of TQS.
  `event_risk_data_available` is `false`, exactly as in the source.

Term structure and 25-delta skew *are* measured, from the fetched chain. Every
unmeasured input is listed in `selection_evidence.unavailable_inputs`.

### Deliberate differences from the source

- Legs are priced at the **executable** side of the book (ask to buy, bid to
  sell). The source reads a candle close because a replay has no order book; a
  live credit that assumes the mid is a credit the account will not receive.
- Round-trip cost is the configured `round_trip_cost_per_lot` per leg per lot,
  not the source's modelled execution buffer. It is an estimate, not a quote of
  IBKR commissions; set it to the account's fee schedule.
- `rps_iv_breach_enabled` defaults to **true** here. The source ships it off so
  backtest payloads written before the gate existed still replay; this runtime
  has no such payloads and no degraded RPS-only mode to preserve.
- The source's MILP portfolio optimizer is not ported. It allocates across
  concurrent slots, and one strategy instance here holds one structure.
- **Known unit mismatch, carried over deliberately.** `GATE_CREDIT` compares a
  net credit in currency against a wing width in strike points, so on an index
  with a large multiplier the gate is close to vacuous. This is the source's
  behaviour and is reproduced rather than corrected, so the two engines agree;
  correcting it is a decision for both, not one.

## Selection and sizing

`OPTION_CHAIN_PROVIDER=IBKR` is the default and is the only provider that can
authorise an automatic entry. IBKR supplies the chain, exact contract
identities, bid/ask, OI, volume, IV and Greeks. Requests observe up to 21 nearby
strikes, both rights, in a bounded batch; `option_strike_count` can reduce that
to 5-21. An optional `option_trading_class` disambiguates classes. Missing,
frozen, future-dated or stale quotes cannot authorize entry, nor can a quote
whose tick type is not the one the session's trading mode entitled it to.

The legacy Dhan provider returns `UNSUPPORTED_PROVIDER`. Its quotes carry no
contract multiplier or exchange lot size, so the approved engines cannot size a
payoff from them, and entering on a weaker rule instead is the divergence this
port exists to remove.

`round_trip_cost_per_lot` is a configurable estimate **per leg, per exchange
lot**, in the contract currency; its default is 40. Existing risk checks,
hedge-first staging and position attribution remain in the execution path.

Multiplier and lot size are independent. IBKR reported multiplier 1, minimum
quantity 65, quantity increment 65 and price tick 0.05 for the NIFTY contract
tested on 2026-09-19. These values are read from contract details, not hardcoded
into strategy sizing. One exchange lot therefore produces 65 broker units for
that contract. Selection loss limits include the requested number of lots.

### Data the RPS features need

The eight factors are derived in `option_analytics/rps.py` from completed daily
sessions, never from the streaming warm-up buffer. With `REMOTE_FEATURE_DB_URL`
set those sessions come from the remote feature source above, up to
`REMOTE_FEATURE_SESSIONS` (500) of them, independently of the execution
timeframe. Without it they come from IBKR daily bars in `MarketBar`, which only
exist when the strategy itself runs daily.

Three factors -- `atr_realized_vol`, `trend_adx` and `bollinger_compression`,
21 of the 100 weight -- are percentile ranks over a 252-session window and
need real depth behind them. `atr_realized_vol` additionally depends on a
60-session realized-vol figure, so it needs roughly 80 sessions before it
produces anything at all. A short history does not fabricate a score: each of
the three is excluded until it can be measured, and the remaining weights
renormalise.

## Local validation stack

`docker-compose.options.yml` starts a separate PostgreSQL, Redis, Kafka, Flink,
backend and paper gateway stack. It uses `username` and `password` from `.env`;
it does not change the deployment account mode. Supply the `LOCAL_*` secrets
required by that file using a second environment file. The validation run used
`/tmp/finflock-options-validation.env` with restricted file permissions.

```sh
docker compose --env-file .env \
  --env-file /tmp/finflock-options-validation.env \
  -f docker-compose.options.yml up -d
```

The stack needs roughly **8 GiB of container memory** before the Flink
TaskManager is counted, and the TaskManager itself holds one JVM plus a Python
worker process per job. Give Docker at least **12 GiB**: below that the
TaskManager is OOM-killed once all five jobs are running, every job drops into
`RESTARTING`, and readiness reports `REQUIRED_FLINK_JOBS_NOT_RUNNING`.

Local backend: `http://127.0.0.1:18000`. Gateway/noVNC:
`http://127.0.0.1:18082`. Flink: `http://127.0.0.1:18081`.
The deployment Dockerfile retains `COPY .env.example .env`.

The published gateway image is
`docker.io/rachitm4555/trading-engine-ib-gateway:options-20260923.3`, pinned in
`.env.example` to
`sha256:525529f92ec2accd919ab47906fa4612ca30a9c560009f053944329c4d3529c6`.
Its build context and tests are in `gateway/IB_gateway`.

Verify a republished digest by running it, not by trusting the build. IBKR
serves only a moving `stable` installer URL, so two builds of the same commit
picked up IB Gateway 1045 and 1050, and the 1050 build left the install4j
launcher pointing at the installer's deleted temp directory. IB Gateway then
crash-looped while `/healthz` — which only proves the Django process is up —
still reported the container healthy. The build now runs `ibgateway-version
verify` and executes `java -version` through the recorded path, so that failure
stops the build instead of reaching a registry.

## Validation on 2026-09-19

- Real IBKR paper login and account reconciliation succeeded.
- NSE NIFTY50 underlying conId 51497778 and its actual option chain were returned.
- NIFTY 25000 call expiring 2026-09-22, conId 911876549, qualified successfully.
- A backend-to-gateway paper transport test submitted BUY 65 at limit 0.05.
  IBKR assigned order ID 6 and permanent ID 17714814, reported `PreSubmitted`,
  then confirmed `Cancelled` after the test. Filled quantity was zero.
- IBKR returned errors **354** and **10090**: the paper session lacks the requested
  NSE option market-data subscription. No executable quote/OI/IV was returned.
  Enable the appropriate data entitlement for the paper session before automatic
  entry can work. An automatic strategy entry/fill has not been demonstrated.
- The separate endpoint created a PAPER strategy, qualified NIFTY50 and completed
  all 75 required warm-up bars through Kafka/Flink. A daily-history duration bug
  was fixed; requests over 365 days now use IBKR's year unit. The live subscription
  then reported **420**, no NSE index market-data permissions, and **354**.
  Validation strategies were paused
  afterward; there is no unattended test strategy waiting to trade on market open.
- All five required Flink jobs ran and completed checkpoints; backend execution
  infrastructure readiness returned `READY`.
- Focused stock/options regression tests: 49 passed. Gateway tests: 136 passed.
  The broader suite reported 603 passed, 5 skipped and 23 failures. All 23 failures
  were reproduced against the unchanged Git baseline: missing research fixtures,
  existing Kafka mocks, workflow expectations and a duplicate lifecycle test.

That run predates the decision-engine port below and describes the ATR/OI entry
score it replaced. No live or paper order has been placed under the ported
rules.

No live order was placed. The paper validation order was cancelled.

IBKR's [historical-data request documentation](https://interactivebrokers.github.io/tws-api/historical_bars.html)
describes the supported duration units.
