"""密度-灰分关系在线辨识（数据驱动 K 增益）。

背景：
- 专家表 expert_adjust 给出"灰分偏差→密度修正量"，但反过来"调多少密度能改变
  多少灰分"的物理增益 K = Δρ/ΔA 此前只有展示常数 K_PREDICT=0.075，
  前端 densityGainK() 写好后从未接入指导计算；
- 表3（灰分、密度）配对是 K 的真实数据源。A/B 是两套并联重介系统，
  同灰分水平下密度设定不同（如 8.49% 灰分时 A=1.475 / B=1.464），
  直接合并回归会把"系统间设定差"混进斜率 → 分系统 OLS 后按样本数加权平均；
- 防御口径与前端 densityGainK 一致：单系统样本 <5、|分母|<1e-9、
  斜率非正或越出物理约束 (0.005, 0.2) 时丢弃；分系统全失效回退 pooled，
  pooled 仍失效返回 valid=False（调用方沿用默认常数）。

与前端逐值对齐：本模块的公式/阈值与 app.js densityGainK 完全一致，
勿单侧修改。
"""
import json
import math

K_MIN = 0.005
K_MAX = 0.2
MIN_POINTS = 5


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def valid_points(store: dict) -> list:
    """从 store.calcLogs 提取有效 (system, ash, rho) 配对（密度 1.3~1.6）。"""
    pts = []
    for l in store.get("calcLogs") or []:
        if l.get("calc_type") != "ash_density":
            continue
        try:
            v = json.loads(l.get("input_json") or "{}")
        except Exception:
            continue
        ash = v.get("ash_content")
        rho = v.get("density")
        if _is_num(ash) and _is_num(rho) and 1.3 <= rho <= 1.6:
            pts.append({"system": str(v.get("system") or "").strip(),
                        "ash": float(ash), "rho": float(rho)})
    return pts


def _ols(pairs: list) -> dict | None:
    """density ~ ash 的 OLS：返回 {k(斜率 dρ/dA), n, r2}；样本/分母不足返回 None。"""
    n = len(pairs)
    if n < MIN_POINTS:
        return None
    sx = sy = sxy = sxx = 0.0
    for a, r in pairs:
        sx += a
        sy += r
        sxy += a * r
        sxx += a * a
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-9:
        return None
    k = (n * sxy - sx * sy) / denom
    # R²（顺带返回，供展示置信度）
    ym = sy / n
    ss_res = 0.0
    ss_tot = 0.0
    for a, r in pairs:
        yh = ym + k * (a - sx / n)
        ss_res += (r - yh) ** 2
        ss_tot += (r - ym) ** 2
    r2 = (1 - ss_res / ss_tot) if ss_tot > 0 else 0.0
    return {"k": k, "n": n, "r2": r2}


def _k_ok(k: float) -> bool:
    """物理约束：密度↑→灰分↑，K 必须为正且有界（与前端 densityGainK 一致）。"""
    return K_MIN < k < K_MAX and math.isfinite(k)


def fit_density_gain(store: dict) -> dict:
    """数据驱动 K 增益：分系统加权 → pooled 回退 → 无效。

    返回 {valid, k, n, source, totalPoints, systems:{sys:{k,n,r2,used}}}。
    k 不做舍入（与前端一致，展示层自行 toFixed）。
    """
    pts = valid_points(store)
    by_sys: dict[str, list] = {}
    for p in pts:
        by_sys.setdefault(p["system"], []).append((p["ash"], p["rho"]))

    systems = {}
    used = []
    for name in sorted(by_sys):
        fit = _ols(by_sys[name])
        if fit is None:
            continue
        entry = {"k": fit["k"], "n": fit["n"], "r2": fit["r2"], "used": False}
        if _k_ok(fit["k"]):
            entry["used"] = True
            used.append(entry)
        systems[name] = entry

    total_pts = len(pts)
    if used:
        wsum = sum(e["n"] for e in used)
        k = sum(e["k"] * e["n"] for e in used) / wsum
        return {"valid": True, "k": k, "n": wsum, "source": "per_system",
                "totalPoints": total_pts, "systems": systems}

    pooled = _ols([(p["ash"], p["rho"]) for p in pts])
    if pooled is not None and _k_ok(pooled["k"]):
        return {"valid": True, "k": pooled["k"], "n": pooled["n"], "source": "pooled",
                "totalPoints": total_pts, "systems": systems}

    return {"valid": False, "k": None, "n": total_pts, "source": "none",
            "totalPoints": total_pts, "systems": systems}
