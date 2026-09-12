"""在线仪表/量/灰分的取值链解析（逐值对齐前端 App.resolve* 系列）。

state 为 store 字典（来自 seed_store.json 或 DB 快照）。本模块覆盖 seed 无手动覆盖的
主路径；手动有效期(manual_at>=auto_at)的完整判定在 validity.py 中单独实现。
"""
from .density import DENSITY_GUIDE
import json
import math

from .density import calc_total_ash
from .modeling import predict_coarse_ash

INSTRUMENT_DEFAULT = {
    # 2026-09 工艺确认:501=总混配皮带(重介+浮精+粗煤泥,灰分应略高于502);
    # 502=仅重介精煤(在线重介灰分,实测均值≈7.85)。旧默认 8.52/10.68 方向颠倒,已校正。
    "ash_501": 8.8, "ash_502": 7.9, "scale_501": 268.5,
    "scale_502": 235.2, "density": 1.450, "level_tail": 55, "float_ash": 9.85,
}


def _is_num(v):
    """数值判定（排除 bool，因 isinstance(True, int) 为 True，否则 True 会被当作 1 穿透）。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _num(v):
    return v if _is_num(v) and not (isinstance(v, float) and math.isnan(v)) else None


def _latest_by_time(items):
    """时间戳最新的一条（字符串序 == 时间序，>= 使数组靠后者在同时刻胜出，与 JS 一致）"""
    best = None
    best_t = None
    for d in items or []:
        t = d.get("timestamp")
        if t is None:
            continue
        if best_t is None or t >= best_t:
            best_t = t
            best = d
    return best


def _latest_calc_value(store, calc_type, belt):
    for l in reversed(store.get("calcLogs") or []):
        if l.get("calc_type") != calc_type:
            continue
        try:
            v = json.loads(l.get("input_json") or "{}")
        except Exception:
            continue
        if (belt is None or str(v.get("belt") or "") == str(belt)) and _is_num(v.get("value")):
            return float(v["value"])
    return None


def resolve_instrument(store, inst_id):
    cfg = (store.get("instrumentInputs") or {}).get(inst_id) or {}
    entry = None
    if inst_id in ("scale_501", "scale_502"):
        entry = _latest_calc_value(store, "belt_scale", "501" if inst_id == "scale_501" else "502")
    elif inst_id in ("ash_501", "ash_502"):
        entry = _latest_calc_value(store, "ash_meter", "501" if inst_id == "ash_501" else "502")
        if entry is None:
            entry = _latest_belt_ash(store, "501" if inst_id == "ash_501" else "502")
        # 密度→灰分仿真（与前端 resolveInstrument 同口径；2026-09-12 补上以消除前后端不一致：
        # 此前只有前端有这一项，同一份 store 两侧取值不同 → 简报与卡片会对不上）：
        #   ① 只在密度**有真实来源**（手动值或 ash_density 录入）时叠加——默认密度是死数据，
        #      叠加会凭空把 502 的 7.9% 变成 6.57%，那是编造出来的偏差；
        #   ② 限幅 ±1.0（远离工作点时不做线性外推）；
        #   ③ 基准点取最新 ash_density 记录的密度，取不到用 simBaseRho。
        density_cfg = (store.get("instrumentInputs") or {}).get("density") or {}
        manual_rho = density_cfg.get("manual")
        has_manual = _is_num(manual_rho) and manual_rho >= 0
        base_rho = None
        for l in reversed(store.get("calcLogs") or []):
            if l.get("calc_type") != "ash_density":
                continue
            try:
                dv = json.loads(l.get("input_json") or "{}").get("density")
                if _is_num(dv) and 1.3 <= dv <= 1.6:
                    base_rho = float(dv)
                    break
            except Exception:
                continue
        if entry is not None and (has_manual or base_rho is not None):
            rho = float(manual_rho) if has_manual else float(base_rho)
            anchor = float(base_rho) if base_rho is not None else float(DENSITY_GUIDE["simBaseRho"])
            delta = (rho - anchor) / DENSITY_GUIDE["simK"]
            entry = round(float(entry) + max(-1.0, min(1.0, delta)), 4)
    elif inst_id == "density":
        for l in reversed(store.get("calcLogs") or []):
            if l.get("calc_type") != "ash_density":
                continue
            try:
                v = json.loads(l.get("input_json") or "{}")
                d = v.get("density")
                if _is_num(d) and 1.3 <= d <= 1.6:
                    entry = float(d)
                    break
            except Exception:
                continue
    elif inst_id == "level_tail":
        for l in reversed(store.get("magneticTail") or []):
            if _is_num(l.get("level")) and l["level"] > 0:
                entry = float(l["level"])
                break
    manual = cfg.get("manual")
    if _is_num(manual) and manual >= 0:
        return manual
    if entry is not None:
        return entry
    return INSTRUMENT_DEFAULT.get(inst_id)


def _latest_belt_ash(store, belt):
    for l in reversed(store.get("calcLogs") or []):
        if l.get("calc_type") != "ash_density":
            continue
        try:
            v = json.loads(l.get("input_json") or "{}")
            if str(v.get("belt") or "") == str(belt) and _is_num(v.get("ash_content")):
                return float(v["ash_content"])
        except Exception:
            continue
    return None


def resolve_total_amount(store):
    cfg = (store.get("amountInputs") or {}).get("totalAmount") or {}
    entry = cfg.get("entry")
    if _is_num(entry) and entry >= 0:
        pass  # 有 entry 时优先
    else:
        e501 = _latest_calc_value(store, "belt_scale", "501")
        e502 = _latest_calc_value(store, "belt_scale", "502")
        entry = round(e501 + e502, 1) if (e501 is not None and e502 is not None) else None
    manual = cfg.get("manual")
    if _is_num(manual) and manual >= 0:
        return manual
    if entry is not None:
        return entry
    return round(resolve_instrument(store, "scale_501") + resolve_instrument(store, "scale_502"), 1)


def resolve_coarse_amount(store):
    cfg = (store.get("amountInputs") or {}).get("coarseAmount") or {}
    cc = store.get("coarseCalc") or {}
    calc = None
    if _is_num(cc.get("screen315")) and _is_num(cc.get("waterUnder")) \
            and cc["screen315"] >= cc["waterUnder"]:
        calc = round(cc["screen315"] - cc["waterUnder"], 1)
    manual = cfg.get("manual")
    if _is_num(manual) and manual >= 0:
        return manual
    if calc is not None:
        return calc
    return None


def resolve_amount(store, key):
    cfg = (store.get("amountInputs") or {}).get(key) or {}
    mode = cfg.get("mode") or "manual"
    auto = None
    if key == "floatAmount" and mode == "auto":
        last = _latest_by_time(store.get("floatCoal"))
        auto = last.get("coal_amount") if (last and _is_num(last.get("coal_amount"))) else None
    elif key == "denseAmount" and mode == "calc":
        t = resolve_amount(store, "totalAmount")
        f = resolve_amount(store, "floatAmount")
        c = resolve_amount(store, "coarseAmount")
        if t is not None and f is not None and c is not None:
            auto = max(0.0, round(t - f - c, 1))
    elif key == "totalAmount":
        auto = resolve_total_amount(store)
    elif key == "coarseAmount":
        auto = resolve_coarse_amount(store)
    manual = cfg.get("manual")
    if _is_num(manual) and manual >= 0:
        return manual
    if auto is not None:
        return auto
    return None


def resolve_float_ash(store):
    cfg = store.get("floatAshInput") or {}
    last = _latest_by_time(store.get("floatCoal"))
    auto = last.get("ash_content") if (last and _is_num(last.get("ash_content"))) else None
    if auto is None:
        auto = INSTRUMENT_DEFAULT["float_ash"]
    manual = cfg.get("manual")
    if _is_num(manual) and manual >= 0:
        return manual
    return auto


def resolve_coarse_ash(store):
    cfg = store.get("coarseAshInput") or {}
    last = _latest_by_time(store.get("coarseCoal"))
    auto = None
    if last:
        raw = predict_coarse_ash(last, _model(store))
        if raw is None and _is_num(last.get("ash_content")):
            raw = last["ash_content"]
        if raw is not None and math.isfinite(raw):
            auto = round(raw, 2)  # seed 首次 EMA 无历史 → 返回原始预测
    manual = cfg.get("manual")
    if _is_num(manual) and manual >= 0:
        return manual
    return auto


def _model(store):
    cm = store.get("coarseModel") or {}
    prod = cm.get("production") or "pls"
    return cm.get(prod) or {}


def get_heavy_ash(store):
    """重介精煤灰分取值：手动(采样/手写,非负有限数值,排除 bool/NaN/inf)
    > 502 皮带灰分仪在线值(2026-09 工艺确认:502 只承载重介精煤,即在线重介灰分)
    > 默认 7.9(502 实测均值)。

    brief 与取值链共用此函数，保证同一数据两处口径一致（GLM 发现的原不一致问题）。
    """
    cfg = store.get("heavyAshInput") or {}
    m = cfg.get("manual")
    if _is_num(m) and math.isfinite(m) and m >= 0:
        return m
    return resolve_instrument(store, "ash_502")


def formula_total_ash(store):
    heavy_amt = resolve_amount(store, "denseAmount")
    float_amt = resolve_amount(store, "floatAmount")
    coarse_amt = resolve_amount(store, "coarseAmount")
    float_ash = resolve_float_ash(store)
    coarse_ash = resolve_coarse_ash(store)
    if heavy_amt is None or heavy_amt <= 0 or float_amt is None or coarse_amt is None \
            or float_ash is None or coarse_ash is None:
        return None
    return round(calc_total_ash(get_heavy_ash(store), heavy_amt, float_ash, float_amt, coarse_ash, coarse_amt), 4)


def ash501_layer(store) -> str:
    """501 灰分的来源层级：'manual' | 'online' | 'none'。

    'none' = 三层都没有真实数据源，回落到 INSTRUMENT_DEFAULT 常量 8.8。
    此时该值**不得**用于推导"建议密度"，也不应在简报里冒充仪表读数。
    实测依据：真实库 360 条 ash_density 记录全部 belt=502、零条 501，
    即 501 尚无在线数据（PLC 未接入），ash_501 一直是常量。
    """
    cfg = (store.get("instrumentInputs") or {}).get("ash_501") or {}
    manual = cfg.get("manual")
    if _is_num(manual) and manual >= 0:
        return "manual"
    if _latest_calc_value(store, "ash_meter", "501") is not None:
        return "online"
    if _latest_belt_ash(store, "501") is not None:
        return "online"
    return "none"


def resolve_total_ash_ex(store) -> dict:
    """总灰分 + 来源层级（'manual'|'formula'|'entry'|'ash501'|'none'）。

    拆出来是为了让调用方知道值是怎么来的：'ash501' 且 ash501_layer=='none' 时
    这个数是常量，不能当实测用（见 ash501_layer 说明）。
    """
    cfg = (store.get("ashInputs") or {}).get("totalAsh") or {}
    manual = cfg.get("manual")
    if _is_num(manual) and manual >= 0:
        return {"value": manual, "source": "manual"}
    formula = formula_total_ash(store)
    if formula is not None:
        return {"value": formula, "source": "formula"}
    entry = cfg.get("entry")
    if _is_num(entry) and entry >= 0:
        return {"value": entry, "source": "entry"}
    # 2026-09 工艺确认:501 皮带承载的就是总精煤混配(重介+浮精+粗),
    # 其灰分仪读数即在线总灰分直读;502(重介组分)不再混入平均(否则重介被重复计入)。
    a501 = resolve_instrument(store, "ash_501")
    if a501 is None:
        return {"value": None, "source": "none"}
    return {"value": round(a501, 4), "source": "ash501"}


def resolve_total_ash(store):
    return resolve_total_ash_ex(store)["value"]


def resolve_density(store):
    return resolve_instrument(store, "density")
