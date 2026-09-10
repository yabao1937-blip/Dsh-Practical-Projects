"""P5 后端侧：静态托管 + /state 整库快照"""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
SEED = json.loads((Path(__file__).resolve().parent.parent / "data" / "seed_store.json").read_text(encoding="utf-8"))


def test_static_serves_index():
    r = client.get("/")
    assert r.status_code == 200
    assert "重介密控系统" in r.text


def test_state_roundtrip():
    r = client.put("/api/v1/state", json=SEED)
    assert r.status_code == 200
    got = client.get("/api/v1/state").json()
    assert len(got["coarseCoal"]) == 113
    assert len(got["floatCoal"]) == 5
    assert len(got["calcLogs"]) == 124


def test_state_feeds_dashboard():
    client.put("/api/v1/state", json=SEED)
    d = client.get("/api/v1/overview/dashboard").json()
    # 2026-09 皮带分工改造后:heavyAsh=502在线(8.1) → 公式 totalAsh=8.5488
    assert abs(d["totalAsh"] - 8.5488) < 1e-6
    assert abs(d["coarseAsh"] - 13.67) < 1e-6


def test_state_stale_guard():
    """防回退守卫:入库记录少于现库 → 拒绝;force=true 可越过。"""
    small = {"coarseCoal": SEED["coarseCoal"][:10], "floatCoal": [], "calcLogs": []}
    j = client.put("/api/v1/state", json=small).json()
    assert j["ok"] is False and j.get("stale") is True
    # 被拒绝后现库数据完好
    assert len(client.get("/api/v1/state").json()["coarseCoal"]) == 113
    # force 覆盖
    assert client.put("/api/v1/state?force=true", json=small).json()["ok"] is True
    assert len(client.get("/api/v1/state").json()["coarseCoal"]) == 10
    # 恢复种子供后续用例
    client.put("/api/v1/state?force=true", json=SEED)
