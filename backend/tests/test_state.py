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


def test_stale_guard_per_category_totals_equal():
    """守卫按类别细分：总数不变但某一类回退，也必须拒绝。

    构造：ash_density 少 1 条、coarse 多 1 条 → 总数相同。
    只比总数的旧规则会放行，从而静默洗掉服务器侧的灰分密度记录。
    """
    client.put("/api/v1/state?force=true", json=SEED)
    st = json.loads(json.dumps(SEED))
    st["coarseCoal"] = st["coarseCoal"] + [dict(st["coarseCoal"][0], timestamp="2026-07-15 09:00:00")]
    st["calcLogs"] = st["calcLogs"][:-1]          # ash_density 少一条
    j = client.put("/api/v1/state", json=st).json()
    assert j["ok"] is False and j.get("stale") is True
    assert "ash_density" in j["error"]             # 错误里点名是哪一类回退
    assert j["regressed"]["ash_density"][0] < j["regressed"]["ash_density"][1]
    # 现库未被改动
    got = client.get("/api/v1/state").json()
    assert len(got["calcLogs"]) == 124
    client.put("/api/v1/state?force=true", json=SEED)


def test_stale_guard_allows_log_shrink():
    """守卫不含日志类数据：前端「清空补录历史」会合法地让 manualEntries 变少，不得被拒。"""
    client.put("/api/v1/state?force=true", json=SEED)
    assert len(SEED.get("manualEntries") or []) > 0
    st = json.loads(json.dumps(SEED))
    st["manualEntries"] = []                      # 相当于 CollectPage.clearHistory()
    j = client.put("/api/v1/state", json=st).json()
    assert j["ok"] is True, j
    assert client.get("/api/v1/state").json().get("manualEntries", []) == []
    client.put("/api/v1/state?force=true", json=SEED)


def test_decision_log_roundtrip():
    """密度决策日志(Stage 0)经 auto_state 持久化:PUT 后 GET 应原样返回。"""
    st = json.loads(json.dumps(SEED))
    st["densityDecisionLog"] = [{
        "ts": "2026-09-10 10:00:00", "trigger": "density_set",
        "scheme": "total", "target": 8.5, "tol": 0.1,
        "rhoCur": 1.49, "rhoNew": 1.48, "deltaRho": -0.01, "deltaA": 0.2,
        "kUsed": 0.075, "kSource": "default", "heavyAsh": 7.85, "totalAsh": 8.46,
        "ctx": {"coalAmount": 800, "rawAsh": 39.15, "desl473": 1, "desl474": 0,
                "miningFace": "6303", "levelTail": 55},
        "response": None,
    }]
    r = client.put("/api/v1/state", json=st)
    assert r.json()["ok"] is True
    got = client.get("/api/v1/state").json()
    entry = got["densityDecisionLog"][0]
    assert entry["rhoNew"] == 1.48 and entry["ctx"]["rawAsh"] == 39.15
    # 响应补记同样无损
    st["densityDecisionLog"][0]["response"] = {"ts": "2026-09-10 11:00:00", "rhoNow": 1.48,
                                               "heavyAshNow": 7.72, "dRhoActual": -0.01, "dAActual": -0.13}
    client.put("/api/v1/state", json=st)
    entry2 = client.get("/api/v1/state").json()["densityDecisionLog"][0]
    assert entry2["response"]["dAActual"] == -0.13
    # 恢复种子
    client.put("/api/v1/state?force=true", json=SEED)
