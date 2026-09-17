# -*- coding: utf-8 -*-
"""K 标定(决策日志)提取/过滤/估计的单元测试 —— 全部合成数据,不依赖真实库。"""
import math

from app.services.k_calibration import extract_points, summarize


def mk(ts, drho, da, lag=60, trigger="density_set", sysA=1, sysB=1,
       coal=500.0, raw=20.0, resp=True, invalid=False):
    """构造一条决策日志条目。dRhoActual=dRho, dAActual=da(符号与 K=Δρ/ΔA 一致)。"""
    e = {"ts": ts, "trigger": trigger, "scheme": "total", "target": 8.5, "tol": 0.1,
         "rhoCur": 1.45, "rhoNew": 1.43, "deltaRho": -0.02, "deltaA": 0.3,
         "kUsed": 0.075, "kSource": "default", "heavyAsh": 8.6, "totalAsh": 8.8,
         "ctx": {"coalAmount": coal, "rawAsh": raw, "sysA": sysA, "sysB": sysB,
                 "miningFace": "3309", "levelTail": 12.0, "densityActual": 1.44,
                 "heavyAshSource": "manual"},
         "response": None}
    if resp:
        e["response"] = ({"invalidated": True, "reason": "窗口内新决策"} if invalid else
                         {"ts": ts, "lagMin": lag, "source": "heavySamples",
                          "rhoNow": 1.44, "heavyAshNow": 8.4,
                          "dRhoActual": drho, "dAActual": da})
    return e


def test_exact_k_estimate():
    # Δρ=-0.012 / ΔA=-0.2 → K=0.06,8 条全采信
    store = {"densityDecisionLog": [mk("2026-09-1%d 08:00:00" % (i + 1), -0.012, -0.2) for i in range(8)]}
    pts = extract_points(store)
    assert len(pts) == 8
    assert all(p["eligible"] and p["stage"] == "eligible" for p in pts)
    for p in pts:
        assert math.isclose(p["k"], 0.06, abs_tol=1e-12)
    s = summarize(pts)
    assert s["counts"]["eligible"] == 8
    assert math.isclose(s["estimates"]["median"], 0.06, abs_tol=1e-12)
    assert math.isclose(s["estimates"]["mean"], 0.06, abs_tol=1e-12)


def test_funnel_exclusions():
    store = {"densityDecisionLog": [
        mk("2026-09-01 08:00:00", -0.012, -0.2),                          # 采信
        mk("2026-09-02 08:00:00", -0.012, -0.2, resp=False),             # pending
        mk("2026-09-03 08:00:00", -0.012, -0.2, invalid=True),           # invalidated
        mk("2026-09-04 08:00:00", -0.012, -0.2, lag=30),                 # lag(太早)
        mk("2026-09-05 08:00:00", -0.012, -0.2, lag=300),                # lag(太晚)
        mk("2026-09-06 08:00:00", -0.001, -0.2),                         # small_drho
        mk("2026-09-07 08:00:00", -0.012, -0.01),                        # weak_da
        mk("2026-09-08 08:00:00", 0.012, -0.2),                          # bounds(K<0)
    ]}
    pts = extract_points(store)
    s = summarize(pts)
    c = s["counts"]
    assert (c["total"], c["pending"], c["invalidated"], c["eligible"]) == (8, 1, 1, 1)
    assert c["closed"] == 6   # 已闭环 = 采信(1) + 排除(5)
    assert c["excluded"] == 5
    stages = s["stages"]
    assert stages.get("lag") == 2 and stages.get("small_drho") == 1
    assert stages.get("weak_da") == 1 and stages.get("bounds") == 1


def test_grouping_by_trigger():
    # density_set: K=0.06(4条); retarget: K=0.08(4条)
    log = ([mk("2026-09-0%d 08:00:00" % (i + 1), -0.012, -0.2, trigger="density_set") for i in range(4)] +
           [mk("2026-09-1%d 08:00:00" % (i + 1), -0.016, -0.2, trigger="retarget") for i in range(4)])
    s = summarize(extract_points({"densityDecisionLog": log}))
    g = s["groups"]["触发类型"]
    assert math.isclose(g["density_set"]["median"], 0.06, abs_tol=1e-12)
    assert math.isclose(g["retarget"]["median"], 0.08, abs_tol=1e-12)


def test_empty_and_missing_key():
    for store in ({}, {"densityDecisionLog": []}):
        s = summarize(extract_points(store))
        assert s["counts"]["total"] == 0 and s["estimates"] is None


def test_coal_amount_buckets_need_enough_samples():
    # 4 条样本 < GROUP_MIN_N*2(6):不应生成"带煤量桶"分组
    log = [mk("2026-09-0%d 08:00:00" % (i + 1), -0.012, -0.2, coal=100.0 * (i + 1)) for i in range(4)]
    s = summarize(extract_points({"densityDecisionLog": log}))
    assert "带煤量桶" not in s["groups"]
