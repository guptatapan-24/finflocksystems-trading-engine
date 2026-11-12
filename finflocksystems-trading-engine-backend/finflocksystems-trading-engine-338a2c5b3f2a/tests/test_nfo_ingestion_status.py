from datetime import date, datetime, timezone
from unittest import mock

import pytest

from apps.market_data import ingestion_status as module

IST_NIGHT = datetime(2026, 11, 5, 2, 0, tzinfo=timezone.utc)  # Thu 07:30 IST, Wed's run window closed
RECENT_OK = [(date(2026, 11, 4), "COMPLETE", 812345, datetime(2026, 11, 4, 17, 5, tzinfo=timezone.utc), None, None)]
LATEST_OK = (date(2026, 11, 4), 812345, datetime(2026, 11, 4, 17, 5, tzinfo=timezone.utc), "NFO/GFDLNFO_CONTRACT_04112026.zip")


@pytest.fixture(autouse=True)
def configured(settings):
    settings.REMOTE_FEATURE_DB_URL = "postgresql://u:p@db.example:5432/x"
    settings.NFO_LEDGER_PROFILE = "PROFILE"


def reader(latest, recent):
    return lambda url, profile: (latest, recent)


def test_expected_session_waits_for_the_run_window_to_close():
    assert module.expected_session(datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)) == date(2026, 11, 3)  # Wed 20:30 IST
    assert module.expected_session(datetime(2026, 11, 4, 17, 0, tzinfo=timezone.utc)) == date(2026, 11, 3)  # Wed 22:30 IST, still running
    assert module.expected_session(datetime(2026, 11, 4, 20, 30, tzinfo=timezone.utc)) == date(2026, 11, 4)  # Thu 02:00 IST


def test_expected_session_skips_weekends():
    assert module.expected_session(datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)) == date(2026, 11, 6)   # Sat -> Fri
    assert module.expected_session(datetime(2026, 11, 8, 20, 0, tzinfo=timezone.utc)) == date(2026, 11, 6)  # Sun -> Fri


def test_up_to_date():
    data = module.ingestion_status(IST_NIGHT, reader(LATEST_OK, RECENT_OK))
    assert (data["state"], data["latest_session"], data["latest_rows"]) == ("UP_TO_DATE", "2026-11-04", 812345)
    assert data["recent"][0]["state"] == "COMPLETE" and data["failure"] is None


def test_behind_without_failure_mentions_holiday():
    latest = (date(2026, 11, 3), 700, None, "x.zip")
    data = module.ingestion_status(IST_NIGHT, reader(latest, []))
    assert data["state"] == "BEHIND" and "holiday" in data["message"]


def test_failed_batch_newer_than_last_good_session():
    recent = [(date(2026, 11, 4), "FAILED_RETRYABLE", 0, None, datetime(2026, 11, 4, 17, tzinfo=timezone.utc), "TimeoutError")]
    latest = (date(2026, 11, 3), 700, None, "x.zip")
    data = module.ingestion_status(IST_NIGHT, reader(latest, recent))
    assert data["state"] == "FAILED" and data["failure"]["error_category"] == "TimeoutError"


def test_empty_ledger():
    assert module.ingestion_status(IST_NIGHT, reader(None, []))["state"] == "EMPTY"


def test_unreadable_ledger_is_reported_not_raised():
    def broken(url, profile):
        raise OSError("connection refused for u:p@db.example")
    data = module.ingestion_status(IST_NIGHT, broken)
    assert (data["available"], data["state"]) == (False, "UNAVAILABLE")
    assert "u:p" not in data["message"] and "db.example" not in data["message"]



def test_unreadable_ledger_shows_the_driver_reason_without_secrets():
    def broken(url, profile):
        raise OSError('failed to resolve host "db.example": [Errno -2] Name or service not known')
    message = module.ingestion_status(IST_NIGHT, broken)["message"]
    assert "OSError: failed to resolve host" in message and "Name or service not known" in message
    assert "db.example" not in message and "<host>" in message

def test_unconfigured(settings):
    settings.REMOTE_FEATURE_DB_URL = ""
    data = module.ingestion_status(IST_NIGHT, reader(None, []))
    assert (data["configured"], data["state"]) == (False, "UNAVAILABLE")


def test_endpoint_is_get_only_and_wraps_envelope(client):
    with mock.patch("apps.market_data.ingestion_status.read_ledger", reader(LATEST_OK, RECENT_OK)):
        ok = client.get("/api/v1/data-providers/nfo-ingestion/")
    assert ok.status_code == 200 and ok.json()["ok"] is True and "state" in ok.json()["data"]
    assert client.post("/api/v1/data-providers/nfo-ingestion/").status_code == 405
