# -*- coding: utf-8 -*-
"""K(密度-灰分增益)标定:从密度决策日志提取迷你阶跃点并估计 K。

口径(与项目既有约定一致):
- 数据源 = store.densityDecisionLog(app.js _logDensityDecision / _completeDensityDecisionResponses)。
  每条已闭环决策记录 (Δρ实测=dRhoActual, ΔA实测=dAActual),即一个迷你阶跃实验点,K_i = Δρ/ΔA。
- 响应量只取人工化验(app.js 补记时已保证);闭环在线值不可辨识(见 density_model 恒 valid=false 的原因)。
- 物理约束复用 density_model.K_MIN/K_MAX 与经验常数对照(K_PREDICT / 专家表等价 1/gain)。

过滤漏斗(stage 字段,阈值均为模块级常量,可调):
  全部 → 已闭环(非作废) → 响应数值完整 → 滞后在 [LAG_MIN, LAG_MAX] → |Δρ|≥DRHO_MIN(有效阶跃)
  → |ΔA|≥DA_MIN(非弱信号/化验噪声) → K_i 物理有效 → 采信(eligible)。
被过滤的条目仍保留在明细里(带 stage/reason),便于回溯与现场解释。
"""
import math

from .density_model import K_MAX, K_MIN

DRHO_MIN = 0.003     # 有效阶跃下限(g/cm³):更小的"调整"落入录入/执行噪声
DA_MIN = 0.05        # 弱信号下限(%):相变化小于化验噪声量级的响应不可辨
LAG_MIN = 40         # 响应滞后下限(分钟):决策后有 45min 驻留,留余量
LAG_MAX = 240        # 上限(分钟):超过 4h 的"响应"混入其它工况变化概率高
GROUP_MIN_N = 3      # 分组估计的最小样本数

_NUM = (int, float)


def _num(v) -> bool:
    return isinstance(v, _NUM) and not isinstance(v, bool) and math.isfinite(v)


def _quantile(xs: list, p: float) -> float:
    n = len(xs)
    if n == 1:
        return xs[0]
    idx = p * (n - 1)
    lo = int(math.floor(idx))
    hi = min(lo + 1, n - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (idx - lo)


def extract_points(store: dict) -> list:
    """densityDecisionLog → 逐条分析记录(stage 判定,不丢弃任何条目)。"""
    out = []
    for e in (store.get("densityDecisionLog") or []):
        if not isinstance(e, dict):
            continue
        r = e.get("response") if isinstance(e.get("response"), dict) else None
        ctx = e.get("ctx") if isinstance(e.get("ctx"), dict) else {}
        row = {
            "ts": e.get("ts") or "", "trigger": e.get("trigger") or "",
            "scheme": e.get("scheme") or "total", "target": e.get("target"),
            "rhoCur": e.get("rhoCur"), "rhoNew": e.get("rhoNew"),
            "deltaRhoSuggested": e.get("deltaRho"), "deltaA": e.get("deltaA"),
            "kUsed": e.get("kUsed"), "kSource": e.get("kSource") or "",
            "heavyAsh": e.get("heavyAsh"), "totalAsh": e.get("totalAsh"),
            "coalAmount": ctx.get("coalAmount"), "rawAsh": ctx.get("rawAsh"),
            "sysA": ctx.get("sysA"), "sysB": ctx.get("sysB"),
            "miningFace": ctx.get("miningFace") or "", "levelTail": ctx.get("levelTail"),
            "densityActual": ctx.get("densityActual"),
            "heavyAshSource": ctx.get("heavyAshSource") or "",
            "respStatus": "待补记", "lagMin": None, "respSource": "",
            "dRhoActual": None, "dAActual": None, "k": None,
            "eligible": False, "stage": "pending", "reason": "",
        }
        if r is None:
            row["reason"] = "待补记(尚未等到决策后的人工化验)"
            out.append(row)
            continue
        if r.get("invalidated"):
            row["respStatus"] = "作废"
            row["stage"] = "invalidated"
            row["reason"] = "作废:" + str(r.get("reason") or "")
            out.append(row)
            continue
        row["respStatus"] = "已闭环"
        row["stage"] = "closed"
        row["lagMin"] = r.get("lagMin")
        row["respSource"] = r.get("source") or ""
        row["dRhoActual"] = r.get("dRhoActual")
        row["dAActual"] = r.get("dAActual")
        dr, da = row["dRhoActual"], row["dAActual"]
        if not _num(dr) or not _num(da):
            row["stage"] = "missing"
            row["reason"] = "响应数值缺失"
            out.append(row)
            continue
        if not _num(row["lagMin"]):
            row["stage"] = "missing"
            row["reason"] = "响应滞后缺失"
            out.append(row)
            continue
        lag = float(row["lagMin"])
        if lag < LAG_MIN or lag > LAG_MAX:
            row["stage"] = "lag"
            row["reason"] = "滞后越界(%.0fmin 不在 [%d,%d])" % (lag, LAG_MIN, LAG_MAX)
            out.append(row)
            continue
        if abs(dr) < DRHO_MIN:
            row["stage"] = "small_drho"
            row["reason"] = "|Δρ| < %.3f(不构成有效阶跃)" % DRHO_MIN
            out.append(row)
            continue
        if abs(da) < DA_MIN:
            row["stage"] = "weak_da"
            row["reason"] = "|ΔA| < %.2f(弱信号/化验噪声量级)" % DA_MIN
            out.append(row)
            continue
        k = dr / da
        row["k"] = k
        if not (K_MIN < k < K_MAX):
            row["stage"] = "bounds"
            row["reason"] = "K 越物理界(%.3f ∉ (%.3f,%.3f))" % (k, K_MIN, K_MAX)
            out.append(row)
            continue
        row["eligible"] = True
        row["stage"] = "eligible"
        row["reason"] = "采信"
        out.append(row)
    return out


def _stats(vals: list):
    n = len(vals)
    if n == 0:
        return None
    xs = sorted(vals)
    mean = sum(xs) / n
    med = xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2
    var = sum((x - mean) ** 2 for x in xs) / n
    return {"n": n, "mean": mean, "median": med,
            "q1": _quantile(xs, 0.25), "q3": _quantile(xs, 0.75),
            "std": math.sqrt(var)}


def _buckets(vals, k=3):
    """分位三分桶:返回 [(标签, lo, hi)];样本 < k*2 时不分组。"""
    xs = sorted(v for v in vals if _num(v))
    if len(xs) < k * 2:
        return []
    qs = [_quantile(xs, i / k) for i in range(k + 1)]
    return [("%s[%.2f~%.2f]" % (lab, qs[i], qs[i + 1]), qs[i], qs[i + 1])
            for i, lab in enumerate(("低", "中", "高"))]


def summarize(points: list) -> dict:
    """漏斗计数 + 总体估计 + 分组估计(均只在采信点上)。"""
    from collections import Counter
    stages = Counter(p["stage"] for p in points)
    eligible = [p for p in points if p["eligible"]]
    est = _stats([p["k"] for p in eligible])

    groups = {}

    def _add(axis, label, vals):
        if len(vals) >= GROUP_MIN_N:
            groups.setdefault(axis, {})[label] = _stats(vals)

    m = {}
    for p in eligible:
        m.setdefault(p["trigger"], []).append(p["k"])
    for kk, vals in sorted(m.items()):
        _add("触发类型", kk, vals)

    m = {}
    for p in eligible:
        lab = "A=%s B=%s" % ("开" if p["sysA"] else "关", "开" if p["sysB"] else "关")
        m.setdefault(lab, []).append(p["k"])
    for kk, vals in sorted(m.items()):
        _add("系统组合", kk, vals)

    for axis, field in (("带煤量桶", "coalAmount"), ("原煤灰分桶", "rawAsh")):
        buckets = _buckets([p[field] for p in eligible])
        if not buckets:
            continue
        m = {}
        for p in eligible:
            if not _num(p[field]):
                continue
            for lab, lo, hi in buckets:
                if lo <= p[field] <= hi:
                    m.setdefault(lab, []).append(p["k"])
                    break
        for kk, vals in sorted(m.items()):
            _add(axis, kk, vals)

    return {
        "counts": {
            "total": len(points),
            "pending": stages.get("pending", 0),
            "invalidated": stages.get("invalidated", 0),
            "closed": len(points) - stages.get("pending", 0) - stages.get("invalidated", 0),
            "excluded": sum(stages.get(s, 0) for s in ("missing", "lag", "small_drho", "weak_da", "bounds")),
            "eligible": stages.get("eligible", 0),
        },
        "stages": dict(stages),
        "estimates": est,
        "groups": groups,
        "eligible": eligible,
    }
