# -*- coding: utf-8 -*-
"""两因子(原煤灰分+脱粉)粗灰模型实验 —— 不改生产代码,独立脚本离线训练+评估。

设计:
- 特征: raw_ash + desliming473 + desliming474 (用户假设的主影响因素)
- 对照: 现行10因子 MLR/PLS 生产模型
- 数据: 干净152条全量(6-7月113 + 8-9月39)
- 评估: R²/Q²(LOOCV)/q2Time(walk-forward)/RMSE/MAE/合格率
"""
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import text as sql                    # noqa: E402
from app.database import SessionLocal                 # noqa: E402
from app.services.training import (                   # noqa: E402
    build_coarse_xy, train_mlr, train_pls, _time_cv_q2, _metrics,
    _js_round, MLR_FEATURES,
)

db = SessionLocal()
recs = []
for r in db.execute(sql(
        "SELECT ts, ash_content, coal_amount, level, raw_ash, moisture, sysA, sysB, "
        "sys401, sys402, desliming473, desliming474, is_stoppage, mining_face "
        "FROM coal_records WHERE category='coarse' ORDER BY ts, id")):
    recs.append({"timestamp": r[0], "ash_content": r[1], "coal_amount": r[2], "level": r[3],
                 "raw_ash": r[4], "moisture": r[5], "sysA": r[6], "sysB": r[7],
                 "sys401": r[8], "sys402": r[9], "desliming473": r[10], "desliming474": r[11],
                 "is_stoppage": r[12], "mining_face": r[13]})
db.close()

print(f"全量记录: {len(recs)} 条")

# ---- 数据分布检查 ----
print("\n=== 数据分布 ===")
raw_ashes = [r["raw_ash"] for r in recs if r["raw_ash"] is not None]
print(f"原煤灰分: {min(raw_ashes):.2f} ~ {max(raw_ashes):.2f}, 均值 {sum(raw_ashes)/len(raw_ashes):.2f}")
from collections import Counter
d473 = Counter(r["desliming473"] for r in recs)
d474 = Counter(r["desliming474"] for r in recs)
print(f"脱粉473: 开={d473.get(1,0)} 停={d473.get(0,0)}")
print(f"脱粉474: 开={d474.get(1,0)} 停={d474.get(0,0)}")
# 脱粉组合分布
combos = Counter((r["desliming473"], r["desliming474"]) for r in recs)
print(f"脱粉组合(473,474): {dict(combos)}")

# 两因子的有效组合数(决定模型自由度)
n_combos = len(combos)
n_raw_ash_levels = len(set(round(v, 1) for v in raw_ashes))
print(f"原煤灰分独立水平: {n_raw_ash_levels}")
print(f"脱粉组合数: {n_combos}")

# ---- 构建两因子特征集 ----
FEATS_2 = ["raw_ash", "desliming473", "desliming474"]

def build_xy_2factor(records, range_="all", features=FEATS_2):
    """与 training.build_coarse_xy 相同逻辑但用自定义特征列表。"""
    rows = [d for d in records
            if isinstance(d.get("ash_content"), (int, float))
            and d["ash_content"] > 0
            and not (isinstance(d.get("ash_content"), float) and math.isnan(d["ash_content"]))]
    if range_ == "jun_jul":
        rows = [d for d in rows if str(d.get("timestamp") or "").startswith(("2026-06", "2026-07"))]
    rows.sort(key=lambda r: str(r.get("timestamp") or ""))
    if len(rows) < len(features) + 2:
        return None
    means = []
    for f in features:
        vals = [r[f] for r in rows
                if isinstance(r.get(f), (int, float))
                and not (isinstance(r.get(f), float) and math.isnan(r[f]))]
        means.append(sum(vals) / len(vals) if vals else 0.0)
    X, y = [], []
    for r in rows:
        row = []
        for i, f in enumerate(features):
            v = r.get(f)
            if isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v)):
                row.append(v)
            else:
                row.append(means[i])
        X.append(row)
        y.append(r["ash_content"])
    return {"X": X, "y": y, "means": means, "rows": rows}

# ---- 训练与评估函数 ----
def evaluate(X, y, features, tag, tol=0.8):
    n = len(y)
    k = len(features)
    print(f"\n{'='*60}")
    print(f"[{tag}]  n={n}  特征={features}")
    print(f"{'='*60}")

    # MLR(岭回归)
    mlr = train_mlr(X, y, tol)
    if mlr:
        m = mlr["metrics"]
        print(f"  MLR: R²={m['r2']:.4f}  Q²={m['q2']:.4f}  RMSE={m['rmse']:.3f}  MAE={m['mae']:.3f}  "
              f"合格率={m['passRate']:.1f}%  λ={mlr['lambda']:.4g}")
        if len(mlr.get("coefs", [])) == len(features):
            print(f"       系数: {['%.4f' % c for c in mlr['coefs']]}")

    # PLS
    pls = train_pls(X, y, tol, len(features))
    if pls:
        m = pls["metrics"]
        print(f"  PLS: R²={m['r2']:.4f}  Q²={m['q2']:.4f}  RMSE={m['rmse']:.3f}  MAE={m['mae']:.3f}  "
              f"合格率={m['passRate']:.1f}%  A={pls['A']}")

    # walk-forward 时序CV
    for kind, mdl in (("MLR", mlr), ("PLS", pls)):
        if mdl is None:
            continue
        if kind == "MLR":
            q2t = _time_cv_q2(X, y, "mlr", None, tol)
        else:
            q2t = _time_cv_q2(X, y, "pls", mdl["A"], tol)
        print(f"  {kind} q2Time: {q2t}")

    return mlr, pls

# ---- 1. 两因子模型(全量152条) ----
d_all = build_xy_2factor(recs, "all")
mlr_2f, pls_2f = evaluate(d_all["X"], d_all["y"], FEATS_2, "两因子(原煤灰分+脱粉) 全量152条")

# ---- 2. 两因子模型(仅6-7月113条,与现行生产模型同范围) ----
d_jj = build_xy_2factor(recs, "jun_jul")
mlr_2f_jj, pls_2f_jj = evaluate(d_jj["X"], d_jj["y"], FEATS_2, "两因子(原煤灰分+脱粉) 6-7月113条")

# ---- 3. 对照:现行10因子模型(全量152条) ----
d_10_all = build_coarse_xy(recs, "all")
mlr_10f, pls_10f = evaluate(d_10_all["X"], d_10_all["y"], MLR_FEATURES, "现行10因子 全量152条")

# ---- 4. 对照:现行10因子模型(6-7月113条) ----
d_10_jj = build_coarse_xy(recs, "jun_jul")
mlr_10f_jj, pls_10f_jj = evaluate(d_10_jj["X"], d_10_jj["y"], MLR_FEATURES, "现行10因子 6-7月113条")

# ---- 5. 7-12~7-13 区间的逐点对比(用户观察到的反向区段) ----
print(f"\n{'='*60}")
print("[7-11~7-14 逐点对比] 两因子 vs 现行10因子(MLR)")
print(f"{'='*60}")
from app.services.modeling import predict_coarse_ash  # noqa: E402

model_10f = None
db2 = SessionLocal()
mrow = db2.execute(sql("SELECT method, intercept, coefs, means, impute_means FROM coarse_models WHERE is_current=1")).fetchone()
model_10f = {"intercept": mrow[1], "coefs": json.loads(mrow[2]),
             "means": json.loads(mrow[3]), "imputeMeans": json.loads(mrow[4])}
db2.close()

sel = [r for r in recs if "2026-07-11" <= r["timestamp"] < "2026-07-15"]
print(f"{'时间':<20} {'实测':>6} {'2因子':>6} {'10因子':>6} {'2因子Δ':>7} {'10因子Δ':>7} 反向?")
print("-" * 75)
prev2, prev10, prevA = None, None, None
for r in sel:
    actual = r["ash_content"]
    x2 = []
    for f in FEATS_2:
        v = r.get(f)
        x2.append(v if isinstance(v, (int, float)) else 0)
    pred2 = mlr_2f["intercept"] + sum(mlr_2f["coefs"][j] * x2[j] for j in range(len(FEATS_2)))
    pred10 = predict_coarse_ash(r, model_10f)
    if pred10 is not None:
        pred10 = round(pred10, 2)
    pred2 = round(pred2, 2)
    dA = actual - prevA if prevA is not None else None
    d2 = pred2 - prev2 if prev2 is not None else None
    d10 = (pred10 - prev10) if (prev10 is not None and pred10 is not None) else None
    tag = ""
    if dA is not None and d2 is not None and dA * d2 < 0:
        tag += " 2因子反向"
    if dA is not None and d10 is not None and dA * d10 < 0:
        tag += " 10因子反向"
    print(f"{r['timestamp']:<20} {actual:>6.2f} {pred2:>6.2f} {pred10 or 0:>6.2f} "
          f"{(d2 or 0):>+7.2f} {(d10 or 0):>+7.2f}{tag}")
    prev2, prev10, prevA = pred2, pred10, actual

print("\n⚠ 实验完成,未写入任何生产代码,未提交git。")
