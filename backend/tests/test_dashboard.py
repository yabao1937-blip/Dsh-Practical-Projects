"""P4 总览 dashboard 端到端：SEED 迁移入库 → load_store → 取值链 → 与前端 JS 一致。"""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app import models
from app.database import SessionLocal
from app.main import app
from app.services.state import load_store
from app.services.guidance import build_density_guidance
from app.services.resolvers import resolve_coarse_ash, resolve_float_ash, resolve_amount
from app.services.migrate import apply

BASE = Path(__file__).resolve().parent.parent / "data"
SEED = json.loads((BASE / "seed_store.json").read_text(encoding="utf-8"))
JS = json.loads((BASE / "resolvers_js.json").read_text(encoding="utf-8"))

client = TestClient(app)


def _reset_db():
    db = SessionLocal()
    try:
        for t in (models.CoalRecord, models.CalcLog, models.CoarseModelHistory,
                  models.CoarseModel, models.RegressionModel, models.HeavySample,
                  models.ManualEntry, models.ImportLog, models.Alert,
                  models.Setting, models.AutoState):
            db.query(t).delete()
        db.commit()
    finally:
        db.close()


def test_dashboard_roundtrip():
    _reset_db()
    apply(SEED)
    with SessionLocal() as db:
        store = load_store(db)
    guide, gain = build_density_guidance(store)
    d = {"heavyAsh": guide["heavyAsh"], "density": guide["rhoCur"], "totalAsh": guide["actualTotal"],
         "coarseAsh": resolve_coarse_ash(store), "floatAsh": resolve_float_ash(store),
         "amounts": {key: resolve_amount(store, key) for key in ("denseAmount", "floatAmount", "coarseAmount", "totalAmount")},
         "guidance": guide}
    assert d["heavyAsh"] == JS["heavyAsh"]
    assert d["density"] == JS["density"]
    assert abs(d["totalAsh"] - JS["totalAsh"]) < 1e-6
    assert abs(d["coarseAsh"] - JS["coarseAsh"]) < 1e-6
    assert d["floatAsh"] == JS["floatAsh"]
    assert d["amounts"]["denseAmount"] == JS["heavyAmt"]
    assert d["amounts"]["floatAmount"] == JS["floatAmt"]
    assert d["amounts"]["coarseAmount"] == JS["coarseAmt"]
    assert d["amounts"]["totalAmount"] == JS["totalAmt"]
    # 2026-09 皮带分工改造后:heavyAsh=502在线(8.1) → totalAsh=8.5488 → deltaA=0.049 ≤ 容差0.1
    # → 已达标,密度保持 rhoCur(1.49)
    assert abs(d["guidance"]["rhoNew"] - 1.49) < 1e-6
    assert d["guidance"]["direction"] == "stable"
