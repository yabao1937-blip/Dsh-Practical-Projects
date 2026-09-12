"""P4 密度指导算法单测：逐值对齐前端 app.js 的专家表/总灰分公式"""
from app.services.density import calc_total_ash, compute_density_guidance, expert_adjust


def test_calc_total_ash_formula():
    # (8.50×436.7 + 8.22×27 + 14.27×40) / (436.7+27+40)
    v = calc_total_ash(8.50, 436.7, 8.22, 27.0, 14.27, 40.0)
    expect = (8.50 * 436.7 + 8.22 * 27.0 + 14.27 * 40.0) / (436.7 + 27.0 + 40.0)
    assert abs(v - expect) < 1e-12


def test_expert_adjust_deadband():
    assert expert_adjust(0.05) == 0.0       # ≤ 死区不调
    assert expert_adjust(0.049) == 0.0


def test_expert_adjust_ramp():
    """现场确认口径（2026-09-12 访谈）：Δρ = min(|ΔA|/15, 0.03)，死区 0.05 不动。

    现场给的三个点：0.15% → 0.01、0.30% → 0.02、更大时最多 0.03。
    """
    assert abs(expert_adjust(0.15) - 0.01) < 1e-12        # 0.15/15 = 0.01（现场点）
    assert abs(expert_adjust(0.25) - 0.25 / 15) < 1e-12   # 0.016667（旧表是 0.02，现场口径更低）
    assert abs(expert_adjust(0.30) - 0.02) < 1e-12        # 现场点
    assert expert_adjust(0.04) == 0.0                     # 死区内不动


def test_expert_adjust_capped():
    """单次修正**封顶 0.03**（现场："最多也就调 0.03"）——旧表无上限，1.5% 偏差会给 0.145（4.8 倍）。"""
    assert abs(expert_adjust(0.60) - 0.03) < 1e-12   # 0.04 被上限截到 0.03
    assert abs(expert_adjust(0.50) - 0.03) < 1e-12
    assert abs(expert_adjust(1.50) - 0.03) < 1e-12   # 旧表此处为 0.145


def test_guidance_total_reach_target():
    # 偏差 0.08 ≤ 容差 0.1 → 达标保持
    st = {"rho_cur": 1.45, "heavy_ash": 8.50, "actual_total": 8.58, "scheme": "total", "tol": 0.1}
    g = compute_density_guidance(st, 8.50)
    assert g["direction"] == "stable" and g["deltaRho"] == 0.0


def test_guidance_total_down_stepwise_and_full_target():
    """灰分偏高 0.50：完整修正 0.045，但**发布的建议只走一步**（≤maxStep=0.01）。

    P0（2026-09-12）：整步修正要么依赖"专家表=增益律"这一未验证前提，要么在闭环里震荡
    （实测 ρ:1.52→1.433→1.588→…）。所以默认逐步：每步之后等新的化验/在线数据再走下一步；
    完整目标仍以 rhoTargetFull 给出，便于人工判断还要走几步。
    """
    st = {"rho_cur": 1.49, "heavy_ash": 8.50, "actual_total": 9.00, "scheme": "total", "tol": 0.1}
    g = compute_density_guidance(st, 8.50)
    assert g["direction"] == "down"
    assert abs(g["deltaRho"] + 0.01) < 1e-9              # 发布的一步（≤maxStep）
    assert abs(g["deltaRhoFull"] + 0.03) < 1e-9          # 完整修正量（现场封顶 0.03）
    assert abs(g["rhoTargetFull"] - 1.46) < 1e-9         # 完整目标
    assert abs(g["rhoNew"] - 1.48) < 1e-9                # 本步目标
    assert g["steps"] >= 3 and g["stepwise"] is True


def test_guidance_full_step_still_available():
    """stepwise=False 时保留整步语义（用于对照/回放，不是默认）。"""
    st = {"rho_cur": 1.49, "heavy_ash": 8.50, "actual_total": 9.00, "scheme": "total",
          "tol": 0.1, "stepwise": False}
    g = compute_density_guidance(st, 8.50)
    assert abs(g["deltaRho"] + 0.03) < 1e-9      # 现场封顶后的整步量
    assert abs(g["rhoNew"] - 1.46) < 1e-9
    assert g["stepwise"] is False


def test_guidance_stepwise_respects_upper_bound():
    """逐步只走一步：1.45 + 0.01 = 1.46（完整目标也不会越过 1.60）。"""
    st = {"rho_cur": 1.45, "heavy_ash": 8.50, "actual_total": 7.0, "scheme": "total", "tol": 0.1}
    g = compute_density_guidance(st, 10.20)
    assert g["direction"] == "up"
    assert g["rhoNew"] == 1.46
    assert g["rhoTargetFull"] <= 1.60


def test_guidance_hold_returns_stable_with_reason():
    """P0②③ 保持：同一份数据已动作过 / 或在驻留窗口内 → 只报"保持"并说明原因。"""
    st = {"rho_cur": 1.49, "heavy_ash": 8.50, "actual_total": 9.00, "scheme": "total", "tol": 0.1,
          "hold": True, "hold_reason": "已按当前这份数据调整过密度（同一份化验只动作一次）"}
    g = compute_density_guidance(st, 8.50)
    assert g["direction"] == "stable" and g["deltaRho"] == 0.0 and g["rhoNew"] == 1.49
    assert "只动作一次" in g["reason"]


def test_guidance_placeholder_manual_blocks_advice():
    """P0④ 占位值守卫：手动灰分等于目标值且长期未更新 → 不给建议（宁可不给，也不给编造的）。"""
    st = {"rho_cur": 1.49, "heavy_ash": 8.50, "actual_total": 8.50, "scheme": "total", "tol": 0.1,
          "placeholder_manual": True}
    g = compute_density_guidance(st, 8.50)
    assert g["valid"] is False
    assert "占位值" in g["reason"]


def test_guidance_heavy_scheme():
    # 重介版：目标重介灰分=(A目标×总量−浮−粗)/重介量
    st = {
        "rho_cur": 1.49, "heavy_ash": 8.50, "actual_total": 9.01, "scheme": "heavy", "tol": 0.1,
        "heavy_amt": 436.7, "float_amt": 27.0, "coarse_amt": 40.0,
        "float_ash": 8.22, "coarse_ash": 14.27,
    }
    g = compute_density_guidance(st, 8.50)
    assert g["targetHeavy"] is not None and g["deltaAHeavy"] is not None
    # 重介灰分 8.50 相对目标重介灰分（偏低，因粗灰 14.27 拉高总灰分→目标重介灰分被压低）
    assert g["deltaAHeavy"] > 0
    assert g["direction"] == "down"
