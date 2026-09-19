# -*- coding: utf-8 -*-
"""诊断:日级模型预测与实际不符的来源分析 —— 工艺操作(人为) vs 数据量不足。

方法:
1. 逐日残差分析:找出预测偏差最大的日期,查看当日工况特征
2. 残差 vs 特征变化相关性:偏差是否与特定因子突变相关(→人为操作)
3. 残差自相关:昨日偏差是否能预测今日偏差(→系统性/工艺惯性)
4. 数据量敏感性:用前N天训练预测后1天(leave-one-out按时间),看残差是否随数据增多而缩小
"""
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import text as sql              # noqa: E402
from app.database import SessionLocal           # noqa: E402
from app.services.training import train_mlr, _col_means_std, _standardize  # noqa: E402

db = SessionLocal()
raw = db.execute(sql(
    "SELECT ts, ash_content, coal_amount, level, raw_ash, moisture, sysA, sysB, "
    "sys401, sys402, desliming473, desliming474, is_stoppage, mining_face "
    "FROM coal_records WHERE category='coarse' ORDER BY ts, id")).fetchall()
db.close()

# ---- 日级聚合(与前端 _dailyAggregate 相同) ----
from collections import defaultdict, Counter
days_map = defaultdict(list)
for r in raw:
    days_map[r[0][:10]].append(r)

FEATS = ['raw_ash', 'coal_amount', 'sysA', 'sysB', 'sys401', 'sys402',
         'desliming473', 'desliming474', 'is_stoppage', 'level']

days = []
for day in sorted(days_map.keys()):
    recs = days_map[day]
    valid = [r for r in recs if r[1] is not None and r[1] > 0]
    if not valid:
        continue
    mean = lambda arr: (lambda v: sum(v)/len(v) if v else None)(
        [x for x in arr if x is not None and isinstance(x, (int, float))])
    maj = lambda fn: 1 if len([r for r in recs if fn(r)]) > len(recs)/2 else 0
    d = {"day": day, "n": len(valid), "ash_content": mean([r[1] for r in valid])}
    for i, f in enumerate(FEATS):
        col_idx = 3 + i  # raw_ash=idx4 in SQL, but our FEATS align to idx 4..13
        if f in ("raw_ash",):
            d[f] = mean([r[4] for r in recs])
        elif f == "coal_amount":
            d[f] = mean([r[2] for r in recs])
        elif f == "level":
            d[f] = mean([r[3] for r in recs])
        elif f == "moisture":
            d[f] = mean([r[5] for r in recs])
        else:
            # binary features by majority
            idx_map = {"sysA": 6, "sysB": 7, "sys401": 8, "sys402": 9,
                       "desliming473": 10, "desliming474": 11, "is_stoppage": 12}
            if f in idx_map:
                d[f] = maj(lambda r, i=idx_map[f]: r[i] == 1)
    d["mining_face"] = (recs[0][13] or "") if recs else ""
    days.append(d)

print(f"日级数据: {len(days)} 天")

# ---- 1. 全量训练,逐日残差 ----
X = [[d.get(f) if d.get(f) is not None else 0 for f in FEATS] for d in days]
y = [d["ash_content"] for d in days]
mlr = train_mlr(X, y, 0.8)
intercept, coefs = mlr["intercept"], mlr["coefs"]

print("\n" + "=" * 80)
print("1. 逐日残差分析(全量训练,标注偏差最大的日期)")
print("=" * 80)
residuals = []
for i, d in enumerate(days):
    pred = intercept + sum(coefs[j] * X[i][j] for j in range(len(FEATS)))
    resid = y[i] - pred
    residuals.append({"day": d["day"], "actual": y[i], "pred": round(pred, 2),
                      "resid": round(resid, 2), "abs_r": abs(resid), "idx": i, **{f: d.get(f) for f in FEATS},
                      "face": d.get("mining_face", "")})

# 按绝对残差排序
by_abs = sorted(residuals, key=lambda r: -r["abs_r"])
print(f"\n{'日期':<12} {'实测':>6} {'预测':>6} {'残差':>6} {'液位':>5} {'原煤灰':>6} {'473':>3} {'474':>3} {'A':>2} {'B':>2} {'工作面':<8} {'标记'}")
print("-" * 85)
for r in by_abs[:15]:
    # 检查与前一天的特征突变
    idx = r["idx"]
    tag = ""
    if idx > 0:
        prev = days[idx - 1]
        changes = []
        for f in ["sysA", "sysB", "sys401", "sys402", "desliming473", "desliming474"]:
            if r.get(f) != prev.get(f):
                changes.append(f"{f}={'开' if r.get(f) else '关'}")
        if prev.get("raw_ash") and r.get("raw_ash"):
            dra = r["raw_ash"] - prev["raw_ash"]
            if abs(dra) > 2:
                changes.append(f"原煤灰分{'+' if dra > 0 else ''}{dra:.1f}")
        if r.get("mining_face") and prev.get("mining_face") and r["mining_face"] != prev["mining_face"]:
            changes.append("换工作面")
        if changes:
            tag = "⚠ " + ", ".join(changes)
    print(f"{r['day']:<12} {r['actual']:>6.2f} {r['pred']:>6.2f} {r['resid']:>+6.2f} "
          f"{r.get('level', 0) or 0:>5.1f} {r.get('raw_ash', 0) or 0:>6.2f} "
          f"{r.get('desliming473', 0):>3} {r.get('desliming474', 0):>3} "
          f"{r.get('sysA', 0):>2} {r.get('sysB', 0):>2} {r.get('face', ''):<8} {tag}")

# 统计:大偏差日中有多少天发生了"工况突变"
big = [r for r in by_abs if r["abs_r"] > 1.5]
small = [r for r in by_abs if r["abs_r"] <= 1.5]
def count_changes(r):
    idx = r["idx"]
    if idx == 0:
        return 0
    prev = days[idx - 1]
    n = sum(1 for f in ["sysA", "sysB", "sys401", "sys402", "desliming473", "desliming474"]
            if r.get(f) != prev.get(f))
    if prev.get("raw_ash") and r.get("raw_ash") and abs(r["raw_ash"] - prev["raw_ash"]) > 2:
        n += 1
    if r.get("mining_face") and prev.get("mining_face") and r["mining_face"] != prev["mining_face"]:
        n += 1
    return n

big_with_change = sum(1 for r in big if count_changes(r) >= 1)
small_with_change = sum(1 for r in small if count_changes(r) >= 1)
print(f"\n偏差>1.5%的天数: {len(big)}, 其中有工况突变: {big_with_change} ({100*big_with_change/max(1,len(big)):.0f}%)")
print(f"偏差≤1.5%的天数: {len(small)}, 其中有工况突变: {small_with_change} ({100*small_with_change/max(1,len(small)):.0f}%)")

# ---- 2. 残差自相关(昨日偏差→今日偏差) ----
print("\n" + "=" * 80)
print("2. 残差自相关(偏差是否有惯性)")
print("=" * 80)
if len(residuals) > 2:
    r1 = [residuals[i]["resid"] for i in range(len(residuals) - 1)]
    r2 = [residuals[i + 1]["resid"] for i in range(len(residuals) - 1)]
    m1, m2 = sum(r1) / len(r1), sum(r2) / len(r2)
    cov = sum((r1[i] - m1) * (r2[i] - m2) for i in range(len(r1)))
    s1 = math.sqrt(sum((v - m1) ** 2 for v in r1))
    s2 = math.sqrt(sum((v - m2) ** 2 for v in r2))
    ac = cov / (s1 * s2) if s1 > 0 and s2 > 0 else 0
    print(f"  昨日偏差 vs 今日偏差 相关系数: {ac:.3f}")
    print(f"  {'强正自相关(>0.5): 偏差有连续性,可能是系统性因素' if ac > 0.5 else '弱自相关: 偏差随机,数据量问题' if abs(ac) < 0.3 else '中等自相关: 混合因素'}")

# ---- 3. 数据量敏感性(前N天训练→预测第N+1天) ----
print("\n" + "=" * 80)
print("3. 数据量敏感性(前N天训练→预测第N+1天)")
print("=" * 80)
print(f"{'训练天数':>8} {'预测日':<12} {'残差':>6} {'|残差|':>6}")
print("-" * 40)
errors_by_n = defaultdict(list)
for split in range(15, len(days)):
    X_tr = X[:split]
    y_tr = y[:split]
    m = train_mlr(X_tr, y_tr, 0.8)
    if not m:
        continue
    pred = m["intercept"] + sum(m["coefs"][j] * X[split][j] for j in range(len(FEATS)))
    err = y[split] - pred
    errors_by_n[split].append(err)
    if split >= len(days) - 10:  # 只打印最后10个
        print(f"{split:>8} {days[split]['day']:<12} {err:>+6.2f} {abs(err):>6.2f}")

# 按训练数据量分段的平均绝对误差
early = [abs(e) for n, errs in errors_by_n.items() if n < 25 for e in errs]
late = [abs(e) for n, errs in errors_by_n.items() if n >= 25 for e in errs]
print(f"\n  训练<25天时平均|残差|: {sum(early)/max(1,len(early)):.3f}")
print(f"  训练≥25天时平均|残差|: {sum(late)/max(1,len(late)):.3f}")
print(f"  {'数据增多残差明显缩小 → 数据量是主因' if late and early and sum(early)/len(early) > sum(late)/len(late) * 1.3 else '数据增多残差未明显缩小 → 工艺/模型问题为主'}")

# ---- 4. 残差与因子突变的相关性 ----
print("\n" + "=" * 80)
print("4. 残差大小 vs 当日工况突变数")
print("=" * 80)
for r in residuals:
    r["n_changes"] = count_changes(r)
groups = defaultdict(list)
for r in residuals:
    groups[min(r["n_changes"], 3)].append(r["abs_r"])
for n_c in sorted(groups.keys()):
    vals = groups[n_c]
    label = f"{n_c}个突变" if n_c < 3 else "3+个突变"
    print(f"  {label}: 平均|残差|={sum(vals)/len(vals):.2f}  (n={len(vals)})")

print("\n" + "=" * 80)
print("结论")
print("=" * 80)
