from __future__ import annotations


# Exchange strike-spacing convention for each supported index underlying.
# Shared between ingestion (nfo_pipeline.runner, computing rolling ATM-offset
# rows) and the consumer (trading.providers.remote_db). FINNIFTY/MIDCPNIFTY/
# NIFTYNXT50 values are empirically confirmed against the raw GlobalData
# source (smallest observed near-ATM strike gap, sampled across the full
# ingested date range 2025-09 through 2026-08) and match NSE's published
# contract convention.
#
# NIFTYFPI also appears in the raw source feed but is deliberately excluded:
# its observed strikes are few (8 across the whole dataset) and irregularly
# spaced (5/10/15/25/30/100), so no stable strike step can be confirmed from
# the data, and its price scale (~1600) is unrelated to NIFTY's -- it needs
# a human decision on what it actually is before onboarding, not a guess.
STRIKE_STEP = {
    "NIFTY": 50,
    "BANKNIFTY": 100,
    "FINNIFTY": 50,
    "MIDCPNIFTY": 25,
    "NIFTYNXT50": 100,
}

# Single source of truth for which underlyings the pipeline selects, models,
# and ingests -- nfo_pipeline.transform's ticker selection, the CHECK
# constraints below, and nfo_pipeline.runner's per-session IBKR spot fetch
# all derive from this list. Adding an underlying here requires it to also be
# registered with a real, verified contract id in nfo_pipeline.ibkr's
# INDEX_REQUESTS -- ingestion will fail loudly (not silently mis-fetch) for
# any underlying listed here without one.
SUPPORTED_UNDERLYINGS = tuple(STRIKE_STEP)
_UNDERLYING_LIST_SQL = ",".join(f"'{u}'" for u in SUPPORTED_UNDERLYINGS)

# index_option_rolling_atm precomputes ATM-relative series out to this many
# strikes on either side. Chosen with headroom above the default adaptive
# backtest scan ceiling (BACKTEST_MAX_STRIKE_OFFSET defaults to 20; see
# trading/backtest_services.py). Offsets beyond this fall back to the slower
# query-time join against index_chain_feature in RemoteDBProvider.
ROLLING_ATM_MAX_OFFSET = 25

# See the add_compression_policy call below for why this differs from the
# 7-day interval used by every other hypertable here.
ROLLING_ATM_COMPRESS_AFTER = "60 days"

OPTION_COLUMNS = (
    "batch_id", "observed_at", "session_date", "ticker", "underlying", "expiry", "strike",
    "option_type", "open", "high", "low", "close", "volume", "open_interest",
    "spot_observed_at", "spot_open", "spot_high", "spot_low", "spot_close", "spot_volume",
    "spot_age_minutes", "spot_match", "derived_iv", "delta", "gamma", "theta", "vega", "rho",
    "risk_free_rate", "dividend_yield", "iv_repricing_error", "dte_days", "expiry_cycle",
    "is_regular_session", "contract_multiplier", "multiplier_source", "quality_flag",
    "schema_version", "model_version", "calculation_profile",
)


DDL = """
CREATE TABLE IF NOT EXISTS nfo_ingestion_batch (
    batch_id text PRIMARY KEY,
    session_date date NOT NULL,
    source_archive text NOT NULL,
    source_member text NOT NULL,
    source_sha256 text NOT NULL,
    source_member_crc bigint NOT NULL,
    state text NOT NULL CHECK (state IN (
        'DISCOVERED','IBKR_FETCHING','ENRICHING','VALIDATING','STAGED','COMPLETE',
        'FAILED_RETRYABLE','FAILED_DATA_QUALITY'
    )),
    source_rows bigint NOT NULL DEFAULT 0,
    selected_rows bigint NOT NULL DEFAULT 0,
    staged_rows bigint NOT NULL DEFAULT 0,
    inserted_rows bigint NOT NULL DEFAULT 0,
    source_fingerprint text,
    parquet_sha256 text,
    quality_counts jsonb NOT NULL DEFAULT '{}'::jsonb,
    schema_version text NOT NULL,
    model_version text NOT NULL,
    calculation_profile text NOT NULL,
    code_revision text NOT NULL,
    error_category text,
    error_message text,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (session_date, source_sha256, source_member, schema_version, model_version, calculation_profile)
);

CREATE TABLE IF NOT EXISTS index_contract_spec (
    underlying text NOT NULL CONSTRAINT index_contract_spec_underlying_check CHECK (underlying IN (""" + _UNDERLYING_LIST_SQL + """)),
    contract_expiry date NOT NULL,
    effective_from date NOT NULL,
    effective_to date NOT NULL,
    multiplier integer NOT NULL CHECK (multiplier > 0),
    source_reference text NOT NULL,
    source_sha256 text NOT NULL,
    version text NOT NULL,
    PRIMARY KEY (underlying, contract_expiry, effective_from),
    CHECK (effective_to >= effective_from)
);

CREATE TABLE IF NOT EXISTS index_underlying_candle (
    observed_at timestamptz NOT NULL,
    -- No enumerated CHECK here (unlike index_contract_spec): this is a
    -- compressed hypertable, and TimescaleDB rejects ADD CONSTRAINT outright
    -- (even NOT VALID) once compression is enabled -- confirmed directly
    -- against production rather than assumed. Widening this list would need
    -- a full decompress/alter/recompress cycle on ~50M rows. App-level
    -- validation (transform.py's ticker selection, remote_db.py's _symbol())
    -- already guards every write path against a bad value.
    underlying text NOT NULL,
    session_date date NOT NULL,
    open double precision NOT NULL,
    high double precision NOT NULL,
    low double precision NOT NULL,
    close double precision NOT NULL,
    volume double precision,
    ibkr_con_id bigint NOT NULL,
    batch_id text NOT NULL REFERENCES nfo_ingestion_batch(batch_id),
    source text NOT NULL DEFAULT 'IBKR_TRADES',
    retrieved_at timestamptz NOT NULL DEFAULT now(),
    quality_flag text NOT NULL DEFAULT 'OK',
    PRIMARY KEY (observed_at, underlying)
);

CREATE TABLE IF NOT EXISTS index_option_candle (
    batch_id text NOT NULL REFERENCES nfo_ingestion_batch(batch_id),
    observed_at timestamptz NOT NULL,
    session_date date NOT NULL,
    ticker text NOT NULL,
    -- No enumerated CHECK -- see the comment on index_underlying_candle.
    underlying text NOT NULL,
    expiry date NOT NULL,
    strike double precision NOT NULL,
    option_type text NOT NULL CHECK (option_type IN ('CALL','PUT')),
    open double precision NOT NULL,
    high double precision NOT NULL,
    low double precision NOT NULL,
    close double precision NOT NULL,
    volume bigint NOT NULL,
    open_interest bigint NOT NULL,
    spot_observed_at timestamptz,
    spot_open double precision,
    spot_high double precision,
    spot_low double precision,
    spot_close double precision,
    spot_volume double precision,
    spot_age_minutes double precision,
    spot_match text NOT NULL CHECK (spot_match IN ('EXACT','CARRIED','MISSING')),
    derived_iv double precision,
    delta double precision,
    gamma double precision,
    theta double precision,
    vega double precision,
    rho double precision,
    risk_free_rate double precision NOT NULL,
    dividend_yield double precision NOT NULL,
    iv_repricing_error double precision,
    dte_days double precision NOT NULL,
    expiry_cycle text NOT NULL CHECK (expiry_cycle IN ('WEEK','MONTH')),
    is_regular_session boolean NOT NULL,
    contract_multiplier integer,
    multiplier_source text NOT NULL,
    quality_flag text NOT NULL,
    schema_version text NOT NULL,
    model_version text NOT NULL,
    calculation_profile text NOT NULL,
    PRIMARY KEY (observed_at, ticker)
);

CREATE UNLOGGED TABLE IF NOT EXISTS index_option_candle_staging
    (LIKE index_option_candle INCLUDING DEFAULTS INCLUDING CONSTRAINTS);
ALTER TABLE index_option_candle_staging ADD COLUMN IF NOT EXISTS staged_at timestamptz NOT NULL DEFAULT now();
CREATE INDEX IF NOT EXISTS index_option_stage_batch ON index_option_candle_staging(batch_id);

CREATE TABLE IF NOT EXISTS index_chain_feature (
    observed_at timestamptz NOT NULL,
    underlying text NOT NULL,
    expiry date NOT NULL,
    batch_id text NOT NULL REFERENCES nfo_ingestion_batch(batch_id),
    spot double precision NOT NULL,
    atm_strike double precision NOT NULL,
    atm_call_close double precision,
    atm_put_close double precision,
    atm_call_iv double precision,
    atm_put_iv double precision,
    atm_iv double precision,
    atm_straddle double precision,
    expected_move_expiry double precision,
    implied_move_expiry_pct double precision,
    implied_move_one_session_pct double precision,
    longer_term_iv double precision,
    term_structure_ratio double precision,
    term_structure_slope double precision,
    put_25d_iv double precision,
    call_25d_iv double precision,
    skew_25d double precision,
    iv_rank_252 double precision,
    iv_percentile_252 double precision,
    realized_volatility_20 double precision,
    iv_rv_ratio double precision,
    put_oi_wall double precision,
    call_oi_wall double precision,
    put_call_oi_ratio double precision,
    put_oi_change bigint,
    call_oi_change bigint,
    total_volume bigint,
    total_open_interest bigint,
    liquidity_score double precision,
    bid_ask_available boolean NOT NULL DEFAULT false,
    asof_timestamp timestamptz NOT NULL,
    quality_flag text NOT NULL,
    PRIMARY KEY (observed_at, underlying, expiry)
);

ALTER TABLE index_chain_feature ADD COLUMN IF NOT EXISTS expected_move_expiry double precision;
ALTER TABLE index_chain_feature ADD COLUMN IF NOT EXISTS longer_term_iv double precision;
ALTER TABLE index_chain_feature ADD COLUMN IF NOT EXISTS term_structure_ratio double precision;
ALTER TABLE index_chain_feature ADD COLUMN IF NOT EXISTS term_structure_slope double precision;
ALTER TABLE index_chain_feature ADD COLUMN IF NOT EXISTS put_25d_iv double precision;
ALTER TABLE index_chain_feature ADD COLUMN IF NOT EXISTS call_25d_iv double precision;
ALTER TABLE index_chain_feature ADD COLUMN IF NOT EXISTS skew_25d double precision;
ALTER TABLE index_chain_feature ADD COLUMN IF NOT EXISTS iv_rank_252 double precision;
ALTER TABLE index_chain_feature ADD COLUMN IF NOT EXISTS iv_percentile_252 double precision;
ALTER TABLE index_chain_feature ADD COLUMN IF NOT EXISTS realized_volatility_20 double precision;
ALTER TABLE index_chain_feature ADD COLUMN IF NOT EXISTS iv_rv_ratio double precision;
ALTER TABLE index_chain_feature ADD COLUMN IF NOT EXISTS total_volume bigint;
ALTER TABLE index_chain_feature ADD COLUMN IF NOT EXISTS total_open_interest bigint;
ALTER TABLE index_chain_feature ADD COLUMN IF NOT EXISTS liquidity_score double precision;
ALTER TABLE index_chain_feature ADD COLUMN IF NOT EXISTS bid_ask_available boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS index_option_rolling_atm (
    batch_id text NOT NULL REFERENCES nfo_ingestion_batch(batch_id),
    observed_at timestamptz NOT NULL,
    -- No enumerated CHECK -- see the comment on index_underlying_candle.
    underlying text NOT NULL,
    expiry date NOT NULL,
    option_type text NOT NULL CHECK (option_type IN ('CALL','PUT')),
    atm_offset smallint NOT NULL,
    strike double precision NOT NULL,
    open double precision NOT NULL,
    high double precision NOT NULL,
    low double precision NOT NULL,
    close double precision NOT NULL,
    volume bigint,
    open_interest bigint,
    derived_iv double precision,
    spot_close double precision,
    PRIMARY KEY (observed_at, underlying, expiry, option_type, atm_offset)
);

CREATE TABLE IF NOT EXISTS index_event_risk_feature (
    observed_at timestamptz NOT NULL,
    -- No enumerated CHECK -- see the comment on index_underlying_candle.
    underlying text NOT NULL,
    expiry date NOT NULL,
    event_risk double precision NOT NULL CHECK (event_risk BETWEEN 0 AND 100),
    pir double precision NOT NULL CHECK (pir >= 0),
    event_expiry_gap_sessions double precision NOT NULL,
    source_cutoff timestamptz NOT NULL,
    model_version text NOT NULL,
    quality_flag text NOT NULL,
    PRIMARY KEY (observed_at,underlying,expiry),
    CHECK (source_cutoff <= observed_at)
);

SELECT create_hypertable('index_underlying_candle', 'observed_at', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
SELECT create_hypertable('index_option_candle', 'observed_at', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
SELECT create_hypertable('index_chain_feature', 'observed_at', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
SELECT create_hypertable('index_option_rolling_atm', 'observed_at', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
SELECT create_hypertable('index_event_risk_feature', 'observed_at', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS index_option_contract_time ON index_option_candle(ticker, observed_at DESC);
CREATE INDEX IF NOT EXISTS index_option_chain_time ON index_option_candle(underlying, expiry, observed_at DESC, strike, option_type);
CREATE INDEX IF NOT EXISTS index_option_session ON index_option_candle(session_date, underlying);
CREATE INDEX IF NOT EXISTS index_underlying_session ON index_underlying_candle(underlying, session_date, observed_at);
CREATE INDEX IF NOT EXISTS index_chain_lookup ON index_chain_feature(underlying, observed_at DESC, expiry);
CREATE INDEX IF NOT EXISTS index_rolling_atm_series ON index_option_rolling_atm(underlying, option_type, atm_offset, observed_at DESC, expiry);
CREATE INDEX IF NOT EXISTS index_event_risk_lookup ON index_event_risk_feature(underlying, observed_at DESC, expiry);

ALTER TABLE index_underlying_candle SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'underlying',
    timescaledb.compress_orderby = 'observed_at DESC'
);
ALTER TABLE index_option_candle SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'underlying,expiry',
    timescaledb.compress_orderby = 'observed_at DESC,ticker'
);
ALTER TABLE index_chain_feature SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'underlying,expiry',
    timescaledb.compress_orderby = 'observed_at DESC'
);
ALTER TABLE index_option_rolling_atm SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'underlying,expiry,option_type,atm_offset',
    timescaledb.compress_orderby = 'observed_at DESC'
);
ALTER TABLE index_event_risk_feature SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'underlying,expiry',
    timescaledb.compress_orderby = 'observed_at DESC'
);
SELECT add_compression_policy('index_underlying_candle', INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_compression_policy('index_option_candle', INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_compression_policy('index_chain_feature', INTERVAL '7 days', if_not_exists => TRUE);
-- index_option_rolling_atm is deliberately NOT compress_after=7 days like its
-- siblings: unlike same-day ingestion, this table is routinely written with
-- rows whose observed_at is already long past (historical backfill, and any
-- --session/--from-date historical catch-up through the regular pipeline).
-- A 7-day window makes those rows compression-eligible the instant they're
-- inserted. That was suspected (not confirmed) as a contributor to a
-- backfill data-loss incident -- the actual root cause turned out to be a
-- dangling-transaction bug in the backfill command, unrelated to compression
-- -- but a wide compress_after margin here is still a reasonable, low-cost
-- precaution against writes ever racing the compression background job.
SELECT add_compression_policy('index_option_rolling_atm', INTERVAL '""" + ROLLING_ATM_COMPRESS_AFTER + """', if_not_exists => TRUE);
SELECT add_compression_policy('index_event_risk_feature', INTERVAL '7 days', if_not_exists => TRUE);
"""


def ensure_schema(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
        row = cursor.fetchone()
        if not row:
            raise RuntimeError("TimescaleDB is not installed in the target database")
        version = tuple(int(part) for part in row[0].split(".")[:2])
        if version < (2, 13):
            raise RuntimeError(f"TimescaleDB 2.13 or newer is required; found {row[0]}")
        cursor.execute(DDL)
        # CREATE TABLE IF NOT EXISTS leaves an already-existing table's inline
        # CHECK constraints untouched, so widening SUPPORTED_UNDERLYINGS above
        # needs an explicit ALTER against a table created under a narrower one.
        # index_contract_spec is a small, uncompressed table -- ALTER is cheap
        # there, so its constraint is dropped and rebuilt widened. The other 4
        # are compressed hypertables, and TimescaleDB rejects ADD CONSTRAINT
        # outright (even NOT VALID) once compression is enabled -- confirmed
        # directly against production, not assumed. Rather than pay for a full
        # decompress/alter/recompress cycle on ~50M rows every time this list
        # changes, their enumerated CHECK is simply dropped and not replaced;
        # app-level validation already guards every write path (see the
        # comment on index_underlying_candle in the DDL above).
        constraint = "index_contract_spec_underlying_check"
        cursor.execute(f"ALTER TABLE index_contract_spec DROP CONSTRAINT IF EXISTS {constraint}")
        cursor.execute(
            f"ALTER TABLE index_contract_spec ADD CONSTRAINT {constraint} CHECK (underlying IN ({_UNDERLYING_LIST_SQL}))"
        )
        for table, constraint in (
            ("index_underlying_candle", "index_underlying_candle_underlying_check"),
            ("index_option_candle", "index_option_candle_underlying_check"),
            # LIKE ... INCLUDING CONSTRAINTS copies the constraint under the
            # SAME name as the source table's, not a staging-specific one.
            ("index_option_candle_staging", "index_option_candle_underlying_check"),
            ("index_option_rolling_atm", "index_option_rolling_atm_underlying_check"),
            ("index_event_risk_feature", "index_event_risk_feature_underlying_check"),
        ):
            cursor.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {constraint}")
    connection.commit()


def configure_compression(connection, after="7 days") -> None:
    with connection.cursor() as cursor:
        for table, segment_by in (
            ("index_underlying_candle", "underlying"),
            ("index_option_candle", "underlying,expiry"),
            ("index_chain_feature", "underlying,expiry"),
            ("index_option_rolling_atm", "underlying,expiry,option_type,atm_offset"),
            ("index_event_risk_feature", "underlying,expiry"),
        ):
            cursor.execute(
                f"ALTER TABLE {table} SET (timescaledb.compress, timescaledb.compress_segmentby = %s)",
                (segment_by,),
            )
            cursor.execute("SELECT remove_compression_policy(%s::regclass, if_exists => TRUE)", (table,))
            # index_option_rolling_atm keeps its own, much wider interval
            # regardless of `after` -- see the comment on its policy in DDL.
            table_after = ROLLING_ATM_COMPRESS_AFTER if table == "index_option_rolling_atm" else after
            cursor.execute("SELECT add_compression_policy(%s::regclass, %s::interval)", (table, table_after))
    connection.commit()
