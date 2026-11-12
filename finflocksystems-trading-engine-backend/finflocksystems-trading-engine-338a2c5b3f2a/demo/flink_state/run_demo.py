#!/usr/bin/env python3
"""Flink state demo: run the real market-pipeline-v1 job and measure the issue and the fix.

Variants (run each on a fresh stack, see README.md):

  baseline  the Flink operators exactly as they were before the fix (commit 13be80a,
            demo/flink_state/baseline_jobs/), and no outbox rehydration
  fixed     the current operators plus the backend outbox rehydration
            (apps/market_streams/rehydration.py)
  compare   print the two result files side by side with PASS/FAIL

What one variant run does, using the production code paths:

  1. Creates SMA_CROSSOVER(5/20) strategies on 1m bars in PostgreSQL and publishes
     their instrument registry and strategy inputs through the real outbox.
  2. Submits market-pipeline-v1 (all five stages) to Flink with the production
     state backend: HashMap on the TaskManager heap, no durable checkpoints.
  3. LOAD: replays synthetic 5-second ticks ~50x faster than real time and samples
     every few seconds: per-operator checkpointed state, Kafka lag, TaskManager
     heap. The backend market consumer persists bars and indicators to PostgreSQL.
  4. TIME PASSES: strategy.inputs.v1 retention is shortened so the activation
     events are deleted, standing in for "activated more than 7 days ago". Half
     the strategies ("recent") are re-registered afterwards.
  5. RESTART: the job is cancelled and resubmitted. A resubmitted job starts with
     empty state because no checkpoint is addressable, as in production.
  6. AFTER RESTART: the market resumes. The run measures, from PostgreSQL, whether
     each strategy still receives bars and non-null indicators, and (fixed only)
     whether the first post-restart indicator equals a recomputation from MarketBar.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parent
BACKEND = DEMO_DIR.parents[1]
RESULTS_DIR = DEMO_DIR / "results"
JOB_NAME = "market-pipeline-v1"
VERTEX = {"bar_aggregation": "bar-aggregation-state", "indicator_computation": "indicator-computation-state"}
BASELINE_UIDS = {"bar_aggregation": "registry-bars-v2", "indicator_computation": "registry-indicators-v2"}
FLINK_GROUPS = (
    ("market-normalization-v2", "market.raw.v1"),
    ("bar-aggregation-v2", "market.canonical.v1"),
    ("indicator-computation-v2", "market.bars.v1"),
)
BACKEND_GROUP = "finflock-backend-market-persistence-v2"
SIM_START = datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc)
STARTED = time.monotonic()


def log(message):
    print(f"[{time.monotonic() - STARTED:7.1f}s] {message}", flush=True)


def env_int(name, default):
    return int(os.getenv(name, str(default)))


# ---------------------------------------------------------------------------
# Kafka helpers
# ---------------------------------------------------------------------------

def bootstrap():
    return os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")


def watermarks(topic, partition=0):
    from confluent_kafka import Consumer, TopicPartition
    consumer = Consumer({"bootstrap.servers": bootstrap(), "group.id": f"flink-demo-probe-{uuid.uuid4().hex}"})
    try:
        return consumer.get_watermark_offsets(TopicPartition(topic, partition), timeout=10, cached=False)
    finally:
        consumer.close()


def group_lag(group, topic):
    from confluent_kafka import Consumer, TopicPartition
    consumer = Consumer({"bootstrap.servers": bootstrap(), "group.id": group, "enable.auto.commit": False})
    try:
        metadata = consumer.list_topics(topic, timeout=10)
        partitions = [TopicPartition(topic, p) for p in metadata.topics[topic].partitions]
        total = 0
        for item in consumer.committed(partitions, timeout=10):
            low, high = consumer.get_watermark_offsets(TopicPartition(topic, item.partition), timeout=10, cached=False)
            total += (high - low) if item.offset < 0 else max(0, high - item.offset)
        return total
    finally:
        consumer.close()


def flink_lag():
    return sum(group_lag(group, topic) for group, topic in FLINK_GROUPS)


def backend_lag():
    return sum(group_lag(BACKEND_GROUP, topic) for topic in ("market.bars.v1", "market.indicators.v1"))


def shorten_strategy_inputs_retention():
    from confluent_kafka.admin import AdminClient, ConfigResource, ResourceType
    admin = AdminClient({"bootstrap.servers": bootstrap()})
    resource = ConfigResource(ResourceType.TOPIC, "strategy.inputs.v1",
                              set_config={"retention.ms": "60000", "segment.ms": "10000"})
    for future in admin.alter_configs([resource]).values():
        future.result(timeout=30)


def produce(topic, key, value):
    from confluent_kafka import Producer
    producer = Producer({"bootstrap.servers": bootstrap()})
    producer.produce(topic, key=str(key).encode(), value=json.dumps(value).encode())
    producer.flush(10)


class MarketSimulator(threading.Thread):
    """Synthetic IBKR-style ticks onto market.raw.v1, one per instrument every 5 simulated seconds."""

    def __init__(self, instruments, steps_per_second):
        super().__init__(daemon=True)
        self.instruments = instruments
        self.interval = 1.0 / float(steps_per_second)
        self.sim = SIM_START
        self.prices = {item["instrument_id"]: Decimal("100") for item in instruments}
        self.random = random.Random(42)
        self.active = threading.Event()
        self.flushed = threading.Event()
        self.stopping = False
        self.ticks = 0
        self.sequence = 0

    def resume(self):
        self.flushed.clear()
        self.active.set()

    def pause(self):
        self.active.clear()
        self.flushed.wait(30)

    def stop(self):
        self.pause()
        self.stopping = True

    def _event(self, item, stamp):
        self.sequence += 1
        move = Decimal(str(round(self.random.gauss(0, 0.05), 2)))
        price = max(Decimal("1.00"), self.prices[item["instrument_id"]] + move)
        self.prices[item["instrument_id"]] = price
        iso = stamp.isoformat()
        raw = {
            "source_event_id": f"demo-{item['conid']}-{self.sequence}", "conid": item["conid"],
            "symbol": item["symbol"], "event_time": iso, "price": str(price), "volume": "10",
            "exchange": "SMART", "currency": "USD", "source": "ibkr", "provider": "IBKR",
            "provider_generation": "flink-demo", "event_kind": "TICK", "market_data_type": "LIVE",
            "received_at": iso, "processing_mode": "LIVE",
        }
        now = datetime.now(timezone.utc).isoformat()
        return {"event_id": str(uuid.uuid4()), "event_type": "market.raw", "schema_version": 1,
                "occurred_at": iso, "produced_at": now, "producer": "flink-state-demo",
                "aggregate_type": "instrument", "aggregate_id": str(item["instrument_id"]),
                "correlation_id": str(uuid.uuid4()), "causation_id": None,
                "idempotency_key": raw["source_event_id"], "payload": raw}

    def run(self):
        from confluent_kafka import Producer
        producer = Producer({"bootstrap.servers": bootstrap(), "linger.ms": 5})
        while not self.stopping:
            if not self.active.is_set():
                if not self.flushed.is_set():
                    producer.flush(30)
                    self.flushed.set()
                time.sleep(0.05)
                continue
            began = time.monotonic()
            for item in self.instruments:
                producer.produce("market.raw.v1", key=str(item["instrument_id"]).encode(),
                                 value=json.dumps(self._event(item, self.sim)).encode())
            producer.poll(0)
            self.ticks += len(self.instruments)
            self.sim += timedelta(seconds=5)
            time.sleep(max(0.0, self.interval - (time.monotonic() - began)))
        producer.flush(30)


# ---------------------------------------------------------------------------
# Flink helpers
# ---------------------------------------------------------------------------

def rest(path, method="get", **kwargs):
    import requests
    response = getattr(requests, method)(os.environ["FLINK_REST_URL"].rstrip("/") + path, timeout=15, **kwargs)
    response.raise_for_status()
    return response.json() if response.content else {}


def prepare_job_dir(variant):
    target = Path("/tmp/flink-state-demo") / variant / "flink"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(BACKEND / "streaming" / "flink", target, ignore=shutil.ignore_patterns("__pycache__"))
    if variant == "baseline":
        for stage, uid in BASELINE_UIDS.items():
            source = (DEMO_DIR / "baseline_jobs" / f"{stage}.py").read_text(encoding="utf-8")
            marker = f'.uid("{uid}")'
            if source.count(marker) != 1:
                raise RuntimeError(f"baseline {stage}.py does not contain {marker} exactly once")
            # Only an operator name is added, so the operator can be found in the
            # Flink REST API; the processing code is the original, byte for byte.
            source = source.replace(marker, f'.name("{VERTEX[stage]}"){marker}')
            (target / "jobs" / f"{stage}.py").write_text(source, encoding="utf-8")
    return target


def submit_job(job_dir):
    command = [
        "flink", "run", "-d", "-m", "flink-jobmanager:8081",
        "-py", str(job_dir / "jobs" / "market_pipeline.py"), "-pyfs", str(job_dir),
        "-pyclientexec", os.environ.get("PYFLINK_CLIENT_EXECUTABLE", "/opt/pyflink-venv/bin/python"),
        "--jarfile", "/opt/flink/lib/flink-sql-connector-kafka-3.3.0-1.20.jar",
    ]
    # Only this variant's copy of the jobs package may be importable by the client.
    environment = {**os.environ, "PYTHONPATH": str(job_dir)}
    result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=600)
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    match = re.search(r"JobID\s+([0-9a-fA-F]{32})", output)
    if result.returncode != 0 or not match:
        print(output[-4000:], flush=True)
        raise RuntimeError(f"Flink submission failed (exit {result.returncode})")
    job_id = match.group(1)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        state = rest(f"/jobs/{job_id}").get("state")
        completed = rest(f"/jobs/{job_id}/checkpoints").get("counts", {}).get("completed", 0)
        if state == "RUNNING" and completed >= 1:
            return job_id
        if state in {"FAILED", "CANCELED", "FINISHED"}:
            raise RuntimeError(f"job {job_id} ended in {state}; see the Flink UI at http://localhost:28081")
        time.sleep(2)
    raise RuntimeError(f"job {job_id} did not reach RUNNING with a completed checkpoint in time")


def cancel_job(job_id):
    rest(f"/jobs/{job_id}?mode=cancel", method="patch")
    for _ in range(120):
        if rest(f"/jobs/{job_id}").get("state") in {"CANCELED", "FAILED", "FINISHED"}:
            return
        time.sleep(1)
    raise RuntimeError(f"job {job_id} did not cancel")


def flink_snapshot(job_id):
    """Per-operator state from the latest completed checkpoint, TaskManager heap, lag."""
    result = {"bar_state": None, "indicator_state": None, "total_state": None,
              "checkpoint_ms": None, "heap_mb": None, "vertices": {}}
    checkpoints = rest(f"/jobs/{job_id}/checkpoints")
    latest = (checkpoints.get("latest") or {}).get("completed")
    if latest:
        result["total_state"] = latest.get("state_size") or latest.get("checkpointed_size")
        result["checkpoint_ms"] = latest.get("end_to_end_duration")
        names = {vertex["id"]: vertex["name"] for vertex in rest(f"/jobs/{job_id}").get("vertices", [])}
        details = rest(f"/jobs/{job_id}/checkpoints/details/{latest['id']}")
        for vertex_id, task in (details.get("tasks") or {}).items():
            size = task.get("state_size") or task.get("checkpointed_size") or 0
            name = names.get(vertex_id, vertex_id)
            result["vertices"][name] = size
            if VERTEX["bar_aggregation"] in name:
                result["bar_state"] = size
            if VERTEX["indicator_computation"] in name:
                result["indicator_state"] = size
    try:
        managers = rest("/taskmanagers").get("taskmanagers", [])
        if managers:
            metrics = rest(f"/taskmanagers/{managers[0]['id']}/metrics?get=Status.JVM.Memory.Heap.Used")
            result["heap_mb"] = round(float(metrics[0]["value"]) / 1048576, 1) if metrics else None
    except Exception:
        pass
    result["flink_lag"] = flink_lag()
    return result


def wait_until_drained(label, timeout):
    log(f"waiting up to {timeout}s for Flink and the backend consumer to catch up ({label})")
    deadline = time.monotonic() + timeout
    lag = None
    while time.monotonic() < deadline:
        lag = (flink_lag(), backend_lag())
        if lag == (0, 0):
            log("caught up")
            return 0
        time.sleep(3)
    log(f"still behind after {timeout}s: Flink lag={lag[0]} messages, backend lag={lag[1]}")
    return lag[0]


# ---------------------------------------------------------------------------
# Backend (Django) side
# ---------------------------------------------------------------------------

def setup_django():
    sys.path.insert(0, str(BACKEND))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django
    django.setup()
    from django.db import connection
    for _ in range(60):
        try:
            connection.ensure_connection()
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError("PostgreSQL is not reachable")


def bind_paper_gateway(portfolio):
    """A connected paper session record so create_instance accepts PAPER; no gateway runs."""
    from apps.broker_gateway.crypto import encrypt_secret
    from apps.broker_gateway.models import BrokerGatewaySession, BrokerSessionAccount
    from apps.broker_gateway.services import container_name_for
    session = BrokerGatewaySession(
        display_name="Flink demo", username_hint="demo", mode="paper",
        status=BrokerGatewaySession.Status.CONNECTED, child_container_name="pending",
        encrypted_gateway_token=encrypt_secret("flink-demo-token"),
        encrypted_novnc_password=encrypt_secret("flink-demo-vnc"),
        commands_enabled=True, last_gateway_state={"connected": True, "reconciled": True, "mode": "paper"},
    )
    session.child_container_name = container_name_for(session.pk)
    session.internal_base_url = f"http://{session.child_container_name}:8080/api/v1"
    session.save()
    portfolio.account.is_reconciled = True
    portfolio.account.save(update_fields=["is_reconciled", "updated_at"])
    BrokerSessionAccount.objects.create(session=session, broker_account=portfolio.account, available=True)
    portfolio.gateway_session = session
    portfolio.save(update_fields=["gateway_session"])


def create_strategies(count):
    """SMA_CROSSOVER strategies whose inputs are registered exactly as an activation does."""
    from django.utils import timezone as dj_timezone
    from apps.accounts.models import BrokerAccount
    from apps.instruments.models import BrokerContract, Instrument
    from apps.instruments.services import publish_instrument_registry
    from apps.portfolios.models import TradingPortfolio
    from apps.strategies.framework import create_instance, register_inputs
    from apps.strategies.models import StrategyInstance

    account = BrokerAccount.objects.create(account_id="DU-FLINK-DEMO", net_liquidation=100000,
                                           available_cash=100000, buying_power=200000)
    portfolio = TradingPortfolio.objects.create(name="Flink state demo", account=account, minimum_notional=1)
    bind_paper_gateway(portfolio)
    strategies = []
    for index in range(count):
        symbol = f"FLDEMO{index}"
        instrument = Instrument.objects.create(symbol=symbol, exchange="SMART", currency="USD")
        contract = BrokerContract.objects.create(instrument=instrument, conid=990100 + index,
                                                 primary_exchange="NASDAQ", local_symbol=symbol)
        instance, _ = create_instance(
            name=f"{symbol}_SMA", definition_key="SMA_CROSSOVER", portfolio=portfolio,
            instrument_id=instrument.pk, timeframe="1m",
            parameters={"fast_window": 5, "slow_window": 20, "direction": "LONG"},
            target_configuration={"target_weight": "0.05"}, execution_mode="PAPER", qualify=False,
        )
        # Activation needs the IBKR gateway; only its streaming effect matters here:
        # the instance is enabled and its input bindings become active, which writes
        # the strategy.inputs.v1 outbox row.
        StrategyInstance.objects.filter(pk=instance.pk).update(
            enabled=True, state="WARMING_UP", effective_from=dj_timezone.now())
        instance.refresh_from_db()
        register_inputs(instance)
        publish_instrument_registry(contract)
        strategies.append({"instrument_id": instrument.pk, "conid": contract.conid, "symbol": symbol,
                           "instance_id": instance.pk,
                           "cohort": "old" if index < (count + 1) // 2 else "recent"})
    return strategies


class BackendLoops(threading.Thread):
    """The beat tasks that matter here: the outbox publisher (both variants) and,
    for the fixed variant only, Flink rehydration and the periodic snapshot."""

    def __init__(self, rehydrate):
        super().__init__(daemon=True)
        self.rehydrate = rehydrate
        self.stopping = False
        self.events = []

    def run(self):
        from django.db import close_old_connections
        from apps.event_bus.services import KafkaPublisher, publish_batch
        from apps.market_streams.rehydration import (
            publish_strategy_inputs_snapshot, rehydrate_flink_state_if_needed)
        publisher = KafkaPublisher()
        last_check = last_snapshot = 0.0
        while not self.stopping:
            close_old_connections()
            try:
                publish_batch(publisher)
                now = time.monotonic()
                if self.rehydrate and now - last_check >= 3:
                    last_check = now
                    result = rehydrate_flink_state_if_needed()
                    if result.get("rehydrated"):
                        self.events.append({"at": round(now - STARTED, 1), **result})
                        log(f"backend: Flink job started without state -> outbox rehydration "
                            f"{json.dumps({k: result.get(k) for k in ('snapshot', 'seed')})}")
                if self.rehydrate and now - last_snapshot >= 30:
                    last_snapshot = now
                    publish_strategy_inputs_snapshot()
            except Exception as exc:
                log(f"backend loop error: {type(exc).__name__}: {exc}")
            time.sleep(1.0)


def wait_for_outbox(timeout=120):
    from apps.audit.models import OutboxEvent
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not OutboxEvent.objects.exclude(status="PUBLISHED").exists():
            return
        time.sleep(1)
    failed = list(OutboxEvent.objects.exclude(status="PUBLISHED").values("topic", "status", "last_error")[:5])
    raise RuntimeError(f"outbox did not publish: {failed}")


def rerelease_recent(strategies):
    """Re-register the 'recent' cohort: its strategy.inputs event is newer than the retention cut-off."""
    from apps.audit.models import OutboxEvent
    for item in strategies:
        if item["cohort"] != "recent":
            continue
        original = OutboxEvent.objects.filter(topic="strategy.inputs.v1", aggregate_type="strategy_instance",
                                              aggregate_id=str(item["instance_id"])).order_by("pk").first()
        OutboxEvent.objects.create(
            topic=original.topic, event_type=original.event_type, aggregate_type=original.aggregate_type,
            aggregate_id=original.aggregate_id, partition_key=original.partition_key, payload=original.payload,
            correlation_id=original.correlation_id,
            idempotency_key=f"{original.idempotency_key}:flink-demo-recent")


def measure_after_restart(strategies, since, check_values):
    from apps.market_streams.models import IndicatorValue, MarketBar
    sys.path.insert(0, str(BACKEND / "streaming" / "flink"))
    from jobs.processing import compute_indicator

    report = []
    for item in strategies:
        bars = MarketBar.objects.filter(instrument_id=item["instrument_id"], interval="1m", is_final=True,
                                        window_start__gte=since).order_by("window_start", "version")
        bar_ids = list(dict.fromkeys(bars.values_list("bar_id", flat=True)))
        indicators = IndicatorValue.objects.filter(instrument_id=item["instrument_id"], timeframe="1m",
                                                   source_bar_id__in=bar_ids)
        row = {"symbol": item["symbol"], "cohort": item["cohort"], "bars": len(bar_ids),
               "indicators": indicators.count(), "null_indicators": indicators.filter(value__isnull=True).count(),
               "checked": 0, "matched": 0}
        if check_values and bar_ids:
            first = bars.filter(bar_id=bar_ids[0]).order_by("-version").first()
            history = {}
            for bar in MarketBar.objects.filter(instrument_id=item["instrument_id"], interval="1m", is_final=True,
                                                processing_mode__in=["LIVE", "WARMUP"],
                                                window_end__lte=first.window_end).order_by("window_end", "version"):
                history[bar.bar_id] = {"bar_id": bar.bar_id, "version": bar.version, "is_final": True,
                                       "window_end": bar.window_end.isoformat(), "close": str(bar.close),
                                       "high": str(bar.high), "low": str(bar.low), "volume": str(bar.volume)}
            ordered = list(history.values())[-int(os.getenv("INDICATOR_HISTORY_MAX_BARS", "1000")):]
            for value in IndicatorValue.objects.filter(source_bar_id=first.bar_id, source_bar_version=first.version):
                expected = compute_indicator(ordered, value.indicator_name, value.parameters)
                row["checked"] += 1
                quantum = Decimal("0.00000001")
                if (value.value is None and expected is None) or (
                        value.value is not None and expected is not None
                        and value.value.quantize(quantum) == Decimal(expected).quantize(quantum)):
                    row["matched"] += 1
        report.append(row)
    return report


# ---------------------------------------------------------------------------
# One variant
# ---------------------------------------------------------------------------

def run_variant(variant, args):
    from django.core.management import call_command

    setup_django()
    log(f"=== variant: {variant} ===")
    call_command("migrate", interactive=False, verbosity=0)
    call_command("seed_strategy_definitions", verbosity=0)
    subprocess.run([sys.executable, "scripts/ensure_kafka_topics.py"], cwd=BACKEND, check=True)
    shorten_strategy_inputs_retention()

    loops = BackendLoops(rehydrate=(variant == "fixed"))
    loops.start()
    strategies = create_strategies(args.instruments)
    wait_for_outbox()
    activation_end = watermarks("strategy.inputs.v1")[1]
    log(f"created {len(strategies)} strategies; registry and strategy inputs published via the outbox "
        f"(strategy.inputs.v1 offsets 0..{activation_end - 1})")

    consumer = subprocess.Popen([sys.executable, "manage.py", "consume_market_streams"], cwd=BACKEND)
    job_dir = prepare_job_dir(variant)
    log(f"submitting {JOB_NAME} ({'pre-fix operators' if variant == 'baseline' else 'fixed operators'})")
    job_id = submit_job(job_dir)
    log(f"job {job_id} RUNNING")
    time.sleep(10)

    market = MarketSimulator(strategies, args.steps_per_second)
    market.start()
    results = {"variant": variant, "settings": vars(args), "samples": [], "strategies": strategies}
    try:
        # ---------------------------------------------------------------- LOAD
        log(f"LOAD: {len(strategies)} instruments x {args.steps_per_second} ticks/s for {args.load_seconds}s "
            f"(~{args.steps_per_second * 5}x real time; one 1m bar per instrument every "
            f"{60 / (args.steps_per_second * 5):.1f}s)")
        print(f"{'wall':>6} {'sim time':>17} {'ticks':>7} | {'bar-agg state':>13} {'indicator st.':>13} "
              f"{'all state':>10} {'ckpt ms':>8} {'TM heap':>8} {'Flink lag':>9}", flush=True)
        market.resume()
        began = time.monotonic()
        while time.monotonic() - began < args.load_seconds:
            time.sleep(args.sample_seconds)
            sample = flink_snapshot(job_id)
            sample.update(wall=round(time.monotonic() - began), sim=market.sim.isoformat(), ticks=market.ticks)
            results["samples"].append(sample)
            kb = lambda value: "-" if value is None else f"{value / 1024:,.0f} KB"
            print(f"{sample['wall']:>5}s {market.sim.strftime('%m-%d %H:%M'):>17} {market.ticks:>7} | "
                  f"{kb(sample['bar_state']):>13} {kb(sample['indicator_state']):>13} {kb(sample['total_state']):>10} "
                  f"{sample['checkpoint_ms'] or '-':>8} {sample['heap_mb'] or '-':>7}M {sample['flink_lag']:>9}",
                  flush=True)
        market.pause()
        if results["samples"] and results["samples"][-1]["bar_state"] is None:
            log("operator names not found in the job graph; vertices: "
                + json.dumps(results["samples"][-1]["vertices"]))
        results["lag_at_end_of_load"] = results["samples"][-1]["flink_lag"] if results["samples"] else None
        results["lag_after_drain"] = wait_until_drained("end of load", args.drain_seconds)
        time.sleep(12)  # one more checkpoint so committed offsets cover everything processed

        # ---------------------------------------------------------- TIME PASSES
        log("TIME PASSES: strategy.inputs.v1 retention deletes the original activation events")
        produce("strategy.inputs.v1", 0, {  # any later registry traffic rolls the old segment
            "event_id": str(uuid.uuid4()), "event_type": "strategy.inputs.changed",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "payload": {"instrument_id": 0, "timeframe": "1m", "requirements": [], "removed_requirement_hashes": []}})
        deadline = time.monotonic() + 180
        while watermarks("strategy.inputs.v1")[0] < activation_end and time.monotonic() < deadline:
            time.sleep(3)
        low = watermarks("strategy.inputs.v1")[0]
        results["activation_events_purged"] = low >= activation_end
        log(f"strategy.inputs.v1 now starts at offset {low}: original activations "
            f"{'deleted' if low >= activation_end else 'STILL PRESENT (retention did not run in time)'}")
        rerelease_recent(strategies)
        wait_for_outbox()
        log("re-registered the 'recent' cohort (its strategy.inputs event is still in Kafka)")

        # -------------------------------------------------------------- RESTART
        log("RESTART: cancelling and resubmitting the job (no addressable checkpoint -> empty state)")
        cancel_job(job_id)
        job_id = submit_job(job_dir)
        log(f"job {job_id} RUNNING with fresh state")
        time.sleep(args.restart_gap_seconds)

        # -------------------------------------------------------- AFTER RESTART
        since = market.sim
        log(f"AFTER RESTART: market resumes at simulated {since.isoformat()} for {args.after_seconds}s")
        market.resume()
        time.sleep(args.after_seconds)
        market.stop()
        wait_until_drained("after restart", args.drain_seconds)
        time.sleep(5)
        results["after_restart"] = measure_after_restart(strategies, since, check_values=(variant == "fixed"))
        results["final"] = flink_snapshot(job_id)
        results["rehydration_events"] = loops.events
    finally:
        loops.stopping = True
        market.stopping = True
        consumer.terminate()
        try:
            consumer.wait(15)
        except subprocess.TimeoutExpired:
            consumer.kill()

    print_variant(results)
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / f"{variant}.json"
    path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    log(f"results written to {path.relative_to(BACKEND)}")


def print_variant(results):
    print(f"\n--- {results['variant']}: after the restart (from PostgreSQL, what strategies see) ---")
    print(f"{'strategy':>12} {'cohort':>7} {'bars':>5} {'indicators':>10} {'null':>5} {'matches DB recompute':>21}")
    for row in results.get("after_restart", []):
        match = f"{row['matched']}/{row['checked']}" if row["checked"] else "-"
        print(f"{row['symbol']:>12} {row['cohort']:>7} {row['bars']:>5} {row['indicators']:>10} "
              f"{row['null_indicators']:>5} {match:>21}")


# ---------------------------------------------------------------------------
# Compare
# ---------------------------------------------------------------------------

def compare():
    loaded = {}
    for variant in ("baseline", "fixed"):
        path = RESULTS_DIR / f"{variant}.json"
        if not path.exists():
            print(f"missing {path.relative_to(BACKEND)}: run the '{variant}' variant first")
            return 1
        loaded[variant] = json.loads(path.read_text(encoding="utf-8"))
    base, fixed = loaded["baseline"], loaded["fixed"]
    checks = []

    def check(name, passed, baseline_value, fixed_value):
        checks.append(passed)
        print(f"[{'PASS' if passed else 'FAIL'}] {name}\n        baseline: {baseline_value}\n        fixed:    {fixed_value}")

    def growth(samples):
        values = [s["bar_state"] for s in samples if s.get("bar_state")]
        if len(values) < 4:
            return None, None, None
        # From the midpoint: by then the fixed operators' demo-scaled TTL (120 s)
        # and bucket retention (20 s) have reached their steady state.
        middle = values[len(values) // 2]
        return middle, values[-1], values[-1] / max(middle, 1)

    def fmt(values):
        quarter, last, ratio = values
        return "n/a" if quarter is None else f"{quarter / 1024:,.0f} KB at mid-run -> {last / 1024:,.0f} KB at the end (x{ratio:.1f})"

    def cohort(results, name, key):
        return sum(row[key] for row in results.get("after_restart", []) if row["cohort"] == name)

    print("\n================ Flink state demo: baseline (pre-fix) vs fixed ================\n")
    base_growth, fixed_growth = growth(base["samples"]), growth(fixed["samples"])
    check("1. Bar-aggregation state: grows with every tick before; bounded after",
          base_growth[2] is not None and fixed_growth[2] is not None
          and base_growth[2] >= 1.5 and fixed_growth[2] <= 1.3,
          fmt(base_growth), fmt(fixed_growth))
    check("2. Keeping up with the market: Flink backlog at the end of the load phase",
          (fixed.get("lag_at_end_of_load") or 0) <= (base.get("lag_at_end_of_load") or 0),
          f"{base.get('lag_at_end_of_load')} messages behind", f"{fixed.get('lag_at_end_of_load')} messages behind")
    check("3. Strategy activated long ago (activation gone from Kafka): bars after the restart",
          cohort(base, "old", "bars") == 0 and cohort(fixed, "old", "bars") > 0,
          f"{cohort(base, 'old', 'bars')} bars -> these strategies silently stop",
          f"{cohort(fixed, 'old', 'bars')} bars -> registry rebuilt from the PostgreSQL snapshot")
    if base.get("lag_after_drain"):
        print(f"[NOTE] baseline still had {base['lag_after_drain']} unprocessed messages at the restart; the replayed "
              "backlog refills its history, so check 4 cannot show the bug. Re-run baseline with a lower "
              "--steps-per-second or a higher --drain-seconds.")
    check("4. Recently activated strategy: indicators after the restart",
          not base.get("lag_after_drain") and cohort(base, "recent", "null_indicators") > 0 and cohort(fixed, "recent", "null_indicators") == 0
          and cohort(fixed, "old", "null_indicators") == 0,
          f"{cohort(base, 'recent', 'null_indicators')} of {cohort(base, 'recent', 'indicators')} indicator values null "
          "(rolling history lost)",
          f"{cohort(fixed, 'recent', 'null_indicators') + cohort(fixed, 'old', 'null_indicators')} of "
          f"{cohort(fixed, 'recent', 'indicators') + cohort(fixed, 'old', 'indicators')} null "
          "(history re-seeded from MarketBar via the outbox)")
    checked = sum(row["checked"] for row in fixed.get("after_restart", []))
    matched = sum(row["matched"] for row in fixed.get("after_restart", []))
    check("5. Fixed: first indicator after the restart equals a recomputation from MarketBar",
          checked > 0 and matched == checked, "n/a (not checked)", f"{matched}/{checked} match")
    check("6. The restart scenario really removed the old activations from Kafka",
          base.get("activation_events_purged") and fixed.get("activation_events_purged"),
          base.get("activation_events_purged"), fixed.get("activation_events_purged"))
    passed = sum(checks)
    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("variant", choices=["baseline", "fixed", "compare"])
    parser.add_argument("--instruments", type=int, default=env_int("DEMO_INSTRUMENTS", 4))
    parser.add_argument("--steps-per-second", type=float, default=env_int("DEMO_STEPS_PER_SECOND", 10),
                        help="ticks per instrument per wall second; each tick is 5 simulated seconds")
    parser.add_argument("--load-seconds", type=int, default=env_int("DEMO_LOAD_SECONDS", 300))
    parser.add_argument("--sample-seconds", type=int, default=env_int("DEMO_SAMPLE_SECONDS", 15))
    parser.add_argument("--drain-seconds", type=int, default=env_int("DEMO_DRAIN_SECONDS", 300))
    parser.add_argument("--restart-gap-seconds", type=int, default=env_int("DEMO_RESTART_GAP_SECONDS", 20))
    parser.add_argument("--after-seconds", type=int, default=env_int("DEMO_AFTER_SECONDS", 90))
    args = parser.parse_args()
    if args.variant == "compare":
        return compare()
    run_variant(args.variant, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
