"""run_nightly orchestration tests. The whole pipeline, the Dropbox listing and the spot adapter are faked:
no network, no ibapi, no Kestra.

Run:  python -m pytest tests/test_nfo_ingest_worker.py -q      (from trading-engine/)
"""
import argparse
from datetime import date, datetime, time as dtime, timedelta
from types import SimpleNamespace

import pytest

from nfo_ingestion import run_nightly as rn
from nfo_ingestion import spot as sg

IST = rn.IST
D = lambda day: date(2026, 11, day)  # noqa: E731


class IngestError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Clock:
    def __init__(self, start):
        self.now = start
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)


class FakePipeline:
    """Fake nfo_pipeline. `local` = files on disk, `on_dropbox` = what the Dropbox listing shows. `events` records the order of
    everything that touches Dropbox, the disk or the DB so tests can assert the sequence."""

    def __init__(self, latest=D(3), local=(), on_dropbox=(), clock=None):
        self.latest, self.local, self.on_dropbox, self.clock = latest, set(local), set(on_dropbox), clock
        self.calls, self.events, self.options, self.acquire_args, self.ingested, self.installed = [], [], [], [], [], []
        self.appear_after = None  # (seconds, sessions): files appear in the listing after that much waiting
        self.ingest_error = self.verify_error = None
        self.list_errors, self.acquire_errors = [], []
        self.free_gb_ok = True
        service = SimpleNamespace(IngestError=IngestError)
        service.precheck_database = lambda: self._note("precheck")
        service.latest_db_session = lambda: self.latest
        service.local_sessions = lambda d: {s: SimpleNamespace(session_date=s) for s in self.local}
        service.validate_session = lambda job: self._note(f"validate {job.session_date}")
        service.check_disk_space = self._disk
        service.build_options = lambda **kw: kw
        service.run_ingestion = self._ingest
        service.verify_session = self._verify
        self.service = service
        self.get_acquirer = lambda name: SimpleNamespace(acquire=self._acquire)
        self.progress = lambda: None
        self.list_remote = self._list
        self.install_spot = lambda spot: (self._note("install_spot"), self.installed.append(spot))

    def _note(self, event):
        self.calls.append(event)
        self.events.append(event)

    def _list(self, latest):
        self._note("list")
        if self.list_errors:
            raise self.list_errors.pop(0)
        if self.appear_after and self.clock and sum(self.clock.sleeps) >= self.appear_after[0]:
            self.on_dropbox |= set(self.appear_after[1])
        return sorted(d for d in self.on_dropbox if d > latest)

    def _disk(self, path, gb):
        if not self.free_gb_ok:
            raise IngestError("disk_space", "full")

    def _acquire(self, input_dir, latest, have):
        self.acquire_args.append((latest, set(have)))
        self._note(f"acquire after {latest}")
        if self.acquire_errors:
            raise self.acquire_errors.pop(0)
        new = {d for d in self.on_dropbox if d > latest and d not in have}
        self.local |= new
        return {"downloaded": sorted(str(d) for d in new), "message": ""}

    def _ingest(self, options, stdout=None, progress=None):
        self.options.append(options)
        if self.ingest_error:
            raise self.ingest_error
        self.ingested.append(options["session"])
        self._note(f"ingest {options['session']}")

    def _verify(self, session):
        self._note(f"verify {session}")
        if self.verify_error:
            raise self.verify_error
        return {"rows": 1000 + session.day, "expected": 1000}


class FakeSpot:
    """Stands in for GatewaySpotClient: records the sequence and can fail on a chosen session."""

    def __init__(self, pipe, fail_on=None, error=None):
        self.pipe, self.fail_on, self.error, self.requests_sent = pipe, fail_on, error, 0

    def prefetch(self, session):
        self.pipe._note(f"prefetch {session}")
        self.requests_sent += 5
        if self.error and (self.fail_on is None or session == self.fail_on):
            raise self.error


def cfg(**over):
    base = dict(input_dir="/data/pipeline", scratch_dir="/data/scratch", tws_host="gw", tws_port=4002, tws_client_id=9241,
                min_session=D(4), max_sessions=5, deadline=dtime(1, 30), poll_minutes=20, max_gap_days=5,
                copy_workers=4, max_scratch_gb=2.0, acquirer="dropbox_api", schedule_enabled=True)
    base.update(over)
    return rn.Config(**base)


def gateway_cfg(**over):
    return cfg(spot_source="gateway", max_sessions=2, **over)


def args(**over):
    base = dict(dry_run=False, session=None, max_sessions=0)
    base.update(over)
    return argparse.Namespace(**base)


def execute(pipe, clock=None, probe=lambda h, p: True, config=None, spot=None, **arg_over):
    clock = clock or Clock(datetime(2026, 11, 4, 22, 0, tzinfo=IST))
    pipe.clock = clock
    factory = (lambda c, log: spot) if spot is not None else None
    return rn.run(config or cfg(), pipe, args(**arg_over), clock=clock, sleep=clock.sleep, probe=probe, log=lambda m: None, spot_factory=factory)


def position(events, prefix):
    return next(i for i, e in enumerate(events) if e.startswith(prefix))


# =====================================================================================================
# Spot source = gateway (production): validate spot first, only then download and ingest
# =====================================================================================================

def test_gateway_order_is_window_then_spot_then_download_then_ingest():
    pipe = FakePipeline(latest=D(3), on_dropbox=[D(4)])
    out = execute(pipe, config=gateway_cfg(), spot=FakeSpot(pipe))
    assert out["exit_code"] == 0 and pipe.ingested == ["2026-11-04"]
    e = pipe.events
    assert position(e, "prefetch") < position(e, "acquire") < position(e, "validate") < position(e, "install_spot") < position(e, "ingest") < position(e, "verify")
    assert out["spot_requests"] == 5 and out["spot_source"] == "gateway"


def test_a_bad_spot_reply_stops_before_any_download_or_write():
    pipe = FakePipeline(latest=D(3), on_dropbox=[D(4)])
    out = execute(pipe, config=gateway_cfg(), spot=FakeSpot(pipe, error=sg.SpotDataInvalid("NIFTY: no bars returned")))
    assert (out["exit_code"], out["code"]) == (30, "spot_invalid")
    assert pipe.acquire_args == [] and pipe.ingested == [] and pipe.local == set() and "install_spot" not in pipe.events
    assert out["failed"] == {"session": "2026-11-04", "code": "spot_invalid"} and "Nothing was downloaded" in out["message"]


def test_an_unavailable_relay_stops_before_any_download_and_is_retryable_infra():
    pipe = FakePipeline(latest=D(3), on_dropbox=[D(4)])
    out = execute(pipe, config=gateway_cfg(), spot=FakeSpot(pipe, error=sg.SpotUnavailable("NIFTY: GATEWAY_UNAVAILABLE")))
    assert (out["exit_code"], out["code"]) == (20, "spot_unavailable") and pipe.acquire_args == [] and pipe.ingested == []


def test_outside_the_idle_window_nothing_at_all_is_contacted():
    pipe = FakePipeline(latest=D(3), on_dropbox=[D(4)])
    out = execute(pipe, config=gateway_cfg(), spot=FakeSpot(pipe), clock=Clock(datetime(2026, 11, 4, 11, 0, tzinfo=IST)))
    assert (out["exit_code"], out["code"]) == (50, "outside_window") and pipe.events == []


@pytest.mark.parametrize("hour,minute,exit_code", [(16, 29, 50), (16, 30, 0), (8, 29, 0), (8, 30, 50)])
def test_window_edges_for_a_whole_run(hour, minute, exit_code):
    pipe = FakePipeline(latest=D(3), on_dropbox=[D(4)])
    when = datetime(2026, 11, 4, hour, minute, tzinfo=IST) if hour >= 16 else datetime(2026, 11, 5, hour, minute, tzinfo=IST)
    assert execute(pipe, config=gateway_cfg(), spot=FakeSpot(pipe), clock=Clock(when))["exit_code"] == exit_code


def test_the_adapter_refusing_mid_run_is_also_exit_50():
    pipe = FakePipeline(latest=D(3), on_dropbox=[D(4)])
    out = execute(pipe, config=gateway_cfg(), spot=FakeSpot(pipe, error=sg.OutsideWindow("morning came")))
    assert (out["exit_code"], out["code"]) == (50, "outside_window") and pipe.acquire_args == []


def test_each_session_is_spot_checked_then_downloaded_then_ingested_before_the_next_starts():
    pipe = FakePipeline(latest=D(3), on_dropbox=[D(4), D(5), D(6)])
    out = execute(pipe, config=gateway_cfg(), spot=FakeSpot(pipe))
    kinds = [e.split()[0] for e in pipe.events if e.split()[0] in ("prefetch", "acquire", "ingest", "verify")]
    assert kinds == ["prefetch", "acquire", "ingest", "verify", "prefetch", "acquire", "ingest", "verify"]
    assert pipe.ingested == ["2026-11-04", "2026-11-05"] and out["exit_code"] == 0   # hard cap of 2 sessions a night


def test_only_the_session_being_ingested_is_downloaded():
    pipe = FakePipeline(latest=D(3), on_dropbox=[D(4), D(5), D(6)])
    execute(pipe, config=gateway_cfg(), spot=FakeSpot(pipe), max_sessions=1)
    assert pipe.local == {D(4)}   # 5 and 6 were seen in the listing but not downloaded


def test_a_failing_second_session_stops_before_its_own_download():
    pipe = FakePipeline(latest=D(3), on_dropbox=[D(4), D(5)])
    out = execute(pipe, config=gateway_cfg(), spot=FakeSpot(pipe, fail_on=D(5), error=sg.SpotDataInvalid("wrong date")))
    assert pipe.ingested == ["2026-11-04"] and pipe.local == {D(4)} and (out["exit_code"], out["code"]) == (30, "spot_invalid")
    assert [s["session"] for s in out["sessions"]] == ["2026-11-04"] and out["failed"]["session"] == "2026-11-05"


def test_a_spot_error_raised_inside_the_pipeline_is_mapped_not_reported_as_unexpected():
    pipe = FakePipeline(latest=D(3), on_dropbox=[D(4)])
    pipe.ingest_error = sg.SpotUnavailable("relay died mid-run")
    out = execute(pipe, config=gateway_cfg(), spot=FakeSpot(pipe))
    assert (out["exit_code"], out["code"]) == (20, "spot_unavailable")


def test_gateway_mode_never_probes_a_tws_port():
    pipe = FakePipeline(latest=D(3), on_dropbox=[D(4)])
    out = execute(pipe, config=gateway_cfg(), spot=FakeSpot(pipe), probe=lambda h, p: pytest.fail("no TWS port in gateway mode"))
    assert out["exit_code"] == 0


def test_gateway_dry_run_works_at_any_hour_and_never_creates_the_adapter():
    pipe = FakePipeline(latest=D(3), local=[D(4)], on_dropbox=[D(4), D(5)])
    out = rn.run(gateway_cfg(), pipe, args(dry_run=True), clock=Clock(datetime(2026, 11, 4, 11, 0, tzinfo=IST)), log=lambda m: None,
                 spot_factory=lambda c, l: pytest.fail("a dry run must not contact the spot relay"))
    assert out["exit_code"] == 0 and out["planned"] == ["2026-11-04", "2026-11-05"] and pipe.acquire_args == [] and pipe.ingested == []


def test_tws_mode_still_works_without_any_spot_step():
    pipe = FakePipeline(latest=D(3), on_dropbox=[D(4)])
    out = execute(pipe)
    assert out["exit_code"] == 0 and "install_spot" not in pipe.events and not any(e.startswith("prefetch") for e in pipe.events)


def test_config_clamps_gateway_mode_to_two_sessions_and_needs_no_tws_host():
    env = {"INPUT_DIR": "/i", "SCRATCH_DIR": "/s", "MIN_SESSION_DATE": "2026-11-04", "RUN_DEADLINE_IST": "01:30",
           "SPOT_SOURCE": "gateway", "MAX_SESSIONS_PER_RUN": "9"}
    c = rn.Config.from_env(env)
    assert (c.max_sessions, c.spot_source, c.spot_spacing, c.spot_retry_wait, c.spot_min_bars) == (2, "gateway", 7.0, 60.0, 300)
    with pytest.raises(SystemExit):
        rn.Config.from_env({**env, "SPOT_SOURCE": "ibkr"})
    assert rn.Config.from_env({**env, "SPOT_SOURCE": "tws", "TWS_HOST": "x"}).max_sessions == 9


def test_main_maps_outside_window_to_exit_50(monkeypatch, capsys):
    monkeypatch.setattr(rn.Config, "from_env", classmethod(lambda cls, env=None: cfg()))
    monkeypatch.setattr(rn, "load_pipeline", lambda: FakePipeline())
    monkeypatch.setattr(rn, "run", lambda *a, **k: {"exit_code": 50, "status": "FAILED", "code": "outside_window", "message": "x"})
    assert rn.main([]) == 50 and '"outside_window"' in capsys.readouterr().out


# =====================================================================================================
# Behaviour common to both spot sources
# =====================================================================================================

def test_normal_night_ingests_todays_session_and_verifies():
    pipe = FakePipeline(latest=D(3), on_dropbox=[D(4)])
    out = execute(pipe)
    assert out["exit_code"] == 0 and out["status"] == "OK" and pipe.ingested == ["2026-11-04"]
    assert out["sessions"][0]["session"] == "2026-11-04" and out["sessions"][0]["rows"] == 1004
    assert out["not_attempted"] == [] and out["failed"] is None


def test_options_pass_the_gateway_settings_and_keep_safety_flags():
    pipe = FakePipeline(latest=D(3), on_dropbox=[D(4)])
    execute(pipe)
    options = pipe.options[0]
    assert (options["tws_host"], options["tws_port"], options["tws_client_id"]) == ("gw", 4002, 9241)
    assert options["resume"] is True and options["skip_netbird_check"] is True
    assert options["session"] == "2026-11-04" and options["copy_workers"] == 4 and options["max_local_scratch_gb"] == 2.0


def test_sessions_before_the_floor_are_never_downloaded_or_ingested():
    pipe = FakePipeline(latest=D(1), local=[D(2)], on_dropbox=[D(2), D(3), D(4)])
    out = execute(pipe, config=cfg(max_gap_days=30))
    assert pipe.acquire_args[0][0] == D(3) and pipe.ingested == ["2026-11-04"] and out["exit_code"] == 0


def test_gap_after_the_db_latest_is_refused_not_ingested():
    pipe = FakePipeline(latest=date(2026, 10, 12), on_dropbox=[D(4)])
    out = execute(pipe)
    assert (out["exit_code"], out["code"]) == (20, "backlog_gap")
    assert pipe.ingested == [] and pipe.acquire_args == [] and "manual command" in out["message"]


def test_nothing_new_waits_then_exits_10_at_the_deadline():
    pipe = FakePipeline(latest=D(3), on_dropbox=[])
    clock = Clock(datetime(2026, 11, 4, 22, 0, tzinfo=IST))
    out = execute(pipe, clock=clock)
    assert (out["exit_code"], out["status"], out["code"]) == (10, "NO_FILE", "no_new_file")
    assert clock.now <= datetime(2026, 11, 5, 1, 30, tzinfo=IST)
    assert len(clock.sleeps) >= 10 and all(s <= 20 * 60 for s in clock.sleeps) and pipe.ingested == []


def test_a_file_that_lands_late_is_picked_up_while_waiting_and_the_spot_runs_only_then():
    pipe = FakePipeline(latest=D(3), on_dropbox=[])
    clock = Clock(datetime(2026, 11, 4, 22, 0, tzinfo=IST))
    pipe.appear_after = (60 * 60, [D(4)])
    spot = FakeSpot(pipe)
    out = execute(pipe, clock=clock, config=gateway_cfg(), spot=spot)
    assert out["exit_code"] == 0 and pipe.ingested == ["2026-11-04"] and 3 <= len(clock.sleeps) <= 4
    assert spot.requests_sent == 5   # no spot requests while waiting for the file


def test_per_run_cap_leaves_the_rest_for_later_nights():
    pipe = FakePipeline(latest=D(3), on_dropbox=[D(4), D(5), D(6), D(9), D(10), D(11), D(12)])
    out = execute(pipe, config=cfg(max_gap_days=10, max_sessions=5))
    assert pipe.ingested == ["2026-11-04", "2026-11-05", "2026-11-06", "2026-11-09", "2026-11-10"] and out["exit_code"] == 0


def test_cli_max_sessions_can_lower_but_not_raise_the_cap():
    pipe = FakePipeline(latest=D(3), on_dropbox=[D(4), D(5), D(6)])
    execute(pipe, max_sessions=2)
    assert len(pipe.ingested) == 2
    pipe = FakePipeline(latest=D(3), on_dropbox=[D(4), D(5), D(6)])
    execute(pipe, max_sessions=99, config=cfg(max_sessions=1))
    assert len(pipe.ingested) == 1


def test_tws_mode_gateway_port_down_fails_closed_before_listing_or_writing():
    pipe = FakePipeline(latest=D(3), on_dropbox=[D(4)])
    out = execute(pipe, probe=lambda h, p: False)
    assert (out["exit_code"], out["code"]) == (20, "gateway_unreachable") and pipe.acquire_args == [] and pipe.ingested == []


def test_database_unreachable_is_exit_20():
    pipe = FakePipeline()
    pipe.service.precheck_database = lambda: (_ for _ in ()).throw(IngestError("db_unreachable", "no route"))
    out = execute(pipe)
    assert (out["exit_code"], out["code"]) == (20, "db_unreachable")


def test_empty_ledger_is_refused():
    pipe = FakePipeline(latest=None, on_dropbox=[D(4)])
    out = execute(pipe)
    assert (out["exit_code"], out["code"]) == (20, "no_data") and pipe.ingested == []


def test_bad_archive_is_exit_30_and_nothing_is_ingested():
    pipe = FakePipeline(latest=D(3), on_dropbox=[D(4), D(5)])
    pipe.service.validate_session = lambda job: (_ for _ in ()).throw(IngestError("validation_failed", "bad header"))
    out = execute(pipe)
    assert (out["exit_code"], out["code"]) == (30, "validation_failed") and pipe.ingested == []


def test_disk_space_is_exit_20():
    pipe = FakePipeline(latest=D(3), on_dropbox=[D(4)])
    pipe.free_gb_ok = False
    out = execute(pipe)
    assert (out["exit_code"], out["code"]) == (20, "disk_space") and pipe.ingested == []


def test_ingestion_data_quality_error_is_exit_30_and_stops_the_run():
    pipe = FakePipeline(latest=D(3), on_dropbox=[D(4), D(5)])
    pipe.ingest_error = ValueError("Option OHLC integrity failed")
    out = execute(pipe)
    assert (out["exit_code"], out["code"]) == (30, "ingestion_failed")
    assert out["failed"] == {"session": "2026-11-04", "code": "ingestion_failed"}
    assert out["not_attempted"] == ["2026-11-05"] and len(pipe.options) == 1


@pytest.mark.parametrize("error", [RuntimeError("TimescaleDB authentication failed"), TimeoutError("handshake"), OSError("net")])
def test_ingestion_infrastructure_error_is_exit_20(error):
    pipe = FakePipeline(latest=D(3), on_dropbox=[D(4)])
    pipe.ingest_error = error
    out = execute(pipe)
    assert (out["exit_code"], out["code"]) == (20, "ingestion_failed")


def test_failed_verification_is_exit_40():
    pipe = FakePipeline(latest=D(3), on_dropbox=[D(4)])
    pipe.verify_error = IngestError("verification_failed", "too few rows")
    out = execute(pipe)
    assert (out["exit_code"], out["code"]) == (40, "verification_failed")
    assert out["failed"]["code"] == "verification_failed" and out["sessions"] == []


def test_transient_dropbox_listing_errors_are_tolerated_but_three_in_a_row_fail():
    pipe = FakePipeline(latest=D(3), on_dropbox=[D(4)])
    pipe.list_errors = [IngestError("download_failed", "503")]
    assert execute(pipe)["exit_code"] == 0
    pipe = FakePipeline(latest=D(3), on_dropbox=[D(4)])
    pipe.list_errors = [IngestError("download_failed", "503")] * 3
    out = execute(pipe)
    assert (out["exit_code"], out["code"]) == (20, "download_failed")


def test_a_failed_download_is_retried_once_after_30_seconds():
    pipe = FakePipeline(latest=D(3), on_dropbox=[D(4)])
    pipe.acquire_errors = [IngestError("download_failed", "reset")]
    clock = Clock(datetime(2026, 11, 4, 22, 0, tzinfo=IST))
    assert execute(pipe, clock=clock)["exit_code"] == 0 and 30 in clock.sleeps
    pipe = FakePipeline(latest=D(3), on_dropbox=[D(4)])
    pipe.acquire_errors = [IngestError("download_failed", "reset")] * 2
    assert (execute(pipe)["exit_code"], pipe.ingested) == (20, [])


def test_dry_run_writes_and_downloads_nothing_and_never_checks_the_gateway():
    pipe = FakePipeline(latest=D(3), local=[D(4), D(5)], on_dropbox=[D(4), D(5), D(6)])
    out = execute(pipe, probe=lambda h, p: pytest.fail("gateway must not be contacted in a dry run"), dry_run=True)
    assert out["exit_code"] == 0 and out["dry_run"] is True
    assert out["planned_local"] == ["2026-11-04", "2026-11-05"]
    assert out["dropbox_newer_than_db"] == ["2026-11-04", "2026-11-05", "2026-11-06"]
    assert pipe.acquire_args == [] and pipe.ingested == [] and pipe.options == []


def test_dry_run_survives_a_dropbox_listing_failure():
    pipe = FakePipeline(latest=D(3), local=[D(4)])
    pipe.list_errors = [RuntimeError("401")]
    out = execute(pipe, dry_run=True)
    assert out["exit_code"] == 0 and out["dropbox_newer_than_db"] is None


def test_manual_session_ignores_floor_and_gap_and_does_not_wait():
    pipe = FakePipeline(latest=date(2026, 10, 12), local=[date(2026, 10, 13)])
    out = execute(pipe, session="2026-10-13", config=cfg(min_session=D(4)))
    assert out["exit_code"] == 0 and pipe.ingested == ["2026-10-13"]


def test_manual_session_already_complete_is_passed_to_the_ledger_which_skips_it():
    pipe = FakePipeline(latest=D(5), local=[D(4)])
    out = execute(pipe, session="2026-11-04")
    assert out["exit_code"] == 0 and pipe.ingested == ["2026-11-04"]


def test_manual_session_that_does_not_exist_fails_immediately_without_waiting():
    clock = Clock(datetime(2026, 11, 4, 22, 0, tzinfo=IST))
    out = execute(FakePipeline(latest=D(3)), clock=clock, session="2026-11-04")
    assert (out["exit_code"], out["code"]) == (20, "no_data") and clock.sleeps == []


def test_unexpected_error_is_reported_without_details_that_could_leak_secrets():
    pipe = FakePipeline(latest=D(3), on_dropbox=[D(4)])
    pipe.service.precheck_database = lambda: (_ for _ in ()).throw(KeyError("postgresql://user:SECRET@host/db"))
    out = execute(pipe)
    assert (out["exit_code"], out["code"]) == (20, "unexpected") and "SECRET" not in str(out)


def test_scheduled_runs_do_nothing_until_the_schedule_is_switched_on():
    pipe = FakePipeline(latest=D(3), on_dropbox=[D(4)])
    out = execute(pipe, config=cfg(schedule_enabled=False))
    assert (out["exit_code"], out["code"]) == (0, "schedule_disabled") and pipe.events == [] and pipe.ingested == []


def test_manual_and_dry_runs_work_while_the_schedule_is_off():
    pipe = FakePipeline(latest=D(3), local=[D(4)], on_dropbox=[D(4)])
    assert execute(pipe, config=cfg(schedule_enabled=False), dry_run=True)["exit_code"] == 0
    assert execute(pipe, config=cfg(schedule_enabled=False), session="2026-11-04")["code"] is None and pipe.ingested == ["2026-11-04"]


def test_schedule_switch_reads_the_environment_and_defaults_to_off():
    env = {"INPUT_DIR": "/i", "SCRATCH_DIR": "/s", "SPOT_SOURCE": "tws", "TWS_HOST": "x", "MIN_SESSION_DATE": "2026-11-03", "RUN_DEADLINE_IST": "01:30"}
    assert rn.Config.from_env(env).schedule_enabled is False
    assert rn.Config.from_env({**env, "SCHEDULE_ENABLED": "TRUE"}).schedule_enabled is True


def test_scheduled_run_before_the_start_date_does_nothing_at_all():
    pipe = FakePipeline(latest=D(2), on_dropbox=[D(3)])
    out = execute(pipe, clock=Clock(datetime(2026, 11, 3, 22, 0, tzinfo=IST)))
    assert (out["exit_code"], out["code"]) == (0, "before_start_date") and pipe.calls == [] and pipe.acquire_args == []


def test_start_date_guard_does_not_block_dry_run():
    pipe = FakePipeline(latest=D(2), local=[D(3)])
    out = execute(pipe, clock=Clock(datetime(2026, 11, 3, 12, 0, tzinfo=IST)), dry_run=True)
    assert out["code"] is None and "precheck" in pipe.calls


def test_missing_weekday_between_db_latest_and_first_new_session_is_reported():
    pipe = FakePipeline(latest=D(2), on_dropbox=[D(4)])
    out = execute(pipe)
    assert out["exit_code"] == 0 and out["weekdays_without_data"] == ["2026-11-03"]


def test_weekend_between_sessions_is_not_flagged():
    pipe = FakePipeline(latest=date(2026, 11, 6), on_dropbox=[date(2026, 11, 9)])
    out = execute(pipe, clock=Clock(datetime(2026, 11, 9, 22, 0, tzinfo=IST)))
    assert out["exit_code"] == 0 and out["weekdays_without_data"] == []


def test_daytime_run_checks_once_and_never_waits_into_the_nightly_run():
    pipe = FakePipeline(latest=D(3), on_dropbox=[])
    clock = Clock(datetime(2026, 11, 5, 10, 0, tzinfo=IST))
    out = execute(pipe, clock=clock)
    assert (out["exit_code"], out["code"]) == (10, "no_new_file") and clock.sleeps == []


def test_work_folders_are_created_when_missing(tmp_path):
    pipe = FakePipeline(latest=D(3), on_dropbox=[D(4)])
    config = cfg(input_dir=str(tmp_path / "in" / "pipeline"), scratch_dir=str(tmp_path / "scratch"))
    assert execute(pipe, config=config)["exit_code"] == 0
    assert (tmp_path / "in" / "pipeline").is_dir() and (tmp_path / "scratch").is_dir()


def test_deadline_rolls_to_the_next_morning():
    start = datetime(2026, 11, 4, 22, 0, tzinfo=IST)
    assert rn.compute_deadline(start, dtime(1, 30)) == datetime(2026, 11, 5, 1, 30, tzinfo=IST)
    assert rn.compute_deadline(datetime(2026, 11, 5, 0, 10, tzinfo=IST), dtime(1, 30)) == datetime(2026, 11, 5, 1, 30, tzinfo=IST)


def test_config_from_env_requires_settings_and_reads_defaults():
    env = {"INPUT_DIR": "/i", "SCRATCH_DIR": "/s", "SPOT_SOURCE": "tws", "TWS_HOST": "gw", "TWS_PORT": "4002", "MIN_SESSION_DATE": "2026-11-04", "RUN_DEADLINE_IST": "01:30"}
    c = rn.Config.from_env(env)
    assert (c.tws_client_id, c.max_sessions, c.deadline, c.poll_minutes, c.acquirer, c.spot_source) == (9241, 5, dtime(1, 30), 20.0, "dropbox_shared", "tws")
    with pytest.raises(SystemExit):
        rn.Config.from_env({k: v for k, v in env.items() if k != "TWS_HOST"})


def test_main_maps_no_file_to_the_requested_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(rn.Config, "from_env", classmethod(lambda cls, env=None: cfg()))
    monkeypatch.setattr(rn, "load_pipeline", lambda: FakePipeline())
    monkeypatch.setattr(rn, "run", lambda *a, **k: {"exit_code": 10, "status": "NO_FILE", "code": "no_new_file", "message": "none today"})
    assert rn.main(["--no-file-exit-code", "0"]) == 0
    out = capsys.readouterr().out
    assert "WARNING: none today" in out and "NFO_INGEST_SUMMARY" in out and '::{"outputs"' in out
    assert rn.main([]) == 10
