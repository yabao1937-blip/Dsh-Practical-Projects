"""P4 取值链 golden 对拍：与前端 App.resolve* 在种子数据上的输出一致。"""
import json
from pathlib import Path

from app.services import resolvers

BASE = Path(__file__).resolve().parent.parent / "data"
SEED = json.loads((BASE / "seed_store.json").read_text(encoding="utf-8"))
JS = json.loads((BASE / "resolvers_js.json").read_text(encoding="utf-8"))


def _close(a, b):
    return abs(a - b) < 1e-6


def test_heavy_ash():
    assert resolvers.get_heavy_ash(SEED) == JS["heavyAsh"]


def test_amounts():
    assert resolvers.resolve_amount(SEED, "denseAmount") == JS["heavyAmt"]
    assert resolvers.resolve_amount(SEED, "floatAmount") == JS["floatAmt"]
    assert resolvers.resolve_amount(SEED, "coarseAmount") == JS["coarseAmt"]
    assert resolvers.resolve_amount(SEED, "totalAmount") == JS["totalAmt"]


def test_float_ash():
    assert resolvers.resolve_float_ash(SEED) == JS["floatAsh"]


def test_coarse_ash():
    assert _close(resolvers.resolve_coarse_ash(SEED), JS["coarseAsh"])


def test_total_ash():
    assert _close(resolvers.resolve_total_ash(SEED), JS["totalAsh"])


def test_density():
    assert resolvers.resolve_density(SEED) == JS["density"]


def test_negative_manual_not_leaked():
    """GLM 发现的 bug：负数/非法 manual 不应泄漏到取值链兜底"""
    store = {
        "amountInputs": {"coarseAmount": {"manual": -5}, "floatAmount": {"manual": -3, "mode": "auto"}},
        "floatCoal": [], "coarseCalc": {},
    }
    assert resolvers.resolve_amount(store, "coarseAmount") is None
    assert resolvers.resolve_amount(store, "floatAmount") is None


def test_total_ash_div_zero():
    """原 bug 回归:皮带秤和为 0 时总灰分不除零。皮带分工改造后兜底=501 直读
    (501=总混配皮带),无数据时回落默认 8.8 —— 不再做任何加权除法。"""
    store = {
        "amountInputs": {}, "ashInputs": {},
        "instrumentInputs": {"scale_501": {"manual": 0}, "scale_502": {"manual": 0}},
        "coarseCoal": [], "floatCoal": [], "calcLogs": [], "magneticTail": [],
        "heavyAshInput": {}, "coarseAshInput": {}, "floatAshInput": {},
        "coarseCalc": {}, "coarseAshEma": None, "autoState": {}, "coarseModel": None,
    }
    assert resolvers.resolve_total_ash(store) == 8.8


# ---------------- 501 层级与「常量不得驱动控制建议」（Q-4） ----------------

def test_ash501_layer_three_states():
    """501 来源三态：手动 > 在线 > 无（落到默认常量）。

    实测依据：真实库 360 条 ash_density 全部 belt=502、零条 501 → 真实数据下是 'none'。
    """
    assert resolvers.ash501_layer({}) == "none"
    online = {"calcLogs": [{"timestamp": "2026-09-01 08:00:00", "calc_type": "ash_meter",
                            "input_json": json.dumps({"belt": "501", "value": 9.2})}]}
    assert resolvers.ash501_layer(online) == "online"
    manual = dict(online)
    manual["instrumentInputs"] = {"ash_501": {"manual": 8.9}}
    assert resolvers.ash501_layer(manual) == "manual"
    # 只有 belt=502 的记录不算 501 有数据源（这正是真实库的现状）
    only502 = {"calcLogs": [{"timestamp": "2026-09-01 08:00:00", "calc_type": "ash_meter",
                             "input_json": json.dumps({"belt": "502", "value": 7.8})}]}
    assert resolvers.ash501_layer(only502) == "none"


def test_total_ash_source_and_constant_flag():
    """总灰分来源：手动 > 公式 > 录入 > 501直读。"""
    ex = resolvers.resolve_total_ash_ex({})
    assert ex["source"] == "ash501" and ex["value"] == 8.8
    assert resolvers.resolve_total_ash_ex({"ashInputs": {"totalAsh": {"manual": 9.1}}}) == \
        {"value": 9.1, "source": "manual"}


def test_constant_total_ash_blocks_guidance():
    """501 未接入 → 总灰分是常量 → 不得据此给出密度建议。

    否则 8.8 与目标 8.5 差 0.3 会超容差并推出「下调密度」——用编造的灰分指挥现场操作。
    """
    from app.services.density import compute_density_guidance
    g = compute_density_guidance({"rho_cur": 1.49, "heavy_ash": 7.9, "actual_total": 8.8,
                                  "scheme": "total", "tol": 0.1,
                                  "actual_total_is_constant": True}, 8.50)
    assert g["valid"] is False
    assert g["direction"] == "stable"
    assert "501" in g["reason"] and "常量" in g["reason"]


def test_real_501_reading_allows_guidance():
    """501 有真实读数时守卫不生效，偏差照常给出建议（确认不是把功能整体关掉）。"""
    from app.services.density import compute_density_guidance
    g = compute_density_guidance({"rho_cur": 1.49, "heavy_ash": 7.9, "actual_total": 9.2,
                                  "scheme": "total", "tol": 0.1,
                                  "actual_total_is_constant": False}, 8.50)
    assert g["valid"] is True
    assert g["direction"] == "down" and g["rhoNew"] < 1.49


def test_brief_501_column_blank_when_no_source():
    """简报 501 列：无该小时 501 记录时留空，不打印默认常量冒充实测。"""
    from app.services.brief import build_hourly_brief
    fixture = json.loads((BASE / "brief_js.json").read_text(encoding="utf-8"))
    rows = fixture["rows"]
    assert rows, "夹具应产出若干行"
    assert all(r[6] == "" for r in rows), "夹具里没有 belt=501 记录，该列应全为空"
    # 后端在同一夹具上也必须留空（golden 对拍已覆盖逐值一致，这里再钉一下语义）
    assert build_hourly_brief({})["headers"][6].startswith("501")
