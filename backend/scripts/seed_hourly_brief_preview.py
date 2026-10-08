"""Create an isolated UI fixture database, never the production database.

Usage: python scripts/seed_hourly_brief_preview.py
Then serve with DMCS_DATABASE_URL set to the printed URL on an unused local port.
"""
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
fixture_dir = BACKEND / "data" / "verification"
fixture_dir.mkdir(exist_ok=True)
path = fixture_dir / ("hourly-ui-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".db")
os.environ["DMCS_DATABASE_URL"] = "sqlite:///" + path.as_posix()

from app.database import Base, engine, SessionLocal
from app.hourly_brief.columns import COLUMNS, cell
from app.hourly_brief.models import HourlyBriefRow, HourlyBriefSchedule
from app.hourly_brief.service import calculate, next_hour
from app.services.modeling import MLR_FEATURES
from app.telemetry.domain import iso_time

Base.metadata.create_all(engine)
now = datetime.now(timezone.utc)
hour = now.replace(minute=0, second=0, microsecond=0)
cells = {key: cell(reason="界面验证样例 · 未配置") for key, _, _ in COLUMNS}
for key, value in dict(raw_ash=42, coal_amount=800, level=55, coarse_amount=40,
    heavy_ash=7.9, heavy_amount=235.2, float_ash=9.85, float_amount=20, density=1.45).items():
    cells[key] = cell(value, "界面验证样例", iso_time(hour-timedelta(seconds=3)), "good")
cells["raw_ash"].update(status="held", reason="设备停机 · 沿用上次非零读数")
cells["heavy_amount"].update(status="default", reason="默认值")
cells["systems"] = cell({"401":True, "402":False, "A":True, "B":False}, status="good")
cells["desliming"] = cell({"473":True, "474":False}, status="good")
cells["float_press"] = cell({"A415":True, "A416":False, "A417":False, "A418":False}, status="good")
metadata = dict(method="mlr", trained_at="界面验证样例", n=100,
    model=dict(type="mlr", featureNames=MLR_FEATURES, intercept=10, coefs=[.1]+[0]*9))
features, guide = calculate(cells, {}, metadata, hour)
with SessionLocal() as db:
    db.add(HourlyBriefSchedule(key="main", activated_at=iso_time(hour-timedelta(hours=1)), next_hour=iso_time(next_hour(now))))
    db.add(HourlyBriefRow(hour=iso_time(hour), generated_at=iso_time(hour), snapshot={
        "cells":cells, "model":metadata, "model_inputs":features, "guidance":guide, "policy":"UI-test-only"}))
    db.commit()
print(os.environ["DMCS_DATABASE_URL"])
