"""MLR/PLS 训练引擎测试：对拍 Node oracle（train_js.json）+ sklearn 等价性。

oracle 由 scripts/dump_train_js.js 在前端 App.trainCoarseModel('jun_jul') 现场运行产出，
保存在 data/train_js.json。数据已相对出厂 DEFAULT_COARSE_MODEL 漂移（raw_ash/is_stoppage），
因此 oracle 是「同一份输入上 JS 训练器的全量输出」，而非存量的出厂常量。
"""
import copy
import json
from pathlib import Path

import pytest

from app.services import training as T
from app.services import training_sklearn as TS

DATA = Path(__file__).resolve().parent.parent / "data"
FIXTURE = DATA / "train_js.json"

pytestmark = pytest.mark.skipif(not FIXTURE.exists(), reason="train_js.json 未生成（node dump_train_js.js）")


def _load():
    d = json.loads(FIXTURE.read_text(encoding="utf-8"))
    cc = d["coarseCoal"]
    xy = T.build_coarse_xy(cc, "jun_jul")
    return d, cc, xy


def _max_abs_diff(a, b):
    if isinstance(a, dict):
        assert set(a.keys()) == set(b.keys()), f"dict keys 不一致: {a.keys()} vs {b.keys()}"
        return max((_max_abs_diff(a[k], b[k]) for k in a), default=0.0)
    if isinstance(a, list):
        assert len(a) == len(b), f"list 长度不一致: {len(a)} vs {len(b)}"
        return max((abs(x - y) for x, y in zip(a, b)), default=0.0)
    return abs(a - b)


def _assert_model_close(py, js, fields=("intercept", "coefs", "means", "stds", "stdCoef")):
    for f in fields:
        assert _max_abs_diff(py[f], js[f]) < 1e-9, f"{f} 超容差: {py[f]} vs {js[f]}"


def _assert_metrics_close(py, js, keys=("r2", "adjR2", "rmse", "mae", "passRate", "passRate1", "passRate15", "q2")):
    for k in keys:
        assert _max_abs_diff(py["metrics"][k], js["metrics"][k]) < 1e-9, f"metrics.{k} 超容差"


def test_mlr_parity_vs_oracle():
    d, cc, xy = _load()
    golden = d["coarseModel"]["mlr"]
    mlr = T.train_mlr(xy["X"], xy["y"], 0.8)
    assert mlr is not None
    _assert_model_close(mlr, golden)
    _assert_metrics_close(mlr, golden)
    # 离散决策零容差
    assert mlr["drop"] == golden["drop"] == []
    assert mlr["k"] == golden["k"] == 10
    assert mlr["n"] == golden["n"] == 113
    assert mlr["lambda"] == golden["lambda"]
    assert mlr["metrics"]["lambda"] == golden["metrics"]["lambda"]


def test_pls_parity_vs_oracle():
    d, cc, xy = _load()
    golden = d["coarseModel"]["pls"]
    pls = T.train_pls(xy["X"], xy["y"], 0.8, 10)
    assert pls is not None
    _assert_model_close(pls, golden)
    _assert_metrics_close(pls, golden)
    assert pls["drop"] == golden["drop"] == []
    assert pls["A"] == golden["A"] == 2


def test_train_coarse_model_parity_vs_oracle():
    d, cc, xy = _load()
    golden = d["coarseModel"]
    gold_hist = d["history"][-1]
    gold_pred = [r["predicted_ash"] for r in d["coarseCoal"]]

    cc2 = copy.deepcopy(cc)
    res = T.train_coarse_model(cc2, "jun_jul", 0.8)
    assert res is not None
    assert res["production"] == golden["production"] == "pls"
    assert res["mlr"]["metrics"]["q2Time"] == golden["mlr"]["metrics"]["q2Time"]
    assert res["pls"]["metrics"]["q2Time"] == golden["pls"]["metrics"]["q2Time"]

    # history 逐字段（已 round4/1/3）
    h = res["history"]
    for m in ("mlr", "pls"):
        for k in ("r2", "passRate", "q2", "q2Time", "rmse", "mae"):
            assert h[m][k] == gold_hist[m][k], f"history.{m}.{k}"
    assert h["pls"]["A"] == gold_hist["pls"]["A"] == 2
    assert h["production"] == gold_hist["production"] == "pls"
    assert h["n"] == gold_hist["n"] == 113

    # 回填 predicted_ash round4 逐值一致
    pred = [r["predicted_ash"] for r in cc2]
    assert pred == gold_pred


def test_mlr_sklearn_equivalent():
    d, cc, xy = _load()
    a = T.train_mlr(xy["X"], xy["y"], 0.8)
    b = TS.train_mlr_sklearn(xy["X"], xy["y"], 0.8)
    assert b is not None
    assert b["lambda"] == a["lambda"]
    assert b["drop"] == a["drop"]
    for f in ("intercept", "coefs", "means", "stds", "stdCoef"):
        assert _max_abs_diff(b[f], a[f]) < 1e-9, f"{f} sklearn 与移植版超容差"


def test_pls_sklearn_equivalent():
    d, cc, xy = _load()
    a = T.train_pls(xy["X"], xy["y"], 0.8, 10)
    b = TS.train_pls_sklearn(xy["X"], xy["y"], 0.8, 10)
    assert b is not None
    assert b["A"] == a["A"]
    assert b["drop"] == a["drop"]
    for f in ("intercept", "coefs", "means", "stds", "stdCoef"):
        assert _max_abs_diff(b[f], a[f]) < 1e-9, f"{f} sklearn 与移植版超容差"


def test_const_and_dup_columns_dropped():
    """覆盖 drop!=[] 分支（当前 seed 天然不触发）：常数列+重复列被剔除，unDrop 回填占位。"""
    X = [
        [1.0, 2.0, 7.0, 1.0],
        [2.0, 3.0, 7.0, 2.0],
        [3.0, 4.0, 7.0, 3.0],
        [4.0, 5.0, 7.0, 4.0],
        [5.0, 6.0, 7.0, 5.0],
    ]
    y = [1.0, 2.0, 3.0, 4.0, 5.0]   # y == 第 0 列
    mlr = T.train_mlr(X, y, 0.8)
    assert mlr is not None
    assert 2 in mlr["drop"] and 3 in mlr["drop"]
    assert len(mlr["coefs"]) == 4          # unDrop 回填到全特征长度
    assert mlr["coefs"][2] == 0.0           # 常数列系数占位 0
    assert mlr["means"][2] == 0.0
    assert mlr["stds"][2] == 1.0
    # 被剔除列系数/均值/std 占位正确即可；剩余两列由岭回归拟合（小样本 LOOCV 会选 λ>0，不做精确恢复断言）


def test_js_round_mirrors_tofixed():
    """_js_round 应镜像 JS toFixed 的 round-half-away（含负数平局与浮点精确值）。"""
    assert T._js_round(0.0406, 4) == 0.0406
    assert T._js_round(4.35, 1) == 4.3        # double 精确值 4.3499... → 下舍
    assert T._js_round(-0.25, 1) == -0.3      # 负数平局远离零
    assert T._js_round(1.5, 0) == 2.0


def test_q2time_none_fallback_to_q2():
    """n<30 时 _time_cv_q2 返回 None，production 回退 LOOCV q2 选型。"""
    d, cc, xy = _load()
    sub = [dict(r) for r in d["coarseCoal"][:20]]
    res = T.train_coarse_model(sub, "jun_jul", 0.8)
    assert res is not None
    assert res["mlr"]["metrics"]["q2Time"] is None
    assert res["pls"]["metrics"]["q2Time"] is None
    expected = "pls" if res["pls"]["metrics"]["q2"] >= res["mlr"]["metrics"]["q2"] else "mlr"
    assert res["production"] == expected


def _rec(ts):
    return {"timestamp": ts, "ash_content": 10.0, "raw_ash": 40.0, "coal_amount": 800.0,
            "level": 55.0, "sysA": 1, "sysB": 0, "sys401": 0, "sys402": 1,
            "desliming473": 1, "desliming474": 1, "is_stoppage": 0}


def test_filter_train_rows_ranges():
    """30d/all 范围过滤（跨月时间戳，覆盖 jun_jul 之外的 branch）。"""
    recs = [
        _rec("2026-05-01 10:00:00"), _rec("2026-05-15 10:00:00"),
        _rec("2026-06-16 09:31:00"), _rec("2026-06-20 10:00:00"),
        _rec("2026-07-01 10:00:00"), _rec("2026-07-14 10:00:00"),
        _rec("2026-08-10 10:00:00"), _rec("2026-08-20 10:00:00"),
        {"timestamp": "2026-06-18 10:00:00", "ash_content": 0.0},   # ash=0 过滤
        {"timestamp": "2026-06-19 10:00:00"},                        # 无 ash 过滤
    ]
    jun = [r["timestamp"] for r in T.filter_train_rows(recs, "jun_jul")]
    assert jun == ["2026-06-16 09:31:00", "2026-06-20 10:00:00",
                   "2026-07-01 10:00:00", "2026-07-14 10:00:00"]

    allr = [r["timestamp"] for r in T.filter_train_rows(recs, "all")]
    assert allr == [r["timestamp"] for r in recs[:8]]   # 8 条 ash>0，原序

    d30 = [r["timestamp"] for r in T.filter_train_rows(recs, "30d")]
    assert d30 == ["2026-08-10 10:00:00", "2026-08-20 10:00:00"]   # max=08-20，30 天内 = >=07-21
