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


def test_stale_guard_allows_mirror_with_empty_local_only_collections():
    """回归：前端从服务器拉完数据后，其"纯本地键"仍是空的，镜像**必须**被接受。

    背景（2026-09 真实事故）：守卫曾把 heavy_samples 纳入比较，而
    App._applyServerState 把 heavySamples 列为纯本地键（从服务器拉取时不覆盖它），
    于是新浏览器的向量里 heavy_samples 恒为 0 < 服务器的 1 → 每次镜像都被判"回退"，
    表现为"前端所有改动静默同步不上去"（真实库上实测复现过）。
    结论：守卫只能比较两端必然一致的口径，不能比较前端设计上就不回传的集合。

    这里构造的正是那种快照：三类测量记录与现库一致，
    而 heavySamples / manualEntries / importLogs / alerts 都是空的。
    """
    client.put("/api/v1/state?force=true", json=SEED)
    # 先在服务器放一条 heavy_sample，制造"服务器有、浏览器没有"的局面
    server_has_sample = json.loads(json.dumps(SEED))
    server_has_sample["heavySamples"] = [{
        "timestamp": "2026-09-01 08:00:00", "rho": 1.50, "ash_content": 7.9,
    }]
    assert client.put("/api/v1/state", json=server_has_sample).json()["ok"] is True

    # 模拟"新浏览器拉完服务器数据"：三类测量记录齐全，但纯本地键为空
    pulled = json.loads(json.dumps(SEED))
    pulled["heavySamples"] = []
    pulled["manualEntries"] = []
    pulled["importLogs"] = []
    pulled["alerts"] = []
    j = client.put("/api/v1/state", json=pulled).json()
    assert j["ok"] is True, f"镜像被守卫误拒（这正是那次事故的现象）：{j}"
    client.put("/api/v1/state?force=true", json=SEED)


def test_mirror_does_not_wipe_heavy_samples():
    """整库镜像不拥有 heavy_samples：新浏览器（本地为空）的镜像不得清零服务器侧采样。

    背景（2026-09 实测事故）：守卫收窄之后镜像能写入了，但整库 PUT 的载荷里
    heavy_samples 是空的（前端把它当纯本地键、从不拉取），而 replace() 会照删照写
    → 每来一个空 profile 的浏览器就把服务器累积的采样清零（实测 heavy_samples 1 → 0）。
    采样有自己的增量通道 POST /api/v1/samples/heavy-ash，整库镜像不该触碰；
    只有 force=true（显式备份恢复）才允许整体覆盖。
    """
    from app.database import SessionLocal
    from app.models import HeavySample

    client.put("/api/v1/state?force=true", json=SEED)
    # 服务器侧先有 1 条采样
    st = json.loads(json.dumps(SEED))
    st["heavySamples"] = [{"timestamp": "2026-09-01 08:00:00", "rho": 1.50, "ash_content": 7.9}]
    assert client.put("/api/v1/state?force=true", json=st).json()["ok"] is True

    db = SessionLocal()
    try:
        assert db.query(HeavySample).count() == 1
    finally:
        db.close()

    # 模拟新浏览器镜像：三类测量记录齐全，但 heavySamples 为空
    pulled = json.loads(json.dumps(SEED))
    pulled["heavySamples"] = []
    j = client.put("/api/v1/state", json=pulled).json()
    assert j["ok"] is True, j

    db = SessionLocal()
    try:
        n = db.query(HeavySample).count()
        assert n == 1, f"镜像把服务器侧采样清零了（{n} 条），这正是那次事故的现象"
    finally:
        db.close()
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
