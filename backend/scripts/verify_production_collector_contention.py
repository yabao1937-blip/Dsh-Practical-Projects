"""Bounded synthetic production-mode collector contention acceptance.

No real OPC server, production database, app.main, or production maintenance
worker is used. This is a controlled 45-second slow-calculation injection, not
a claim about the actual host's lock holder or a production-size benchmark.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import random
import socket
import sys
import tempfile
import threading
import time
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("slow-computation", "external-lock"), required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = (args.output or Path("output/kepserver-20261021/fix") / args.scenario).resolve()
    output.mkdir(parents=True, exist_ok=True)
    backend = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(backend))
    # Isolation must precede every application import. No ignored local-mode
    # marker or inherited read-only bridge can select a real database.
    for key in tuple(os.environ):
        if key.startswith("DMCS_"):
            os.environ.pop(key)
    temporary = tempfile.TemporaryDirectory(prefix="dmcs-contention-")
    root = Path(temporary.name).resolve()
    os.environ["DMCS_ISOLATED_TEST_DB"] = "1"
    os.environ["DMCS_DATABASE_URL"] = "sqlite:///" + (root / "synthetic.db").as_posix()
    from fastapi import FastAPI
    from sqlalchemy import event, func, select, text
    import uvicorn
    from app.database import Base, SessionLocal, engine
    from app.telemetry.config import CollectorConfig, PointConfig
    from app.telemetry.domain import iso_time, normalize, utc_now
    from app.telemetry.models import TelemetryReading
    from app.telemetry.repository import TelemetryRepository
    from app.telemetry.spool import DurableSpool
    from app.telemetry.worker import run_collector
    from app.telemetry.views import ensure_sqlite_views
    from app.decision_hourly import service
    from app.routers.telemetry import router as telemetry_router
    assert "app.main" not in sys.modules
    assert Path(engine.url.database).resolve() == root / "synthetic.db"

    started = time.monotonic()
    mutex = threading.RLock()
    stopping = threading.Event()
    injection_requested = threading.Event()
    lock_acquired = threading.Event()
    lock_released = threading.Event()
    events: list[dict] = []
    responses: list[dict] = []
    read_times: list[float] = []
    counters = {"reader_connects": 0, "reader_closes": 0, "reader_reads": 0,
                "observations": 0, "collector_exit": None}
    generated: set[str] = set()
    acknowledged: set[str] = set()
    sql_errors: list[dict] = []
    last_checkpoint = [0.0]
    spool = None
    result = {"scope": "synthetic controlled 45-second injection; not a host-size benchmark",
              "scenario": args.scenario,
              "mode": "production-local-sqlite", "database_is_temporary": True,
              "real_opc_connection": False, "state": "initializing", "passed": False}

    def elapsed():
        return round(time.monotonic() - started, 3)

    def checkpoint(force=False):
        with mutex:
            if not force and time.monotonic() - last_checkpoint[0] < 1:
                return
            last_checkpoint[0] = time.monotonic()
            result.update(elapsed_seconds=elapsed(), counters=dict(counters),
                          generated_batches=len(generated), acknowledged_batches=len(acknowledged),
                          events=list(events), sql_errors=list(sql_errors),
                          http_observations=list(responses), upstream_read_times=list(read_times),
                          spool=spool.stats() if spool is not None else {})
            target = output / "checkpoint.json"
            target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    def record(kind, **details):
        with mutex:
            events.append({"seconds": elapsed(), "kind": kind, **details})
        checkpoint()

    class SafeCollectorLog(logging.Handler):
        def emit(self, row):
            message = row.getMessage()
            with mutex:
                with (output / "collector.log").open("a", encoding="utf-8") as stream:
                    stream.write(f"{elapsed():.3f} {row.levelname} {message}\n")
            record("collector_log", level=row.levelname, message=message)

    handler = SafeCollectorLog()
    collector_logger = logging.getLogger("dmcs.collector")
    collector_logger.addHandler(handler)
    collector_logger.setLevel(logging.INFO)

    @event.listens_for(engine, "handle_error")
    def db_error(context):
        original = context.original_exception
        item = {"seconds": elapsed(), "type": type(original).__name__,
                "sqlite_errorcode": getattr(original, "sqlite_errorcode", None),
                "sqlite_errorname": getattr(original, "sqlite_errorname", None)}
        with mutex:
            sql_errors.append(item)
        record("sqlite_error", **{k: v for k, v in item.items() if k != "seconds"})

    Base.metadata.create_all(engine)
    ensure_sqlite_views(engine)
    with engine.connect() as connection:
        result["sqlite"] = {"journal_mode": connection.scalar(text("PRAGMA journal_mode")),
                            "busy_timeout_ms": connection.scalar(text("PRAGMA busy_timeout"))}
    assert result["sqlite"] == {"journal_mode": "wal", "busy_timeout_ms": 15000}
    cfg = CollectorConfig(enabled=True, source_id="synthetic-contention", name="Synthetic contention probe",
        endpoint="opc.tcp://127.0.0.1:49320", security="None", allow_insecure=True,
        points=[PointConfig(key=f"p{i:02d}", name=f"Synthetic {i:02d}",
                            node_id=f"ns=2;s=synthetic.{i:02d}", unit="%") for i in range(30)])
    assert (cfg.poll_seconds, cfg.retry_min_seconds, cfg.retry_max_seconds,
            cfg.lease_seconds) == (5, 1, 30, 60)
    result["configuration"] = dict(points=30, poll_seconds=cfg.poll_seconds,
        point_max_age_seconds=30, timeout_seconds=cfg.timeout_seconds,
        retry_min_seconds=cfg.retry_min_seconds, retry_max_seconds=cfg.retry_max_seconds,
        lease_seconds=cfg.lease_seconds, injected_lock_seconds=45)

    class RecordedSpool(DurableSpool):
        def enqueue(self, batch):
            path = super().enqueue(batch)
            with mutex:
                generated.add(batch["batch_id"])
            record("batch_enqueued", batch_id=batch["batch_id"])
            return path

        def ack(self, path):
            batch_id = self.read(path)["batch_id"]
            super().ack(path)
            with mutex:
                acknowledged.add(batch_id)
            record("batch_committed", batch_id=batch_id)

    spool = RecordedSpool(root / "spool", cfg.source_id)
    repository = TelemetryRepository()

    class FakeOpcReader:
        def __init__(self, config):
            self.cfg = config

        async def __aenter__(self):
            with mutex:
                counters["reader_connects"] += 1
            record("reader_connected")
            return self

        async def __aexit__(self, *exc):
            with mutex:
                counters["reader_closes"] += 1
            record("reader_closed", upstream_failed=False)

        async def read(self):
            now = utc_now()
            with mutex:
                counters["reader_reads"] += 1
                read_times.append(elapsed())
            # Every requested upstream read succeeds with a fresh all-good batch.
            return [normalize(point, 10 + i / 10, 0, now, now, now)
                    for i, point in enumerate(self.cfg.points)]

    original_snapshot = service.shared_snapshot

    def delayed_snapshot(db, *positional, **keywords):
        if (args.scenario == "slow-computation" and injection_requested.is_set()
                and not lock_acquired.is_set()):
            # The fixed observation_tick computes in a read snapshot, outside
            # the short write transaction. A real SQLite WAL writer continues.
            assert db.in_transaction()
            lock_acquired.set()
            record("injected_calculation_started", sqlite_transaction_active=True,
                   write_lock_expected=False)
            if stopping.wait(45):
                raise InterruptedError("probe cancelled")
            record("injected_delay_ended")
        return original_snapshot(db, *positional, **keywords)

    service.shared_snapshot = delayed_snapshot
    loop_box = []
    stop_box = []
    errors: list[str] = []

    def collector_thread():
        async def execute():
            loop_box.append(asyncio.get_running_loop())
            stop_box.append(asyncio.Event())
            counters["collector_exit"] = await run_collector(cfg, repository, spool,
                stop=stop_box[0], reader_factory=FakeOpcReader)
        try:
            asyncio.run(execute())
        except Exception as exc:
            errors.append("collector:" + type(exc).__name__)
            record("thread_error", thread="collector", exception=type(exc).__name__)

    def observation_thread():
        while not stopping.is_set():
            try:
                service.observation_tick()
                with mutex:
                    counters["observations"] += 1
                if (args.scenario == "slow-computation" and lock_acquired.is_set()
                        and not lock_released.is_set()):
                    lock_released.set()
                    record("observation_committed_lock_released")
            except Exception as exc:
                from app.telemetry.worker import sqlite_busy
                if args.scenario == "external-lock" and sqlite_busy(exc):
                    record("observation_storage_busy", exception=type(exc).__name__)
                    stopping.wait(5)
                    continue
                errors.append("observation:" + type(exc).__name__)
                record("thread_error", thread="observation", exception=type(exc).__name__)
                return
            stopping.wait(5)

    application = FastAPI()
    # Only GET routes are exposed: reconnect/write paths are never mounted.
    for route in telemetry_router.routes:
        if route.methods == {"GET"}:
            application.router.routes.append(route)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(64)
    address = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(application, log_level="critical", access_log=False))
    http_thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    workers = [threading.Thread(target=collector_thread, name="synthetic-collector", daemon=True),
               threading.Thread(target=observation_thread, name="actual-observation-tick", daemon=True)]

    def external_lock_thread():
        if not injection_requested.wait(30) or stopping.is_set():
            return
        # A separate real SQLite connection represents another process holding
        # the writer slot. No product transaction or timestamp is mocked.
        with engine.connect() as connection:
            connection.execute(text("BEGIN IMMEDIATE"))
            lock_acquired.set()
            record("injected_calculation_started", sqlite_transaction_active=True,
                   external_writer_lock=True)
            stopping.wait(45)
            connection.rollback()
        lock_released.set()
        record("observation_committed_lock_released", external_writer_lock=True)

    if args.scenario == "external-lock":
        workers.append(threading.Thread(target=external_lock_thread, name="external-sqlite-writer", daemon=True))

    def reader_thread(number):
        while not stopping.is_set():
            before = time.monotonic()
            try:
                with urllib.request.urlopen(address + "/telemetry/latest", timeout=8) as response:
                    view = json.load(response)
                item = dict(seconds=elapsed(), reader=number,
                    available=view["summary"]["available"], points=view["summary"]["points"],
                    source_state=view["sources"][0]["state"] if view["sources"] else "absent",
                    statuses=sorted({point["status"] for point in view["items"]}),
                    milliseconds=round((time.monotonic() - before) * 1000, 2))
                with mutex:
                    responses.append(item)
            except Exception as exc:
                errors.append("http:" + type(exc).__name__)
                record("http_error", reader=number, exception=type(exc).__name__)
            checkpoint()
            stopping.wait(1)

    def wait_until(condition, timeout, message):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(.2)
        raise AssertionError(message)

    try:
        result["state"] = "baseline"
        http_thread.start()
        wait_until(lambda: server.started, 10, "HTTP server did not start")
        for thread in workers:
            thread.start()
        for number in range(3):
            thread = threading.Thread(target=reader_thread, args=(number,), daemon=True)
            workers.append(thread)
            thread.start()
        wait_until(lambda: elapsed() >= 12 and counters["reader_reads"] >= 3
                   and len([r for r in responses if r["available"] == 30]) >= 20,
                   25, "baseline never reached stable 30/30")
        baseline_end = elapsed()
        result["baseline"] = dict(seconds=baseline_end, reader_connects=counters["reader_connects"],
            reader_reads=counters["reader_reads"], observations=counters["observations"],
            good_http_samples=len([r for r in responses if r["available"] == 30]))
        assert counters["reader_connects"] == 1 and counters["reader_closes"] == 0
        record("baseline_passed", **result["baseline"])
        print(json.dumps({"baseline": result["baseline"]}), flush=True)
        result["state"] = "injected_" + args.scenario
        injection_requested.set()
        wait_until(lock_acquired.is_set, 10, "slow calculation did not acquire write lock")
        wait_until(lock_released.is_set, 55, "slow calculation did not finish")
        wait_until(lambda: any(r["seconds"] > next(e["seconds"] for e in events
                   if e["kind"] == "observation_committed_lock_released") and r["available"] == 30
                   for r in responses) and not spool.pending(), 35, "collector did not recover")
        result["state"] = "recovered"
        record("recovery_passed")
        stopping.set()
        if loop_box and stop_box:
            loop_box[0].call_soon_threadsafe(stop_box[0].set)
        for thread in workers:
            thread.join(20)
        with SessionLocal() as db:
            rows = db.scalar(select(func.count()).select_from(TelemetryReading))
            saved_batches = set(db.scalars(select(TelemetryReading.batch_id).distinct()))
        lock_start = next(e["seconds"] for e in events if e["kind"] == "injected_calculation_started")
        release = next(e["seconds"] for e in events if e["kind"] == "observation_committed_lock_released")
        zeros = [r for r in responses if lock_start < r["seconds"] < release and r["available"] == 0]
        logs = [e["message"] for e in events if e["kind"] == "collector_log"]
        samples_during = [t for t in read_times if lock_start < t < release]
        gaps = [b-a for a, b in zip(read_times, read_times[1:])]
        ua_closes_before_stop = [e for e in events if e["kind"] == "reader_closed"
                                and e["seconds"] < next(e["seconds"] for e in events if e["kind"] == "recovery_passed")]
        result["assertions"] = {
            "stable_baseline_30_of_30": result["baseline"]["good_http_samples"] >= 20,
            "upstream_never_failed": not errors,
            "five_second_sampling_continues": len(samples_during) >= 7 and max(gaps, default=0) < 7.5,
            "ua_context_not_closed_or_reconnected": counters["reader_connects"] == 1 and not ua_closes_before_stop,
            "recovery_30_of_30": any(r["seconds"] > release and r["available"] == 30 for r in responses),
            "spool_empty": not spool.pending(),
            "all_generated_batches_saved": generated == saved_batches == acknowledged,
            "all_samples_saved": rows == len(generated) * 30,
            "collector_clean_exit": counters["collector_exit"] == 0,
            "no_real_app_main_import": "app.main" not in sys.modules,
        }
        if args.scenario == "slow-computation":
            result["assertions"].update(no_sqlite_busy_during_slow_computation=not sql_errors,
                http_stays_30_of_30=not zeros and all(r["available"] == 30 for r in responses
                                                    if lock_start < r["seconds"] < release))
        else:
            result["assertions"].update(real_sqlite_busy_observed=any(e["sqlite_errorcode"] == 5 for e in sql_errors),
                storage_failure_logged_separately=any("collector_storage_unavailable" in x for x in logs),
                genuine_database_staleness_visible=bool(zeros) and any("stale" in r["statuses"] for r in zeros))
        result["statistics"] = dict(lock_start_seconds=lock_start, lock_release_seconds=release,
            lock_duration_seconds=round(release-lock_start, 3),
            first_zero_seconds=min((r["seconds"] for r in zeros), default=None),
            zero_http_samples=len(zeros), historical_readings=rows, persisted_batches=len(saved_batches),
            upstream_reads_during_injection=len(samples_during), max_sampling_gap_seconds=round(max(gaps, default=0), 3),
            final_spool=spool.stats(), thread_errors=errors)
        result["passed"] = all(result["assertions"].values())
        result["state"] = "completed"
        checkpoint(True)
        print(json.dumps({k: result[k] for k in ("passed", "baseline", "statistics", "assertions")},
                         ensure_ascii=False), flush=True)
        return 0 if result["passed"] else 1
    except Exception as exc:
        result.update(state="failed", error_type=type(exc).__name__, error=str(exc))
        checkpoint(True)
        print(json.dumps({"state": "failed", "error_type": type(exc).__name__, "error": str(exc)}), flush=True)
        return 1
    finally:
        stopping.set()
        if loop_box and stop_box and loop_box[0].is_running():
            loop_box[0].call_soon_threadsafe(stop_box[0].set)
        for thread in workers:
            if thread.is_alive():
                thread.join(20)
        service.shared_snapshot = original_snapshot
        server.should_exit = True
        http_thread.join(10)
        listener.close()
        collector_logger.removeHandler(handler)
        engine.dispose()
        result["cleanup"] = {"worker_threads_stopped": not any(t.is_alive() for t in workers),
                             "http_server_stopped": not http_thread.is_alive()}
        result["final_spool"] = spool.stats()
        checkpoint(True)
        try:
            temporary.cleanup()
            result["cleanup"]["temporary_directory_removed"] = True
        except OSError:
            result["cleanup"]["temporary_directory_removed"] = False
        (output / "checkpoint.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
