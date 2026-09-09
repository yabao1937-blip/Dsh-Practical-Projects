"""训练接口端到端：SEED 入库 → POST /training/coarse-model → 持久化 + 回填 + /state 回读。"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import models
from app.database import SessionLocal
from app.main import app
from app.services.migrate import apply

BASE = Path(__file__).resolve().parent.parent / "data"
SEED = json.loads((BASE / "seed_store.json").read_text(encoding="utf-8"))
FIXTURE = BASE / "train_js.json"

client = TestClient(app)


def _load_gold():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


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


@pytest.mark.skipif(not FIXTURE.exists(), reason="train_js.json 未生成（node dump_train_js.js）")
def test_train_coarse_model_endpoint():
    GOLD = _load_gold()
    _reset_db()
    apply(SEED)

    r = client.post("/api/v1/training/coarse-model", params={"range": "jun_jul"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["production"] == GOLD["coarseModel"]["production"] == "pls"
    assert body["n"] == 113
    assert body["pls"]["A"] == 2
    assert body["mlr"]["lambda"] == GOLD["coarseModel"]["mlr"]["lambda"]
    assert body["pls"]["q2Time"] == GOLD["coarseModel"]["pls"]["metrics"]["q2Time"]
    # 响应含完整 coarseModel + history，供前端直接落本地
    assert body["coarseModel"]["production"] == "pls"
    assert "coefs" in body["coarseModel"]["pls"] and "imputeMeans" in body["coarseModel"]["pls"]
    assert abs(body["coarseModel"]["pls"]["intercept"] - GOLD["coarseModel"]["pls"]["intercept"]) < 1e-4
    assert body["history"]["production"] == "pls"
    assert body["history"]["pls"]["A"] == 2
    assert body["history"]["mlr"]["q2Time"] == GOLD["coarseModel"]["mlr"]["metrics"]["q2Time"]

    # 持久化：coarse_models 两行，pls 为当前，系数为「当前数据训练」而非出厂常量
    db = SessionLocal()
    try:
        rows = db.query(models.CoarseModel).all()
        assert len(rows) == 2
        by_method = {m.method: m for m in rows}
        assert by_method["pls"].is_current is True
        assert by_method["mlr"].is_current is False
        assert abs(by_method["pls"].intercept - GOLD["coarseModel"]["pls"]["intercept"]) < 1e-4
        assert abs(by_method["mlr"].intercept - GOLD["coarseModel"]["mlr"]["intercept"]) < 1e-4
        assert by_method["pls"].pls_A == 2
        assert by_method["pls"].feature_names == GOLD["features"]

        hist = db.query(models.CoarseModelHistory).order_by(models.CoarseModelHistory.id.desc()).first()
        assert hist is not None
        assert hist.production == "pls"
        assert hist.n == 113

        # 回填 predicted_ash：全部 coarse 记录与前端训练后一致（round4）
        expected = {r["timestamp"]: r["predicted_ash"] for r in GOLD["coarseCoal"]}
        recs = db.query(models.CoalRecord).filter(models.CoalRecord.category == "coarse").all()
        assert len(recs) == 113
        for rec in recs:
            assert rec.predicted_ash == expected[rec.ts], f"{rec.ts} predicted_ash 不一致"
    finally:
        db.close()

    # /state 回读：coarseModel 为训练后模型
    state = client.get("/api/v1/state").json()
    assert state["coarseModel"]["production"] == "pls"
    assert abs(state["coarseModel"]["pls"]["intercept"] - GOLD["coarseModel"]["pls"]["intercept"]) < 1e-4


def test_train_coarse_model_insufficient():
    _reset_db()
    # 无 coarse 记录 → 422
    r = client.post("/api/v1/training/coarse-model", params={"range": "jun_jul"})
    assert r.status_code == 422


def test_train_coarse_model_invalid_range():
    _reset_db()
    apply(SEED)
    # 非法 range → FastAPI Literal 校验 422
    r = client.post("/api/v1/training/coarse-model", params={"range": "foo"})
    assert r.status_code == 422


def test_train_coarse_model_range_30d_all():
    """30d/all 范围：插入一条 5月（jun_jul/30d 之外）记录，验证三个范围的样本数。"""
    _reset_db()
    apply(SEED)
    client.post("/api/v1/records", json={
        "category": "coarse", "ts": "2026-05-01 10:00:00", "system": "合并",
        "ash_content": 12.0, "coal_amount": 800.0, "sysA": 1})

    jun = client.post("/api/v1/training/coarse-model", params={"range": "jun_jul"})
    assert jun.status_code == 200 and jun.json()["n"] == 113      # 不含 5月

    d30 = client.post("/api/v1/training/coarse-model", params={"range": "30d"})
    assert d30.status_code == 200 and d30.json()["n"] == 55       # seed 跨 06-16~07-30，最近30天=55；5月不在窗口

    allr = client.post("/api/v1/training/coarse-model", params={"range": "all"})
    assert allr.status_code == 200 and allr.json()["n"] == 114    # 含 5月
