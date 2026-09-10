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
