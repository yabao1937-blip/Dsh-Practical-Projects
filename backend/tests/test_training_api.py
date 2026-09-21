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
FIXTURE = BASE / "coarse_v2_js.json"

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


@pytest.mark.skipif(not FIXTURE.exists(), reason="coarse_v2_js.json 未生成（node scripts/refresh_algorithm_goldens.js）")
def test_train_coarse_model_endpoint():
    GOLD = _load_gold()
    _reset_db()
    apply(SEED)

    r = client.post("/api/v1/training/coarse-model", params={"range": "jun_jul", "engine": "gpt"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["production"] == GOLD["coarseModel"]["production"] == "mlr"
    assert body["n"] == 113
    assert body["pls"]["A"] == GOLD["coarseModel"]["pls"]["A"]
    assert body["mlr"]["lambda"] == GOLD["coarseModel"]["mlr"]["lambda"]
    assert abs(body["pls"]["q2Time"] - GOLD["coarseModel"]["pls"]["metrics"]["q2Time"]) < 1e-9
    # 响应含完整 coarseModel + history，供前端直接落本地
    assert body["coarseModel"]["production"] == "mlr"
    assert "coefs" in body["coarseModel"]["pls"] and "imputeMeans" in body["coarseModel"]["pls"]
    assert abs(body["coarseModel"]["pls"]["intercept"] - GOLD["coarseModel"]["pls"]["intercept"]) < 1e-4
    assert body["history"]["production"] == "mlr"
    assert body["history"]["pls"]["A"] == GOLD["coarseModel"]["pls"]["A"]
    assert abs(body["history"]["mlr"]["q2Time"] - GOLD["coarseModel"]["mlr"]["metrics"]["q2Time"]) < 1e-9

    # 持久化：coarse_models 两行，mlr 为当前，系数为「当前数据训练」而非出厂常量
    db = SessionLocal()
    try:
        rows = db.query(models.CoarseModel).all()
        assert len(rows) == 2
        by_method = {m.method: m for m in rows}
        assert by_method["pls"].is_current is False
        assert by_method["mlr"].is_current is True
        assert abs(by_method["pls"].intercept - GOLD["coarseModel"]["pls"]["intercept"]) < 1e-4
        assert abs(by_method["mlr"].intercept - GOLD["coarseModel"]["mlr"]["intercept"]) < 1e-4
        assert by_method["pls"].pls_A == GOLD["coarseModel"]["pls"]["A"]
        assert by_method["pls"].feature_names == GOLD["features"]

        hist = db.query(models.CoarseModelHistory).order_by(models.CoarseModelHistory.id.desc()).first()
        assert hist is not None
        assert hist.production == "mlr"
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
    assert state["_revision"] == body["revision"]
    assert state["coarseModel"]["mlr"]["metrics"]["validation"]["method"] == "nested-day-walk-forward"
    assert state["coarseModel"]["production"] == "mlr"
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


def test_ds_default_and_variant_roundtrip():
    """原版可复现、两版独立保存，镜像切换后重读/后端预测仍一致。"""
    _reset_db()
    apply(SEED)
    gold = json.loads((BASE / "train_js.json").read_text(encoding="utf-8"))
    ds = client.post("/api/v1/training/coarse-model").json()
    assert ds["engine"] == "ds"
    for method in ("mlr", "pls"):
        actual, expected = ds["coarseModel"][method], gold["coarseModel"][method]
        assert actual["coefs"] == pytest.approx(expected["coefs"], abs=1e-6)
        assert actual["metrics"]["q2"] == pytest.approx(expected["metrics"]["q2"], abs=1e-6)
    gpt_response = client.post("/api/v1/training/coarse-model", params={"engine": "gpt"})
    assert gpt_response.status_code == 200, gpt_response.text
    state = client.get("/api/v1/state").json()
    assert set(state["coarseModelVariants"]) == {"ds", "gpt"}
    assert state["coarseModel"]["mlr"]["metrics"]["version"] == 2
    assert state["coarseModelVariants"]["ds"]["mlr"]["coefs"] == ds["coarseModel"]["mlr"]["coefs"]
    assert state["coarseModelVariants"]["ds"] == ds["coarseModel"]
    gpt_snapshot = state["coarseModelVariants"]["gpt"]
    # 与前端切换已训练版本的镜像动作相同（临时测试库）。
    state["coarseModel"] = state["coarseModelVariants"]["ds"]
    from app.services.modeling import predict_coarse_ash
    from app.services.training import _js_round
    selected = state["coarseModel"][state["coarseModel"]["production"]]
    for rec in state["coarseCoal"]:
        rec["predicted_ash"] = _js_round(predict_coarse_ash(rec, selected), 4)
    response = client.put("/api/v1/state", json=state)
    assert response.status_code == 200 and response.json()["ok"], response.text
    restored = client.get("/api/v1/state").json()
    assert restored["coarseModel"]["mlr"]["metrics"].get("version") != 2
    assert restored["coarseModelVariants"]["gpt"] == gpt_snapshot
    assert restored["coarseModel"]["mlr"]["coefs"] == ds["coarseModel"]["mlr"]["coefs"]
    before_revision = restored["_revision"]
    invalid = client.post("/api/v1/training/coarse-model", params={"engine": "unknown"})
    assert invalid.status_code == 422
    assert client.get("/api/v1/state").json()["_revision"] == before_revision


def test_training_revision_conflict_does_not_replace_models():
    _reset_db()
    apply(SEED)
    before = client.get("/api/v1/state").json()
    r = client.post("/api/v1/training/coarse-model", headers={"X-DMCS-Revision": "stale"})
    assert r.status_code == 409
    assert client.get("/api/v1/state").json()["_revision"] == before["_revision"]
    r = client.post("/api/v1/training/coarse-model", headers={"X-DMCS-Revision": before["_revision"]})
    assert r.status_code == 200
    assert client.get("/api/v1/state").json()["_revision"] == r.json()["revision"]


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
