"""Instrument projection tests use isolated SQLite and synthetic samples only."""
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.telemetry.config import CollectorConfig, PointConfig
from app.telemetry.domain import definition_id, normalize, utc_now
from app.telemetry.models import TelemetryPoint
from app.telemetry.repository import TelemetryRepository, latest_view
from app.telemetry.views import ensure_sqlite_views


def setup(tmp_path, points):
    engine = create_engine(f"sqlite:///{tmp_path / 'instruments.db'}")
    Base.metadata.create_all(engine)
    ensure_sqlite_views(engine)
    cfg = CollectorConfig(points=points)
    repo = TelemetryRepository(sessionmaker(engine))
    repo.acquire(cfg, "owner")
    return engine, cfg, repo


def test_exact_six_columns_preserve_zero_false_and_unread_points(tmp_path):
    points = [PointConfig(key=key, name=key, node_id=f"ns=2;s={key}", device="PLC", tag_name=key,
        description="说明", item_id=f"PLC.Device.{key}", value_type="boolean" if key == "run" else "number")
        for key in ["zero", "run", "unread"]]
    engine, cfg, repo = setup(tmp_path, points)
    try:
        now = utc_now()
        samples = [normalize(points[0], 0, 0, now, now, now), normalize(points[1], False, 0, now, now, now)]
        repo.persist(cfg, "owner", {"source_id":cfg.source_id, "batch_id":uuid.uuid4().hex, "samples":samples})
        with engine.connect() as db:
            result = db.execute(text("SELECT * FROM online_instruments"))
            assert list(result.keys()) == ["设备", "标签名", "说明", "ItemID", "值", "时间"]
            rows = {r[1]:tuple(r) for r in result}
            assert rows["zero"][4] == 0 and rows["run"][4] == 0
            assert rows["unread"][4:] == (None, None)
            assert rows["zero"][:4] == ("PLC", "zero", "说明", "PLC.Device.zero")
    finally:
        engine.dispose()


def test_metadata_change_keeps_latest_and_existing_sample_definition(tmp_path):
    point = PointConfig(key="density", name="密度", node_id="ns=2;s=Density")
    engine, cfg, repo = setup(tmp_path, [point])
    try:
        now = utc_now()
        sample = normalize(point, 1.5, 0, now, now, now)
        repo.persist(cfg, "owner", {"source_id":cfg.source_id, "batch_id":uuid.uuid4().hex, "samples":[sample]})
        changed = cfg.model_copy(deep=True)
        changed.points[0].device = "PLC"
        changed.points[0].tag_name = "MD"
        changed.points[0].description = "密度读数"
        changed.points[0].item_id = "PLC.Device.MD"
        assert definition_id(point.model_dump()) == definition_id(changed.points[0].model_dump())
        repo.acquire(changed, "owner")
        repo.state(changed, "owner", "connected")
        with repo.sessions() as db:
            assert db.get(TelemetryPoint, (cfg.source_id, point.key)).latest == sample
            item = latest_view(db)["items"][0]
            assert item["device"] == "PLC" and item["tag_name"] == "MD" and item["available"]
            assert item["sample"]["value"] == 1.5
    finally:
        engine.dispose()
