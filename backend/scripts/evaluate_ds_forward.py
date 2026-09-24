# -*- coding: utf-8 -*-
"""DS 粗精煤泥灰分模型「真向前」评估（只读生产库，不训练上线模型、不写库、不改数据）。

为什么需要它（2026-09-24）：用户要求「不能仅追求拟合度，还要保证向前预测的能力」。
本脚本把"向前"变成可复跑的数字：**训练只用切点之前的数据，评估只用切点之后的数据**，
并把模型与三条朴素基线（训练均值 / 在线近 k 条均值 / 在线指数平滑）放在一起比。

口径与纪律：
  · 默认两个窗口：开发侧（训练 →8-22，评估 8-23~9-03）与锁定窗（训练 →9-03，评估 9-04~9-21）。
    锁定窗的数据是 2026-09-24 才导入的，开发期从未使用 → 属于真·时间外推。
  · 特征子集/目标变换等一切选择，只允许看开发侧窗口；锁定窗只看一次（`--select-only` 可只看开发侧）。
  · 方向信号用 app.services.coarse_direction（特征仅 t 时刻已知量，含惯性基线对照）。

用法：
  python backend/scripts/evaluate_ds_forward.py                 # 打印报告
  python backend/scripts/evaluate_ds_forward.py --json out.json # 另存 JSON（供 Excel/页面对照）
  python backend/scripts/evaluate_ds_forward.py --select-only   # 只跑开发侧窗口（选型时用）
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.coarse_direction import direction_report                      # noqa: E402
from app.services.modeling import MLR_FEATURES                                  # noqa: E402
from app.services.training import _time_cv_q2, build_coarse_xy, train_mlr, train_pls  # noqa: E402

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "dense_medium.db"
# 默认窗口：(名字, 训练截止=评估起点, 评估终点或 None=到数据末尾)
DEFAULT_WINDOWS = [("开发侧", "2026-08-23", "2026-09-04"), ("锁定窗", "2026-09-04", None)]
# 现行 DS 特征（10 因子）与候选精简集（去掉 6 个接近常量的开关列）
FEATURE_SETS = {
    "10因子(现行)": list(MLR_FEATURES),
    "9因子(去sysA)": [f for f in MLR_FEATURES if f != "sysA"],
    "6因子(去sysA/sysB/sys401/473/474)": [f for f in MLR_FEATURES
                                          if f not in ("sysA", "sysB", "sys401", "desliming473", "desliming474")],
    "5因子(仅留is_stoppage)": ["raw_ash", "coal_amount", "is_stoppage", "level", "moisture"],
    "4因子(全去开关)": ["raw_ash", "coal_amount", "level", "moisture"],
}


def load(db_path):
    con = sqlite3.connect(str(db_path))
    cols = [c[1] for c in con.execute("pragma table_info(coal_records)")]
    rows = [dict(zip(cols, r)) for r in con.execute(
        "select * from coal_records where category='coarse' and ash_content is not null order by ts, id")]
    con.close()
    for r in rows:
        r["timestamp"] = r["ts"]
    return rows


def _mae(p, y):
    return sum(abs(a - b) for a, b in zip(p, y)) / len(y)


def _bias(p, y):
    return sum(a - b for a, b in zip(p, y)) / len(y)


def _r2(p, y):
    m = sum(y) / len(y)
    ss_res = sum((a - b) ** 2 for a, b in zip(p, y))
    ss_tot = sum((b - m) ** 2 for b in y)
    return 1 - ss_res / ss_tot if ss_tot else float("nan")


def _fit_predict(train, target, feats, kind):
    """在 train 上拟合（可选目标变换），返回对 target 的预测列表。"""
    d = build_coarse_xy([dict(r) for r in train], "all", feats)
    if d is None:
        return None
    m = train_mlr(d["X"], d["y"]) if kind == "mlr" else train_pls(d["X"], d["y"], amax=len(feats))
    if m is None:
        return None
    means, coefs = d["means"], m.get("coefs") or []
    out = []
    for r in target:
        s = m.get("intercept", 0.0)
        for j, f in enumerate(feats):
            v = r.get(f)
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
                v = means[j]
            if j < len(coefs):
                s += coefs[j] * v
        out.append(s)
    return {"preds": out, "r2": m["metrics"]["r2"], "q2": m["metrics"]["q2"],
            "q2Time": _time_cv_q2(d["X"], d["y"], kind, len(feats) if kind == "pls" else None),
            "lambda": m.get("lambda"), "A": m.get("A")}


def _online_baselines(seq, test_pos, n_test):
    """只用 t 之前读数（在线可得）的基线。seq 为全序列时间序，test_pos 为评估样本下标。"""
    out = {}
    for k in (1, 3, 5, 10, 20):
        out["在线·近%d条均值" % k] = [
            (lambda past: sum(past) / len(past) if past else None)(
                [seq[j]["ash_content"] for j in range(max(0, i - k), i)]) for i in test_pos]
    for a in (0.2, 0.3):
        s = seq[0]["ash_content"]
        series = {}
        for i, r in enumerate(seq):
            if i in set(test_pos):
                series[i] = s
            s = a * r["ash_content"] + (1 - a) * s
        out["在线·EWMA%.1f" % a] = [series[i] for i in test_pos]
    return out


def evaluate(records, train_end, test_end, bootstrap_dir=None):
    seq = records
    pos = {id(r): i for i, r in enumerate(seq)}
    train = [r for r in seq if r["ts"] < train_end]
    test = [r for r in seq if r["ts"] >= train_end and (test_end is None or r["ts"] < test_end)]
    if len(train) < 20 or len(test) < 5:
        return {"skipped": True, "note": "训练 %d 条 / 评估 %d 条，样本不足" % (len(train), len(test))}
    tpos = [pos[id(r)] for r in test]
    y = [r["ash_content"] for r in test]
    const = sum(r["ash_content"] for r in train) / len(train)
    res = {"trainN": len(train), "testN": len(test), "trainEnd": train[-1]["ts"], "testFrom": test[0]["ts"],
           "testTo": test[-1]["ts"], "testMean": round(sum(y) / len(y), 3),
           "trainMean": round(const, 3), "models": {}, "baselines": {}, "direction": None}
    res["baselines"]["训练均值"] = {"mae": round(_mae([const] * len(y), y), 3),
                                    "bias": round(_bias([const] * len(y), y), 3),
                                    "r2": round(_r2([const] * len(y), y), 3)}
    for name, p in _online_baselines(seq, tpos, len(test)).items():
        p = [x if x is not None else const for x in p]
        res["baselines"][name] = {"mae": round(_mae(p, y), 3), "bias": round(_bias(p, y), 3),
                                  "r2": round(_r2(p, y), 3)}
    for fname, feats in FEATURE_SETS.items():
        for kind in ("mlr", "pls"):
            r = _fit_predict(train, test, feats, kind)
            if r is None:
                continue
            p = r["preds"]
            res["models"]["%s|%s" % (fname, kind.upper())] = {
                "mae": round(_mae(p, y), 3), "bias": round(_bias(p, y), 3), "r2": round(_r2(p, y), 3),
                "inSampleR2": round(r["r2"], 3), "q2": round(r["q2"], 3),
                "q2Time": None if r["q2Time"] is None else round(r["q2Time"], 3),
                "lambda": None if r.get("lambda") is None else round(r["lambda"], 3), "A": r.get("A")}
    d = direction_report(records, split_ts=train_end)
    res["direction"] = {k: d[k] for k in ("usable", "hit", "baseline", "inertia", "ci", "n", "note")}
    return res


def print_window(name, res):
    if res.get("skipped"):
        print("  [%s] %s" % (name, res["note"]))
        return
    print("\n=== %s：训练 %d 条（→%s） → 评估 %d 条（%s ~ %s）==="
          % (name, res["trainN"], res["trainEnd"][:10], res["testN"], res["testFrom"][:10], res["testTo"][:10]))
    print("    训练均值 %.3f / 评估期真值均值 %.3f（中枢漂移 %+.3f）"
          % (res["trainMean"], res["testMean"], res["testMean"] - res["trainMean"]))
    print("  -- 朴素基线（向前）--")
    for k, v in res["baselines"].items():
        print("     %-20s MAE %.3f  R2 %+.3f  偏差 %+.3f" % (k, v["mae"], v["r2"], v["bias"]))
    print("  -- 模型（样本内 R² 与向前 MAE 并列，看两者是否脱节）--")
    for k, v in sorted(res["models"].items(), key=lambda kv: kv[1]["mae"]):
        print("     %-38s MAE %.3f  偏差 %+.3f | 样本内R2 %.3f q2 %.3f q2Time %s"
              % (k, v["mae"], v["bias"], v["inSampleR2"], v["q2"], v["q2Time"]))
    d = res["direction"]
    print("  -- 方向（第 t+1 条相对第 t 条涨跌，特征仅用 t 时刻已知量）--")
    print("     命中 %s  多数基线 %s  惯性基线 %s  95%%区间 %s  n=%s  可用=%s"
          % (d["hit"], d["baseline"], d["inertia"], d["ci"], d["n"], d["usable"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--select-only", action="store_true", help="只跑开发侧窗口（选型纪律）")
    ap.add_argument("--json", type=Path, default=None)
    a = ap.parse_args()
    records = load(a.db)
    print("粗精煤泥样本 %d 条（%s ~ %s）" % (len(records), records[0]["ts"], records[-1]["ts"]))
    windows = DEFAULT_WINDOWS[:1] if a.select_only else DEFAULT_WINDOWS
    out = {"db": str(a.db), "n": len(records), "windows": {}}
    for name, cut, end in windows:
        res = evaluate(records, cut, end)
        out["windows"][name] = {"trainEnd": cut, "testEnd": end, **res}
        print_window(name, res)
    if a.json:
        a.json.write_text(json.dumps(out, ensure_ascii=False, indent=1, allow_nan=False) + "\n", encoding="utf-8")
        print("\nJSON 已写出：%s" % a.json)
    print("\n提示：拟合度（样本内 R²）与向前 MAE 是两件事；选型只能看开发侧窗口。")


if __name__ == "__main__":
    main()
