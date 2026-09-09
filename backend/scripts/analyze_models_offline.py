# -*- coding: utf-8 -*-
"""离线分析(只读 DB):
1) K 增益可辨识性:按 系统 / 系统×皮带 / 分时段 回归,验证闭环辨识问题
2) 训练范围对比:jun_jul vs 30d vs all(MLR/PLS 的 r2/q2/q2Time/passRate)
3) mining_face one-hot(3301/6303, 基线3309)特征消融:10特征 vs 12特征
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from app.database import SessionLocal                      # noqa: E402
from app.models import CoalRecord                          # noqa: E402
from app.routers.training import _record_to_dict           # noqa: E402
from app.services import training as T                     # noqa: E402
from app.services.training_sklearn import train_coarse_model_sklearn  # noqa: E402
from app.services.density_model import _ols                # noqa: E402

db = SessionLocal()

# ---------- 1) K 可辨识性 ----------
print("=" * 70)
print("1) 密度-灰分 K 可辨识性分析")
recs3 = db.query(CoalRecord).filter(CoalRecord.category == "ash_density").all()
pts = []
for r in recs3:
    if r.ash_content is not None and r.density is not None and 1.3 <= r.density <= 1.6:
        pts.append({"sys": r.system, "belt": r.belt or "?", "ash": r.ash_content,
                    "rho": r.density, "ts": r.ts})
print(f"有效配对 {len(pts)} 条, 时间 {min(p['ts'] for p in pts)} ~ {max(p['ts'] for p in pts)}")

groups = {}
for p in pts:
    groups.setdefault(f"{p['sys']}/{p['belt']}", []).append((p["ash"], p["rho"]))
periods = {"May(05月)": lambda t: t.startswith("2026-05"), "AugSep(08-09月)": lambda t: t.startswith("2026-08") or t.startswith("2026-09")}
for name, pred in periods.items():
    groups[name + " A/*"] = [(p["ash"], p["rho"]) for p in pts if p["sys"] == "A" and pred(p["ts"])]
    groups[name + " B/*"] = [(p["ash"], p["rho"]) for p in pts if p["sys"] == "B" and pred(p["ts"])]
# 密度→下一时刻灰分(滞后配对,尝试打破闭环):按系统排序后 rho[t] vs ash[t+1]
for sysname in ("A", "B"):
    seq = sorted([p for p in pts if p["sys"] == sysname], key=lambda p: p["ts"])
    lagged = [(seq[i]["rho"], seq[i + 1]["ash"]) for i in range(len(seq) - 1)]
    groups[f"滞后 {sysname}: rho[t]→ash[t+1]"] = lagged

print(f"\n{'分组':<28}{'n':>5}{'斜率k':>12}{'R²':>9}  物理有效(0.005<k<0.2)")
for name in sorted(groups):
    fit = _ols(groups[name])
    if fit is None:
        print(f"{name:<28}{len(groups[name]):>5}{'—':>12}{'—':>9}  样本/分母不足")
    else:
        ok = "✓" if 0.005 < fit["k"] < 0.2 else "✗"
        print(f"{name:<28}{fit['n']:>5}{fit['k']:>12.5f}{fit['r2']:>9.3f}  {ok}")

# ---------- 2) 训练范围对比 ----------
print()
print("=" * 70)
print("2) 训练范围对比(同一引擎,MLR/PLS)")
recs = [_record_to_dict(r) for r in
        db.query(CoalRecord).filter(CoalRecord.category == "coarse")
        .order_by(CoalRecord.ts, CoalRecord.id).all()]

def show(tag, result):
    if result is None:
        print(f"{tag:<22}训练数据不足")
        return
    h = result["history"]
    print(f"{tag:<22}n={h['n']:<4}production={h['production']:<4}"
          f"MLR r2={h['mlr']['r2']:.3f} q2={h['mlr']['q2']:.3f} q2T={h['mlr']['q2Time']}"
          f" passRate={h['mlr']['passRate']:.1f}%")
    print(f"{'':<22}{'':<18}PLS r2={h['pls']['r2']:.3f} q2={h['pls']['q2']:.3f} q2T={h['pls']['q2Time']}"
          f" passRate={h['pls']['passRate']:.1f}% A={h['pls']['A']}")

for rg in ("jun_jul", "30d", "all"):
    show(f"range={rg}", T.train_coarse_model(recs, rg, 0.8))

# ---------- 3) mining_face 特征消融 ----------
print()
print("=" * 70)
print("3) mining_face one-hot 特征消融(基线 3309)")
faces = {}
for r in recs:
    faces[r.get("mining_face") or ""] = faces.get(r.get("mining_face") or "", 0) + 1
print("工作面分布:", faces)

FEATS10 = T.MLR_FEATURES  # 10 特征基线


def with_face(records):
    """给记录派生 face_3301 / face_6303(基线 3309 与缺失→0)。"""
    out = []
    for r in records:
        r2 = dict(r)
        f = str(r.get("mining_face") or "").strip()
        r2["face_3301"] = 1 if f == "3301" else 0
        r2["face_6303"] = 1 if f == "6303" else 0
        out.append(r2)
    return out


FEATS12 = FEATS10 + ["face_3301", "face_6303"]

# build_coarse_xy 支持 features 参数;train_mlr/train_pls 不依赖特征名,直接吃 X
from app.services.training import build_coarse_xy, train_mlr, train_pls, _time_cv_q2, _metrics, _mean, _std, _sst, _un_drop  # noqa: E402

for tag, feats, data in (("10特征(现行)", FEATS10, recs), ("12特征(+face)", FEATS12, with_face(recs))):
    d = build_coarse_xy(data, "all", feats)
    if d is None:
        print(f"{tag}: 数据不足")
        continue
    mlr = train_mlr(d["X"], d["y"], 0.8)
    pls = train_pls(d["X"], d["y"], 0.8, len(feats))
    mlr_q2t = _time_cv_q2(d["X"], d["y"], "mlr", None, 0.8)
    pls_q2t = _time_cv_q2(d["X"], d["y"], "pls", pls["A"], 0.8)
    print(f"\n{tag}  range=all  n={len(d['y'])}")
    print(f"  MLR r2={mlr['metrics']['r2']:.4f} q2={mlr['metrics']['q2']:.4f} q2Time={mlr_q2t:.4f} passRate={mlr['metrics']['passRate']:.1f}%")
    print(f"  PLS r2={pls['metrics']['r2']:.4f} q2={pls['metrics']['q2']:.4f} q2Time={pls_q2t:.4f} passRate={pls['metrics']['passRate']:.1f}% A={pls['A']}")
    if tag.startswith("12"):
        # 打印 face 特征的标准化系数(相对重要性)
        full = _un_drop({"coefs": mlr["coefs"], "means": mlr["means"], "stds": mlr["stds"],
                         "stdCoef": mlr["stdCoef"]}, mlr["drop"], len(feats))
        sc = full["stdCoef"]
        for j, f in enumerate(feats):
            if f.startswith("face_"):
                print(f"  face 特征标准系数 {f}: {sc[j]:.4f}")

db.close()
print()
print("done.")
