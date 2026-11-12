"""The G1 comparison logic and its refusals. No network, no database."""
import importlib.util
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("nfo_equivalence_check", Path(__file__).resolve().parents[1] / "scripts" / "nfo_equivalence_check.py")
eq = importlib.util.module_from_spec(_spec)
sys.modules["nfo_equivalence_check"] = eq
_spec.loader.exec_module(eq)

START = datetime(2026, 11, 2, 3, 45, tzinfo=timezone.utc)   # 09:15 IST


def db(n=5, **change):
    rows = [(START + timedelta(minutes=i), 100.0 + i, 102.0 + i, 99.0 + i, 101.0 + i, 0.0) for i in range(n)]
    for index, row in change.items():
        rows[int(index[1:])] = row
    return rows


def bars(n=5, shift=0.0, volume=0.0, offset=0):
    return [{"observed_at": START + timedelta(minutes=i + offset), "open": 100.0 + i + shift, "high": 102.0 + i, "low": 99.0 + i,
             "close": 101.0 + i, "volume": volume} for i in range(n)]


def test_identical_series_have_no_differences():
    assert eq.compare(db(), bars()) == []


def test_a_difference_below_the_tolerance_is_accepted_and_above_is_not():
    assert eq.compare(db(), bars(shift=5e-7)) == []
    assert any("OHLC" in p for p in eq.compare(db(), bars(shift=1e-3)))


def test_missing_or_extra_bars_are_reported_with_their_counts():
    problems = eq.compare(db(5), bars(4))
    assert any("bar count differs: gateway 4 vs database 5" in p for p in problems) and any("only in the database" in p for p in problems)
    assert any("only in the gateway" in p for p in eq.compare(db(4), bars(5)))


def test_shifted_timestamps_are_reported():
    problems = eq.compare(db(), bars(offset=1))
    assert any("only in the gateway" in p for p in problems) and any("only in the database" in p for p in problems)


def test_volume_must_match_exactly():
    assert any("different volume" in p for p in eq.compare(db(), bars(volume=5.0)))


def test_null_volume_in_the_database_counts_as_zero():
    rows = [(r[0], r[1], r[2], r[3], r[4], None) for r in db()]
    assert eq.compare(rows, bars()) == []


def test_the_request_is_built_by_the_backends_own_function():
    from apps.market_data import nfo_spot

    assert nfo_spot.build_history_payload("NIFTY", date(2026, 11, 2))["end_time"] == "20261102-18:30:00"


def test_refuses_during_strategy_hours_and_without_credentials(monkeypatch, capsys):
    monkeypatch.setattr(eq.sg, "in_window", lambda now: False)
    assert eq.main(["--sessions", "2026-11-02"]) == 50 and "REFUSED" in capsys.readouterr().out
    monkeypatch.setattr(eq.sg, "in_window", lambda now: True)
    for name in ("GATEWAY_URL", "GATEWAY_SERVICE_TOKEN", "TIMESCALE_DB_URL"):
        monkeypatch.delenv(name, raising=False)
    assert eq.main(["--sessions", "2026-11-02"]) == 2


def test_dry_run_contacts_nothing(monkeypatch, capsys):
    monkeypatch.setattr(eq.sg, "in_window", lambda now: pytest.fail("a dry run must not even check"))
    assert eq.main(["--sessions", "2026-11-02,2026-10-30", "--dry-run"]) == 0
    assert "10 requests" in capsys.readouterr().out
