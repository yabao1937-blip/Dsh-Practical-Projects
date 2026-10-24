"""Retired pages cannot schedule writes or expose their former API surface."""
import asyncio

from app.main import app
from app.decision_hourly import worker


def test_only_core_and_analysis_routes_remain():
    paths = set(app.openapi()["paths"])
    for prefix in ("overview", "records", "inputs", "manual-entries", "import", "migrate", "hourly-brief"):
        assert not any(path == f"/api/v1/{prefix}" or path.startswith(f"/api/v1/{prefix}/") for path in paths)
    for path in ("/api/v1/decision/current", "/api/v1/telemetry/latest", "/api/v1/manual-measurements", "/api/v1/state"):
        assert path in paths


def test_decision_lifespan_starts_and_drains_only_its_worker(monkeypatch):
    events = []

    async def run(stop):
        events.append("started")
        await stop.wait()
        await asyncio.sleep(0)
        events.append("drained")

    monkeypatch.setattr(worker, "run", run)

    async def scenario():
        async with worker.lifespan(None):
            await asyncio.sleep(0)
            assert events == ["started"]
        assert events == ["started", "drained"]

    asyncio.run(scenario())
