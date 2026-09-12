"""密度指导：总灰分公式 + 专家表建议密度（逐值对齐前端 app.js）。

常量/公式与前端 app.js 完全一致，保证 golden 对拍。
"""
import math

# 专家经验调整表（总灰分偏差 → 密度修正量）——2026-09-12 现场访谈确认后的口径：
#   偏差 0.15% → 0.01；0.30% → 0.02；更大时**最多 0.03**；|ΔA| ≤ 0.05% 不动。
#   等价：Δρ = min(|ΔA| / gain, cap)，隐含增益 15%/单位密度、单次封顶 0.03。
# 旧口径（p1 0.15→0.01、p2 0.25→0.02、slope 0.1 无上限）与现场不符：
#   1.5% 偏差时旧表给 0.145（专家上限的 4.8 倍）；0.30% 时旧表 0.025、现场 0.02。
EXPERT_ADJUST = {
    "deadband": 0.05,
    "gain": 15,
    "cap": 0.03,
}

# 专家经验反推的物理增益 K（仅用于"调密后重介灰分预测"展示，不参与调整量）

# 501 未接入时常量灰分不得驱动建议（与前端 computeDensityGuidance 同文案）
REASON_CONSTANT_TOTAL_ASH = (
    "501 皮带灰分仪尚未接入（无在线总灰分数据），当前总灰分取默认常量、非实测 —— "
    "不做密度调整建议；请录入总灰分实测值，或等 501 数据接入"
)
K_PREDICT = 0.075

# P0 安全守卫（2026-09-12，三模型会诊结论）：逐步建议 + 保持 + 占位值守卫。
# stepwise=True 时发布的建议不超过 maxStep，同时给出完整目标与步数：
# 整步修正要么依赖"专家表=增益律"这一未验证前提，要么在闭环里震荡（实测 ρ:1.52→1.433→1.588→…）。
DENSITY_GUIDE = {
    # maxStep：单次建议步长上限（2026-09-12 现场确认：最多 0.03，默认取 0.02）
    "deadband": 0.05, "maxStep": 0.02, "rhoMin": 1.35, "rhoMax": 1.60,
    # simK：密度→灰分仿真增益倒数（2026-09-12 按现场确认增益 15%/单位反推：0.867/15 ≈ 0.0578）
    "kFallback": 0.03, "simBaseRho": 1.49, "simK": 0.0578, "stepwise": True,
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
    # 现场做法：比例律（15%/单位）× 封顶 0.03；**不减死区**（现场三点落在 |ΔA|/15 上）
    return min(d_abs / t["gain"], t["cap"])


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
        # P0 状态位（与前端 densityGuardState 同口径；由调用方通过 state 传入）
        "placeholderManual": bool(state.get("placeholder_manual")),
        "hold": bool(state.get("hold")), "holdReason": state.get("hold_reason") or "",
        "driveKey": state.get("drive_key") or "",
        "deltaRhoFull": None, "rhoTargetFull": None, "steps": 1, "stepwise": False,
        "deltaRhoPredict": None, "rhoPredict": None,
        "expertRangeDA": None, "predictBeyondRange": False,
    }
    # 常量灰分不得驱动控制建议：501 皮带灰分仪尚未接入时，总灰分取的是默认常量，
    # 若照它算 deltaA（如 8.8−8.5=0.3 超容差）会推出"下调密度"——用编造的灰分指挥现场操作。
    # 宁可不给建议，也不给一个基于常量的建议。
    # 必须排在「数据不完整」通用判定之前：否则两侧会给出不同的 reason（前端曾因此不一致）。
    if state.get("actual_total_is_constant"):
        r["valid"] = False
        r["reason"] = REASON_CONSTANT_TOTAL_ASH
        return r

    if not r["valid"]:
        r["reason"] = "总精煤灰分数据不完整，暂无密度调整建议"
        return r

    # P0④ 占位值守卫：手动灰分等于目标值且长期未更新 → 视为占位值，宁可不给建议
    if r["placeholderManual"]:
        r["valid"] = False
        r["reason"] = ("当前灰分取的是「手动值」，且该值等于目标灰分并已超过 24 小时未更新 —— "
                       "疑似占位值（不是新化验结果）。请录入新的化验值后再看密度建议。")
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

    # P0② 动作闩锁 / P0③ 最短驻留：同一份驱动数据只允许一次动作（由前端组装 state 传入）
    if r["hold"]:
        r["direction"] = "stable"
        r["rhoNew"] = rho_cur
        r["deltaRho"] = 0.0
        r["reason"] = r["holdReason"] or "已按当前这份数据调整过密度，等新的化验/在线数据后再给下一步建议"
        return r

    sign = 1.0 if r["deltaA"] > 0 else -1.0
    d_rho_full = -sign * expert_adjust(abs(r["deltaA"]))
    # P0① 逐步建议：发布的建议不超过 maxStep
    stepwise = bool(state.get("stepwise", DENSITY_GUIDE["stepwise"]))
    step_limit = max_step if stepwise and max_step and max_step > 0 else abs(d_rho_full)
    d_rho = max(-step_limit, min(step_limit, d_rho_full))
    r["deltaRhoFull"] = round(d_rho_full, 4)
    r["rhoTargetFull"] = max(DENSITY_GUIDE["rhoMin"],
                             min(DENSITY_GUIDE["rhoMax"], round(rho_cur + d_rho_full, 3)))
    # 建议密度（推测值）：一步到位的终点（按现场确认增益把偏差外推归零），不受专家法则封顶限制
    d_rho_predict = -(r["deltaA"] / EXPERT_ADJUST["gain"])
    r["deltaRhoPredict"] = round(d_rho_predict, 4)
    r["rhoPredict"] = max(DENSITY_GUIDE["rhoMin"],
                          min(DENSITY_GUIDE["rhoMax"], round(rho_cur + d_rho_predict, 3)))
    # 经验范围：法则封顶 0.03 × 现场确认增益 15 = 0.45%（超过则推测值仅供参考）
    expert_range = round(EXPERT_ADJUST["cap"] * EXPERT_ADJUST["gain"], 4)
    r["expertRangeDA"] = expert_range
    r["predictBeyondRange"] = abs(r["deltaA"]) > expert_range
    r["steps"] = max(1, int(-(-abs(d_rho_full) // (step_limit or 1)))) if step_limit else 1
    r["stepwise"] = bool(stepwise and r["steps"] > 1)
    r["deltaRho"] = round(d_rho, 4)
    r["rhoNew"] = max(DENSITY_GUIDE["rhoMin"], min(DENSITY_GUIDE["rhoMax"], round(rho_cur + d_rho, 3)))
    r["direction"] = "down" if r["deltaA"] > 0 else "up"
    if scheme == "heavy":
        r["reason"] = (f"重介灰分{'偏高' if r['deltaAHeavy'] > 0 else '偏低'} {abs(r['deltaAHeavy']):.2f}%"
                       f"（实测{heavy_ash:.2f}% / 目标{r['targetHeavy']:.2f}%，等效总灰分偏差{r['deltaA']:+.2f}%），"
                       f"按专家经验建议{'下调' if r['direction'] == 'down' else '上调'}密度至 {r['rhoNew']:.3f} g/cm³（人工执行）")
    else:
        r["reason"] = (f"实际总灰分{'偏高' if r['deltaA'] > 0 else '偏低'} {abs(r['deltaA']):.2f}%"
                       f"（{actual_total:.2f}% / 期望{target_total_ash:.2f}%），"
                       f"按专家经验建议{'下调' if r['direction'] == 'down' else '上调'}密度至 {r['rhoNew']:.3f} g/cm³（人工执行）")
        if r["stepwise"]:
            r["reason"] += (f"；完整修正目标 {r['rhoTargetFull']:.3f}"
                            f"（本步只走 {abs(r['deltaRho']):.3f}，共约 {r['steps']} 步，"
                            f"每步之后等新的化验/在线数据再走下一步）")
    return r
