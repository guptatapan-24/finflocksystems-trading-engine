#!/usr/bin/env python3
"""Nightly NFO ingestion worker, started by the Backend's scheduler (nfo_ingestion/scheduler.py; see ingestionclaude.md section 17).

Orchestration only: every real step (Dropbox download, validation, spot enrichment, load, verification) is the
existing `nfo_pipeline` code, unchanged. Nothing here changes the ledger, the staging logic or the DB safety checks.
Safe to re-run: the batch ledger skips sessions that are already COMPLETE and `resume=True` reuses a retained scratch
file after a crash.

Spot source (SPOT_SOURCE): `gateway` (production) fetches the index bars through the Backend's own read-only spot access
(nfo_ingestion/spot.py -> apps.market_data.nfo_spot -> the paper IB Gateway's HTTP history endpoint) and installs that adapter in
place of the pipeline's direct-IBKR client at runtime; `tws` connects to a TWS/Gateway API port directly (local testing only).
In `gateway` mode the order per session is fixed:
  window check -> fetch + validate all five spot series -> ONLY THEN download the zip -> validate -> ingest -> verify.
A bad or missing spot reply therefore stops the night before anything is downloaded or written.

Exit codes: 0 done / nothing to do, 10 no new file by the deadline, 20 infrastructure or precondition (DB, gateway,
Dropbox, disk, backlog gap), 30 data quality (incl. an untrustworthy spot reply), 40 verification,
50 outside the idle window (never contacts the gateway while the strategy trades).
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
EXIT_OK, EXIT_NO_FILE, EXIT_INFRA, EXIT_DATA, EXIT_VERIFY, EXIT_WINDOW = 0, 10, 20, 30, 40, 50
MAX_SESSIONS_GATEWAY = 2   # hard cap per night in gateway mode (each session costs 5 history requests on the strategy's connection)
# IngestError.code -> exit code. Unknown codes are treated as infrastructure (fail closed, alert).
CODE_EXIT = {
    "db_unreachable": EXIT_INFRA, "download_failed": EXIT_INFRA, "disk_space": EXIT_INFRA,
    "no_data": EXIT_INFRA, "ingestion_failed": EXIT_INFRA,
    "validation_failed": EXIT_DATA, "verification_failed": EXIT_VERIFY, "outside_window": EXIT_WINDOW,
}
MAX_CONSECUTIVE_DOWNLOAD_FAILURES = 3


@dataclass(frozen=True)
class Config:
    input_dir: str
    scratch_dir: str
    tws_host: str
    tws_port: int
    tws_client_id: int
    min_session: date
    max_sessions: int
    deadline: dtime
    poll_minutes: float
    max_gap_days: int
    copy_workers: int
    max_scratch_gb: float
    acquirer: str
    spot_source: str = "tws"
    spot_spacing: float = 7.0
    spot_retry_wait: float = 60.0
    spot_min_bars: int = 300
    schedule_enabled: bool = False

    @classmethod
    def from_env(cls, env=os.environ) -> "Config":
        def need(name):
            value = env.get(name, "").strip()
            if not value:
                raise SystemExit(f"{name} is not set")
            return value

        hour, minute = need("RUN_DEADLINE_IST").split(":")
        spot_source = env.get("SPOT_SOURCE", "gateway").strip().lower()
        if spot_source not in ("tws", "gateway"):
            raise SystemExit("SPOT_SOURCE must be 'tws' or 'gateway'")
        max_sessions = int(env.get("MAX_SESSIONS_PER_RUN", "5"))
        if spot_source == "gateway":
            max_sessions = min(max_sessions, MAX_SESSIONS_GATEWAY)
        return cls(
            input_dir=need("INPUT_DIR"), scratch_dir=need("SCRATCH_DIR"),
            tws_host=env.get("TWS_HOST", "").strip() or ("unused" if spot_source == "gateway" else need("TWS_HOST")),
            tws_port=int(env.get("TWS_PORT", "4002")), tws_client_id=int(env.get("TWS_CLIENT_ID", "9241")),
            min_session=date.fromisoformat(need("MIN_SESSION_DATE")),
            max_sessions=max_sessions,
            deadline=dtime(int(hour), int(minute)), poll_minutes=float(env.get("POLL_MINUTES", "20")),
            max_gap_days=int(env.get("MAX_GAP_DAYS", "5")), copy_workers=int(env.get("COPY_WORKERS", "4")),
            max_scratch_gb=float(env.get("MAX_SCRATCH_GB", "2")), acquirer=env.get("GDF_ACQUIRER", "dropbox_shared"),
            spot_source=spot_source,
            spot_spacing=float(env.get("SPOT_SPACING_SECONDS", "7")), spot_retry_wait=float(env.get("SPOT_RETRY_WAIT_SECONDS", "60")),
            spot_min_bars=int(env.get("SPOT_MIN_BARS", "300")),
            schedule_enabled=env.get("SCHEDULE_ENABLED", "false").strip().lower() == "true",
        )


class Stop(Exception):
    """End the run with an exit code and a machine-readable reason."""

    def __init__(self, exit_code: int, code: str, message: str):
        super().__init__(message)
        self.exit_code, self.code = exit_code, code


def load_pipeline():
    """Imported lazily so tests (and --help) need neither the repo nor ibapi."""
    from nfo_pipeline import service
    from nfo_pipeline.acquire import get_acquirer as pipeline_get_acquirer

    def progress():
        from nfo_pipeline.progress import IngestionProgress
        return IngestionProgress(enabled=True, log_interval=30.0)

    def get_acquirer(name):
        """`dropbox_shared` is ours (dropbox_shared.py): the pipeline's DropboxAcquirer asks Dropbox for a recursive listing of a
        shared link, which Dropbox refuses, and downloads nested files from the wrong path. Other names go to the pipeline."""
        if name == "dropbox_shared":
            from nfo_ingestion import dropbox_shared
            return dropbox_shared.SharedFolderAcquirer(service.IngestError)
        return pipeline_get_acquirer(name)

    def list_remote(latest):
        """Dropbox listing (no download). Network/auth problems become download_failed like the pipeline's own."""
        try:
            if os.environ.get("GDF_ACQUIRER", "dropbox_shared") == "dropbox_shared":
                from nfo_ingestion import dropbox_shared
                return dropbox_shared.SharedFolderAcquirer(service.IngestError).list_sessions(latest)
            return remote_sessions(None, latest)
        except service.IngestError:
            raise
        except Exception as exc:  # noqa: BLE001 - requests errors are not wrapped by the pipeline's listing code
            raise service.IngestError("download_failed", f"Dropbox listing failed ({type(exc).__name__}).") from None

    def install_spot(spot):
        """Swap the pipeline's direct-IBKR client for the gateway adapter, at runtime (no pipeline file is edited)."""
        from nfo_ingestion import spot as spot_adapter
        from nfo_pipeline import runner

        runner.ReadOnlyIBKRClient = lambda: spot
        runner.audit_api_surface = spot_adapter.audit_gateway_adapter

    return SimpleNamespace(service=service, get_acquirer=get_acquirer, progress=progress, list_remote=list_remote,
                           install_spot=install_spot)


def tcp_open(host: str, port: int, timeout: float = 5.0) -> bool:
    """Port check only. No API login, so it cannot disturb the gateway or the strategy."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


WAIT_FROM = dtime(19, 0)  # the provider uploads around 20:00-21:00 IST; a start before this is a daytime run


def compute_deadline(start: datetime, deadline: dtime) -> datetime:
    """When to stop waiting for the provider's file.

    A night run (19:00 to the deadline) waits until the deadline: 22:00 start -> 01:30 next morning.
    A daytime run (a catch-up after the laptop was asleep, or a manual start) checks once and does
    not wait, so it can never hold the lock into the real 22:00 run.
    """
    end = datetime.combine(start.date(), deadline, tzinfo=IST)
    if start.time() < WAIT_FROM and start >= end:
        return start
    return end if end > start else end + timedelta(days=1)


def remote_sessions(pipe, latest):
    """Sessions on Dropbox newer than `latest`, from a listing only: nothing is downloaded or written."""
    from nfo_pipeline import acquire

    client = acquire.DropboxAcquirer()
    headers = {"Authorization": f"Bearer {client._token()}"}
    found = {acquire.daily_session(e["name"]) for e in client._list(headers, client._need("GDF_DROPBOX_SHARED_LINK"))}
    return sorted(d for d in found if d and (latest is None or d > latest))


def fail(summary, session, code):
    summary["failed"] = {"session": str(session), "code": code}
    if str(session) in summary["not_attempted"]:
        summary["not_attempted"].remove(str(session))


def _spot_module():
    from nfo_ingestion import spot   # lazy: the dry-run path does not need it

    return spot


def run(cfg: Config, pipe, args, *, clock=lambda: datetime.now(IST), sleep=time.sleep, probe=tcp_open, log=print,
        spot_factory=None) -> dict:
    """Returns the summary dict (always has `exit_code`). Never raises for pipeline failures."""
    service = pipe.service
    summary = {"status": "RUNNING", "exit_code": EXIT_OK, "code": None, "message": "", "floor": None, "spot_source": cfg.spot_source,
               "latest_db_before": None, "weekdays_without_data": [], "sessions": [], "failed": None, "not_attempted": [],
               "dry_run": args.dry_run, "spot_requests": 0}
    started = clock()
    holder = {}
    try:
        _run(cfg, pipe, args, summary, started, clock, sleep, probe, log, spot_factory, holder)
    except Stop as stop:
        summary.update(exit_code=stop.exit_code, code=stop.code, message=str(stop))
        summary["status"] = {EXIT_OK: "OK", EXIT_NO_FILE: "NO_FILE"}.get(stop.exit_code, "FAILED")
    except service.IngestError as exc:
        summary.update(exit_code=CODE_EXIT.get(exc.code, EXIT_INFRA), code=exc.code, message=str(exc), status="FAILED")
    except Exception as exc:  # noqa: BLE001 - last resort: fail closed with a clear, secret-free message
        summary.update(exit_code=EXIT_INFRA, code="unexpected", message=f"{type(exc).__name__}", status="FAILED")
        log(f"UNEXPECTED {type(exc).__name__}")
    else:
        summary["status"] = "OK"
    summary["spot_requests"] = getattr(holder.get("spot"), "requests_sent", 0)
    summary["seconds"] = round((clock() - started).total_seconds())
    return summary


def _spot_stop(exc, session, summary):
    """Map a spot-adapter failure to an exit code. Nothing has been downloaded or written for this session yet."""
    spot = _spot_module()
    fail(summary, session, exc.code)
    if isinstance(exc, spot.OutsideWindow):
        return Stop(EXIT_WINDOW, "outside_window", f"{session}: {exc}")
    if isinstance(exc, spot.SpotDataInvalid):
        return Stop(EXIT_DATA, "spot_invalid", f"{session}: {exc}. Nothing was downloaded or written.")
    return Stop(EXIT_INFRA, "spot_unavailable", f"{session}: {exc}. Nothing was downloaded or written; the next run retries.")


def _run(cfg, pipe, args, summary, started, clock, sleep, probe, log, spot_factory, holder):
    service = pipe.service
    gateway = cfg.spot_source == "gateway"
    manual = args.session is not None
    floor = date.fromisoformat(args.session) if manual else cfg.min_session
    cap = min(args.max_sessions or cfg.max_sessions, cfg.max_sessions)
    summary["floor"] = str(floor)

    if not manual and not args.dry_run and not cfg.schedule_enabled:
        raise Stop(EXIT_OK, "schedule_disabled", "Scheduled runs are switched off (SCHEDULE_ENABLED is not 'true'); manual and dry runs still work.")
    if not manual and not args.dry_run and started.date() < cfg.min_session:
        raise Stop(EXIT_OK, "before_start_date", f"Scheduled ingestion starts on {cfg.min_session}; nothing to do yet.")
    if gateway and not args.dry_run and not _spot_module().in_window(started):
        raise Stop(EXIT_WINDOW, "outside_window",
                   "Refusing to run: the idle window is 16:30-08:30 IST (the strategy trades 09:00-16:00 IST). Nothing was contacted.")

    # A fresh container (QCH child) starts with empty, ephemeral work folders; the ledger, not the disk, is the record.
    for folder in (cfg.input_dir, cfg.scratch_dir):
        os.makedirs(folder, exist_ok=True)

    log("step 1 database reachable?")
    service.precheck_database()
    latest = service.latest_db_session()
    summary["latest_db_before"] = str(latest) if latest else None
    log(f"latest COMPLETE session in DB: {latest}")
    if latest is None:
        raise service.IngestError("no_data", "The database has no completed sessions; run the initial load manually first.")

    if args.dry_run:
        local = service.local_sessions(cfg.input_dir)
        try:
            remote = pipe.list_remote(latest)
        except Exception as exc:  # noqa: BLE001 - listing is informational in a dry run
            remote = None
            log(f"Dropbox listing unavailable: {type(exc).__name__}")
        planned = sorted(d for d in set(local) | set(remote or []) if d > latest and d >= floor)
        summary.update(planned_local=[str(d) for d in sorted(d for d in local if d > latest and d >= floor)[:cap]],
                       planned=[str(d) for d in planned[:cap]],
                       dropbox_newer_than_db=None if remote is None else [str(d) for d in remote],
                       message="dry run: nothing downloaded, nothing written, IBKR and the gateway not contacted")
        return

    spot = None
    if gateway:
        log("step 2 spot source: the Backend's gateway access (no IBKR API connection from here)")
        spot = holder["spot"] = (spot_factory or _default_spot_factory)(cfg, log)
    else:
        log(f"step 2 gateway port {cfg.tws_host}:{cfg.tws_port} open? (port check only, no API login)")
        if not probe(cfg.tws_host, cfg.tws_port):
            raise Stop(EXIT_INFRA, "gateway_unreachable",
                       f"IB Gateway is not accepting connections on {cfg.tws_host}:{cfg.tws_port}. Is the trading stack running and logged in?")

    deadline = compute_deadline(started, cfg.deadline)
    acquirer = pipe.get_acquirer(cfg.acquirer)
    failures = 0
    while True:
        log("step 3 looking for new sessions (Dropbox listing + local files; nothing is downloaded yet)")
        try:
            remote = pipe.list_remote(latest)
            failures = 0
        except service.IngestError as exc:
            if exc.code != "download_failed":
                raise
            failures += 1
            log(f"Dropbox attempt failed ({failures}/{MAX_CONSECUTIVE_DOWNLOAD_FAILURES}): {exc}")
            if failures >= MAX_CONSECUTIVE_DOWNLOAD_FAILURES:
                raise
            remote = []
        local = service.local_sessions(cfg.input_dir)
        known = set(local) | set(remote)
        candidates = sorted(d for d in known if d > latest and d >= floor)
        if manual:
            candidates = [floor] if floor in known else []
            if not candidates:
                raise Stop(EXIT_INFRA, "no_data", f"Session {floor} is not available locally or on Dropbox.")
        if candidates:
            break
        remaining = (deadline - clock()).total_seconds()
        if remaining <= 0:
            raise Stop(EXIT_NO_FILE, "no_new_file",
                       f"No new session file by {cfg.deadline:%H:%M} IST (exchange holiday or late upload). The next run picks it up.")
        log(f"no new file yet; checking again in {cfg.poll_minutes:g} min (deadline {deadline:%Y-%m-%d %H:%M} IST)")
        sleep(min(cfg.poll_minutes * 60, remaining))

    gap = (candidates[0] - latest).days
    between = [latest + timedelta(days=n) for n in range(1, gap)]
    summary["weekdays_without_data"] = [str(d) for d in between if d.weekday() < 5]
    if summary["weekdays_without_data"] and not manual:
        log(f"WARNING: no data between the DB's latest ({latest}) and {candidates[0]} for weekdays "
            f"{summary['weekdays_without_data']}: exchange holidays, or sessions that were never loaded.")
    if not manual and gap > cfg.max_gap_days:
        raise Stop(EXIT_INFRA, "backlog_gap",
                   f"First new session {candidates[0]} is {gap} days after the DB's latest ({latest}). Load the sessions in "
                   "between with the manual command first, so rolling features stay correct. Nothing was ingested.")

    sessions = candidates[:cap]
    summary["not_attempted"] = [str(d) for d in sessions]
    log(f"step 4 plan: {[str(d) for d in sessions]} (cap {cap}, {len(candidates) - len(sessions)} left for later nights)")
    service.check_disk_space(cfg.scratch_dir, cfg.max_scratch_gb)

    for d in sessions:
        began = clock()
        if spot is not None:
            log(f"step 5 {d}: fetching and validating spot bars for all five indexes (nothing is downloaded or written yet)")
            try:
                spot.prefetch(d)
            except _spot_module().SpotError as exc:
                raise _spot_stop(exc, d, summary) from None
        if d not in local:
            log(f"step 6 {d}: downloading the session file")
            local = _download(cfg, pipe, acquirer, service, sleep, log, d, known)
        service.validate_session(local[d])
        if spot is not None:
            pipe.install_spot(spot)
        log(f"step 7 ingesting {d}")
        options = service.build_options(
            input_dir=cfg.input_dir, scratch_dir=cfg.scratch_dir, session=str(d), resume=True,
            copy_workers=cfg.copy_workers, max_local_scratch_gb=cfg.max_scratch_gb, tws_host=cfg.tws_host,
            tws_port=cfg.tws_port, tws_client_id=cfg.tws_client_id, skip_netbird_check=True,
        )
        try:
            service.run_ingestion(options, stdout=log, progress=pipe.progress())
        except (ValueError, AssertionError) as exc:  # ledger state FAILED_DATA_QUALITY
            fail(summary, d, "ingestion_failed")
            raise Stop(EXIT_DATA, "ingestion_failed", f"{d}: {type(exc).__name__}: {str(exc)[:300]}") from None
        except Exception as exc:  # noqa: BLE001 - spot adapter, RuntimeError/TimeoutError/OSError: map and stop
            if spot is not None and isinstance(exc, _spot_module().SpotError):
                raise _spot_stop(exc, d, summary) from None
            fail(summary, d, "ingestion_failed")
            raise Stop(EXIT_INFRA, "ingestion_failed", f"{d}: {type(exc).__name__}: {str(exc)[:300]}") from None
        log(f"step 8 verifying {d}")
        try:
            verified = service.verify_session(d)
        except service.IngestError:
            fail(summary, d, "verification_failed")
            raise
        summary["sessions"].append({"session": str(d), "rows": verified["rows"], "seconds": round((clock() - began).total_seconds())})
        summary["not_attempted"].remove(str(d))
        log(f"{d}: verified, {verified['rows']:,} rows")
    summary["message"] = f"ingested {len(sessions)} session(s)"


def _download(cfg, pipe, acquirer, service, sleep, log, session, known):
    """Download exactly one session: the acquirer skips sessions <= latest_db and any in `have`, so pass the day before and
    every other known session as already-have. One retry after 30 s, then the failure propagates."""
    for attempt in (1, 2):
        try:
            acquirer.acquire(cfg.input_dir, session - timedelta(days=1), {d for d in known if d != session})
            break
        except service.IngestError as exc:
            if exc.code != "download_failed" or attempt == 2:
                raise
            log(f"download failed ({exc}); retrying once in 30s")
            sleep(30)
    local = service.local_sessions(cfg.input_dir)
    if session not in local:
        raise Stop(EXIT_INFRA, "download_failed", f"{session}: the file was not found after downloading.")
    return local


def _default_spot_factory(cfg, log):
    import uuid

    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()   # the in-process spot access uses the Backend's ORM and Redis
    spot = _spot_module()
    return spot.GatewaySpotClient(run_id=uuid.uuid4().hex, spacing=cfg.spot_spacing, retry_wait=cfg.spot_retry_wait,
                                  min_bars=cfg.spot_min_bars, log=log)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Nightly NFO ingestion worker")
    parser.add_argument("--dry-run", action="store_true", help="Report only: no download, no DB write, no IBKR contact.")
    parser.add_argument("--session", help="Ingest exactly this YYYY-MM-DD session (manual; ignores the start-date floor).")
    parser.add_argument("--max-sessions", type=int, default=0, help="Lower the per-run cap (never raises it).")
    parser.add_argument("--no-file-exit-code", type=int, default=EXIT_NO_FILE,
                        help="Exit code when no new file arrives by the deadline (flow passes 0 so a holiday is not a failure).")
    args = parser.parse_args(argv)
    summary = run(Config.from_env(), load_pipeline(), args)
    if summary["exit_code"] == EXIT_NO_FILE:
        print("WARNING: " + summary["message"])
    print("NFO_INGEST_SUMMARY " + json.dumps(summary, default=str, sort_keys=True))
    failed = (summary.get("failed") or {}).get("session", "")
    print('::' + json.dumps({"outputs": {"state": summary["status"], "code": summary["code"] or "", "exit_code": summary["exit_code"],
                                         "failed_session": failed, "message": " ".join(str(summary["message"]).split())[:300]}}) + '::')
    return args.no_file_exit_code if summary["exit_code"] == EXIT_NO_FILE else summary["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
