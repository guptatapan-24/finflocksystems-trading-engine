#!/usr/bin/env python3
"""G1 equivalence test: do spot bars fetched through the IB Gateway's HTTP history endpoint match, bar for bar, what the
pipeline already stored in index_underlying_candle from its direct IBKR connection?

Strictly READ-ONLY: nothing is written to the database or to disk, and the only gateway call is the same history request the
production relay makes. Guardrails are enforced in code, like in production:
  * refuses to run outside the idle window 16:30-08:30 IST (the strategy trades 09:00-16:00 IST)
  * one request at a time, SPACING seconds apart, each with a timeout, NIFTY first as a canary
  * stops at the first failure of any kind and reports; never retries
  * the gateway token and the database URL are read from the environment and never printed

Run from trading-engine/ with the backend virtualenv, AFTER 16:30 IST, with the paper gateway logged in:
    set GATEWAY_URL=http://127.0.0.1:18082/api/v1
    set GATEWAY_SERVICE_TOKEN=<the gateway's service token>
    set TIMESCALE_DB_URL=<read access to the ingestion database>
    python scripts/nfo_equivalence_check.py --sessions 2026-11-02,2026-10-30
Exit code 0 only if every session and index matches exactly (same bar count, identical timestamps, OHLC within 1e-6, same volume).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]   # trading-engine/
sys.path.insert(0, str(ROOT))                # the nfo_ingestion package and the Backend's apps (for the exact request builder)

from nfo_ingestion import spot as sg  # noqa: E402

TOLERANCE = 1e-6


def compare(db_rows, bars):
    """db_rows: [(observed_at, open, high, low, close, volume)] from the DB; bars: validated adapter bars.
    Returns a list of human-readable differences (empty = identical)."""
    problems = []
    fetched = {bar["observed_at"].astimezone(timezone.utc).replace(second=0, microsecond=0): bar for bar in bars}
    stored = {row[0].astimezone(timezone.utc): row for row in db_rows}
    if len(fetched) != len(stored):
        problems.append(f"bar count differs: gateway {len(fetched)} vs database {len(stored)}")
    only_gateway, only_db = sorted(set(fetched) - set(stored)), sorted(set(stored) - set(fetched))
    if only_gateway:
        problems.append(f"{len(only_gateway)} timestamps only in the gateway reply, first {only_gateway[0].isoformat()}")
    if only_db:
        problems.append(f"{len(only_db)} timestamps only in the database, first {only_db[0].isoformat()}")
    worst, wrong_volume = 0.0, []
    for stamp in sorted(set(fetched) & set(stored)):
        row, bar = stored[stamp], fetched[stamp]
        for name, stored_value in zip(("open", "high", "low", "close"), row[1:5]):
            worst = max(worst, abs(float(stored_value) - bar[name]))
        if float(row[5] or 0) != float(bar["volume"]):
            wrong_volume.append(stamp)
    if worst > TOLERANCE:
        problems.append(f"largest OHLC difference {worst:.8f} exceeds {TOLERANCE}")
    if wrong_volume:
        problems.append(f"{len(wrong_volume)} bars with a different volume, first {wrong_volume[0].isoformat()}")
    return problems


def fetch_via_gateway(http, base_url, token, underlying, session_date, timeout=60.0, poll=1.0, sleep=time.sleep):
    """The same request the Backend relay sends (built by the Backend's own function), then poll the command until done."""
    from apps.market_data import nfo_spot

    payload = nfo_spot.build_history_payload(underlying, session_date)
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": f"g1-check:{underlying}:{session_date}:{int(time.time())}"}
    reply = http.post(f"{base_url}/market-data/history/", json=payload, headers=headers, timeout=(10, 30))
    if reply.status_code not in (200, 202):
        raise sg.SpotUnavailable(f"{underlying}: gateway answered HTTP {reply.status_code}")
    command_id = reply.json()["data"]["command_id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sleep(poll)
        current = http.get(f"{base_url}/commands/{command_id}/", headers={"Authorization": f"Bearer {token}"}, timeout=(10, 30)).json()["data"]
        if current.get("status") == "COMPLETED":
            result = current.get("result") or {}
            return {"underlying": underlying, "session_date": session_date.isoformat(), "conid": result.get("conid"),
                    "symbol": result.get("symbol"), "bars": [{"observed_at": b["date"], **{k: b[k] for k in ("open", "high", "low", "close", "volume")}}
                                                              for b in result.get("bars", [])]}
        if current.get("status") in ("FAILED", "UNKNOWN"):
            raise sg.SpotUnavailable(f"{underlying}: gateway command {current['status']}: {str(current.get('last_error', ''))[:150]}")
    raise sg.SpotUnavailable(f"{underlying}: no answer within {timeout:g}s. The request may still be queued in the gateway; do not send more.")


def read_db_bars(url, underlying, session_date):
    import psycopg

    with psycopg.connect(url, connect_timeout=10, autocommit=True, options="-c default_transaction_read_only=on -c statement_timeout=20000") as conn, conn.cursor() as cur:
        cur.execute("SELECT observed_at, open, high, low, close, volume FROM index_underlying_candle "
                    "WHERE session_date=%s AND underlying=%s ORDER BY observed_at", (session_date, underlying))
        return cur.fetchall()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--sessions", required=True, help="comma-separated session dates (two recommended)")
    parser.add_argument("--spacing", type=float, default=7.0, help="seconds between gateway requests (5-10)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan only; no gateway call, no DB read")
    args = parser.parse_args(argv)
    sessions = [date.fromisoformat(item) for item in args.sessions.split(",")]
    plan = [(s, name) for s in sessions for name in sg.CANARY_FIRST]
    print(f"plan: {len(plan)} requests ({len(sessions)} sessions x 5 indexes), {args.spacing:g}s apart, NIFTY first, stop at first failure")
    if args.dry_run:
        return 0
    if not sg.in_window(datetime.now(sg.IST)):
        print("REFUSED: outside the idle window 16:30-08:30 IST (the strategy trades 09:00-16:00 IST). Nothing was contacted.")
        return 50
    base_url, token, db_url = (os.environ.get(n, "").strip().rstrip("/") for n in ("GATEWAY_URL", "GATEWAY_SERVICE_TOKEN", "TIMESCALE_DB_URL"))
    if not (base_url and token and db_url):
        print("REFUSED: set GATEWAY_URL, GATEWAY_SERVICE_TOKEN and TIMESCALE_DB_URL in the environment.")
        return 2
    import requests

    http, failures, first = requests.Session(), 0, True
    for session_date, underlying in plan:
        if not sg.in_window(datetime.now(sg.IST)):
            print("STOPPED: the idle window ended mid-run.")
            return 50
        if not first:
            time.sleep(args.spacing)
        first = False
        try:
            reply = fetch_via_gateway(http, base_url, token, underlying, session_date)
            bars = sg.validate_bars(underlying, session_date, reply, min_bars=1)
            problems = compare(read_db_bars(db_url, underlying, session_date), bars)
        except sg.SpotError as exc:
            print(f"STOPPED at {underlying} {session_date}: {exc}")
            return 1
        status = "MATCH" if not problems else "MISMATCH"
        print(f"{status:8} {session_date} {underlying:11} gateway bars={len(bars)}" + ("" if not problems else "  " + "; ".join(problems)))
        if problems:
            print("Stopping at the first mismatch; nothing else is sent.")
            return 1
    print(f"ALL {len(plan)} series identical to the database (count, timestamps, OHLC within {TOLERANCE}, volume).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
