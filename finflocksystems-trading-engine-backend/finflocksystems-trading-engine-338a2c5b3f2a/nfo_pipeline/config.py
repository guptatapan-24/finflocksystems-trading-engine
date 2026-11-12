from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / "pipeline" / "NFO(Contractwise)_03.09.2025 to 03.09.2027_1 Min Data"
DEFAULT_SCRATCH = ROOT / ".nfo_ingestion_scratch"


def database_url() -> str:
    load_dotenv(Path(os.getenv("ENV_FILE", ROOT / ".env")), override=False)
    value = os.getenv("TIMESCALE_DB_URL", "").strip()
    if not value:
        raise RuntimeError("TIMESCALE_DB_URL is not configured")
    parsed = urlparse(value)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname or not parsed.path.strip("/"):
        raise RuntimeError("TIMESCALE_DB_URL is not a valid PostgreSQL URL")
    return value


@dataclass(frozen=True)
class IngestionOptions:
    input_dir: Path = DEFAULT_INPUT
    scratch_dir: Path = DEFAULT_SCRATCH
    session: str | None = None
    from_date: str | None = None
    to_date: str | None = None
    resume: bool = False
    dry_run: bool = False
    max_local_scratch_gb: float = 2.0
    chunksize: int = 100_000
    copy_workers: int = 4
    carry_minutes: int = 15
    tws_host: str = "127.0.0.1"
    tws_port: int = 7497
    tws_client_id: int = 9241
    tws_timeout: float = 45.0
    request_delay: float = 0.35
    risk_free_rate: float = 0.068
    dividend_yield: float = 0.012
    skip_netbird_check: bool = False
    stable_file_seconds: int = 120
    # None means the full SUPPORTED_UNDERLYINGS set (normal behavior). A
    # non-empty subset scopes this run to backfilling just those underlyings
    # into sessions that may already be COMPLETE for the rest -- see
    # NFOIndexIngestion's use of this for why that needs its own
    # calculation_profile rather than reusing the default one.
    underlyings: tuple[str, ...] | None = None

    def validate(self) -> None:
        if self.session and (self.from_date or self.to_date):
            raise ValueError("--session cannot be combined with --from-date or --to-date")
        if bool(self.from_date) != bool(self.to_date):
            raise ValueError("--from-date and --to-date must be supplied together")
        if self.max_local_scratch_gb <= 0:
            raise ValueError("--max-local-scratch-gb must be positive")
        if self.chunksize < 1:
            raise ValueError("--chunksize must be positive")
        if not 1 <= self.copy_workers <= 8:
            raise ValueError("--copy-workers must be between 1 and 8")
        if self.tws_client_id <= 0:
            raise ValueError("The TWS client ID must be nonzero")
        if self.underlyings is not None:
            from .schema import SUPPORTED_UNDERLYINGS
            if not self.underlyings:
                raise ValueError("--underlyings cannot be empty when supplied")
            invalid = set(self.underlyings) - set(SUPPORTED_UNDERLYINGS)
            if invalid:
                raise ValueError(f"--underlyings has unsupported values: {sorted(invalid)}")
