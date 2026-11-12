from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import zipfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import psycopg
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from .config import IngestionOptions, database_url
from .ibkr import ReadOnlyIBKRClient, audit_api_surface
from .schema import OPTION_COLUMNS, ROLLING_ATM_MAX_OFFSET, STRIKE_STEP, SUPPORTED_UNDERLYINGS, ensure_schema
from .transform import (
    LOT_SPEC_VERSION, MODEL_VERSION, SCHEMA_VERSION, SOURCE_COLUMNS,
    derive_analytics, match_spot, prepare_identity, select_index_options,
)


CALCULATION_PROFILE = "INR_INDEX_OPTIONS_6P8_1P2_DECISION_FEATURES_V1"

CHAIN_COLUMNS = (
    "observed_at", "underlying", "expiry", "batch_id", "spot", "atm_strike",
    "atm_call_close", "atm_put_close", "atm_call_iv", "atm_put_iv", "atm_iv",
    "atm_straddle", "expected_move_expiry", "implied_move_expiry_pct",
    "implied_move_one_session_pct", "longer_term_iv", "term_structure_ratio",
    "term_structure_slope", "put_25d_iv", "call_25d_iv", "skew_25d",
    "put_oi_wall", "call_oi_wall", "put_call_oi_ratio", "put_oi_change",
    "call_oi_change", "total_volume", "total_open_interest", "liquidity_score",
    "bid_ask_available", "asof_timestamp", "quality_flag",
)

ROLLING_ATM_COLUMNS = (
    "batch_id", "observed_at", "underlying", "expiry", "option_type", "atm_offset",
    "strike", "open", "high", "low", "close", "volume", "open_interest",
    "derived_iv", "spot_close",
)


@dataclass(frozen=True)
class SourceJob:
    session_date: date
    archive: Path
    member: str
    member_crc: int


def sha256_file(path: Path, progress=None) -> str:
    digest = hashlib.sha256()
    processed = 0
    total = path.stat().st_size
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            processed += len(block)
            if progress:
                progress(processed, total)
    return digest.hexdigest()


def discover_jobs(input_dir: Path) -> list[SourceJob]:
    if not input_dir.is_dir():
        raise ValueError(f"Input directory does not exist: {input_dir}")
    jobs = []
    for archive in sorted(input_dir.rglob("*.zip")):
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                if not member.filename.lower().endswith(".csv"):
                    continue
                match = __import__("re").search(r"(\d{8})\.csv$", member.filename)
                if not match:
                    raise ValueError(f"Cannot determine session date from {member.filename}")
                session = pd.to_datetime(match.group(1), format="%d%m%Y").date()
                jobs.append(SourceJob(session, archive, member.filename, member.CRC))
    return sorted(jobs, key=lambda item: item.session_date)


class NFOIndexIngestion:
    def __init__(self, options: IngestionOptions, stdout=print, progress=None):
        options.validate()
        self.options = options
        self.stdout = stdout
        self.progress = progress
        self.connection = None
        self.ibkr: ReadOnlyIBKRClient | None = None
        self._archive_hashes: dict[Path, str] = {}
        self._session_index = 0
        self._session_total = 0
        self._session_label = "-"
        self._session_fraction = 0.0
        self._phase = "INITIALIZING"
        self._underlyings = frozenset(options.underlyings) if options.underlyings else frozenset(SUPPORTED_UNDERLYINGS)
        # A partial-universe run (--underlyings) must get its own
        # calculation_profile: nfo_ingestion_batch's UNIQUE constraint is
        # (session_date, source_sha256, source_member, schema_version,
        # model_version, calculation_profile) -- NOT batch_id -- so
        # reprocessing an already-COMPLETE session to add underlyings it
        # didn't originally cover would otherwise collide with that
        # constraint on INSERT (raising a raw IntegrityError) even though
        # batch_id itself differs. Full-universe runs are untouched.
        self._calculation_profile = (
            CALCULATION_PROFILE if self._underlyings == frozenset(SUPPORTED_UNDERLYINGS)
            else f"{CALCULATION_PROFILE}_PARTIAL_UNIVERSE_{'_'.join(sorted(self._underlyings))}"
        )

    def _report(self, phase: str, fraction: float, message: str = "", rows=None) -> None:
        self._phase = phase
        self._session_fraction = max(0.0, min(1.0, float(fraction)))
        if self.progress is not None:
            self.progress.update(
                session_index=self._session_index, session_total=self._session_total,
                session=self._session_label, phase=phase, fraction=self._session_fraction,
                message=message, rows=rows,
            )

    def run(self) -> dict:
        self._report("DISCOVERY", 0.0, "Scanning source ZIP inventory")
        jobs = self._selected_jobs(discover_jobs(self.options.input_dir))
        if not jobs:
            raise ValueError("No matching source sessions were found")
        self._session_total = len(jobs)
        if self.options.dry_run:
            return self._inventory(jobs)
        completed = skipped = 0
        try:
            self._report("PREFLIGHT", 0.0, "Checking NetBird, database, schema and IBKR")
            self._preflight()
            for number, job in enumerate(jobs, 1):
                self._session_index = number
                self._session_label = str(job.session_date)
                self._report("START", 0.0, "Starting source session")
                self.stdout(f"[{number}/{len(jobs)}] {job.session_date} starting")
                outcome = self._process(job)
                if outcome == "SKIPPED":
                    skipped += 1
                else:
                    completed += 1
            result = {"sessions": len(jobs), "completed": completed, "skipped": skipped}
            self._report("COMPLETE", 1.0, f"Run complete: {completed} promoted, {skipped} skipped")
            self.stdout(f"Complete: {completed} promoted, {skipped} already complete")
            return result
        finally:
            try:
                if self.ibkr:
                    self.ibkr.close()
            finally:
                if self.connection:
                    self.connection.close()

    def _selected_jobs(self, jobs: list[SourceJob]) -> list[SourceJob]:
        if self.options.session:
            selected = date.fromisoformat(self.options.session)
            return [job for job in jobs if job.session_date == selected]
        if self.options.from_date and self.options.to_date:
            start, end = date.fromisoformat(self.options.from_date), date.fromisoformat(self.options.to_date)
            if end <= start:
                raise ValueError("--to-date must be later than --from-date and is exclusive")
            return [job for job in jobs if start <= job.session_date < end]
        return jobs

    def _inventory(self, jobs: list[SourceJob]) -> dict:
        source_rows = selected_rows = 0
        sessions = []
        expiries = {underlying: set() for underlying in self._underlyings}
        for number, job in enumerate(jobs, 1):
            self._session_index = number
            self._session_label = str(job.session_date)
            self._report("INVENTORY", 0.0, "Reading source member")
            day_source = day_selected = 0
            with zipfile.ZipFile(job.archive) as archive, archive.open(job.member) as stream:
                for raw in pd.read_csv(stream, chunksize=self.options.chunksize):
                    selected, parsed = select_index_options(raw, self._underlyings)
                    day_source += len(raw)
                    day_selected += len(selected)
                    for underlying, expiry in zip(parsed.underlying, parsed.expiry):
                        expiries[underlying].add(str(expiry))
                    member_size = archive.getinfo(job.member).file_size
                    self._report(
                        "INVENTORY", min(1.0, stream.tell() / max(member_size, 1)),
                        f"Scanning CSV: {day_selected:,} selected of {day_source:,}", rows=day_source,
                    )
            source_rows += day_source
            selected_rows += day_selected
            sessions.append({"session": str(job.session_date), "source_rows": day_source, "selected_rows": day_selected})
            self._report(
                "INVENTORY_COMPLETE", 1.0,
                f"Selected {day_selected:,} index-option rows from {day_source:,}",
                rows=day_source,
            )
            self.stdout(f"{job.session_date}: {day_selected:,} index-option rows of {day_source:,} rows")
        return {
            "sessions": sessions, "source_rows": source_rows, "selected_rows": selected_rows,
            "contract_expiry_codes": {name: sorted(values) for name, values in expiries.items()},
        }

    def _preflight(self) -> None:
        self._report("PREFLIGHT_NETBIRD", 0.0, "Checking encrypted NetBird connection")
        if not self.options.skip_netbird_check:
            try:
                status = subprocess.run(
                    ["netbird", "status"], capture_output=True, text=True, timeout=10, check=False,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError("NetBird is unavailable; remote ingestion is disabled") from exc
            if status.returncode or "Management: Connected" not in status.stdout:
                raise RuntimeError("NetBird is not connected; remote ingestion is disabled")
        url = database_url()
        parsed = __import__("urllib.parse", fromlist=["urlparse"]).urlparse(url)
        port = parsed.port or 5432
        self._report("PREFLIGHT_TCP", 0.0, f"Checking database TCP port {port}")
        try:
            with socket.create_connection((parsed.hostname, port), timeout=5):
                pass
        except OSError as exc:
            raise RuntimeError("The configured TimescaleDB endpoint is unreachable") from exc
        try:
            self._report("PREFLIGHT_DATABASE", 0.0, "Authenticating and validating TimescaleDB schema")
            self.connection = psycopg.connect(url, connect_timeout=10)
            ensure_schema(self.connection)
        except psycopg.Error:
            raise RuntimeError("TimescaleDB authentication or schema preflight failed") from None
        self.ibkr = ReadOnlyIBKRClient()
        audit_api_surface(self.ibkr)
        self._report("PREFLIGHT_IBKR", 0.0, "Connecting to local TWS/Gateway in read-only mode")
        self.ibkr.connect_ready(
            self.options.tws_host, self.options.tws_port,
            self.options.tws_client_id, self.options.tws_timeout,
        )
        for underlying in self._underlyings:
            self._report("PREFLIGHT_IBKR", 0.0, f"Loading pinned {underlying} index contract")
            self.ibkr.resolve_index(underlying, self.options.tws_timeout)
        self.options.scratch_dir.mkdir(parents=True, exist_ok=True)
        for partial in self.options.scratch_dir.glob("*.parquet.partial"):
            partial.unlink()
        scratch_files = list(self.options.scratch_dir.glob("*.parquet"))
        if len(scratch_files) > 1:
            raise RuntimeError("Scratch directory contains more than one session Parquet file")
        self._report("PREFLIGHT_OK", 0.0, "All preflight checks passed")

    def _archive_sha256(self, archive: Path) -> str:
        if archive not in self._archive_hashes:
            self.stdout(f"Hashing source archive {archive.name}")
            self._archive_hashes[archive] = sha256_file(
                archive,
                progress=lambda done, total: self._report(
                    "CHECKSUM", 0.03 * done / max(total, 1),
                    f"Hashing source archive: {done / 1048576:.1f}/{total / 1048576:.1f} MiB",
                ),
            )
        return self._archive_hashes[archive]

    def _batch_identity(self, job: SourceJob, source_sha256: str) -> str:
        payload = {
            "source_sha256": source_sha256, "member": job.member, "crc": job.member_crc,
            "universe": sorted(self._underlyings), "schema": SCHEMA_VERSION,
            "model": MODEL_VERSION, "profile": self._calculation_profile,
            "lot_spec": LOT_SPEC_VERSION, "code": self._code_revision(),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _code_revision() -> str:
        try:
            return subprocess.run(
                ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=3, check=True,
            ).stdout.strip()
        except Exception:
            return "UNVERSIONED"

    def _process(self, job: SourceJob) -> str:
        if time.time() - job.archive.stat().st_mtime < self.options.stable_file_seconds:
            raise RuntimeError(f"Source archive is still inside the stability window: {job.archive.name}")
        source_sha = self._archive_sha256(job.archive)
        batch_id = self._batch_identity(job, source_sha)
        scratch = self.options.scratch_dir / f"{job.session_date}_{batch_id[:12]}.parquet"
        self._report("LEDGER", 0.04, "Checking remote batch ledger")
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT state FROM nfo_ingestion_batch WHERE batch_id=%s", (batch_id,))
            existing = cursor.fetchone()
        if existing and existing[0] == "COMPLETE":
            scratch.unlink(missing_ok=True)
            self._report("SKIPPED", 1.0, "Identical batch is already complete")
            self.stdout(f"{job.session_date}: verified complete, skipped")
            return "SKIPPED"
        if not existing:
            # batch_id hashes in code_revision, so a commit landing between
            # runs (unrelated to this session's data) changes batch_id for an
            # already-COMPLETE session without changing anything that
            # actually matters to the ledger's own uniqueness rule: (session,
            # source file, schema/model/profile). Without this check,
            # _upsert_batch's INSERT below hits that UNIQUE constraint and
            # aborts the whole run with a raw IntegrityError instead of a
            # safe skip -- this isn't specific to any one code change, it
            # would recur for every future commit against the historical
            # backlog.
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """SELECT state FROM nfo_ingestion_batch
                       WHERE session_date=%s AND source_sha256=%s AND source_member=%s
                         AND schema_version=%s AND model_version=%s AND calculation_profile=%s""",
                    (job.session_date, source_sha, job.member, SCHEMA_VERSION, MODEL_VERSION, self._calculation_profile),
                )
                same_source = cursor.fetchone()
            if same_source and same_source[0] == "COMPLETE":
                scratch.unlink(missing_ok=True)
                self._report("SKIPPED", 1.0, "Identical source already complete under a different code revision")
                self.stdout(f"{job.session_date}: verified complete (different code revision), skipped")
                return "SKIPPED"
        self._upsert_batch(job, batch_id, source_sha, "DISCOVERED")
        try:
            if not (self.options.resume and scratch.exists()):
                self._enrich_session(job, batch_id, scratch)
            else:
                self._report("RESUME", 0.54, "Reusing retained validated session scratch file")
            self._report("VALIDATE", 0.55, "Validating Parquet, causality, Greeks and row conservation")
            validation = self._validate_parquet(scratch, batch_id, job.session_date)
            self._update_batch(batch_id, "VALIDATING", **validation)
            equivalent = self._equivalent_complete_batch(batch_id, job.session_date, validation)
            if equivalent:
                self._report(
                    "EQUIVALENT", 0.99,
                    f"Verified identical rows already stored by batch {equivalent[:12]}; skipping upload",
                    rows=validation["selected_rows"],
                )
                self._update_batch(
                    batch_id, "COMPLETE", staged_rows=0, inserted_rows=0,
                    error_category=None, error_message=None,
                )
                scratch.unlink()
                self._report("COMPLETE", 1.0, "Equivalent completed session verified; scratch deleted")
                self.stdout(f"{job.session_date}: equivalent completed data verified, skipped")
                return "SKIPPED"
            self._stage_and_promote(job, batch_id, scratch, validation)
            self._report("CLEANUP", 0.995, "Remote verification passed; deleting local scratch file")
            scratch.unlink()
            self._report("COMPLETE", 1.0, f"Promoted {validation['selected_rows']:,} rows")
            self.stdout(f"{job.session_date}: promoted {validation['selected_rows']:,} rows")
            return "COMPLETE"
        except Exception as exc:
            failed_phase = self._phase
            self._report("FAILED", self._session_fraction, f"Stopped safely: {type(exc).__name__}")
            self.connection.rollback()
            state = "FAILED_DATA_QUALITY" if isinstance(exc, (ValueError, AssertionError)) else "FAILED_RETRYABLE"
            safe_message = self._safe_error_message(exc, failed_phase)
            try:
                self._update_batch(
                    batch_id, state, error_category=type(exc).__name__, error_message=safe_message,
                )
            except Exception:
                # Preserve the initiating exception when the database itself is unavailable.
                pass
            if isinstance(exc, psycopg.Error):
                raise RuntimeError(safe_message) from None
            raise

    @staticmethod
    def _safe_error_message(exc: Exception, phase: str) -> str:
        if isinstance(exc, psycopg.Error):
            sqlstate = exc.sqlstate or "unavailable"
            return (
                f"Database {type(exc).__name__} during {phase} "
                f"(SQLSTATE {sqlstate}); retained scratch can be retried with --resume"
            )
        return str(exc)[:2000]

    def _equivalent_complete_batch(self, batch_id: str, session_date: date, validation: dict) -> str | None:
        """Find an independently completed batch with identical selected source rows."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                """SELECT previous.batch_id
                   FROM nfo_ingestion_batch previous
                   WHERE previous.batch_id <> %s
                     AND previous.session_date = %s
                     AND previous.state = 'COMPLETE'
                     AND previous.schema_version = %s
                     AND previous.model_version = %s
                     AND previous.calculation_profile = %s
                     AND previous.selected_rows = %s
                     AND previous.source_fingerprint = %s
                     AND (SELECT count(*) FROM index_option_candle stored
                          WHERE stored.batch_id=previous.batch_id) = previous.selected_rows
                   ORDER BY previous.completed_at DESC
                   LIMIT 1""",
                (
                    batch_id, session_date, SCHEMA_VERSION, MODEL_VERSION,
                    self._calculation_profile, validation["selected_rows"],
                    validation["source_fingerprint"],
                ),
            )
            row = cursor.fetchone()
        return row[0] if row else None

    def _upsert_batch(self, job, batch_id, source_sha, state) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO nfo_ingestion_batch
                   (batch_id,session_date,source_archive,source_member,source_sha256,source_member_crc,
                    state,schema_version,model_version,calculation_profile,code_revision)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (batch_id) DO UPDATE SET state=EXCLUDED.state, updated_at=now(),
                     error_category=NULL,error_message=NULL""",
                (batch_id, job.session_date, str(job.archive.relative_to(self.options.input_dir)), job.member,
                 source_sha, job.member_crc, state, SCHEMA_VERSION, MODEL_VERSION,
                 self._calculation_profile, self._code_revision()),
            )
        self.connection.commit()

    def _update_batch(self, batch_id, state, **fields) -> None:
        allowed = {
            "source_rows", "selected_rows", "staged_rows", "inserted_rows", "source_fingerprint",
            "parquet_sha256", "quality_counts", "error_category", "error_message",
        }
        invalid = set(fields) - allowed
        if invalid:
            raise ValueError(f"Invalid batch fields: {sorted(invalid)}")
        assignments = ["state=%s", "updated_at=now()"]
        values = [state]
        for key, value in fields.items():
            assignments.append(f"{key}=%s")
            values.append(json.dumps(value) if key == "quality_counts" else value)
        if state == "COMPLETE":
            assignments.append("completed_at=now()")
        values.append(batch_id)
        with self.connection.cursor() as cursor:
            cursor.execute(f"UPDATE nfo_ingestion_batch SET {', '.join(assignments)} WHERE batch_id=%s", values)
        self.connection.commit()

    def _lot_sizes(self, session_date: date) -> dict[tuple[str, date], tuple[int, str]]:
        result = {}
        with self.connection.cursor() as cursor:
            cursor.execute(
                """SELECT DISTINCT ON (underlying,contract_expiry)
                     underlying,contract_expiry,multiplier,source_reference
                   FROM index_contract_spec WHERE effective_from <= %s AND effective_to >= %s
                   ORDER BY underlying,contract_expiry,effective_from DESC""",
                (session_date, session_date),
            )
            for underlying, expiry, multiplier, source in cursor.fetchall():
                result[(underlying, expiry)] = (int(multiplier), source)
        return result

    def _enrich_session(self, job: SourceJob, batch_id: str, scratch: Path) -> None:
        self._report("SCRATCH", 0.05, "Preparing the single-session scratch file")
        self._ensure_single_scratch(scratch)
        scratch.unlink(missing_ok=True)
        partial = scratch.with_suffix(".parquet.partial")
        partial.unlink(missing_ok=True)
        self._update_batch(batch_id, "IBKR_FETCHING")
        self._report(
            "IBKR", 0.07,
            f"Fetching {len(self._underlyings)} underlying(s) one-minute index OHLCV from local TWS/Gateway",
        )
        # Each underlying's reqHistoricalData is an independent request/response
        # pair keyed by its own reqId (see ReadOnlyIBKRClient._allocate/_wait) --
        # firing them concurrently instead of waiting on each one sequentially
        # turns the fetch's cost from sum(latency) into ~max(latency). ibapi's
        # EClient serializes the actual socket writes internally, so concurrent
        # calls from multiple threads on this one connection are safe.
        with ThreadPoolExecutor(max_workers=len(self._underlyings), thread_name_prefix="nfo-ibkr-spot") as executor:
            futures = {
                executor.submit(self.ibkr.historical_index, underlying, job.session_date, self.options.tws_timeout): underlying
                for underlying in self._underlyings
            }
            spots = {}
            for future in futures:
                underlying = futures[future]
                spots[underlying] = future.result()
        for underlying, frame in spots.items():
            self._report("IBKR", 0.11, f"Received {len(frame):,} {underlying} candles", rows=len(frame))
        self._report("UNDERLYING_UPLOAD", 0.15, "Storing causal underlying candles")
        self._store_underlying(batch_id, job.session_date, spots)
        self._report("CONTRACT_SPECS", 0.17, "Loading effective-dated lot sizes")
        lots = self._lot_sizes(job.session_date)
        writer = None
        source_rows = selected_rows = 0
        source_digest = hashlib.sha256()
        quality_counts: dict[str, int] = {}
        self._update_batch(batch_id, "ENRICHING")
        try:
            with zipfile.ZipFile(job.archive) as archive, archive.open(job.member) as stream:
                member_size = archive.getinfo(job.member).file_size
                for raw in pd.read_csv(stream, chunksize=self.options.chunksize):
                    source_rows += len(raw)
                    selected, parsed = select_index_options(raw, self._underlyings)
                    if selected.empty:
                        continue
                    selected_rows += len(selected)
                    hashes = pd.util.hash_pandas_object(selected[SOURCE_COLUMNS], index=False).to_numpy(dtype="uint64")
                    source_digest.update(hashes.tobytes())
                    frame = prepare_identity(selected, parsed, job.session_date)
                    frame = match_spot(frame, spots, self.options.carry_minutes)
                    frame = derive_analytics(frame, self.options.risk_free_rate, self.options.dividend_yield)
                    frame["batch_id"] = batch_id
                    lot_values = [lots.get((u, e)) for u, e in zip(frame.underlying, frame.expiry)]
                    frame["contract_multiplier"] = [value[0] if value else None for value in lot_values]
                    frame["multiplier_source"] = [value[1] if value else "UNRESOLVED_HISTORICAL_INDEX_LOT" for value in lot_values]
                    frame["schema_version"] = SCHEMA_VERSION
                    frame["model_version"] = MODEL_VERSION
                    frame["calculation_profile"] = self._calculation_profile
                    output = self._database_frame(frame)
                    for flag, count in output.quality_flag.value_counts().items():
                        quality_counts[str(flag)] = quality_counts.get(str(flag), 0) + int(count)
                    table = pa.Table.from_pandas(output, preserve_index=False)
                    if writer is None:
                        writer = pq.ParquetWriter(partial, table.schema, compression="zstd")
                    writer.write_table(table)
                    member_fraction = min(1.0, stream.tell() / max(member_size, 1))
                    self._report(
                        "ENRICH", 0.18 + 0.34 * member_fraction,
                        f"Streaming ZIP -> causal spot -> IV/Greeks -> Parquet; selected={selected_rows:,}",
                        rows=source_rows,
                    )
                    if partial.stat().st_size > self.options.max_local_scratch_gb * 1024 ** 3:
                        raise OSError("The session scratch file exceeded --max-local-scratch-gb")
        finally:
            if writer is not None:
                writer.close()
        if selected_rows == 0 or not partial.exists():
            raise ValueError(f"No index-option rows found for {job.session_date}")
        partial.replace(scratch)
        self._report(
            "PARQUET_READY", 0.53,
            f"Session scratch ready ({scratch.stat().st_size / 1048576:.1f} MiB); calculating checksum",
            rows=selected_rows,
        )
        self._update_batch(
            batch_id, "ENRICHING", source_rows=source_rows, selected_rows=selected_rows,
            source_fingerprint=source_digest.hexdigest(), quality_counts=quality_counts,
            parquet_sha256=sha256_file(scratch),
        )

    @staticmethod
    def _database_frame(frame: pd.DataFrame) -> pd.DataFrame:
        renamed = frame.rename(columns={
            "Ticker": "ticker", "Open": "open", "High": "high", "Low": "low",
            "Close": "close", "Volume": "volume", "Open Interest": "open_interest",
        })
        output = renamed[list(OPTION_COLUMNS)].copy()
        output["contract_multiplier"] = pd.array(output.contract_multiplier, dtype="Int64")
        output["volume"] = pd.array(output.volume, dtype="Int64")
        output["open_interest"] = pd.array(output.open_interest, dtype="Int64")
        return output

    def _store_underlying(self, batch_id: str, session_date: date, spots: dict[str, pd.DataFrame]) -> None:
        sql = """INSERT INTO index_underlying_candle
                 (observed_at,underlying,session_date,open,high,low,close,volume,ibkr_con_id,batch_id)
                 VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                 ON CONFLICT (observed_at,underlying) DO NOTHING"""
        with self.connection.cursor() as cursor:
            for frame in spots.values():
                cursor.executemany(sql, [
                    (r.observed_at.to_pydatetime(), r.underlying, session_date, r.open, r.high, r.low,
                     r.close, r.volume, int(r.ibkr_con_id), batch_id)
                    for r in frame.itertuples(index=False)
                ])
        self.connection.commit()

    def _validate_parquet(self, scratch: Path, batch_id: str, session_date: date) -> dict:
        if not scratch.exists():
            raise ValueError(f"Scratch file is missing for {session_date}")
        table = pq.read_table(scratch)
        frame = table.to_pandas()
        if tuple(frame.columns) != OPTION_COLUMNS:
            raise ValueError("Parquet schema does not match the ingestion schema")
        if frame.empty or not frame.batch_id.eq(batch_id).all():
            raise ValueError("Parquet batch identity is invalid")
        if not pd.to_datetime(frame.session_date).dt.date.eq(session_date).all():
            raise ValueError("Parquet contains another session")
        if frame.duplicated(["observed_at", "ticker"]).any():
            raise ValueError("Duplicate option row identity detected")
        if (frame.high < frame[["open", "low", "close"]].max(axis=1)).any() or (frame.low > frame[["open", "high", "close"]].min(axis=1)).any():
            raise ValueError("Option OHLC integrity failed")
        causal = frame.spot_observed_at.isna() | (frame.spot_observed_at <= frame.observed_at)
        age = frame.spot_age_minutes.dropna()
        if not causal.all() or (age < 0).any() or (age > self.options.carry_minutes).any():
            raise ValueError("Causal spot alignment failed")
        valid = frame.derived_iv.notna()
        if (frame.loc[valid, "iv_repricing_error"] > 1e-5).any():
            raise ValueError("IV repricing residual failed")
        for name in ("delta", "gamma", "theta", "vega", "rho"):
            if not np.isfinite(frame.loc[valid, name]).all():
                raise ValueError(f"Non-finite {name} with valid IV")
        if (frame.loc[valid, "gamma"] < 0).any() or (frame.loc[valid, "vega"] < 0).any():
            raise ValueError("Greek sign validation failed")
        source = frame.rename(columns={
            "ticker": "Ticker", "open": "Open", "high": "High", "low": "Low", "close": "Close",
            "volume": "Volume", "open_interest": "Open Interest",
        })
        source["Date"] = pd.to_datetime(frame.observed_at).dt.strftime("%d/%m/%Y")
        source["Time"] = pd.to_datetime(frame.observed_at).dt.strftime("%H:%M:%S")
        hashes = pd.util.hash_pandas_object(source[SOURCE_COLUMNS], index=False).to_numpy(dtype="uint64")
        fingerprint = hashlib.sha256(hashes.tobytes()).hexdigest()
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT selected_rows,source_fingerprint FROM nfo_ingestion_batch WHERE batch_id=%s", (batch_id,))
            expected_rows, expected_fingerprint = cursor.fetchone()
        if len(frame) != expected_rows or fingerprint != expected_fingerprint:
            raise ValueError("Source preservation or row conservation failed")
        return {
            "selected_rows": len(frame), "source_fingerprint": fingerprint,
            "parquet_sha256": sha256_file(scratch),
            "quality_counts": {str(k): int(v) for k, v in frame.quality_flag.value_counts().items()},
        }

    def _stage_and_promote(self, job, batch_id, scratch, validation) -> None:
        session_start, session_end = self._session_bounds(job.session_date)
        self._report("STAGING", 0.60, "Clearing this batch's remote staging rows")
        self._update_batch(batch_id, "STAGED")
        with self.connection.cursor() as cursor:
            cursor.execute("DELETE FROM index_option_candle_staging WHERE batch_id=%s", (batch_id,))
        self.connection.commit()
        self._parallel_copy_to_staging(scratch, validation["selected_rows"])
        with self.connection.cursor() as cursor:
            self._report("STAGING_VERIFY", 0.81, "Checking remote staging row count")
            cursor.execute("SELECT count(*) FROM index_option_candle_staging WHERE batch_id=%s", (batch_id,))
            staged = cursor.fetchone()[0]
            if staged != validation["selected_rows"]:
                raise ValueError(f"Staging row mismatch: expected {validation['selected_rows']}, found {staged}")
        self.connection.commit()
        self._report("CHAIN_PREP", 0.825, "Calculating session chain features locally")
        chain_features, rolling_atm = self._calculate_chain_features(scratch, batch_id)
        self._report("PROMOTION", 0.84, "Promoting the complete session transactionally")
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """SELECT count(*) FROM index_option_candle_staging s JOIN index_option_candle f
                       ON f.observed_at=s.observed_at AND f.ticker=s.ticker
                       WHERE s.batch_id=%s
                         AND s.observed_at >= %s AND s.observed_at < %s
                         AND f.observed_at >= %s AND f.observed_at < %s""",
                    (batch_id, session_start, session_end, session_start, session_end),
                )
                if cursor.fetchone()[0]:
                    raise ValueError("Final table contains conflicting identities for this session")
                columns = ",".join(OPTION_COLUMNS)
                cursor.execute(
                    f"""INSERT INTO index_option_candle ({columns})
                        SELECT {columns} FROM index_option_candle_staging
                        WHERE batch_id=%s AND observed_at >= %s AND observed_at < %s""",
                    (batch_id, session_start, session_end),
                )
                inserted = cursor.rowcount
                if inserted != validation["selected_rows"]:
                    raise ValueError(f"Promotion row mismatch: expected {validation['selected_rows']}, inserted {inserted}")
                self._report("CHAIN_FEATURES", 0.90, "Building ATM, expected-move, OI-wall, skew and term features")
                self._build_chain_features(cursor, batch_id, job.session_date, chain_features)
                self._report("ROLLING_ATM", 0.93, f"Storing ATM-relative rolling series (+/-{ROLLING_ATM_MAX_OFFSET})")
                self._copy_rolling_atm(cursor, batch_id, session_start, session_end, rolling_atm)
                self._report("LEDGER_FINALIZE", 0.97, "Finalizing batch ledger and clearing staging")
                cursor.execute("DELETE FROM index_option_candle_staging WHERE batch_id=%s", (batch_id,))
                cursor.execute(
                    """UPDATE nfo_ingestion_batch SET state='COMPLETE',staged_rows=%s,inserted_rows=%s,
                       completed_at=now(),updated_at=now() WHERE batch_id=%s""",
                    (inserted, inserted, batch_id),
                )
        with self.connection.cursor() as cursor:
            self._report("FINAL_VERIFY", 0.985, "Independently verifying final rows and COMPLETE state")
            cursor.execute(
                """SELECT count(*) FROM index_option_candle
                   WHERE batch_id=%s AND observed_at >= %s AND observed_at < %s""",
                (batch_id, session_start, session_end),
            )
            final_count = cursor.fetchone()[0]
            cursor.execute("SELECT state FROM nfo_ingestion_batch WHERE batch_id=%s", (batch_id,))
            final_state = cursor.fetchone()[0]
        if final_count != validation["selected_rows"] or final_state != "COMPLETE":
            raise RuntimeError("Independent post-promotion verification failed")

    @staticmethod
    def _copy_csv_block(batch: pa.RecordBatch) -> bytes:
        """Encode an Arrow batch directly for PostgreSQL CSV COPY.

        Arrow writes nulls as unquoted empty fields and preserves real empty
        strings as quoted fields, matching PostgreSQL's default CSV semantics.
        Floating NaNs are normalized to null to retain the previous row-copy
        behavior for unavailable analytics.
        """
        table = pa.Table.from_batches([batch])
        for index, field in enumerate(table.schema):
            if not pa.types.is_floating(field.type):
                continue
            column = table.column(index)
            nan_mask = pc.is_nan(column)
            if pc.any(nan_mask).as_py():
                clean = pc.if_else(nan_mask, pa.scalar(None, type=field.type), column)
                table = table.set_column(index, field, clean)
        sink = pa.BufferOutputStream()
        pacsv.write_csv(
            table,
            sink,
            write_options=pacsv.WriteOptions(include_header=False, quoting_style="needed"),
        )
        return sink.getvalue().to_pybytes()

    def _parallel_copy_to_staging(self, scratch: Path, total: int) -> None:
        workers = self.options.copy_workers
        copy_sql = (
            f"COPY index_option_candle_staging ({','.join(OPTION_COLUMNS)}) "
            "FROM STDIN WITH (FORMAT CSV)"
        )
        local = threading.local()
        connection_lock = threading.Lock()
        worker_connections = []

        def upload(block: bytes, rows: int) -> tuple[int, int]:
            connection = getattr(local, "connection", None)
            if connection is None:
                connection = psycopg.connect(database_url(), connect_timeout=10)
                local.connection = connection
                with connection_lock:
                    worker_connections.append(connection)
            try:
                with connection.cursor() as cursor:
                    with cursor.copy(copy_sql) as copy:
                        copy.write(block)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            return rows, len(block)

        uploaded = uploaded_bytes = 0
        upload_started = time.monotonic()

        def collect(done) -> None:
            nonlocal uploaded, uploaded_bytes
            for future in done:
                rows, byte_count = future.result()
                uploaded += rows
                uploaded_bytes += byte_count
                elapsed = max(time.monotonic() - upload_started, 0.001)
                self._report(
                    "UPLOAD", 0.62 + 0.18 * uploaded / max(total, 1),
                    f"{workers} parallel block COPY streams: "
                    f"{uploaded_bytes / 1048576:.1f} MiB at "
                    f"{uploaded_bytes / 1048576 / elapsed:.1f} MiB/s",
                    rows=uploaded,
                )

        pending = set()
        try:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="nfo-copy") as executor:
                for batch in pq.ParquetFile(scratch).iter_batches(batch_size=25_000):
                    while len(pending) >= workers:
                        done, pending = wait(pending, return_when=FIRST_COMPLETED)
                        collect(done)
                    pending.add(executor.submit(self._copy_csv_block_and_upload, upload, batch))
                while pending:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    collect(done)
        finally:
            for connection in worker_connections:
                connection.close()
        if uploaded != total:
            raise ValueError(f"COPY row mismatch: expected {total}, uploaded {uploaded}")

    @classmethod
    def _copy_csv_block_and_upload(cls, upload, batch: pa.RecordBatch) -> tuple[int, int]:
        block = cls._copy_csv_block(batch)
        return upload(block, batch.num_rows)

    @staticmethod
    def _calculate_chain_features(scratch: Path, batch_id: str) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Calculate the session-only chain surface in local memory.

        The source Parquet is already validated and resident on the ingestion
        Mac. Pandas completes this aggregation in a fraction of a second and
        avoids repeatedly sorting the same wide session inside PostgreSQL.

        Also derives the ATM-relative rolling series (index_option_rolling_atm):
        for each observed_at/underlying/expiry snapshot, whichever strikes
        actually traded within +/-ROLLING_ATM_MAX_OFFSET steps of that
        snapshot's ATM strike, tagged with their offset. This precomputes at
        ingestion time exactly the join RemoteDBProvider used to do per query
        (atm_strike from this same session + offset*step -> matching option
        row) so backtests reading an "ATM+N" series become a direct indexed
        lookup instead of a per-query join.
        """
        columns = [
            "observed_at", "underlying", "expiry", "spot_close", "strike",
            "open", "high", "low", "close",
            "derived_iv", "option_type", "open_interest", "volume", "delta",
            "is_regular_session",
        ]
        base = pq.read_table(scratch, columns=columns).to_pandas()
        base = base.loc[base.is_regular_session & base.spot_close.notna()].copy()
        if base.empty:
            raise ValueError("No regular-session rows are available for chain features")

        keys = ["observed_at", "underlying", "expiry"]
        base["_atm_distance"] = (base.strike - base.spot_close).abs()
        atm = (
            base.sort_values(keys + ["_atm_distance", "strike"], kind="mergesort")
            .drop_duplicates(keys)[keys + ["spot_close", "strike"]]
            .rename(columns={"strike": "atm_strike"})
        )
        rolling_atm = NFOIndexIngestion._calculate_rolling_atm(base, atm, keys, batch_id)
        quotes = base.merge(
            atm[keys + ["atm_strike"]],
            left_on=keys + ["strike"], right_on=keys + ["atm_strike"], how="inner",
        )
        pair_keys = keys + ["spot_close", "atm_strike"]
        paired = atm.copy()
        for option_type, prefix in (("CALL", "call"), ("PUT", "put")):
            side = quotes.loc[quotes.option_type.eq(option_type)]
            values = side.groupby(pair_keys, as_index=False, sort=False).agg(
                **{
                    f"{prefix}_close": ("close", "max"),
                    f"{prefix}_iv": ("derived_iv", "max"),
                }
            )
            paired = paired.merge(values, on=pair_keys, how="left")

        summary = base.groupby(keys, as_index=False, sort=False).agg(
            total_volume=("volume", "sum"),
            total_open_interest=("open_interest", "sum"),
            _row_count=("strike", "size"),
        )
        liquid = (
            base.assign(_liquid=base.volume.gt(0) & base.open_interest.gt(0))
            .groupby(keys, as_index=False, sort=False)["_liquid"].sum()
        )
        summary = summary.merge(liquid, on=keys)
        for option_type, prefix in (("PUT", "put"), ("CALL", "call")):
            side = base.loc[base.option_type.eq(option_type)]
            oi = (
                side.groupby(keys, as_index=False, sort=False).open_interest.sum()
                .rename(columns={"open_interest": f"{prefix}_oi"})
            )
            wall = (
                side.sort_values(
                    keys + ["open_interest", "strike"],
                    ascending=[True, True, True, False, True], kind="mergesort",
                )
                .drop_duplicates(keys)[keys + ["strike"]]
                .rename(columns={"strike": f"{prefix}_wall"})
            )
            valid = side.loc[side.derived_iv.notna() & side.delta.notna()].copy()
            valid["_delta_distance"] = (valid.delta.abs() - 0.25).abs()
            delta_25 = (
                valid.sort_values(keys + ["_delta_distance", "strike"], kind="mergesort")
                .drop_duplicates(keys)[keys + ["derived_iv"]]
                .rename(columns={"derived_iv": f"{prefix}_25d_iv"})
            )
            summary = summary.merge(oi, on=keys, how="left")
            summary = summary.merge(wall, on=keys, how="left")
            summary = summary.merge(delta_25, on=keys, how="left")

        result = paired.merge(summary, on=keys, how="inner")
        result["liquidity_score"] = result._liquid / result._row_count
        result = result.sort_values(["underlying", "expiry", "observed_at"], kind="mergesort")
        result["put_oi_change"] = result.put_oi - result.groupby(
            ["underlying", "expiry"], sort=False
        ).put_oi.shift()
        result["call_oi_change"] = result.call_oi - result.groupby(
            ["underlying", "expiry"], sort=False
        ).call_oi.shift()
        both_iv = result.call_iv.notna() & result.put_iv.notna()
        result["atm_iv"] = np.where(
            both_iv, (result.call_iv + result.put_iv) / 2,
            result.call_iv.combine_first(result.put_iv),
        )
        result = result.sort_values(["observed_at", "underlying", "expiry"], kind="mergesort")
        result["longer_term_iv"] = result.groupby(
            ["observed_at", "underlying"], sort=False
        ).atm_iv.shift(-1)

        complete_pair = result.call_close.notna() & result.put_close.notna()
        result["observed_at"] = pd.to_datetime(result.observed_at)
        result["batch_id"] = batch_id
        result["spot"] = result.spot_close
        result["atm_call_close"] = result.call_close
        result["atm_put_close"] = result.put_close
        result["atm_call_iv"] = result.call_iv
        result["atm_put_iv"] = result.put_iv
        result["atm_straddle"] = (result.call_close + result.put_close).where(complete_pair)
        result["expected_move_expiry"] = result.atm_straddle
        result["implied_move_expiry_pct"] = (100 * result.atm_straddle / result.spot).where(complete_pair)
        result["implied_move_one_session_pct"] = 100 * result.atm_iv / np.sqrt(252.0)
        result["term_structure_ratio"] = result.atm_iv / result.longer_term_iv.replace(0, np.nan)
        result["term_structure_slope"] = 100 * (result.longer_term_iv - result.atm_iv)
        result["put_25d_iv"] = result.put_25d_iv
        result["call_25d_iv"] = result.call_25d_iv
        result["skew_25d"] = result.put_25d_iv - result.call_25d_iv
        result["put_oi_wall"] = result.put_wall
        result["call_oi_wall"] = result.call_wall
        result["put_call_oi_ratio"] = result.put_oi / result.call_oi.replace(0, np.nan)
        result["bid_ask_available"] = False
        result["asof_timestamp"] = result.observed_at
        result["quality_flag"] = np.select(
            [~complete_pair, result.call_iv.isna() & result.put_iv.isna()],
            ["INCOMPLETE_ATM_PAIR", "MISSING_ATM_IV"], default="OK",
        )
        for name in ("put_oi_change", "call_oi_change", "total_volume", "total_open_interest"):
            result[name] = pd.array(result[name], dtype="Int64")
        return result[list(CHAIN_COLUMNS)].reset_index(drop=True), rolling_atm

    @staticmethod
    def _calculate_rolling_atm(base: pd.DataFrame, atm: pd.DataFrame, keys: list, batch_id: str) -> pd.DataFrame:
        """For each (observed_at,underlying,expiry) snapshot's ATM strike, find
        whichever strikes within +/-ROLLING_ATM_MAX_OFFSET steps actually
        traded that minute (both option types), tagged with their integer
        offset from ATM. Strikes with no matching row simply produce no row
        here -- same "sparse" semantics RemoteDBProvider's query-time join
        always had.
        """
        offsets = np.arange(-ROLLING_ATM_MAX_OFFSET, ROLLING_ATM_MAX_OFFSET + 1)
        targets = atm.copy()
        targets["_step"] = targets.underlying.map(STRIKE_STEP)
        targets = targets.loc[targets.index.repeat(len(offsets))].reset_index(drop=True)
        targets["atm_offset"] = np.tile(offsets, len(atm))
        targets["target_strike"] = targets.atm_strike + targets.atm_offset * targets._step
        rolling = base.merge(
            targets[keys + ["atm_offset", "target_strike"]],
            left_on=keys + ["strike"], right_on=keys + ["target_strike"], how="inner",
        )
        rolling["batch_id"] = batch_id
        rolling["atm_offset"] = pd.array(rolling.atm_offset, dtype="Int64")
        rolling["volume"] = pd.array(rolling.volume, dtype="Int64")
        rolling["open_interest"] = pd.array(rolling.open_interest, dtype="Int64")
        return rolling[list(ROLLING_ATM_COLUMNS)].reset_index(drop=True)

    @classmethod
    def _copy_rolling_atm(cls, cursor, batch_id: str, session_start: datetime, session_end: datetime, frame: pd.DataFrame) -> None:
        cursor.execute(
            "DELETE FROM index_option_rolling_atm WHERE batch_id=%s AND observed_at >= %s AND observed_at < %s",
            (batch_id, session_start, session_end),
        )
        if frame.empty:
            return
        columns = ",".join(ROLLING_ATM_COLUMNS)
        table = pa.Table.from_pandas(frame, preserve_index=False)
        with cursor.copy(f"COPY index_option_rolling_atm ({columns}) FROM STDIN WITH (FORMAT CSV)") as copy:
            for batch in table.to_batches(max_chunksize=10_000):
                copy.write(cls._copy_csv_block(batch))

    @classmethod
    def _copy_chain_features(cls, cursor, frame: pd.DataFrame) -> None:
        columns = ",".join(CHAIN_COLUMNS)
        table = pa.Table.from_pandas(frame, preserve_index=False)
        with cursor.copy(f"COPY index_chain_feature ({columns}) FROM STDIN WITH (FORMAT CSV)") as copy:
            for batch in table.to_batches(max_chunksize=10_000):
                copy.write(cls._copy_csv_block(batch))

    def _build_chain_features(
        self, cursor, batch_id: str, session_date: date,
        chain_features: pd.DataFrame | None = None,
    ) -> None:
        session_start, session_end = self._session_bounds(session_date)
        # The chain query has several ordered aggregates and reuses its base CTE.
        # The server default is intentionally small and makes a session spill
        # heavily to temporary files. Keep the larger budget transaction-local
        # so it cannot affect Django or any other database workload.
        cursor.execute("SELECT set_config('work_mem', %s, true)", ("64MB",))
        cursor.execute(
            """DELETE FROM index_chain_feature
               WHERE batch_id=%s AND observed_at >= %s AND observed_at < %s""",
            (batch_id, session_start, session_end),
        )
        if chain_features is not None:
            self._copy_chain_features(cursor, chain_features)
            self._update_rolling_features(cursor, batch_id, session_start, session_end)
            return
        cursor.execute(
            """WITH base AS (
                 SELECT observed_at,underlying,expiry,spot_close,strike,close,derived_iv,
                   option_type,open_interest,volume,delta
                 FROM index_option_candle
                 WHERE batch_id=%s AND observed_at >= %s AND observed_at < %s
                   AND is_regular_session AND spot_close IS NOT NULL
               ), atm AS (
                 SELECT DISTINCT ON (observed_at,underlying,expiry)
                   observed_at,underlying,expiry,spot_close,strike AS atm_strike
                 FROM base ORDER BY observed_at,underlying,expiry,abs(strike-spot_close),strike
               ), paired AS (
                 SELECT a.observed_at,a.underlying,a.expiry,a.spot_close,a.atm_strike,
                   max(b.close) FILTER (WHERE b.option_type='CALL') AS call_close,
                   max(b.close) FILTER (WHERE b.option_type='PUT') AS put_close,
                   max(b.derived_iv) FILTER (WHERE b.option_type='CALL') AS call_iv,
                   max(b.derived_iv) FILTER (WHERE b.option_type='PUT') AS put_iv
                 FROM atm a JOIN base b USING (observed_at,underlying,expiry)
                 WHERE b.strike=a.atm_strike GROUP BY 1,2,3,4,5
               ), aggregates AS (
                 SELECT observed_at,underlying,expiry,
                   (array_agg(strike ORDER BY open_interest DESC,strike) FILTER (WHERE option_type='PUT'))[1] AS put_wall,
                   (array_agg(strike ORDER BY open_interest DESC,strike) FILTER (WHERE option_type='CALL'))[1] AS call_wall,
                   sum(open_interest) FILTER (WHERE option_type='PUT') AS put_oi,
                   sum(open_interest) FILTER (WHERE option_type='CALL') AS call_oi,
                   sum(volume)::bigint AS total_volume,
                   sum(open_interest)::bigint AS total_open_interest,
                   count(*) FILTER (WHERE volume > 0 AND open_interest > 0)::double precision
                     / nullif(count(*),0) AS liquidity_score,
                   (array_agg(derived_iv ORDER BY abs(abs(delta)-0.25),strike)
                     FILTER (WHERE option_type='PUT' AND derived_iv IS NOT NULL AND delta IS NOT NULL))[1] AS put_25d_iv,
                   (array_agg(derived_iv ORDER BY abs(abs(delta)-0.25),strike)
                     FILTER (WHERE option_type='CALL' AND derived_iv IS NOT NULL AND delta IS NOT NULL))[1] AS call_25d_iv
                 FROM base GROUP BY 1,2,3
               ), ready AS (
                 SELECT p.*,a.put_wall,a.call_wall,a.put_oi,a.call_oi,a.total_volume,a.total_open_interest,
                   a.liquidity_score,a.put_25d_iv,a.call_25d_iv,
                   lag(a.put_oi) OVER (PARTITION BY p.underlying,p.expiry ORDER BY p.observed_at) AS prior_put_oi,
                   lag(a.call_oi) OVER (PARTITION BY p.underlying,p.expiry ORDER BY p.observed_at) AS prior_call_oi
                 FROM paired p JOIN aggregates a USING (observed_at,underlying,expiry)
               ), metrics AS (
                 SELECT ready.*,
                   CASE WHEN call_iv IS NOT NULL AND put_iv IS NOT NULL THEN (call_iv+put_iv)/2
                        ELSE coalesce(call_iv,put_iv) END AS calculated_atm_iv
                 FROM ready
               ), surface AS (
                 SELECT metrics.*,
                   lead(calculated_atm_iv) OVER (PARTITION BY observed_at,underlying ORDER BY expiry) AS longer_term_iv
                 FROM metrics
               )
               INSERT INTO index_chain_feature
                 (observed_at,underlying,expiry,batch_id,spot,atm_strike,atm_call_close,atm_put_close,
                  atm_call_iv,atm_put_iv,atm_iv,atm_straddle,expected_move_expiry,implied_move_expiry_pct,
                  implied_move_one_session_pct,longer_term_iv,term_structure_ratio,term_structure_slope,
                  put_25d_iv,call_25d_iv,skew_25d,put_oi_wall,call_oi_wall,put_call_oi_ratio,
                  put_oi_change,call_oi_change,total_volume,total_open_interest,liquidity_score,
                  bid_ask_available,asof_timestamp,quality_flag)
               SELECT observed_at,underlying,expiry,%s,spot_close,atm_strike,call_close,put_close,
                 call_iv,put_iv,calculated_atm_iv,
                 CASE WHEN call_close IS NOT NULL AND put_close IS NOT NULL THEN call_close+put_close END,
                 CASE WHEN call_close IS NOT NULL AND put_close IS NOT NULL THEN call_close+put_close END,
                 CASE WHEN call_close IS NOT NULL AND put_close IS NOT NULL THEN 100*(call_close+put_close)/spot_close END,
                 100*calculated_atm_iv/sqrt(252.0),
                 longer_term_iv,calculated_atm_iv/nullif(longer_term_iv,0),
                 100*(longer_term_iv-calculated_atm_iv),
                 put_25d_iv,call_25d_iv,put_25d_iv-call_25d_iv,
                 put_wall,call_wall,put_oi/nullif(call_oi,0),put_oi-prior_put_oi,call_oi-prior_call_oi,
                 total_volume,total_open_interest,liquidity_score,false,observed_at,
                 CASE WHEN call_close IS NULL OR put_close IS NULL THEN 'INCOMPLETE_ATM_PAIR'
                      WHEN call_iv IS NULL AND put_iv IS NULL THEN 'MISSING_ATM_IV' ELSE 'OK' END
               FROM surface ON CONFLICT (observed_at,underlying,expiry) DO NOTHING""",
            (batch_id, session_start, session_end, batch_id),
        )
        self._update_rolling_features(cursor, batch_id, session_start, session_end)

    def _update_rolling_features(
        self, cursor, batch_id: str, session_start: datetime, session_end: datetime,
    ) -> None:
        self._report("ROLLING_FEATURES", 0.94, "Building causal IV rank, percentile and realized volatility")
        cursor.execute(
            """WITH current_rows AS MATERIALIZED (
                 SELECT * FROM index_chain_feature
                 WHERE batch_id=%s AND observed_at >= %s AND observed_at < %s
               ), batch_context AS (
                 SELECT min((observed_at AT TIME ZONE 'Asia/Kolkata')::date) AS session_day
                 FROM current_rows
               ), historical_daily AS MATERIALIZED (
                 SELECT DISTINCT ON (f.underlying,(f.observed_at AT TIME ZONE 'Asia/Kolkata')::date)
                   f.underlying,(f.observed_at AT TIME ZONE 'Asia/Kolkata')::date AS session_day,f.atm_iv
                 FROM index_chain_feature f CROSS JOIN batch_context context
                 WHERE f.atm_iv IS NOT NULL
                   AND f.observed_at < (context.session_day::timestamp AT TIME ZONE 'Asia/Kolkata')
                 ORDER BY f.underlying,(f.observed_at AT TIME ZONE 'Asia/Kolkata')::date DESC,
                          f.observed_at DESC,f.expiry
               ), ranked_iv AS (
                 SELECT *,row_number() OVER (PARTITION BY underlying ORDER BY session_day DESC) AS position
                 FROM historical_daily
               ), iv_history AS MATERIALIZED (
                 SELECT underlying,count(*) AS samples,min(atm_iv) AS minimum_iv,
                   max(atm_iv) AS maximum_iv,array_agg(atm_iv) AS values
                 FROM ranked_iv WHERE position <= 252 GROUP BY underlying
               ), historical_close AS MATERIALIZED (
                 SELECT DISTINCT ON (u.underlying,u.session_date)
                   u.underlying,u.session_date,u.close
                 FROM index_underlying_candle u CROSS JOIN batch_context context
                 WHERE u.session_date < context.session_day
                 ORDER BY u.underlying,u.session_date DESC,u.observed_at DESC
               ), ranked_close AS (
                 SELECT *,row_number() OVER (PARTITION BY underlying ORDER BY session_date DESC) AS position
                 FROM historical_close
               ), returns AS (
                 SELECT underlying,
                   ln(close/nullif(lag(close) OVER (PARTITION BY underlying ORDER BY session_date),0)) AS return_value
                 FROM ranked_close WHERE position <= 21
               ), realized AS MATERIALIZED (
                 SELECT underlying,count(return_value) AS samples,
                   stddev_samp(return_value)*sqrt(252.0) AS realized_volatility
                 FROM returns GROUP BY underlying
               ), calculated AS (
                 SELECT c.observed_at,c.underlying,c.expiry,c.atm_iv,
                   history.samples,history.minimum_iv,history.maximum_iv,
                   percentile.value AS percentile,rv.samples AS rv_samples,rv.realized_volatility
                 FROM current_rows c
                 LEFT JOIN iv_history history USING (underlying)
                 LEFT JOIN realized rv USING (underlying)
                 LEFT JOIN LATERAL (
                   SELECT avg(CASE WHEN value < c.atm_iv THEN 1.0
                                   WHEN value = c.atm_iv THEN 0.5 ELSE 0.0 END) AS value
                   FROM unnest(history.values) value
                 ) percentile ON true
               )
               UPDATE index_chain_feature target SET
                 iv_rank_252=CASE WHEN x.samples >= 20 AND x.maximum_iv > x.minimum_iv
                   THEN 100*greatest(0,least(1,(x.atm_iv-x.minimum_iv)/(x.maximum_iv-x.minimum_iv))) END,
                 iv_percentile_252=CASE WHEN x.samples >= 20 THEN 100*x.percentile END,
                 realized_volatility_20=CASE WHEN x.rv_samples >= 20 THEN x.realized_volatility END,
                 iv_rv_ratio=CASE WHEN x.rv_samples >= 20 AND x.realized_volatility > 0
                   THEN x.atm_iv/x.realized_volatility END
               FROM calculated x
               WHERE target.observed_at=x.observed_at AND target.underlying=x.underlying
                 AND target.expiry=x.expiry
                 AND target.observed_at >= %s AND target.observed_at < %s""",
            (batch_id, session_start, session_end, session_start, session_end),
        )

    @staticmethod
    def _session_bounds(session_date: date) -> tuple[datetime, datetime]:
        start = pd.Timestamp(session_date, tz="Asia/Kolkata").to_pydatetime()
        return start, start + timedelta(days=1)

    def _ensure_single_scratch(self, target: Path) -> None:
        files = [path for path in self.options.scratch_dir.glob("*.parquet") if path != target]
        if files:
            retained = files[0]
            raise RuntimeError(
                f"Retained scratch belongs to a different source or batch: {retained.name}. "
                "Resume with the same --input-dir that created it; otherwise move or delete it "
                "only after reviewing its remote batch ledger entry."
            )
        free = shutil.disk_usage(self.options.scratch_dir).free
        if free < self.options.max_local_scratch_gb * 1024 ** 3:
            raise OSError("Insufficient local disk for the configured scratch cap")
