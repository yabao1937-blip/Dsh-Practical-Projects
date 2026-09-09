"""密度-灰分 K 增益在线辨识单测（分系统 OLS / 物理约束回退 / pooled）。"""
import json

from app.services.density_model import fit_density_gain, valid_points


def _store(pairs):
    """pairs: [(system, ash, rho)] → store.calcLogs 形态。"""
    return {"calcLogs": [
        {"calc_type": "ash_density",
         "input_json": json.dumps({"system": s, "belt": "501", "ash_content": a, "density": r})}
        for s, a, r in pairs
    ]}


def test_valid_points_filters():
    st = _store([("A", 8.5, 1.45), ("A", 9.0, None), ("A", 9.0, 1.2)])  # 后两条无效
    assert valid_points(st) == [{"system": "A", "ash": 8.5, "rho": 1.45}]


def test_per_system_fit():
    # A 系统斜率 0.04，B 系统斜率 0.06（相同灰分网格，不同截距 → 合并会偏）
    pairs = []
    for i in range(6):
        a = 8.0 + i * 0.2
        pairs.append(("A", a, 1.40 + 0.04 * (a - 8.0)))
        pairs.append(("B", a, 1.44 + 0.06 * (a - 8.0)))
    r = fit_density_gain(_store(pairs))
    assert r["valid"] is True
    assert r["source"] == "per_system"
    # 加权平均 = (6*0.04 + 6*0.06)/12 = 0.05
    assert abs(r["k"] - 0.05) < 1e-9
    assert set(r["systems"]) == {"A", "B"}
    assert r["systems"]["A"]["used"] and r["systems"]["B"]["used"]


def test_negative_slope_system_dropped():
    # A 系统负斜率（物理不合理）丢弃；B 系统正常 → 仅 B 生效
    pairs = []
    for i in range(6):
        a = 8.0 + i * 0.2
        pairs.append(("A", a, 1.50 - 0.05 * (a - 8.0)))   # k=-0.05
        pairs.append(("B", a, 1.40 + 0.04 * (a - 8.0)))
    r = fit_density_gain(_store(pairs))
    assert r["valid"] is True
    assert r["systems"]["A"]["used"] is False
    assert abs(r["k"] - 0.04) < 1e-9


def test_insufficient_points_invalid():
    r = fit_density_gain(_store([("A", 8.5, 1.45)] * 3))   # 同点重复 denom=0 → None
    assert r["valid"] is False
    assert r["k"] is None


def test_dashboard_exposes_density_k():
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    d = client.get("/api/v1/overview/dashboard").json()
    assert "densityK" in d
    assert d["densityK"]["valid"] in (True, False)
    assert "k" in d["densityK"]
    assert "guidance" in d and "K" in d["guidance"]
