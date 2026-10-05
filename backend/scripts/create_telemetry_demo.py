"""创建一次性的临时展示库，输出 URL；不连接设备、不读取或修改现场库。"""
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
path = Path(tempfile.mkdtemp(prefix="dmcs-telemetry-demo-")) / "demo.db"
url = f"sqlite:///{path.as_posix()}"
os.environ["DMCS_DATABASE_URL"] = url
os.environ["DMCS_WRITE_AUTH"] = "off"

from app import models
from app.database import Base, SessionLocal, engine
from app.telemetry.config import CollectorConfig, PointConfig
from app.telemetry.domain import iso_time, normalize, utc_now
from app.telemetry.models import TelemetrySource
from app.telemetry.repository import TelemetryRepository

Base.metadata.create_all(engine)
points = [
    PointConfig(key="density", name="演示密度", node_id="ns=2;s=Demo.Density", unit="g/cm³", max_age_seconds=3600),
    PointConfig(key="zero", name="演示零值", node_id="ns=2;s=Demo.Zero", unit="t/h", max_age_seconds=3600),
    PointConfig(key="switch", name="演示停机状态", node_id="ns=2;s=Demo.Switch", value_type="boolean", max_age_seconds=3600),
    PointConfig(key="bad", name="演示坏质量", node_id="ns=2;s=Demo.Bad", max_age_seconds=3600),
    PointConfig(key="uncertain", name="演示不确定质量", node_id="ns=2;s=Demo.Uncertain", max_age_seconds=3600),
    PointConfig(key="stale", name="演示超时", node_id="ns=2;s=Demo.Stale"),
]
cfg = CollectorConfig(enabled=True, source_id="demo-only", name="临时演示数据", endpoint="opc.tcp://127.0.0.1:49320", security="None", allow_insecure=True, points=points)
repository = TelemetryRepository()
repository.acquire(cfg, "demo-fixture")
now = utc_now()
samples = [normalize(point, value, quality,
    now - timedelta(seconds=62) if point.key == "stale" else now - timedelta(seconds=2),
    now - timedelta(seconds=61) if point.key == "stale" else now - timedelta(seconds=1),
    now - timedelta(seconds=60) if point.key == "stale" else now)
    for point, value, quality in zip(points, [1.48, 0, False, 99, 12, 1.5], [0, 0, 0, 0x80000000, 0x40000000, 0])]
repository.persist(cfg, "demo-fixture", {"source_id": cfg.source_id, "batch_id": "demo-batch", "samples": samples})
repository.state(cfg, "demo-fixture", "connected")
with SessionLocal() as db:
    db.get(TelemetrySource, cfg.source_id).lease_until = iso_time(now + timedelta(hours=1))
    db.commit()
print(url)
