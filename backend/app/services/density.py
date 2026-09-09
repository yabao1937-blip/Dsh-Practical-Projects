"""密度指导：总灰分公式 + 专家表建议密度（逐值对齐前端 app.js）。

常量/公式与前端 app.js 完全一致，保证 golden 对拍。
"""
import math

# 专家经验调整表（总灰分偏差 → 密度修正量，分段线性）
EXPERT_ADJUST = {
    "deadband": 0.05,
    "p1": {"dA": 0.15, "dRho": 0.01},
    "p2": {"dA": 0.25, "dRho": 0.02},
    "slope": 0.1,
}

# 专家经验反推的物理增益 K（仅用于"调密后重介灰分预测"展示，不参与调整量）
K_PREDICT = 0.075

DENSITY_GUIDE = {
    "deadband": 0.05, "maxStep": 0.01, "rhoMin": 1.35, "rhoMax": 1.60,
    "kFallback": 0.03, "simBaseRho": 1.49, "simK": 0.03,
}


def calc_total_ash(heavy_ash, heavy_amt, float_ash, float_amt, coarse_ash, coarse_amt):
    """总精煤灰分加权公式：与前端 calcTotalAsh 一致。"""
    total = heavy_amt + float_amt + coarse_amt
    if total == 0:
        return 0.0
    return (heavy_ash * heavy_amt + float_ash * float_amt + coarse_ash * coarse_amt) / total


def expert_adjust(d_abs):
    """专家经验：|总灰分偏差| → 密度修正量。与前端 expertAdjust 一致。"""
    t = EXPERT_ADJUST
    if not (d_abs > t["deadband"]):
        return 0.0
    if d_abs <= t["p1"]["dA"]:
        return (d_abs - t["deadband"]) / (t["p1"]["dA"] - t["deadband"]) * t["p1"]["dRho"]
    if d_abs <= t["p2"]["dA"]:
        return t["p1"]["dRho"] + (d_abs - t["p1"]["dA"]) / (t["p2"]["dA"] - t["p1"]["dA"]) * (t["p2"]["dRho"] - t["p1"]["dRho"])
    return t["p2"]["dRho"] + (d_abs - t["p2"]["dA"]) * t["slope"]


def compute_density_guidance(state: dict, target_total_ash: float) -> dict:
    """建议密度计算（纯函数）。与前端 computeDensityGuidance 一致。

    state 需含：rho_cur、heavy_ash、actual_total、scheme('total'|'heavy')、tol(可选0.1)；
    heavy 版额外需：heavy_amt、float_amt、coarse_amt、float_ash、coarse_ash。
    k(可选)：数据驱动的密度-灰分增益（services.density_model 拟合），
    缺省回退展示常数 K_PREDICT（与前端 densityGainK 回退口径一致）。
    """
    scheme = state.get("scheme", "total")
    rho_cur = state["rho_cur"]
    heavy_ash = state["heavy_ash"]
    actual_total = state["actual_total"]
    tol = state.get("tol", 0.1)
    k_gain = state.get("k")
    k_used = k_gain if (isinstance(k_gain, (int, float)) and k_gain > 0) else K_PREDICT
    # maxStep 仅回显给前端做“逐步走向目标密度”动画（页面可调钳制幅度），
    # 后端纯函数不参与计算，保留以与前端 computeDensityGuidance 输出结构逐值一致。
    max_step = state.get("max_step", DENSITY_GUIDE["maxStep"])

    r = {
        "valid": actual_total is not None,
        "rhoCur": rho_cur, "K": k_used, "heavyAsh": heavy_ash,
        "kSource": "data" if k_used is not K_PREDICT else "default",
        "targetTotal": target_total_ash, "actualTotal": actual_total, "scheme": scheme,
        "targetHeavy": None, "deltaAHeavy": None,
        "deltaA": None, "deltaRho": 0.0, "rhoNew": rho_cur, "direction": "stable",
        "reason": "", "deadband": tol, "maxStep": max_step,
    }
    if not r["valid"]:
        r["reason"] = "总精煤灰分数据不完整，暂无密度调整建议"
        return r

    if scheme == "heavy":
        heavy_amt = state.get("heavy_amt")
        float_amt = state.get("float_amt")
        coarse_amt = state.get("coarse_amt")
        float_ash = state.get("float_ash")
        coarse_ash = state.get("coarse_ash")
        amounts_ok = (heavy_amt is not None and heavy_amt > 0 and float_amt is not None
                      and coarse_amt is not None and float_ash is not None and coarse_ash is not None)
        if not amounts_ok:
            r["valid"] = False
            r["reason"] = "量/灰分数据不完整，重介精煤灰分版无法换算目标重介灰分"
            return r
        total_amt = heavy_amt + float_amt + coarse_amt
        r["heavyAmt"] = heavy_amt
        r["totalAmt"] = total_amt
        r["targetHeavy"] = round((target_total_ash * total_amt - float_ash * float_amt - coarse_ash * coarse_amt) / heavy_amt, 3)
        r["deltaAHeavy"] = round(heavy_ash - r["targetHeavy"], 3)
        r["deltaA"] = round(r["deltaAHeavy"] * heavy_amt / total_amt, 3)
    else:
        r["deltaA"] = round(actual_total - target_total_ash, 3)

    if abs(r["deltaA"]) <= tol:
        if scheme == "heavy":
            r["reason"] = (f"重介灰分偏差 {r['deltaAHeavy']:+.2f}%（实测{heavy_ash:.2f}% / "
                           f"目标{r['targetHeavy']:.2f}%，等效总灰分 {r['deltaA']:+.2f}%）≤ ±{tol}% —— 已达标，密度保持")
        else:
            r["reason"] = (f"实际总灰分 {actual_total:.2f}% 与期望 {target_total_ash:.2f}% 偏差 "
                           f"{r['deltaA']:+.2f}% ≤ ±{tol}% —— 已达标，密度保持")
        return r

    sign = 1.0 if r["deltaA"] > 0 else -1.0
    d_rho_full = -sign * expert_adjust(abs(r["deltaA"]))
    r["deltaRho"] = round(d_rho_full, 4)
    r["rhoNew"] = max(DENSITY_GUIDE["rhoMin"], min(DENSITY_GUIDE["rhoMax"], round(rho_cur + d_rho_full, 3)))
    r["direction"] = "down" if r["deltaA"] > 0 else "up"
    if scheme == "heavy":
        r["reason"] = (f"重介灰分{'偏高' if r['deltaAHeavy'] > 0 else '偏低'} {abs(r['deltaAHeavy']):.2f}%"
                       f"（实测{heavy_ash:.2f}% / 目标{r['targetHeavy']:.2f}%，等效总灰分偏差{r['deltaA']:+.2f}%），"
                       f"按专家经验建议{'下调' if r['direction'] == 'down' else '上调'}密度至 {r['rhoNew']:.3f} g/cm³（人工执行）")
    else:
        r["reason"] = (f"实际总灰分{'偏高' if r['deltaA'] > 0 else '偏低'} {abs(r['deltaA']):.2f}%"
                       f"（{actual_total:.2f}% / 期望{target_total_ash:.2f}%），"
                       f"按专家经验建议{'下调' if r['direction'] == 'down' else '上调'}密度至 {r['rhoNew']:.3f} g/cm³（人工执行）")
    return r
