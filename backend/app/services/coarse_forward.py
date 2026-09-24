# -*- coding: utf-8 -*-
"""DS 粗精煤泥灰分模型的「真向前」验证（只读：不改模型、不写库、不动训练口径）。

背景（2026-09-24，用户要求「不能仅追求拟合度，还要保证向前预测的能力」）：
  · 拟合度（样本内 R²）与向前精度是两件事。实测：把目标换成「灰分−原煤灰」后样本内 R²
    从 0.374 升到 0.754，而向前 MAE 从 1.814 恶化到 1.939 —— 只看 R² 会选错模型。
  · 所以本模块把"向前"做成系统里的常规数字：**训练只用切点之前的数据，评估只用切点之后的数据**，
    并与三条朴素基线同台比较（取训练均值 / 在线近 k 条均值 / 在线指数平滑）。

两个窗口口径：
  · leftover（主口径，就是现场用法）：按 `range` 训练后，把**训练范围之后**的数据当检验集。
    现场是"隔两三周导一次 Excel → 重训 → 往后用"，这正是该口径。
  · tail（回退口径）：当 `range` 已覆盖全部数据（例如 all）时没有剩余数据可检验 —— 这时
    本身就是一个结论（"选了 all 就永远测不出向前能力"）。此时退化为"留尾"：最后一段数据
    当检验集、之前的数据当训练集，并在返回里明确标注 kind=tail，不与部署模型混为一谈。

方向信号见 coarse_direction.direction_report（特征仅用 t 时刻已知量，含惯性基线对照）。
"""
from __future__ import annotations

import math

from .coarse_direction import direction_report
from .modeling import MLR_FEATURES
from .training import _time_cv_q2, build_coarse_xy, filter_train_rows, train_mlr, train_pls

# 基线：在线近 k 条读数的均值 + 指数平滑系数（现场"看最近几次化验"的自然做法）
ONLINE_KS = (1, 5, 20)
EWMA_ALPHAS = (0.2,)
# 4 因子备选（去掉 6 个接近常量的开关列）。2026-09-24 实测：锁定窗向前 MAE 1.521 vs 10 因子 1.814，
# 但用户当时决定"模型先不动"，故这里只作为对照存档，不参与生产模型。
FEATURE_SETS = {
    "10因子(现行)": list(MLR_FEATURES),
    "4因子(原煤灰/煤量/液位/水分)": ["raw_ash", "coal_amount", "level", "moisture"],
}
TAIL_MIN_ROWS = 10


def _mae(pred, y):
    return sum(abs(a - b) for a, b in zip(pred, y)) / len(y)


def _bias(pred, y):
    return sum(a - b for a, b in zip(pred, y)) / len(y)


def _r2(pred, y):
    m = sum(y) / len(y)
    ss_res = sum((a - b) ** 2 for a, b in zip(pred, y))
    ss_tot = sum((b - m) ** 2 for b in y)
    return 1 - ss_res / ss_tot if ss_tot else 0.0


def _round(v, nd=3):
    return None if v is None else round(v + 0.0, nd)


def _ts(rec):
    """时间戳：库行是 `ts`、store 行是 `timestamp`（两条路都要能用，2026-09-24 踩过 KeyError）。"""
    return str(rec.get("ts") or rec.get("timestamp") or "")


def _seq(records):
    return sorted(records, key=_ts)


def fit_predict(train, target, feats=None, kind="mlr"):
    """在 train 上拟合（与生产同一套求解器），返回对 target 的预测与样本内指标。"""
    feats = list(feats or MLR_FEATURES)
    d = build_coarse_xy([dict(r) for r in train], "all", feats)
    if d is None or len(d["y"]) < len(feats) + 2:
        return None
    m = train_mlr(d["X"], d["y"]) if kind == "mlr" else train_pls(d["X"], d["y"], amax=len(feats))
    if m is None:
        return None
    means, coefs = d["means"], m.get("coefs") or []
    preds = []
    for r in target:
        s = m.get("intercept", 0.0)
        for j, f in enumerate(feats):
            v = r.get(f)
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
                v = means[j]
            if j < len(coefs):
                s += coefs[j] * v
        preds.append(s)
    q2t = _time_cv_q2(d["X"], d["y"], kind, len(feats) if kind == "pls" else None)
    return {"preds": preds, "inSampleR2": m["metrics"]["r2"], "q2": m["metrics"]["q2"],
            "q2Time": q2t, "lambda": m.get("lambda"), "A": m.get("A"), "n": len(d["y"])}


def baselines(seq, test_idx, const):
    """朴素基线：全是**在线可得**的量（只用第 t 条之前的数据）。"""
    out = {}
    for k in ONLINE_KS:
        out["在线·近%d条均值" % k] = [
            (sum(seq[j]["ash_content"] for j in range(max(0, i - k), i)) / len(range(max(0, i - k), i))
             if i > 0 else const) for i in test_idx]
    for a in EWMA_ALPHAS:
        s, series = seq[0]["ash_content"], {}
        for i, r in enumerate(seq):
            if i in set(test_idx):
                series[i] = s
            s = a * r["ash_content"] + (1 - a) * s
        out["在线·EWMA%.1f" % a] = [series[i] for i in test_idx]
    return out


def evaluate_window(records, train_end, test_end=None, feature_sets=None, with_direction=True, kinds=("mlr", "pls")):
    """训练 < train_end ≤ 评估 < test_end（test_end=None 表示到数据末尾）。"""
    seq = _seq(records)
    idx = {id(r): i for i, r in enumerate(seq)}
    train = [r for r in seq if _ts(r) < train_end]
    test = [r for r in seq if _ts(r) >= train_end and (test_end is None or _ts(r) < test_end)]
    if len(train) < 20 or len(test) < 5:
        return {"usable": False, "note": "样本不足（训练 %d 条 / 评估 %d 条）" % (len(train), len(test)),
                "trainN": len(train), "testN": len(test)}
    y = [r["ash_content"] for r in test]
    tpos = [idx[id(r)] for r in test]
    const = sum(r["ash_content"] for r in train) / len(train)
    out = {"usable": True, "trainN": len(train), "testN": len(test),
           "trainEnd": _ts(train[-1]), "testFrom": _ts(test[0]), "testTo": _ts(test[-1]),
           "trainMean": _round(const), "testMean": _round(sum(y) / len(y)),
           "models": {}, "baselines": {}, "direction": None}
    out["baselines"]["取训练均值"] = {"mae": _round(_mae([const] * len(y), y)),
                                      "bias": _round(_bias([const] * len(y), y)),
                                      "r2": _round(_r2([const] * len(y), y))}
    for name, p in baselines(seq, tpos, const).items():
        out["baselines"][name] = {"mae": _round(_mae(p, y), 3), "bias": _round(_bias(p, y), 3),
                                  "r2": _round(_r2(p, y), 3)}
    for fname, feats in (feature_sets or {"%d因子(现行)" % len(MLR_FEATURES): list(MLR_FEATURES)}).items():
        for kind in kinds:
            r = fit_predict(train, test, feats, kind)
            if r is None:
                continue
            p = r["preds"]
            out["models"]["%s|%s" % (fname, kind.upper())] = {
                "mae": _round(_mae(p, y)), "bias": _round(_bias(p, y)), "r2": _round(_r2(p, y)),
                "inSampleR2": _round(r["inSampleR2"]), "q2": _round(r["q2"]),
                "q2Time": _round(r["q2Time"]), "lambda": _round(r.get("lambda")),
                "A": r.get("A"), "fitN": r["n"]}
    if with_direction:
        d = direction_report(seq, split_ts=train_end)
        out["direction"] = {k: d.get(k) for k in ("usable", "hit", "baseline", "inertia", "ci", "n", "note")}
    # 结论：现行生产配置（10 因子、MLR/PLS 中向前更好的那个）与最强朴素基线比
    prod = [v for k, v in out["models"].items() if k.startswith("%d因子(现行)" % len(MLR_FEATURES))]
    best_base = min(out["baselines"], key=lambda k: out["baselines"][k]["mae"])
    if prod:
        best_model = min(prod, key=lambda v: v["mae"])
        out["verdict"] = {
            "model": best_model, "bestBaseline": best_base,
            "modelMae": best_model["mae"], "baselineMae": out["baselines"][best_base]["mae"],
            "aheadOfBaseline": best_model["mae"] < out["baselines"][best_base]["mae"],
            "text": ("模型向前 MAE %.3f，%s MAE %.3f —— 模型%s"
                     % (best_model["mae"], best_base, out["baselines"][best_base]["mae"],
                        "更好" if best_model["mae"] < out["baselines"][best_base]["mae"]
                        else "不如该基线（拟合度不代表向前能力）")),
        }
    return out


def forward_windows(records, range_="jun_jul"):
    """返回 [(kind, label, train_end, test_end)]：主口径 leftover（若有剩余数据）+ 回退口径 tail。"""
    seq = _seq(records)
    rows = filter_train_rows(seq, range_)
    wins = []
    if rows:
        last = max(_ts(r) for r in rows)
        rest = [r for r in seq if _ts(r) > last]
        if len(rest) >= 5:
            wins.append(("leftover", "训练范围之后（%s 起，%d 条）" % (_ts(rest[0])[:10], len(rest)),
                         _ts(rest[0]), None))
    if not wins and len(seq) >= 30:
        hold = max(TAIL_MIN_ROWS, int(len(seq) * 0.2))
        cut = _ts(seq[-hold])
        wins.append(("tail", "留尾检验（最后 %d 条，%s 起）" % (hold, cut[:10]), cut, None))
    return wins


def forward_report(records, range_="jun_jul", feature_sets=None, kinds=("mlr", "pls")):
    """页面/接口用的完整报告：主口径 + 回退口径。只读。"""
    wins = forward_windows(records, range_)
    rep = {"ok": True, "range": range_, "n": len(records), "windows": [], "note": ""}
    for kind, label, cut, end in wins:
        w = evaluate_window(records, cut, end, feature_sets=feature_sets, kinds=kinds)
        w["kind"], w["label"] = kind, label
        rep["windows"].append(w)
    if not rep["windows"]:
        rep["note"] = "没有可用的向前窗口（数据不足）"
    elif all(w.get("kind") == "tail" for w in rep["windows"]):
        rep["note"] = ("当前训练范围 %s 已覆盖全部数据 → 没有剩余数据可做真向前检验，"
                       "下面给的是留尾参考。想测真向前能力，训练范围应留出最新一段数据"
                       "（如选 jun_jul 或 30 天）。" % range_)
    return rep


# ---------------- 进程内缓存（页面懒加载用；PLS 折内选 A 约 6 秒，不能每次点都算） ----------------
_CACHE: dict = {}
_CACHE_MAX = 8


def _signature(records, range_, feature_sets, kinds):
    seq = _seq(records)
    return (len(seq), _ts(seq[0]) if seq else "", _ts(seq[-1]) if seq else "",
            range_, tuple(sorted((feature_sets or {}).keys())), tuple(kinds))


def report_cached(records, range_="jun_jul", feature_sets=None, kinds=("mlr", "pls")):
    """带缓存的 forward_report：数据/范围/特征集不变时直接回放上次结果（含耗时与命中缓存标记）。"""
    import time

    key = _signature(records, range_, feature_sets, kinds)
    hit = _CACHE.get(key)
    if hit is not None:
        return {**hit, "cached": True}
    t0 = time.time()
    rep = forward_report(records, range_, feature_sets, kinds)
    rep["cached"] = False
    rep["elapsedMs"] = int((time.time() - t0) * 1000)
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = rep
    return rep


def _cli():
    """自检：python -m app.services.coarse_forward --db backend/data/dense_medium.db --range all"""
    import argparse
    import json
    import sqlite3

    ap = argparse.ArgumentParser(description="DS 粗灰真向前验证（只读）")
    ap.add_argument("--db", required=True)
    ap.add_argument("--range", default="all")
    ap.add_argument("--sets", action="store_true", help="附特征集对照（含 4 因子备选）")
    a = ap.parse_args()
    con = sqlite3.connect(a.db)
    cols = [c[1] for c in con.execute("pragma table_info(coal_records)")]
    rows = [dict(zip(cols, r)) for r in con.execute(
        "select * from coal_records where category='coarse' and ash_content is not null order by ts, id")]
    con.close()
    rep = forward_report(rows, a.range, FEATURE_SETS if a.sets else None)
    print(json.dumps(rep, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    _cli()
