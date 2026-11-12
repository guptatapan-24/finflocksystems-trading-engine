"""The in-Backend scheduler: when it starts the worker, retries, locking, and what it records. Redis and the worker are faked."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from nfo_ingestion import scheduler as sch

IST = sch.IST


def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=IST).astimezone(timezone.utc)


class FakeRedis:
    def __init__(self, fail=False):
        self.store, self.fail = {}, fail

    def _check(self):
        if self.fail:
            raise ConnectionError("redis down")

    def get(self, key):
        self._check()
        return self.store.get(key)

    def set(self, key, value, ex=None, nx=False, px=None):
        self._check()
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def incr(self, key):
        self._check()
        self.store[key] = str(int(self.store.get(key, 0)) + 1)
        return int(self.store[key])

    def expire(self, key, seconds):
        return True

    def eval(self, *args, **kwargs):
        raise PermissionError("NOPERM this user has no permissions to run the 'eval' command")

    def delete(self, key):
        self._check()
        return 1 if self.store.pop(key, None) is not None else 0


class Harness:
    def __init__(self, exit_codes=(0,), enabled=True, redis=None):
        self.redis, self.exit_codes, self.runs, self.now = redis or FakeRedis(), list(exit_codes), [], at(4, 22, 5)
        self.logs = []
        self.scheduler = sch.Scheduler(state=sch.RunState(self.redis), runner=self.runner, enabled=lambda: enabled, clock=lambda: self.now, log=self.logs.append)

    def runner(self, day, attempt):
        self.runs.append((day, attempt))
        code = self.exit_codes.pop(0) if self.exit_codes else 0
        return {"status": "OK" if code == 0 else "FAILED", "exit_code": code, "code": None if code == 0 else "x", "message": "m", "sessions": []}


@pytest.mark.parametrize("day,hour,minute,expected", [
    (4, 21, 59, None),                 # Wed 21:59: not yet
    (4, 22, 0, date(2026, 11, 4)),     # Wed 22:00: Wednesday's run starts
    (4, 23, 59, date(2026, 11, 4)),
    (5, 0, 0, date(2026, 11, 4)),      # Thu 00:00 still belongs to Wednesday's run
    (5, 1, 29, date(2026, 11, 4)),
    (5, 1, 30, None),                  # the deadline: no new run
    (5, 12, 0, None),                  # daytime: never
])
def test_run_date_belongs_to_the_evening_the_run_started(day, hour, minute, expected):
    assert sch.run_date_for(at(day, hour, minute)) == expected


def test_weekends_have_no_run():
    assert sch.run_date_for(at(7, 23)) is None      # Saturday evening
    assert sch.run_date_for(at(8, 23)) is None      # Sunday evening
    assert sch.run_date_for(at(9, 0, 30)) is None   # Monday 00:30 belongs to Sunday
    assert sch.run_date_for(at(6, 23)) == date(2026, 11, 6)   # Friday evening runs
    assert sch.run_date_for(at(7, 0, 30)) == date(2026, 11, 6)  # and finishes into Saturday 01:30


def test_does_nothing_while_the_schedule_is_switched_off():
    h = Harness(enabled=False)
    assert h.scheduler.tick() == "disabled" and h.runs == []


def test_idle_outside_the_evening_window():
    h = Harness()
    h.now = at(4, 12)
    assert h.scheduler.tick() == "idle" and h.runs == []


def test_starts_the_worker_in_the_window_and_is_done_for_the_night_after_success():
    h = Harness(exit_codes=[0])
    assert h.scheduler.tick() == "ran" and h.runs == [(date(2026, 11, 4), 1)]
    h.now += timedelta(minutes=40)
    assert h.scheduler.tick() == "done" and len(h.runs) == 1


def test_a_backend_that_was_down_at_22_00_still_runs_if_back_before_01_30():
    h = Harness(exit_codes=[0])
    h.now = at(5, 0, 50)
    assert h.scheduler.tick() == "ran" and h.runs == [(date(2026, 11, 4), 1)]


def test_an_infrastructure_failure_is_retried_after_30_minutes_at_most_three_times():
    h = Harness(exit_codes=[20, 20, 20, 20])
    assert h.scheduler.tick() == "ran"
    h.now += timedelta(minutes=10)
    assert h.scheduler.tick() == "waiting" and len(h.runs) == 1
    for attempt in (2, 3):
        h.now += timedelta(minutes=31)
        assert h.scheduler.tick() == "ran" and h.runs[-1][1] == attempt
    h.now += timedelta(minutes=31)
    assert h.scheduler.tick() == "exhausted" and len(h.runs) == 3


@pytest.mark.parametrize("code", [30, 40, 50])
def test_data_quality_verification_and_window_failures_are_not_retried(code):
    h = Harness(exit_codes=[code, 0])
    assert h.scheduler.tick() == "ran"
    h.now += timedelta(minutes=45)
    assert h.scheduler.tick() == "exhausted" and len(h.runs) == 1


def test_only_one_run_can_be_active():
    h = Harness()
    token = sch.RunState(h.redis).acquire(60)
    assert token and h.scheduler.tick() == "busy" and h.runs == []
    sch.RunState(h.redis).release(token)
    assert h.scheduler.tick() == "ran"


def test_the_lock_is_released_even_if_the_runner_blows_up():
    h = Harness()

    def boom(day, attempt):
        raise RuntimeError("worker crashed")
    h.scheduler.runner = boom
    with pytest.raises(RuntimeError):
        h.scheduler.tick()
    assert not any(k.endswith("lock") for k in h.redis.store)


def test_redis_down_fails_closed_and_never_runs():
    h = Harness(redis=FakeRedis(fail=True))
    assert h.scheduler.tick() == "state_unavailable" and h.runs == []


def test_the_loop_survives_a_failing_tick():
    h = Harness()
    calls = []

    def flaky():
        calls.append(1)
        raise RuntimeError("boom")
    h.scheduler.tick = flaky
    h.scheduler.loop(once=True)
    assert calls == [1]


@pytest.mark.django_db
def test_every_run_leaves_a_secret_free_record():
    from apps.audit.models import AuditEvent

    sch.record_run(date(2026, 11, 4), 1, {"status": "FAILED", "exit_code": 20, "code": "spot_unavailable", "message": "relay down", "TIMESCALE_DB_URL": "postgresql://u:SECRET@h/d"})
    row = AuditEvent.objects.get(event_type="NFO_INGEST_RUN")
    assert row.data["code"] == "spot_unavailable" and row.data["attempt"] == 1 and "SECRET" not in json.dumps(row.data)


# ---- the worker process ---------------------------------------------------------------------------------------

class FakeProcess:
    def __init__(self, lines, code=0):
        self.stdout, self._code, self.killed = iter(lines), code, False

    def wait(self):
        return self._code

    def kill(self):
        self.killed = True


def test_worker_environment_is_isolated_from_baked_files_and_carries_the_gates(settings):
    settings.NFO_INGEST_MIN_SESSION_DATE, settings.NFO_INGEST_MAX_SESSIONS, settings.REMOTE_FEATURE_DB_URL = "2026-11-03", 2, "postgresql://u:p@h/d"
    env = sch.worker_environment(schedule_enabled=True)
    assert env["ENV_FILE"] == "/nonexistent" and env["SPOT_SOURCE"] == "gateway" and env["GDF_ACQUIRER"] == "dropbox_shared"
    assert env["SCHEDULE_ENABLED"] == "true" and env["MAX_SESSIONS_PER_RUN"] == "2" and env["MIN_SESSION_DATE"] == "2026-11-03"
    assert sch.worker_environment(schedule_enabled=False)["SCHEDULE_ENABLED"] == "false"


def test_a_blank_timescale_url_falls_back_to_the_remote_feature_db(settings, monkeypatch):
    # The baked .env once carried an empty TIMESCALE_DB_URL= line that hid the real value: blank must mean "not set".
    settings.REMOTE_FEATURE_DB_URL = "postgresql://u:p@h/d"
    monkeypatch.setenv("TIMESCALE_DB_URL", "  ")
    assert sch.worker_environment(schedule_enabled=False)["TIMESCALE_DB_URL"] == "postgresql://u:p@h/d"
    monkeypatch.setenv("TIMESCALE_DB_URL", "postgresql://x:y@other/db")
    assert sch.worker_environment(schedule_enabled=False)["TIMESCALE_DB_URL"] == "postgresql://x:y@other/db"
    monkeypatch.delenv("TIMESCALE_DB_URL")
    assert sch.worker_environment(schedule_enabled=False)["TIMESCALE_DB_URL"] == "postgresql://u:p@h/d"


def test_the_baked_env_file_yields_a_real_database_url():
    from dotenv import dotenv_values
    values = dotenv_values(".env.example")
    # A later blank line wins over an earlier real value, so TIMESCALE_DB_URL must be either absent or non-blank.
    assert values.get("TIMESCALE_DB_URL") is None or values["TIMESCALE_DB_URL"].strip()


def test_worker_output_is_streamed_and_the_summary_is_parsed():
    summary = {"status": "OK", "exit_code": 0, "code": None, "sessions": [{"session": "2026-11-03", "rows": 5}]}
    lines = ["step 1\n", sch.SUMMARY_PREFIX + json.dumps(summary) + "\n"]
    logged = []
    out = sch.run_worker(["--dry-run"], popen=lambda cmd, **kw: (logged.append((cmd, kw["env"]["SCHEDULE_ENABLED"])) or FakeProcess(lines)), log=lambda m: None)
    assert out["sessions"][0]["rows"] == 5 and logged[0][0][-3:] == ["--no-file-exit-code", "0", "--dry-run"] and logged[0][1] == "false"


def test_a_worker_that_dies_without_a_summary_is_reported_as_a_failure():
    out = sch.run_worker(popen=lambda cmd, **kw: FakeProcess(["Killed\n"], code=137), log=lambda m: None)
    assert out["exit_code"] == 137 and out["code"] == "no_summary" and out["status"] == "FAILED"
