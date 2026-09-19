# -*- coding: utf-8 -*-
"""日级聚合两因子模型实验 —— 按天/班聚合粗灰,用 原煤灰分+脱粉 预测日级均值。

聚合口径:
- 日级: 每天全部采样点的 灰分均值、液位均值、带煤量均值
- 因子: 当日原煤灰分(唯一值或均值)、脱粉473/474(当日占比>50%记1)
- 对照: 同日级聚合下的10因子
"""
import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import text as sql                    # noqa: E402
from app.database import SessionLocal                 # noqa: E402
from app.services.training import train_mlr, train_pls, _time_cv_q2  # noqa: E402

db = SessionLocal()
raw = db.execute(sql(
    "SELECT ts, ash_content, coal_amount, level, raw_ash, moisture, sysA, sysB, "
    "sys401, sys402, desliming473, desliming474, is_stoppage "
    "FROM coal_records WHERE category='coarse' ORDER BY ts, id")).fetchall()
db.close()

# ---- 日级聚合 ----
days = defaultdict(list)
for r in raw:
    day = r[0][:10]  # YYYY-MM-DD
    days[day].append(r)

rows = []
for day in sorted(days.keys()):
    recs = days[day]
    ashes = [r[1] for r in recs if r[1] is not None and r[1] > 0]
    if not ashes:
        continue
    ash_mean = sum(ashes) / len(ashes)
    raw_ashes = [r[4] for r in recs if r[4] is not None]
    raw_mean = sum(raw_ashes) / len(raw_ashes) if raw_ashes else None
    levels = [r[3] for r in recs if r[3] is not None]
    level_mean = sum(levels) / len(levels) if levels else None
    coals = [r[2] for r in recs if r[2] is not None]
    coal_mean = sum(coals) / len(coals) if coals else None
    d473 = 1 if sum(1 for r in recs if r[10] == 1) > len(recs) / 2 else 0
    d474 = 1 if sum(1 for r in recs if r[11] == 1) > len(recs) / 2 else 0
    sysA = 1 if sum(1 for r in recs if r[6] == 1) > len(recs) / 2 else 0
    sysB = 1 if sum(1 for r in recs if r[7] == 1) > len(recs) / 2 else 0
    sys401 = 1 if sum(1 for r in recs if r[8] == 1) > len(recs) / 2 else 0
    sys402 = 1 if sum(1 for r in recs if r[9] == 1) > len(recs) / 2 else 0
    stop = 1 if sum(1 for r in recs if r[12] == 1) > len(recs) / 2 else 0
    rows.append({
        "day": day, "n": len(ashes), "ash_mean": ash_mean,
        "raw_ash": raw_mean, "level": level_mean, "coal_amount": coal_mean,
        "desliming473": d473, "desliming474": d474,
        "sysA": sysA, "sysB": sysB, "sys401": sys401, "sys402": sys402,
        "is_stoppage": stop,
    })

print(f"日级聚合: {len(rows)} 天")
print(f"\n{'日期':<12} {'n':>2} {'日灰分':>6} {'原煤灰':>6} {'液位':>5} {'473':>3} {'474':>3} {'A':>2} {'B':>2}")
print("-" * 55)
for r in rows:
    print(f"{r['day']:<12} {r['n']:>2} {r['ash_mean']:>6.2f} {r['raw_ash'] or 0:>6.2f} "
          f"{r['level'] or 0:>5.1f} {r['desliming473']:>3} {r['desliming474']:>3} {r['sysA']:>2} {r['sysB']:>2}")

# ---- 构建数据集 ----
def build_day_xy(rows, features):
    X, y = [], []
    for r in rows:
        if r["raw_ash"] is None:
            continue
        X.append([r[f] if r[f] is not None else 0 for f in features])
        y.append(r["ash_mean"])
    return X, y

def evaluate(X, y, features, tag, tol=0.8):
    n = len(y)
    if n < len(features) + 2:
        print(f"[{tag}] 样本不足(n={n})")
        return None, None
    print(f"\n{'='*55}")
    print(f"[{tag}]  n={n}天  特征={features}")
    print(f"{'='*55}")

    results = {}
    mlr = train_mlr(X, y, tol)
    if mlr:
        m = mlr["metrics"]
        print(f"  MLR: R²={m['r2']:.4f}  Q²={m['q2']:.4f}  RMSE={m['rmse']:.3f}  MAE={m['mae']:.3f}  "
              f"合格率={m['passRate']:.1f}%  λ={mlr['lambda']:.4g}")
        if len(mlr.get("coefs", [])) == len(features):
            for f, c in zip(features, mlr["coefs"]):
                print(f"    {f:<16} {c:+.4f}")
        q2t = _time_cv_q2(X, y, "mlr", None, tol)
        print(f"  MLR q2Time: {q2t:.4f}" if q2t is not None else "  MLR q2Time: None(样本不足)")
        results["mlr"] = mlr

    pls = train_pls(X, y, tol, len(features))
    if pls:
        m = pls["metrics"]
        print(f"  PLS: R²={m['r2']:.4f}  Q²={m['q2']:.4f}  RMSE={m['rmse']:.3f}  MAE={m['mae']:.3f}  "
              f"合格率={m['passRate']:.1f}%  A={pls['A']}")
        q2t = _time_cv_q2(X, y, "pls", pls["A"], tol)
        print(f"  PLS q2Time: {q2t:.4f}" if q2t is not None else "  PLS q2Time: None(样本不足)")
        results["pls"] = pls
    return results

# ---- 1. 日级两因子(原煤灰分+脱粉) ----
FEATS_2 = ["raw_ash", "desliming473", "desliming474"]
X2, y2 = build_day_xy(rows, FEATS_2)
r2f = evaluate(X2, y2, FEATS_2, "日级两因子(原煤灰分+脱粉)")

# ---- 2. 日级四因子(加液位+带煤量) ----
FEATS_4 = ["raw_ash", "desliming473", "desliming474", "level"]
X4, y4 = build_day_xy(rows, FEATS_4)
r4f = evaluate(X4, y4, FEATS_4, "日级四因子(+液位)")

# ---- 3. 日级全因子(所有10个的日均值) ----
FEATS_ALL = ["raw_ash", "coal_amount", "sysA", "sysB", "sys401", "sys402",
             "desliming473", "desliming474", "is_stoppage", "level"]
XA, yA = build_day_xy(rows, FEATS_ALL)
rAll = evaluate(XA, yA, FEATS_ALL, "日级全因子(10个)")

# ---- 4. 对照:小时级10因子(已有的最佳结果) ----
print(f"\n{'='*55}")
print("[对照:小时级10因子 6-7月113条(现行生产模型)]")
print(f"{'='*55}")
print("  MLR: R²=0.5380  Q²=0.4215  RMSE=2.347  合格率=29.2%")
print("  PLS: R²=0.5295  Q²=0.4337  RMSE=2.368  合格率=32.7%")

print("\n⚠ 实验完成,未写入生产代码,未提交git。")
