# -*- coding: utf-8 -*-
"""DS 专用：粗精煤泥灰分「下一读数方向」判断（前向可用信号），水平值精度不承诺。

为什么只做方向（2026-09-23 三轮实验结论）：
  · 水平值：所有候选（10 因子 MLR/PLS、加滞后、增量模型）在锁死检验集上都打不过
    「取开发集均值」基线（最好 MAE 2.63 vs 基线 2.26）—— 因为灰分中枢跨期漂移
    （6-7 月约 14.0、8-9 月约 14.9），水平预测无前向价值。
  · 方向：用「第 t 条**已知**信息」预测「第 t+1 条相对第 t 条的涨跌」，在三个不同切分点上
    稳定命中 0.727 / 0.771 / 0.771（多数基线 0.514~0.523），主切分 bootstrap 95% 区间
    [0.629, 0.914]。机理是**均值回复**（当前偏高→下次多半回落），旁证：惯性基线仅 0.343。

两条必须守住的纪律（都是踩过坑换来的）：
  1. **特征只能用 t 时刻已知量**：曾经把「第 t 条自身的灰分同源测量」与「第 t 条自身的变化」配对，
     得到 0.92 的假命中率 —— 那是同时刻解释，含未来信息，生产上第 t 条还不存在。
  2. **不加入「到下次读数的时间间隔」**：该特征可能让模型"靠采样节奏猜方向"；
     实测去掉它命中率不变（0.727/0.771/0.771），故正式版不采用。

用法（供 train_ds 编排与页面调用）：
    from .coarse_direction import direction_report
    rep = direction_report(records, dev_months=["2026-06", "2026-07"], test_months=["2026-08", "2026-09"])
    # rep = {"usable": bool, "hit": float, "baseline": float, "ci": [lo, hi], "n": int, "note": str}
命令行自检（不改任何数据，只读库）：
    python -m app.services.coarse_direction --db backend/data/dense_medium.db
"""
from __future__ import annotations

import math
from datetime import datetime

FEATS = ["raw_ash", "coal_amount", "sysA", "sysB", "sys401", "sys402",
         "desliming473", "desliming474", "is_stoppage", "level"]
DELTA_TH = 0.5          # |Δ| ≤ 0.5% 视为"平"，不计入方向命中
RIDGE_ALPHA = 1.0
BOOTSTRAP = 2000
SEED = 20260923


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _ts(rec):
    """取时间戳：库行是 `ts`，前端 store 行是 `timestamp`（2026-09-24 修：
    训练编排传进来的是 store 行，只读 `ts` 会让方向报告永远返回"样本不足"）。"""
    return rec.get("ts") or rec.get("timestamp")


def _parse_ts(s):
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(s).strip(), f)
        except Exception:
            continue
    return None


def _design(seq):
    """X = 第 t 条**已知**信息（10 因子 + Δprev + prev 水平）；标签 = 第 t+1 条相对第 t 条的变化。

    → (X, d_next, ts_cur)：ts_cur 是每行对应的"第 t 条"时间戳，供页面显示"最近几次判断对不对"
    （用户 Q6 选 B：方向先只展示、观察一段时间再决定是否接入操作建议）。
    """
    X, d_next, ts_cur = [], [], []
    for i in range(2, len(seq) - 1):
        cur, p1 = seq[i], seq[i - 1]
        if not (_num(cur.get("ash_content")) and _num(p1.get("ash_content"))
                and _num(seq[i + 1].get("ash_content"))):
            continue
        row = [float(cur[f]) if _num(cur.get(f)) else 0.0 for f in FEATS]
        row.append(float(cur["ash_content"]) - float(p1["ash_content"]))   # 刚发生的变化（已知）
        row.append(float(p1["ash_content"]))                               # 上一条水平（已知）
        X.append(row)
        d_next.append(float(seq[i + 1]["ash_content"]) - float(cur["ash_content"]))
        ts_cur.append(str(_ts(cur)))
    return X, d_next, ts_cur


def _ridge_fit(X, y, alpha=RIDGE_ALPHA):
    """标准化 + 岭回归（闭式解）：返回 (means, stds, coefs, intercept)。"""
    n, k = len(X), len(X[0])
    means = [sum(r[j] for r in X) / n for j in range(k)]
    stds = []
    for j in range(k):
        v = sum((r[j] - means[j]) ** 2 for r in X) / n
        stds.append(math.sqrt(v) if v > 1e-12 else 1.0)
    Z = [[(r[j] - means[j]) / stds[j] for j in range(k)] for r in X]
    # (ZᵀZ + αI)β = Zᵀy，用高斯消元（不引第三方依赖）
    A = [[sum(Z[i][p] * Z[i][q] for i in range(n)) + (alpha if p == q else 0.0) for q in range(k)]
         for p in range(k)]
    b = [sum(Z[i][p] * y[i] for i in range(n)) for p in range(k)]
    for c in range(k):
        piv = max(range(c, k), key=lambda r: abs(A[r][c]))
        if abs(A[piv][c]) < 1e-12:
            continue
        A[c], A[piv] = A[piv], A[c]
        b[c], b[piv] = b[piv], b[c]
        for r in range(k):
            if r == c:
                continue
            f = A[r][c] / A[c][c]
            for q in range(c, k):
                A[r][q] -= f * A[c][q]
            b[r] -= f * b[c]
    coefs = [b[j] / A[j][j] if abs(A[j][j]) > 1e-12 else 0.0 for j in range(k)]
    intercept = sum(y) / n
    return means, stds, coefs, intercept


def _predict(model, X):
    means, stds, coefs, ic = model
    out = []
    for r in X:
        s = ic
        for j, c in enumerate(coefs):
            s += c * (r[j] - means[j]) / stds[j]
        out.append(s)
    return out


def direction_report(records, dev_months=None, test_months=None, bootstrap=BOOTSTRAP, split_ts=None):
    """按时间切分做"真向前"方向验收。records 需含 ts 与 10 个特征 + ash_content。

    两种切分口径：
      · split_ts="2026-09-04"：**按时间点切**，ts < split_ts 为开发、ts ≥ split_ts 为检验（优先）。
        为什么要它：按月份切在"开发集已覆盖最新月份"时会把检验集切成空集 —— 2026-09-24 导入
        9-04~9-21 新数据后，开发集是 6-16~9-03（含 2026-09），检验月份就成了 []，报告只能返回
        "样本不足"。数据是连续导入的，真正的向前窗口是"最后一个训练样本之后"，而不是"下一个月"。
      · dev_months / test_months：按月切（保留原口径，供三切分对照使用）。
    """
    seq = sorted([r for r in records if _parse_ts(_ts(r)) is not None],
                 key=lambda r: str(_ts(r)))
    if split_ts:
        dev = [r for r in seq if str(_ts(r)) < split_ts]
        test = [r for r in seq if str(_ts(r)) >= split_ts]
    else:
        dev = [r for r in seq if str(_ts(r))[:7] in (dev_months or [])]
        test = [r for r in seq if str(_ts(r))[:7] in (test_months or [])]
    Xtr, dtr, _ts_tr = _design(dev)
    Xte, dte, ts_te = _design(test)
    mtr = [i for i, d in enumerate(dtr) if abs(d) > DELTA_TH]
    mte = [i for i, d in enumerate(dte) if abs(d) > DELTA_TH]
    if len(mtr) < 8 or len(mte) < 5:
        return {"usable": False, "hit": None, "baseline": None, "ci": None,
                "n": len(mte), "note": "样本不足（开发≥8、检验≥5 才给结论）",
                "gating": "特征仅用 t 时刻已知量；不含到下次读数的时间间隔", "recent": []}
    model = _ridge_fit([Xtr[i] for i in mtr], [dtr[i] for i in mtr])
    pred = [1 if v > 0 else 0 for v in _predict(model, [Xte[i] for i in mte])]
    y = [1 if dte[i] > 0 else 0 for i in mte]
    n = len(y)
    hit = sum(1 for a, b in zip(pred, y) if a == b) / n
    baseline = max(sum(y), n - sum(y)) / n
    # 惯性基线：直接假设"下一次的变化方向与刚发生的变化同号"（均值回复时它应当很差）
    # X[i][len(FEATS)] 就是第 t 条相对上一条的已知变化量（见 _design）。
    inertia = sum(1 for i in mte
                  if (dte[i] > 0) == (Xte[i][len(FEATS)] > 0)) / n
    # bootstrap 95% 区间（自实现线性同余，避免引入 numpy 依赖）
    state = SEED
    boots = []
    for _ in range(bootstrap):
        acc = 0
        for _ in range(n):
            state = (1103515245 * state + 12345) % (1 << 31)
            i = state % n
            acc += 1 if pred[i] == y[i] else 0
        boots.append(acc / n)
    boots.sort()
    lo, hi = boots[int(n * 0) + int(len(boots) * 0.025)], boots[int(len(boots) * 0.975)]
    usable = lo > max(baseline, inertia)
    # 最近若干次"判断 vs 实际"（供页面观察；用户 Q6 选 B：先只展示、不接入操作建议）
    recent = [{"t": ts_te[i], "pred": "涨" if pred[k] else "跌",
               "actual": "涨" if y[k] else "跌", "ok": pred[k] == y[k]}
              for k, i in enumerate(mte)][-20:]
    return {"usable": usable, "hit": round(hit, 3), "baseline": round(baseline, 3),
            "inertia": round(inertia, 3), "ci": [round(lo, 3), round(hi, 3)], "n": n,
            "note": ("方向可用：区间下界高于多数基线与惯性基线" if usable
                     else "方向命中未能显著超过基线，不作可用结论"),
            "gating": "特征仅用 t 时刻已知量；不含到下次读数的时间间隔",
            "recent": recent,
            "recentHit": (round(sum(1 for r in recent if r["ok"]) / len(recent), 3) if recent else None)}


def _cli():
    import argparse
    import json
    import sqlite3

    ap = argparse.ArgumentParser(description="DS 粗灰方向判断自检（只读）")
    ap.add_argument("--db", required=True)
    ap.add_argument("--dev", default="2026-06,2026-07")
    ap.add_argument("--test", default="2026-08,2026-09")
    a = ap.parse_args()
    con = sqlite3.connect(a.db)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "select * from coal_records where category='coarse' and ash_content is not null order by ts")]
    con.close()
    rep = direction_report(rows, a.dev.split(","), a.test.split(","))
    print(json.dumps(rep, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    _cli()
