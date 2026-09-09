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
    assert abs(expert_adjust(0.15) - 0.01) < 1e-12   # 0.15% → 0.01
    assert abs(expert_adjust(0.25) - 0.02) < 1e-12   # 0.25% → 0.02


def test_expert_adjust_extrapolate():
    assert abs(expert_adjust(0.30) - 0.025) < 1e-12  # 0.02 + 0.05×0.1
    assert abs(expert_adjust(0.50) - 0.045) < 1e-12  # 0.02 + 0.25×0.1


def test_guidance_total_reach_target():
    # 偏差 0.08 ≤ 容差 0.1 → 达标保持
    st = {"rho_cur": 1.45, "heavy_ash": 8.50, "actual_total": 8.58, "scheme": "total", "tol": 0.1}
    g = compute_density_guidance(st, 8.50)
    assert g["direction"] == "stable" and g["deltaRho"] == 0.0


def test_guidance_total_down_and_clamp():
    # 灰分偏高 0.50 → 下调 0.045；当前密度 1.49 → 1.445
    st = {"rho_cur": 1.49, "heavy_ash": 8.50, "actual_total": 9.00, "scheme": "total", "tol": 0.1}
    g = compute_density_guidance(st, 8.50)
    assert g["direction"] == "down"
    assert abs(g["deltaRho"] + 0.045) < 1e-9
    assert abs(g["rhoNew"] - 1.445) < 1e-9


def test_guidance_clamp_upper():
    # 灰分远低 → 大幅上调，钳制到 1.60
    st = {"rho_cur": 1.45, "heavy_ash": 8.50, "actual_total": 7.0, "scheme": "total", "tol": 0.1}
    g = compute_density_guidance(st, 10.20)
    assert g["direction"] == "up"
    assert g["rhoNew"] == 1.60


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
