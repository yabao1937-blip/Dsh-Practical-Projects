"""Disposable synthetic host -> authenticated bridge -> laptop -> three Chrome pages.

Never opens a production database. Only the temporary loopback host accepts assays.
Run from the repository: python backend/scripts/verify_readonly_settlement_e2e.py.
The browser uses normal application polling; no forced refresh or clock override.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path[:0] = [str(BACKEND), str(BACKEND / "tests")]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_http(url, process=None):
    for _ in range(240):
        if process is not None and process.poll() is not None:
            raise RuntimeError("Isolated laptop exited before readiness")
        try:
            with urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(.25)
    raise RuntimeError("Isolated loopback service did not become ready")


def isolated_env(database):
    env = {key: value for key, value in os.environ.items() if not key.startswith("DMCS_")}
    env.update(DMCS_DATABASE_URL="sqlite:///" + Path(database).as_posix(),
               DMCS_ISOLATED_TEST_DB="1", DMCS_WRITE_TOKEN="off", PYTHONUTF8="1")
    return env


def digest_tables(engine, tables):
    from sqlalchemy import text
    with engine.connect() as connection:
        return {name: hashlib.sha256(json.dumps(
            [tuple(row) for row in connection.execute(text(f'SELECT * FROM "{name}" ORDER BY 1'))],
            ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest() for name in tables}


def seed(database):
    """700 distinct five-second slots/hour; real flow evidence, synthetic chemistry."""
    from sqlalchemy import create_engine, delete, select
    from sqlalchemy.orm import sessionmaker
    import pytest
    from app.database import Base
    from app.models import AutoState, ManualMeasurement
    from app.telemetry.views import ensure_sqlite_views
    from app.services.state import load_store
    from app.services.decision_sources import shared_snapshot
    from app.decision_hourly.service import build_hour, context_definition
    from app.decision_hourly.ash_settlement import latest_end, build_settlement
    from app.decision_hourly.models import DecisionCycleSample, DecisionCycleContext, DecisionCycleHour, DecisionAshSettlement
    from app.gpt_density.hourly_anchor import capture, read as read_anchor
    from app.decision_hourly.quality_advice import empirical_target
    from app.telemetry.domain import iso_time
    from app.gpt_coarse.expert8 import FEATURES, KIND
    from app.gpt_coarse.repository import activate
    import test_decision_sources as source_fixture
    from test_hourly_brief import send
    from test_ash_settlement import sample

    now = datetime.now(timezone.utc)
    assert now.minute not in (58, 59), "Run away from the hour boundary (two-minute safety margin)"
    hour = now.replace(minute=0, second=0, microsecond=0)
    source_fixture.HOUR = now
    engine = create_engine("sqlite:///" + database.as_posix(), connect_args={"check_same_thread": False, "timeout": 15})
    Base.metadata.create_all(engine)
    ensure_sqlite_views(engine)
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    sessions = sessionmaker(engine, autoflush=False)
    patch = pytest.MonkeyPatch()
    cfg, repo, values = source_fixture.seeded.__wrapped__(sessions, patch)
    # No real training rows/models are loaded; the legal eight-factor expert
    # artifact has zero synthetic correction and a fixed expert level response.
    model = dict(kind=KIND, feature_names=FEATURES, reference=[400, 0, 1, 1, 1, 1, 0],
                 scale=[1] * 7, coefs=[0] * 7, bounds=[[0, 1000]] + [[0, 1]] * 6,
                 expert=dict(intercept=30.5, slope=-.3, level_range=[25, 75]), correction_limit=2.0, n=0)
    with sessions.begin() as db:
        activate(db, dict(model=model, status="production_ready", trained_at=iso_time(now-timedelta(days=1)),
                          report={}, source="synthetic-e2e", rows=[]), now-timedelta(days=1))
        db.execute(delete(ManualMeasurement).where(ManualMeasurement.metric == "heavy_clean_coal_ash"))
    # Keep all live observations fresh for baseline setup; historical evidence
    # below is immutable and never contacts any actual OPC endpoint.
    send(repo, cfg, now - timedelta(seconds=2), values)
    end = latest_end(now)
    starts = [end - timedelta(hours=index) for index in range(10, 0, -1)]
    with sessions.begin() as db:
        db.add(ManualMeasurement(metric="heavy_clean_coal_ash", name="重介精煤灰分", value=7, unit="%",
            observed_at=iso_time(starts[0] - timedelta(minutes=30)), created_at=iso_time(now - timedelta(seconds=1)),
            request_hash="synthetic-initial", time_basis="production-time-v1"))
        db.flush()
        current = shared_snapshot(db)
        assert current["model"]["method"] == "expert8", current["model"]
        definition = context_definition(current, load_store(db))
        frozen = capture(db, now, current, definition)
        assert frozen is not None, current.get("execution_basis")
        anchor = read_anchor(db, now)
        for start in starts:
            template = sample(db, start, rate=400)
            template.snapshot.update(sources=deepcopy(current["sources"]), model=deepcopy(current["model"]),
                target=deepcopy(current["target"]), guidance=deepcopy(current["guidance"]), warnings=[])
            db.get(DecisionCycleContext, "ctx").definition = deepcopy(definition)
            # One missing slot per 36 slots, not a long artificial gap.
            for index in range(1, 720):
                if index % 36 == 35:
                    continue
                stamp = start + timedelta(seconds=5 * index)
                snapshot = deepcopy(template.snapshot)
                for key in ("belt501", "belt502"):
                    snapshot["cells"][key].update(observed_at=iso_time(stamp), received_at=iso_time(stamp),
                                                  batch_id=f"{start.timestamp()}-{index}")
                db.add(DecisionCycleSample(slot=int(stamp.timestamp()) // 5, sampled_at=iso_time(stamp),
                    context_key="ctx", fingerprint=f"synthetic-{int(stamp.timestamp())}", snapshot=snapshot))
            db.flush()
            snapshot = build_hour(db, start, start + timedelta(hours=1), now)
            db.add(DecisionCycleHour(start=iso_time(start), end=iso_time(start + timedelta(hours=1)),
                                    generated_at=iso_time(now), snapshot=snapshot))
            db.flush()
            settlement = build_settlement(db, start, now)
            assert settlement["item"]["coverage"] > .8, settlement["item"]
            assert settlement["item"]["sample_count"] == 700, settlement["item"]
            assert abs(settlement["item"]["value"] - 7.95) < 1e-9
            db.add(DecisionAshSettlement(start=iso_time(start), end=iso_time(start + timedelta(hours=1)),
                deadline=iso_time(start + timedelta(hours=2)), generated_at=iso_time(now), snapshot=settlement))
        db.flush()
        # read_hour uses the immediately preceding process hour as its wrapper;
        # advice itself reads the published ash hour and the frozen current anchor.
        previous = deepcopy(db.get(DecisionCycleHour, iso_time(starts[-1])).snapshot)
        previous["period"].update(start=iso_time(hour-timedelta(hours=1)), end=iso_time(hour),
                                  apply_start=iso_time(hour), apply_end=iso_time(hour+timedelta(hours=1)))
        db.add(DecisionCycleHour(start=iso_time(hour-timedelta(hours=1)), end=iso_time(hour),
                                generated_at=iso_time(now), snapshot=previous))
        tables = [name for name in Base.metadata.tables if name != "manual_measurements"]
        total = db.query(DecisionCycleSample).count()
        assert total == 7000, total
    patch.undo()
    def expected(heavy):
        ash = .85 * heavy + 2
        return dict(ash=round(ash, 2), density=round(empirical_target(ash, anchor["value"], current["target"])["value"], 3))
    manifest = dict(starts=[iso_time(start) for start in starts], anchor=anchor, initial=expected(7),
                    after_first=expected(8), after_latest=expected(9), after_next=expected(8.6),
                    after_backfill=expected(8.4), sample_count=total, slots_per_hour=700,
                    coverage=700/720, model="EXPERT8", synthetic_only=True)
    return engine, sessions, tables, manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="output/acceptance-20261017")
    parser.add_argument("--node", default="D:/KF/nodejs/node.exe")
    parser.add_argument("--playwright-module", default=os.environ.get("DMCS_PLAYWRIGHT_MODULE",
        "C:/Users/25925/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright"))
    args = parser.parse_args()
    output = (ROOT / args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / "readonly-settlement-e2e.json"
    result = dict(passed=False, synthetic_only=True, production_requests=0)
    with tempfile.TemporaryDirectory(prefix="dmcs-settlement-e2e-", ignore_cleanup_errors=True) as temporary:
        temp = Path(temporary)
        database = temp / "synthetic-host.db"
        os.environ.update(isolated_env(database))
        os.environ.pop("DMCS_REMOTE_READONLY_CONFIG", None)
        engine, sessions, tables, manifest = seed(database)
        from fastapi import FastAPI
        import uvicorn
        from app.database import get_db
        from app.remote_readonly.server import ReadOnlyManager, create_router
        from app.routers import manual_measurements
        token = "synthetic-e2e-" + secrets.token_hex(24)
        manager = ReadOnlyManager(str(database), dict(token=token, clients=["127.0.0.1"]))
        host = FastAPI()
        host.include_router(create_router(manager), prefix="/api/v1")
        host.include_router(manual_measurements.router, prefix="/api/v1")
        def dependency():
            with sessions() as db:
                yield db
        host.dependency_overrides[get_db] = dependency
        @host.get("/ready")
        def ready():
            return dict(synthetic=True)
        host_port, laptop_port = free_port(), free_port()
        server = uvicorn.Server(uvicorn.Config(host, host="127.0.0.1", port=host_port, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        laptop = None
        before = digest_tables(engine, tables)
        try:
            host_url = f"http://127.0.0.1:{host_port}"
            wait_http(host_url + "/ready")
            config = temp / "readonly.json"
            config.write_text(json.dumps(dict(mode="readonly-test", server_url=host_url, token=token)), encoding="utf-8")
            forbidden = temp / "must-not-create-laptop.db"
            env = isolated_env(forbidden)
            env["DMCS_REMOTE_READONLY_CONFIG"] = str(config)
            manifest.update(host_url=host_url, laptop_url=f"http://127.0.0.1:{laptop_port}")
            manifest_path = temp / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            with (output / "readonly-settlement-laptop.log").open("w", encoding="utf-8") as log:
                laptop = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "uvicorn", "app.main:app",
                    "--host", "127.0.0.1", "--port", str(laptop_port), "--log-level", "warning"],
                    cwd=BACKEND, env=env, stdout=log, stderr=subprocess.STDOUT)
                wait_http(manifest["laptop_url"] + "/api/v1/runtime-mode", laptop)
                browser_env = dict(os.environ, DMCS_PLAYWRIGHT_MODULE=args.playwright_module)
                # No global timeout: the browser owns bounded per-stage deadlines,
                # so startup/cold replay cannot be killed by an unrelated CLI cap.
                completed = subprocess.run([args.node, str(Path(__file__).with_suffix(".js")),
                    str(manifest_path), str(report_path)], cwd=ROOT, env=browser_env)
                result = json.loads(report_path.read_text(encoding="utf-8"))
                result.update(original_tables_unchanged=digest_tables(engine, tables) == before,
                              local_database_created=forbidden.exists(), bridge_transactions_released=not manager._transactions)
                result["passed"] = (completed.returncode == 0 and result["original_tables_unchanged"]
                                    and not result["local_database_created"] and result["bridge_transactions_released"])
        finally:
            if laptop is not None:
                laptop.terminate()
                try:
                    laptop.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    laptop.kill()
                    laptop.wait(timeout=10)
            server.should_exit = True
            thread.join(timeout=10)
            manager.close()
            engine.dispose()
            from app.database import engine as imported_engine
            imported_engine.dispose()
            report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
