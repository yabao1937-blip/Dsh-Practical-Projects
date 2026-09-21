"""在线仪表/量/灰分的取值链解析（逐值对齐前端 App.resolve* 系列）。

state 为 store 字典（来自 seed_store.json 或 DB 快照）。
仪表值只来自录入/测量；密度变化不得改写灰分测量值。
"""
import json
import math
import re

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
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def manual_valid(store, cfg, key):
    """与前端 _manualValid 一致：自动层接管后，旧手动值不再生效。"""
    value = cfg.get("manual")
    if not _is_num(value) or value < 0:
        return False
    auto = (store.get("autoState") or {}).get(key)
    return not auto or (cfg.get("manualAt") or 0) >= (auto.get("t") or 0)


def heavy_ash_source(store):
    cfg = store.get("heavyAshInput") or {}
    usable = store.get("heavyAshManualOn") is not False and manual_valid(store, cfg, "heavyAsh")
    return "manual" if usable else "calc"


def _num(v):
    return v if _is_num(v) and not (isinstance(v, float) and math.isnan(v)) else None


def measurement_number(value):
    """兼容旧补录数字文本，拒绝空值、布尔及非有限值。"""
    if isinstance(value, str):
        value = value.strip()
        if not re.fullmatch(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?", value, flags=re.ASCII):
            return None
        value = float(value)
    return value if _is_num(value) else None


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
    for l in reversed(sorted(store.get("calcLogs") or [], key=lambda r: r.get("timestamp") or "")):
        if l.get("calc_type") != calc_type:
            continue
        try:
            v = json.loads(l.get("input_json") or "{}")
        except Exception:
            continue
        if (belt is None or str(v.get("belt") or "") == str(belt)) and measurement_number(v.get("value")) is not None:
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
    elif inst_id == "density":
        measured = _latest_calc_value(store, "density_meter", None)
        if measured is not None and 1.3 <= measured <= 1.65:
            entry = measured
        for l in reversed(sorted(store.get("calcLogs") or [], key=lambda r: r.get("timestamp") or "")):
            if entry is not None:
                break
            if l.get("calc_type") != "ash_density":
                continue
            try:
                v = json.loads(l.get("input_json") or "{}")
                d = measurement_number(v.get("density"))
                if d is not None and 1.3 <= d <= 1.65:
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
    if manual_valid(store, cfg, inst_id):
        return manual
    if entry is not None:
        return entry
    return INSTRUMENT_DEFAULT.get(inst_id)


def _latest_belt_ash(store, belt):
    for l in reversed(sorted(store.get("calcLogs") or [], key=lambda r: r.get("timestamp") or "")):
        if l.get("calc_type") != "ash_density":
            continue
        try:
            v = json.loads(l.get("input_json") or "{}")
            if str(v.get("belt") or "") == str(belt) and measurement_number(v.get("ash_content")) is not None:
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
    if heavy_ash_source(store) == "manual":
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


def back_calc_heavy_ash(store, total_ash):
    """质量平衡反推展示值，与 App.backCalcHeavyAsh 一致；不拟造调密响应。"""
    if not _is_num(total_ash) or total_ash < 0:
        return None
    heavy_amt = resolve_amount(store, "denseAmount")
    float_amt = resolve_amount(store, "floatAmount")
    coarse_amt = resolve_amount(store, "coarseAmount")
    float_ash = resolve_float_ash(store)
    coarse_ash = resolve_coarse_ash(store)
    if not all(_is_num(v) and v >= 0 for v in
               (heavy_amt, float_amt, coarse_amt, float_ash, coarse_ash)) or heavy_amt <= 0:
        return None
    ash = (total_ash * (heavy_amt + float_amt + coarse_amt)
           - float_ash * float_amt - coarse_ash * coarse_amt) / heavy_amt
    return round(ash, 6) if math.isfinite(ash) else None


def ash501_layer(store) -> str:
    """501 灰分的来源层级：'manual' | 'online' | 'none'。

    'none' = 三层都没有真实数据源，回落到 INSTRUMENT_DEFAULT 常量 8.8。
    此时该值**不得**用于推导"建议密度"，也不应在简报里冒充仪表读数。
    实测依据：真实库 360 条 ash_density 记录全部 belt=502、零条 501，
    即 501 尚无在线数据（PLC 未接入），ash_501 一直是常量。
    """
    cfg = (store.get("instrumentInputs") or {}).get("ash_501") or {}
    if manual_valid(store, cfg, "ash_501"):
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
    manual_on = store.get("totalAshManualOn")
    if manual_on is not False and _is_num(manual) and manual >= 0 and (
            manual_on is True or manual_valid(store, cfg, "totalAsh")):
        return {"value": manual, "source": "manual"}
    formula = formula_total_ash(store)
    if formula is not None:
        return {"value": formula, "source": "formula"}
    entry = cfg.get("entry")
    if _is_num(entry) and entry >= 0:
        return {"value": entry, "source": "entry"}
    entry = _latest_calc_value(store, "ash_meter", "501")
    if entry is None:
        entry = _latest_belt_ash(store, "501")
    if entry is not None:
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
