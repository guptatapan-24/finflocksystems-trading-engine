from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import BrokerAccount
from apps.broker_gateway.models import BrokerGatewaySession
from apps.event_bus.models import StreamHealthMetric
from apps.execution.models import BrokerCommand
from apps.execution.readiness import (
    DEFAULT_REQUIRED_FLINK_JOBS,
    REQUIRED_WORKER_ROLES,
    collect_execution_readiness,
    record_worker_heartbeat,
)
from apps.instruments.models import Instrument
from apps.market_streams.models import (
    InstrumentMarketState,
    MarketBar,
    MarketDataSubscription,
    StrategyEvaluationJob,
)
from apps.oms.models import Order, OrderIntent
from apps.portfolios.models import TradingPortfolio
from apps.reconciliation.models import ReconciliationBreak, ReconciliationRun
from apps.strategies.models import (
    StrategyDefinition,
    StrategyInstance,
    StrategyVersion,
)


pytestmark = pytest.mark.django_db


class JsonResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def healthy_flink(now, *, missing=(), stopped=(), checkpoint_age=5):
    jobs = []
    ids = {}
    for index, name in enumerate(DEFAULT_REQUIRED_FLINK_JOBS, start=1):
        if name in missing:
            continue
        job_id = f"job-{index}"
        ids[job_id] = name
        jobs.append(
            {
                "jid": job_id,
                "name": name,
                "state": "FAILED" if name in stopped else "RUNNING",
            }
        )

    def get(url, timeout):
        if url.endswith("/jobs/overview"):
            return JsonResponse({"jobs": jobs})
        job_id = url.split("/jobs/", 1)[1].split("/", 1)[0]
        assert job_id in ids
        checkpoint_at = now - timedelta(seconds=checkpoint_age)
        return JsonResponse(
            {
                "latest": {
                    "completed": {
                        "id": 7,
                        "status": "COMPLETED",
                        "latest_ack_timestamp": int(
                            checkpoint_at.timestamp() * 1000
                        ),
                    }
                }
            }
        )

    return get


def flink_inventory(now, jobs, *, checkpoint_age=5):
    job_ids = {
        job.get("jid") or job.get("id")
        for job in jobs
        if job.get("jid") or job.get("id")
    }

    def get(url, timeout):
        if url.endswith("/jobs/overview"):
            return JsonResponse({"jobs": jobs})
        job_id = url.split("/jobs/", 1)[1].split("/", 1)[0]
        assert job_id in job_ids
        checkpoint_at = now - timedelta(seconds=checkpoint_age)
        return JsonResponse(
            {
                "latest": {
                    "completed": {
                        "id": 7,
                        "status": "COMPLETED",
                        "latest_ack_timestamp": int(
                            checkpoint_at.timestamp() * 1000
                        ),
                    }
                }
            }
        )

    return get


def healthy_runtime(settings):
    settings.KAFKA_ENABLED = True
    settings.EXECUTION_REQUIRED_FLINK_JOBS = DEFAULT_REQUIRED_FLINK_JOBS
    settings.FLINK_CHECKPOINT_STALE_SECONDS = 180
    settings.MARKET_RAW_PRODUCER_HEARTBEAT_STALE_SECONDS = 30
    settings.MARKET_CONSUMER_HEARTBEAT_STALE_SECONDS = 30
    settings.EXECUTION_WORKER_HEARTBEAT_STALE_SECONDS = 30
    settings.GATEWAY_CONNECTIVITY_STALE_SECONDS = 30
    settings.STRATEGY_JOB_BACKLOG_THRESHOLD = 100
    settings.TARGET_COORDINATION_BACKLOG_THRESHOLD = 100
    settings.PENDING_INTENT_MAX_AGE_SECONDS = 60
    settings.BROKER_COMMAND_MAX_AGE_SECONDS = 60
    settings.KAFKA_HEALTH_STALE_SECONDS = 60
    settings.OUTBOX_PUBLISHER_HEARTBEAT_STALE_SECONDS = 30
    StreamHealthMetric.objects.create(
        component="kafka",
        metric="connectivity",
        status="HEALTHY",
        value={
            "enabled": True,
            "topics": list(settings.EXECUTION_REQUIRED_KAFKA_TOPICS),
        },
    )
    StreamHealthMetric.objects.create(
        component="outbox-publisher",
        metric="heartbeat",
        status="HEALTHY",
        value={"published": 0, "failed": 0},
    )
    StreamHealthMetric.objects.create(
        component="market-raw-producer",
        metric="heartbeat",
        status="HEALTHY",
        value={"producer": "test"},
    )
    StreamHealthMetric.objects.create(
        component="backend-market-consumer",
        metric="heartbeat",
        status="HEALTHY",
        value={"consumer": "test"},
    )
    for role in REQUIRED_WORKER_ROLES:
        record_worker_heartbeat(role, worker=f"{role}@test")


def paper_scope(now):
    account = BrokerAccount.objects.create(
        account_id="DU-READINESS",
        net_liquidation=100000,
        available_cash=100000,
        is_reconciled=True,
    )
    session = BrokerGatewaySession.objects.create(
        display_name="Readiness Gateway",
        username_hint="paper",
        mode="paper",
        status=BrokerGatewaySession.Status.CONNECTED,
        child_container_name="readiness-gateway",
        internal_base_url="http://readiness-gateway:8080/api/v1",
        encrypted_gateway_token="encrypted",
        encrypted_novnc_password="encrypted",
        commands_enabled=True,
        last_gateway_state={"connected": True, "reconciled": True, "mode": "paper"},
        last_checked_at=now,
    )
    portfolio = TradingPortfolio.objects.create(
        name="Readiness Portfolio",
        account=account,
        gateway_session=session,
    )
    instrument = Instrument.objects.create(symbol="READY")
    definition = StrategyDefinition.objects.create(
        key="READINESS",
        name="Readiness",
        plugin_path="tests.strategy_plugin_fixture.Readiness",
    )
    strategy = StrategyInstance.objects.create(
        name="Readiness Strategy",
        definition=definition,
        portfolio=portfolio,
        instrument=instrument,
        timeframe="1m",
        execution_mode="PAPER",
        state="FLAT",
        enabled=True,
    )
    version = StrategyVersion.objects.create(
        strategy_instance=strategy,
        version=1,
        configuration_snapshot={},
        parameter_hash="readiness",
        activated_at=now,
    )
    InstrumentMarketState.objects.create(
        instrument=instrument,
        status="FRESH",
        reference_price=100,
        latest_event_at=now,
        watermark_at=now,
        stale_after_seconds=300,
    )
    MarketDataSubscription.objects.create(
        instrument=instrument,
        gateway_session=session,
        conid=12345,
        timeframe="1m",
        state="ACTIVE",
        consumer_count=1,
        last_event_at=now,
    )
    bar = MarketBar.objects.create(
        instrument=instrument,
        bar_id="readiness-bar",
        interval="1m",
        window_start=now - timedelta(minutes=1),
        window_end=now,
        open=100,
        high=101,
        low=99,
        close=100,
        volume=1000,
        version=1,
        is_final=True,
        produced_at=now,
    )
    return account, session, portfolio, instrument, strategy, version, bar


def blocker_codes(result):
    return {item["code"] for item in result["blockers"]}


def test_execution_readiness_endpoint_reports_all_healthy_signals(
    client, settings, monkeypatch
):
    healthy_runtime(settings)
    now = timezone.now()
    monkeypatch.setattr(
        "apps.execution.readiness.requests.get", healthy_flink(now)
    )

    response = client.get("/api/v1/execution/readiness/")

    assert response.status_code == 200
    result = response.json()["data"]
    assert result["ready"] is True
    assert result["automatic_execution_ready"] is True
    assert result["signals"]["flink"]["running_jobs"] == sorted(
        DEFAULT_REQUIRED_FLINK_JOBS
    )
    assert all(
        item["checkpoint_id"] == 7
        for item in result["signals"]["flink"]["jobs"]
    )
    assert result["signals"]["workers"].keys() == set(REQUIRED_WORKER_ROLES)
    assert result["signals"]["gateway"]["status"] == "NOT_REQUIRED"


# The market-data stages share a single Flink job, so "a required job is
# missing" and "a running job's checkpoint is stale" can no longer be staged in
# one scenario: removing the only required job leaves nothing to checkpoint.
# They are asserted separately instead.


def test_missing_required_flink_job_is_blocking(settings):
    healthy_runtime(settings)
    now = timezone.now()
    result = collect_execution_readiness(
        http_get=healthy_flink(
            now,
            # Named by the constant so this stays honest if the job is renamed.
            missing={DEFAULT_REQUIRED_FLINK_JOBS[0]},
        ),
        now=now,
    )

    assert result["ready"] is False
    assert "REQUIRED_FLINK_JOBS_MISSING" in blocker_codes(result)
    assert result["signals"]["flink"]["missing_jobs"] == [
        DEFAULT_REQUIRED_FLINK_JOBS[0]
    ]


def test_stale_checkpoint_on_a_running_flink_job_is_blocking(settings):
    healthy_runtime(settings)
    now = timezone.now()
    result = collect_execution_readiness(
        http_get=healthy_flink(now, checkpoint_age=300),
        now=now,
    )

    assert result["ready"] is False
    assert "FLINK_CHECKPOINT_STALE" in blocker_codes(result)
    # The job is present and RUNNING -- only its checkpoint is stale.
    assert result["signals"]["flink"]["missing_jobs"] == []


def test_running_flink_job_is_selected_over_older_canceled_job(settings):
    healthy_runtime(settings)
    now = timezone.now()
    name = DEFAULT_REQUIRED_FLINK_JOBS[0]
    settings.EXECUTION_REQUIRED_FLINK_JOBS = (name,)
    jobs = [
        {
            "jid": "running-job",
            "name": name,
            "state": "RUNNING",
            "start-time": int((now - timedelta(minutes=1)).timestamp() * 1000),
        },
        {
            "jid": "canceled-job",
            "name": name,
            "state": "CANCELED",
            "start-time": int((now - timedelta(hours=1)).timestamp() * 1000),
        },
    ]

    result = collect_execution_readiness(
        http_get=flink_inventory(now, jobs),
        now=now,
    )

    flink = result["signals"]["flink"]
    assert flink["jobs"][0]["job_id"] == "running-job"
    assert flink["running_jobs"] == [name]
    assert "REQUIRED_FLINK_JOBS_NOT_RUNNING" not in blocker_codes(result)


def test_newest_running_flink_job_is_selected(settings):
    healthy_runtime(settings)
    now = timezone.now()
    name = DEFAULT_REQUIRED_FLINK_JOBS[0]
    settings.EXECUTION_REQUIRED_FLINK_JOBS = (name,)
    jobs = [
        {
            "jid": "new-running-job",
            "name": name,
            "state": "RUNNING",
            "start-time": int((now - timedelta(minutes=1)).timestamp() * 1000),
        },
        {
            "jid": "old-running-job",
            "name": name,
            "state": "RUNNING",
            "start-time": int((now - timedelta(hours=1)).timestamp() * 1000),
        },
    ]

    result = collect_execution_readiness(
        http_get=flink_inventory(now, jobs),
        now=now,
    )

    assert result["signals"]["flink"]["jobs"][0]["job_id"] == "new-running-job"


def test_only_canceled_flink_job_is_reported_not_running(settings):
    healthy_runtime(settings)
    now = timezone.now()
    name = DEFAULT_REQUIRED_FLINK_JOBS[0]
    settings.EXECUTION_REQUIRED_FLINK_JOBS = (name,)
    jobs = [
        {
            "jid": "canceled-job",
            "name": name,
            "state": "CANCELED",
            "start-time": int((now - timedelta(hours=1)).timestamp() * 1000),
        }
    ]

    result = collect_execution_readiness(
        http_get=flink_inventory(now, jobs),
        now=now,
    )

    flink = result["signals"]["flink"]
    assert flink["jobs"][0]["job_id"] == "canceled-job"
    assert flink["not_running_jobs"] == [name]
    assert "REQUIRED_FLINK_JOBS_NOT_RUNNING" in blocker_codes(result)


def test_all_required_flink_jobs_running_with_historical_canceled_jobs(settings):
    healthy_runtime(settings)
    now = timezone.now()
    running_jobs = [
        {
            "jid": f"running-{index}",
            "name": name,
            "state": "RUNNING",
            "start-time": int((now - timedelta(minutes=index)).timestamp() * 1000),
        }
        for index, name in enumerate(DEFAULT_REQUIRED_FLINK_JOBS, start=1)
    ]
    canceled_jobs = [
        {
            "jid": f"canceled-{index}",
            "name": name,
            "state": "CANCELED",
            "start-time": int((now - timedelta(days=index)).timestamp() * 1000),
        }
        for index, name in enumerate(DEFAULT_REQUIRED_FLINK_JOBS, start=1)
    ]

    result = collect_execution_readiness(
        http_get=flink_inventory(now, running_jobs + canceled_jobs),
        now=now,
    )

    flink = result["signals"]["flink"]
    assert result["ready"] is True
    assert flink["running_jobs"] == sorted(DEFAULT_REQUIRED_FLINK_JOBS)
    assert flink["not_running_jobs"] == []
    assert {job["job_id"] for job in flink["jobs"]} == {
        job["jid"] for job in running_jobs
    }


def test_worker_heartbeat_and_strategy_backlog_block_readiness(settings):
    healthy_runtime(settings)
    now = timezone.now()
    _, _, _, _, strategy, version, bar = paper_scope(now)
    settings.STRATEGY_JOB_BACKLOG_THRESHOLD = 0
    StrategyEvaluationJob.objects.create(
        strategy_instance=strategy,
        strategy_version=version,
        bar=bar,
        market_bar_id=bar.bar_id,
        bar_version=bar.version,
        event_id="readiness-event",
        event_time=now,
        source_data_version=1,
        expected_input_identity_hashes=[],
        status="PENDING",
        idempotency_key="readiness-evaluation-job",
    )
    stale = StreamHealthMetric.objects.get(
        component="execution-worker", metric="intent_execution"
    )
    StreamHealthMetric.objects.filter(pk=stale.pk).update(
        observed_at=now - timedelta(minutes=5)
    )

    result = collect_execution_readiness(http_get=healthy_flink(now), now=now)

    codes = blocker_codes(result)
    assert "EXECUTION_WORKERS_ABSENT" in codes
    assert "STRATEGY_JOB_BACKLOG_EXCEEDED" in codes
    assert result["signals"]["strategy_job_backlog"]["count"] == 1
    assert (
        result["signals"]["workers"]["intent_execution"]["status"] == "STALE"
    )


def test_stale_market_unreconciled_gateway_and_uncertain_order_block_portfolio(
    settings,
):
    healthy_runtime(settings)
    now = timezone.now()
    account, session, portfolio, instrument, _, _, _ = paper_scope(now)
    InstrumentMarketState.objects.filter(instrument=instrument).update(
        latest_event_at=now - timedelta(hours=1)
    )
    session.last_gateway_state = {
        "connected": True,
        "reconciled": False,
        "mode": "paper",
    }
    session.save(update_fields=["last_gateway_state", "updated_at"])
    account.is_reconciled = False
    account.save(update_fields=["is_reconciled", "updated_at"])
    run = ReconciliationRun.objects.create(
        broker_account=account,
        gateway_session=session,
        trigger="readiness",
        status="FAILED",
    )
    ReconciliationBreak.objects.create(
        run=run,
        category="ORDER",
        severity="ERROR",
        material=True,
        resolved=False,
    )
    intent = OrderIntent.objects.create(
        portfolio=portfolio,
        instrument=instrument,
        side="BUY",
        quantity=1,
        reference_price=100,
        idempotency_key="readiness-intent",
        operation_status="QUEUED",
        source="REBALANCE",
        mode="PAPER",
    )
    order = Order.objects.create(
        intent=intent,
        internal_id="readiness-order",
        status="UNKNOWN",
        quantity=1,
    )
    BrokerCommand.objects.create(
        order=order,
        internal_order_id=order.internal_id,
        gateway_session=session,
        command_type=BrokerCommand.CommandType.PLACE,
        idempotency_key="readiness-command",
        request_payload={"internal_id": order.internal_id},
        request_hash="readiness-command-hash",
        status=BrokerCommand.Status.UNCERTAIN,
        uncertainty_reason="response lost",
    )

    result = collect_execution_readiness(http_get=healthy_flink(now), now=now)

    codes = blocker_codes(result)
    assert "MARKET_DATA_STALE" in codes
    assert "GATEWAY_NOT_RECONCILED" in codes
    assert "BROKER_RECONCILIATION_NOT_READY" in codes
    assert "UNRESOLVED_UNCERTAIN_ORDER" in codes
    assert result["signals"]["broker_commands"]["scoped_uncertain_count"] == 1
    assert result["signals"]["broker_reconciliation"][
        "unresolved_material_breaks"
    ] == 1


def test_readiness_waits_for_exactly_the_jobs_bootstrap_submits():
    """Readiness and bootstrap must name the same Flink jobs.

    They are separate constants in separate modules. If bootstrap submits
    `market-pipeline-v1` while readiness still waits for the five per-stage
    names it replaced, every readiness check reports the streaming path as down
    and nothing will trade.
    """
    from scripts.ensure_flink_jobs import DEFAULT_REQUIRED_JOBS

    assert tuple(DEFAULT_REQUIRED_FLINK_JOBS) == tuple(DEFAULT_REQUIRED_JOBS)


@pytest.fixture
def coalescing_heartbeats(settings):
    """Enable heartbeat write coalescing with a clean per-process cache.

    config.test_settings pins the interval to 0 so ordinary cases always write;
    these cases are specifically about the throttle, so they turn it on and
    clear the module-level cache that survives the per-test database rollback.
    """
    from apps.execution import readiness

    settings.EXECUTION_WORKER_HEARTBEAT_WRITE_INTERVAL_SECONDS = 300
    readiness._LAST_HEARTBEAT_WRITE.clear()
    yield
    readiness._LAST_HEARTBEAT_WRITE.clear()


def heartbeat_row(role):
    return StreamHealthMetric.objects.get(component="execution-worker", metric=role)


def test_idle_heartbeats_are_coalesced_into_one_write(coalescing_heartbeats):
    """An idle order-path task runs once a second but need not write that often.

    Readiness only asks whether the row is fresher than
    EXECUTION_WORKER_HEARTBEAT_STALE_SECONDS, so repeating the identical
    "nothing to do" result inside the write interval must not touch the row.
    """
    role = "intent_execution"
    idle = {"last_result": {"claimed": 0, "commands_created": 0}}

    assert record_worker_heartbeat(role, worker="w1", details=idle) is not None
    observed_at = heartbeat_row(role).observed_at

    for _ in range(30):
        assert record_worker_heartbeat(role, worker="w1", details=idle) is None

    assert StreamHealthMetric.objects.filter(
        component="execution-worker", metric=role
    ).count() == 1
    assert heartbeat_row(role).observed_at == observed_at


def test_a_run_that_did_work_writes_through_the_throttle(coalescing_heartbeats):
    """The payload carries the task's return value, so real work changes it."""
    role = "intent_execution"
    idle = {"last_result": {"claimed": 0, "commands_created": 0}}
    worked = {"last_result": {"claimed": 2, "commands_created": 2}}

    record_worker_heartbeat(role, worker="w1", details=idle)
    idle_observed_at = heartbeat_row(role).observed_at

    assert record_worker_heartbeat(role, worker="w1", details=worked) is not None
    assert heartbeat_row(role).observed_at > idle_observed_at
    assert heartbeat_row(role).value["last_result"]["claimed"] == 2


def test_degraded_and_recovery_both_bypass_the_throttle(coalescing_heartbeats):
    """A failing worker must surface immediately, and so must its recovery."""
    role = "broker_commands"
    idle = {"last_result": {"claimed": 0}}

    record_worker_heartbeat(role, worker="w1", details=idle)

    record_worker_heartbeat(
        role, status="DEGRADED", worker="w1", details={"error": "gateway down"}, force=True
    )
    assert heartbeat_row(role).status == "DEGRADED"

    # Same idle payload as the first call, but the last write was DEGRADED, so
    # this is a status transition rather than a repeat and must land.
    assert record_worker_heartbeat(role, worker="w1", details=idle) is not None
    assert heartbeat_row(role).status == "HEALTHY"


def test_shipped_heartbeat_write_interval_stays_under_the_staleness_window():
    """The shipped write interval must leave margin under the staleness window.

    config.test_settings pins the interval to 0, so the `settings` fixture would
    make this pass for the wrong reason. Read what config.settings itself
    computes: if coalescing is ever configured to write less often than
    readiness tolerates, an idle-but-healthy worker reads as absent and the
    stack stops trading.
    """
    import config.settings as production_settings

    assert 0 < production_settings.EXECUTION_WORKER_HEARTBEAT_WRITE_INTERVAL_SECONDS
    assert (
        production_settings.EXECUTION_WORKER_HEARTBEAT_WRITE_INTERVAL_SECONDS
        < production_settings.EXECUTION_WORKER_HEARTBEAT_STALE_SECONDS
    )
