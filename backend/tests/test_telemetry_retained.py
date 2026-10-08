"""Last usable display values survive zeros, outages, pruning and collector restarts."""
import copy
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.telemetry.config import CollectorConfig, PointConfig
from app.telemetry.domain import normalize, utc_now
from app.telemetry.derived import DENSITY_AVERAGE_MEMBERS
from app.telemetry.models import TelemetryPoint, TelemetryReading
from app.telemetry.repository import TelemetryRepository, latest_view
from app.telemetry.retained import MEMORY, DENSITY_MEMORY


@pytest.fixture
def repo(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "retained.db"))
    Base.metadata.create_all(engine)
    yield TelemetryRepository(sessionmaker(engine, autoflush=False))
    engine.dispose()


def config(points=None):
    return CollectorConfig(enabled=True, endpoint="opc.tcp://localhost:49320", security="None", allow_insecure=True,
        points=points or [PointConfig(key="ash", name="灰分", node_id="ns=2;s=Ash", unit="%")])


def send(repo, cfg, values, when, statuses=None):
    samples = [normalize(p, value, status, when, when, when)
               for p, value, status in zip(cfg.points, values, statuses or [0] * len(values))]
    payload = {"source_id": cfg.source_id, "batch_id": uuid.uuid4().hex, "samples": samples}
    original = copy.deepcopy(payload)
    repo.persist(cfg, "owner", payload)
    assert payload == original, "Retaining display values must not mutate raw batches/spool"
    return payload


def view(repo, cfg, now):
    with repo.sessions() as db:
        return latest_view(db, cfg.source_id, now)


def test_nonzero_memory_keeps_exact_last_poll_not_throttled_history(repo):
    cfg, start = config(), utc_now() - timedelta(seconds=10)
    repo.acquire(cfg, "owner")
    send(repo, cfg, [42], start)
    newest = send(repo, cfg, [43], start + timedelta(seconds=1))
    send(repo, cfg, [0], start + timedelta(seconds=2))
    send(repo, cfg, [99], start + timedelta(seconds=3), [0x80000000])
    item = view(repo, cfg, utc_now())["items"][0]
    assert item["sample"]["value"] == 99 and not item["available"]
    assert item["last_nonzero_sample"] == {**newest["samples"][0], "batch_id": newest["batch_id"]}
    assert item["last_valid_sample"]["value"] == 0, "Zero remains a genuine raw value"
    with repo.sessions() as db:
        readings = list(db.scalars(select(TelemetryReading)))
        assert len(readings) == 2, "Display retention must not increase raw history frequency"
        assert all(MEMORY not in r.sample and DENSITY_MEMORY not in r.sample for r in readings)
    repo.state(cfg, "owner", "stopped")
    repo.acquire(cfg, "owner")
    send(repo, cfg, [0], start + timedelta(seconds=4))
    assert view(repo, cfg, utc_now())["items"][0]["last_nonzero_sample"]["value"] == 43
    # An older replay cannot replace the last valid memory.
    send(repo, cfg, [12], start)
    assert view(repo, cfg, utc_now())["items"][0]["last_nonzero_sample"]["value"] == 43


def test_bootstrap_legacy_history_and_definition_isolation(repo):
    cfg, start = config(), utc_now() - timedelta(minutes=3)
    repo.acquire(cfg, "owner")
    send(repo, cfg, [42], start)
    send(repo, cfg, [0], start + timedelta(minutes=1))
    with repo.sessions() as db:
        point = db.get(TelemetryPoint, (cfg.source_id, "ash"))
        point.latest = {k: v for k, v in point.latest.items() if k != MEMORY}
        db.commit()
        engine = db.get_bind()
    scans = []
    def track(_conn, _cursor, statement, _parameters, _context, _executemany):
        if "FROM telemetry_readings" in statement:
            scans.append(statement)
    event.listen(engine, "before_cursor_execute", track)
    try:
        assert view(repo, cfg, utc_now())["items"][0]["last_nonzero_sample"]["value"] == 42
        initial_scans = len(scans)
        assert initial_scans > 0
        assert view(repo, cfg, utc_now())["items"][0]["last_nonzero_sample"]["value"] == 42
        assert len(scans) == initial_scans, "An offline legacy collector must not trigger repeated history scans"
    finally:
        event.remove(engine, "before_cursor_execute", track)
    repo.state(cfg, "owner", "stopped")
    cfg.points[0].scale = 2
    repo.acquire(cfg, "owner")
    send(repo, cfg, [0], utc_now())
    assert view(repo, cfg, utc_now())["items"][0]["last_nonzero_sample"] is None


def test_pruning_keeps_last_value_and_fresh_one_is_valid(repo):
    cfg, start = config(), utc_now() - timedelta(days=10)
    repo.acquire(cfg, "owner")
    send(repo, cfg, [1.47], start)
    send(repo, cfg, [None], utc_now(), [0x80000000])
    repo.prune(cfg)
    assert view(repo, cfg, utc_now())["items"][0]["last_valid_sample"]["value"] == 1.47
    send(repo, cfg, [1.0], utc_now())
    assert view(repo, cfg, utc_now())["items"][0]["last_valid_sample"]["value"] == 1.0


def test_density_holds_a_complete_pair_not_individual_last_values(repo):
    cfg = config([PointConfig(key=label, name=label, node_id="ns=2;s=" + item_id, item_id=item_id, unit="g/cm³")
                  for label, item_id in DENSITY_AVERAGE_MEMBERS])
    start = utc_now() - timedelta(seconds=10)
    repo.acquire(cfg, "owner")
    send(repo, cfg, [1.4, 1.6], start)
    send(repo, cfg, [1.5, 1.7], start + timedelta(seconds=1))
    send(repo, cfg, [1.8, None], start + timedelta(seconds=2), [0, 0x80000000])
    repo.state(cfg, "owner", "connected")
    average = view(repo, cfg, utc_now())["derived_items"][0]
    assert not average["available"] and average["sample"]["value"] is None
    assert average["last_valid_sample"]["value"] == pytest.approx(1.6)
    assert [c["value"] for c in average["last_valid_sample"]["components"]] == [1.5, 1.7]
    send(repo, cfg, [1.0, 1.0], start + timedelta(seconds=3))
    average = view(repo, cfg, utc_now())["derived_items"][0]
    assert average["available"] and average["sample"]["value"] == 1
    expired = view(repo, cfg, utc_now() + timedelta(minutes=5))["derived_items"][0]
    assert not expired["available"] and expired["last_valid_sample"]["value"] == 1


def test_density_legacy_pair_bootstrap_and_no_history(repo):
    cfg = config([PointConfig(key=label, name=label, node_id="ns=2;s=" + item_id, item_id=item_id)
                  for label, item_id in DENSITY_AVERAGE_MEMBERS])
    start = utc_now() - timedelta(minutes=3)
    repo.acquire(cfg, "owner")
    send(repo, cfg, [None, None], start, [0x80000000] * 2)
    assert view(repo, cfg, utc_now())["derived_items"][0]["last_valid_sample"] is None
    send(repo, cfg, [1.4, 1.6], start + timedelta(minutes=1))
    send(repo, cfg, [None, None], start + timedelta(minutes=2), [0x80000000] * 2)
    with repo.sessions() as db:
        for point in db.scalars(select(TelemetryPoint)):
            point.latest = {k: v for k, v in point.latest.items() if k != DENSITY_MEMORY}
        db.commit()
    assert view(repo, cfg, utc_now())["derived_items"][0]["last_valid_sample"]["value"] == 1.5


def test_same_timestamp_different_batches_cannot_replace_density_memory(repo):
    cfg = config([PointConfig(key=label, name=label, node_id="ns=2;s=" + item_id, item_id=item_id)
                  for label, item_id in DENSITY_AVERAGE_MEMBERS])
    start = utc_now() - timedelta(seconds=10)
    repo.acquire(cfg, "owner")
    send(repo, cfg, [1.4, 1.6], start)
    when = start + timedelta(seconds=1)
    for point, value in zip(cfg.points, [1.6, 1.8]):
        repo.persist(cfg, "owner", {"source_id":cfg.source_id, "batch_id":uuid.uuid4().hex,
            "samples":[normalize(point, value, 0, when, when, when)]})
    repo.state(cfg, "owner", "connected")
    average = view(repo, cfg, utc_now())["derived_items"][0]
    assert not average["available"] and average["status"] == "mixed_batches"
    assert average["last_valid_sample"]["value"] == 1.5
