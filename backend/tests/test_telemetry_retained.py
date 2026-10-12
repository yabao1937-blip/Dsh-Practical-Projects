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
    cfg.history_interval_seconds = 60  # Explicit legacy throttled deployment.
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


def test_raw_pair_unchanged_but_effective_density_holds_each_channel(repo):
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
    effective = average["effective_density"]
    assert effective["value"] == pytest.approx(1.75)
    assert effective["status"] == "held"
    assert [p["value"] for p in effective["components"]] == [1.8, 1.7]
    assert [p["status"] for p in effective["components"]] == ["good", "held"]
    send(repo, cfg, [1.0, 1.0], start + timedelta(seconds=3))
    average = view(repo, cfg, utc_now())["derived_items"][0]
    assert average["available"] and average["sample"]["value"] == 1
    assert average["effective_density"]["value"] == pytest.approx(1.75)
    assert [p["raw_value"] for p in average["effective_density"]["components"]] == [1, 1]
    expired = view(repo, cfg, utc_now() + timedelta(minutes=5))["derived_items"][0]
    assert not expired["available"] and expired["last_valid_sample"]["value"] == 1
    assert expired["effective_density"]["value"] == pytest.approx(1.75)


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
    assert average["effective_density"]["value"] is None


def density_config():
    return config([PointConfig(key=label, name=label, node_id="ns=2;s=" + item_id,
        item_id=item_id, unit="g/cm³") for label, item_id in DENSITY_AVERAGE_MEMBERS])


def test_no_history_never_borrows_the_other_channel_or_uses_one(repo):
    cfg, start = density_config(), utc_now() - timedelta(seconds=10)
    repo.acquire(cfg, "owner")
    send(repo, cfg, [1, 1.5], start)
    repo.state(cfg, "owner", "connected")
    result = view(repo, cfg, utc_now())
    assert result["derived_items"][0]["effective_density"]["value"] is None
    a, b = result["items"]
    assert a["sample"]["value"] == 1 and a["available"]
    assert a["effective_density"]["status"] == "missing"
    assert "无本路" in a["effective_density"]["reason"]
    assert b["effective_density"]["value"] == 1.5


@pytest.mark.parametrize("invalid,quality", [(1,0), (None,0x80000000), (0,0), (-1,0), (1.9,0x80000000)])
def test_channel_hold_survives_bad_samples_pruning_restart_and_definition_change(repo, invalid, quality):
    cfg, start = density_config(), utc_now() - timedelta(days=10)
    repo.acquire(cfg, "owner")
    send(repo, cfg, [1.47, 1.57], start)
    send(repo, cfg, [invalid, 1.6], utc_now(), [quality, 0])
    repo.prune(cfg)
    repo.state(cfg, "owner", "stopped")
    repo.acquire(cfg, "owner")
    send(repo, cfg, [1, 1.62], utc_now())
    repo.state(cfg, "owner", "connected")
    result = view(repo, cfg, utc_now())["derived_items"][0]["effective_density"]
    assert result["value"] == pytest.approx((1.47+1.62)/2)
    assert result["components"][0]["received_at"] == start.isoformat(timespec="milliseconds")
    repo.state(cfg, "owner", "stopped")
    cfg.points[0].offset = .01
    repo.acquire(cfg, "owner")
    send(repo, cfg, [None, 1.62], utc_now(), [0x80000000,0])
    assert view(repo, cfg, utc_now())["derived_items"][0]["effective_density"]["value"] is None


def test_legacy_channel_memory_bootstrap_ignores_one_and_bad_quality(repo):
    cfg, start = density_config(), utc_now() - timedelta(minutes=5)
    repo.acquire(cfg, "owner")
    send(repo, cfg, [1.43, 1.63], start)
    send(repo, cfg, [1.9, 1.9], start + timedelta(minutes=1), [0x80000000]*2)
    send(repo, cfg, [1, 1], start + timedelta(minutes=2))
    with repo.sessions() as db:
        for p in db.scalars(select(TelemetryPoint)):
            p.latest = {**p.latest, MEMORY:{k:v for k,v in p.latest[MEMORY].items() if k != "last_density"}}
        db.commit()
    result = view(repo, cfg, utc_now())
    assert result["derived_items"][0]["effective_density"]["value"] == pytest.approx(1.53)
    send(repo, cfg, [1, 1], utc_now())
    with repo.sessions() as db:
        assert db.get(TelemetryPoint, (cfg.source_id,"A303")).latest[MEMORY]["last_density"]["value"] == 1.43


def test_future_density_cannot_return_as_a_held_live_value(repo):
    cfg, now = density_config(), utc_now()
    repo.acquire(cfg, "owner")
    send(repo, cfg, [1.42, 1.62], now - timedelta(seconds=5))
    send(repo, cfg, [1.9, 1.9], now + timedelta(seconds=20))
    repo.state(cfg, "owner", "connected")
    effective = view(repo, cfg, now)["derived_items"][0]["effective_density"]
    assert effective["value"] == pytest.approx(1.52)
    assert effective["status"] == "held"


def test_ambiguous_density_configuration_is_not_arbitrarily_averaged(repo):
    cfg, now = density_config(), utc_now()
    cfg.points.append(PointConfig(key="duplicate", name="duplicate", node_id="ns=2;s=Other",
        item_id=DENSITY_AVERAGE_MEMBERS[0][1], unit="g/cm³"))
    repo.acquire(cfg, "owner")
    send(repo, cfg, [1.4, 1.6, 1.7], now)
    repo.state(cfg, "owner", "connected")
    effective = view(repo, cfg, now)["derived_items"][0]["effective_density"]
    assert effective["value"] is None
    assert "不唯一" in effective["reason"]


def test_latest_view_density_alarm_uses_raw_values_and_marks_offline_unknown(repo):
    cfg, now = density_config(), utc_now()
    for label, item in DENSITY_AVERAGE_MEMBERS:
        cfg.points.append(PointConfig(key=label+"_SET", name=label+"设定", node_id="ns=2;s="+item+"_SET",
            item_id=item+"_SET", unit="g/cm³"))
    repo.acquire(cfg,"owner")
    send(repo,cfg,[1,1.477,1.48,1.48],now)
    repo.state(cfg,"owner","connected")
    result = view(repo,cfg,now)
    assert result["derived_items"][0]["effective_density"]["value"] is None
    assert [c["status"] for c in result["density_alarm_checks"]] == ["abnormal","normal"]
    assert result["density_alarm_checks"][0]["deviation"] == .48
    repo.state(cfg,"owner","stopped")
    assert all(c["status"] == "unknown" for c in view(repo,cfg,now)["density_alarm_checks"])
