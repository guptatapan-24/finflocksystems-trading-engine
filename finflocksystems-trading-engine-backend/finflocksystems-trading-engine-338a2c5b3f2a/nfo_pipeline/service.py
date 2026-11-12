"""Shared ingestion service used by both the CLI command and the
"Ingest Latest Data" job. Every DB-touching helper here is a boundary that
tests mock; nothing in this module needs Django."""
from __future__ import annotations

import shutil
import socket
import zipfile
from dataclasses import replace
from typing import TYPE_CHECKING
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import psycopg

from .config import DEFAULT_INPUT, DEFAULT_SCRATCH, IngestionOptions, database_url
from .transform import SOURCE_COLUMNS

if TYPE_CHECKING:
    from .runner import SourceJob

# runner.py imports the IBKR client (ibapi), so it is imported lazily.

DB_HINT = "Database unreachable - check that NetBird/VPN is connected."


class IngestError(Exception):
    """User-readable failure. `code` is one of: db_unreachable, no_data,
    download_failed, validation_failed, disk_space, ingestion_failed,
    verification_failed."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def run_ingestion(options: IngestionOptions, stdout=print, progress=None) -> dict:
    """Core of `manage.py ingest_nfo_index_options`."""
    from .runner import NFOIndexIngestion

    return NFOIndexIngestion(options, stdout=stdout, progress=progress).run()


def build_options(*, input_dir=None, scratch_dir=None, **overrides) -> IngestionOptions:
    base = IngestionOptions(
        input_dir=Path(input_dir or DEFAULT_INPUT), scratch_dir=Path(scratch_dir or DEFAULT_SCRATCH),
    )
    return replace(base, **overrides)


def local_sessions(input_dir: Path) -> dict[date, SourceJob]:
    if not Path(input_dir).is_dir():
        raise IngestError("no_data", f"Pipeline directory not found: {input_dir}")
    from .runner import discover_jobs

    return {job.session_date: job for job in discover_jobs(Path(input_dir))}


def plan_missing(local: dict[date, SourceJob], latest_db: date | None) -> list[date]:
    """Local sessions strictly newer than the newest COMPLETE session in the DB, oldest first."""
    if latest_db is None:
        raise IngestError(
            "no_data",
            "The database has no completed sessions; run the initial load with "
            "manage.py ingest_nfo_index_options instead of the button.",
        )
    return sorted(d for d in local if d > latest_db)


def _url() -> str:
    try:
        return database_url()
    except RuntimeError as exc:
        raise IngestError("db_unreachable", f"{exc}; set it in .env on the ingestion host.") from None


def _connect():
    return psycopg.connect(_url(), connect_timeout=10)


def precheck_database() -> None:
    parsed = urlparse(_url())
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 5432), timeout=5):
            pass
    except OSError:
        raise IngestError("db_unreachable", DB_HINT) from None


def latest_db_session() -> date | None:
    from .runner import CALCULATION_PROFILE

    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT max(session_date) FROM nfo_ingestion_batch WHERE state='COMPLETE' AND calculation_profile=%s",
                (CALCULATION_PROFILE,),
            )
            return cur.fetchone()[0]
    except (psycopg.OperationalError, OSError):
        raise IngestError("db_unreachable", DB_HINT) from None
    except psycopg.Error:
        raise IngestError("db_unreachable", "Could not read the ingestion ledger; check the database schema/credentials.") from None


def validate_session(job: SourceJob) -> dict:
    """Cheap pre-ingest checks: file present/non-empty, header, sample rows, date match."""
    if not job.archive.is_file() or job.archive.stat().st_size == 0:
        raise IngestError("validation_failed", f"{job.session_date}: archive missing or empty ({job.archive.name})")
    try:
        with zipfile.ZipFile(job.archive) as zf:
            info = zf.getinfo(job.member)
            if info.file_size == 0:
                raise IngestError("validation_failed", f"{job.session_date}: {job.member} is empty")
            with zf.open(job.member) as stream:
                sample = pd.read_csv(stream, nrows=500)
    except IngestError:
        raise
    except Exception as exc:
        raise IngestError("validation_failed", f"{job.session_date}: cannot read {job.archive.name}: {type(exc).__name__}") from None
    missing = set(SOURCE_COLUMNS) - set(sample.columns)
    if missing:
        raise IngestError("validation_failed", f"{job.session_date}: missing columns {sorted(missing)}")
    if sample.empty:
        raise IngestError("validation_failed", f"{job.session_date}: no data rows")
    dates = pd.to_datetime(sample["Date"], format="%d/%m/%Y", errors="coerce")
    if dates.isna().any() or set(dates.dt.date) != {job.session_date}:
        raise IngestError("validation_failed", f"{job.session_date}: Date column does not match the file's session date")
    return {"size_bytes": job.archive.stat().st_size, "sample_rows": int(len(sample))}


def check_disk_space(scratch_dir: Path, needed_gb: float) -> None:
    probe = Path(scratch_dir)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    free_gb = shutil.disk_usage(probe).free / 1024**3
    if free_gb < needed_gb:
        raise IngestError("disk_space", f"Not enough disk space: {free_gb:.1f} GB free, {needed_gb:.1f} GB needed.")


def verify_session(session: date) -> dict:
    """After ingestion: batch is COMPLETE and rows for the session exist in the DB."""
    from .runner import CALCULATION_PROFILE

    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT selected_rows FROM nfo_ingestion_batch WHERE session_date=%s AND state='COMPLETE' "
                "AND calculation_profile=%s ORDER BY completed_at DESC NULLS LAST LIMIT 1",
                (session, CALCULATION_PROFILE),
            )
            batch = cur.fetchone()
            cur.execute("SELECT count(*) FROM index_option_candle WHERE session_date=%s", (session,))
            rows = int(cur.fetchone()[0])
    except (psycopg.Error, OSError):
        raise IngestError("db_unreachable", DB_HINT) from None
    if not batch:
        raise IngestError("verification_failed", f"{session}: no COMPLETE batch found in the ledger")
    expected = int(batch[0] or 0)
    if rows <= 0 or (expected and rows < 0.9 * expected):
        raise IngestError("verification_failed", f"{session}: DB holds {rows:,} rows, expected about {expected:,}")
    return {"rows": rows, "expected": expected}
