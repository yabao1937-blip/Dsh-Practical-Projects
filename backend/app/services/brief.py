"""推测简报：三表 1h 对齐 + 递归软测量（逐值对齐前端 buildHourlyBrief）。

时间戳用字符串 "YYYY-MM-DD HH:MM:SS"，字符串序 == 时间序（中国无夏令时，
与前端 new Date().getTime() 的 UTC 整点切桶 + 本地显示等价）。
"""
import math
from datetime import datetime, timedelta

from .density import DENSITY_GUIDE, calc_total_ash, expert_adjust
from .modeling import predict_coarse_ash
from .resolvers import get_heavy_ash

BRIEF_HEADERS = ['时间',
                 '501皮带秤(t/h)', '502皮带秤(t/h)', '粗精煤泥量(t/h)', '重介精煤灰分(%)', '总精煤量(t/h)',
                 '501皮带灰分仪(%)', '502皮带灰分仪(%)', '精磁尾液位(%)', '浮精灰分(%)', '浮精量(t/h)', '总精煤灰分(%)',
                 '建议密度(g/cm³)', '实测密度(g/cm³)', '预测粗精煤泥灰分(%)', '实测粗精煤泥灰分(%)']

COARSE_AMT = 40
SCALE_501 = 268.5
SCALE_502 = 235.2
TOTAL_AMT = round(SCALE_501 + SCALE_502, 1)  # 503.7
DEF = {"ash501": 8.52, "ash502": 10.68, "density": 1.450, "level": 55, "floatAsh": 9.85}


def _hour_of(ts: str) -> str:
    return ts[:13] + ":00"


def _next_hour(h: str) -> str:
    return (datetime.strptime(h, "%Y-%m-%d %H:%M") + timedelta(hours=1)).strftime("%Y-%m-%d %H:00")


def _fmt(v, n):
    if v is None:
        return ""
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return ""
    return f"{float(v):.{n}f}"


def _num(v):
    return v if isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v)) else None


def build_hourly_brief(store: dict) -> dict:
    tol = store.get("ashTargetTol", 0.1)
    target = store.get("ashTarget", 8.50)
    scheme = "heavy" if store.get("guideScheme") == "heavy" else "total"
    heavy_ash = get_heavy_ash(store)

    # 表1/表2/表3 记录按时间字符串排序（== 时间序）
    coarse_recs = sorted((r for r in (store.get("coarseCoal") or []) if r.get("timestamp")),
                         key=lambda r: r["timestamp"])
    float_recs = sorted((r for r in (store.get("floatCoal") or []) if r.get("timestamp")),
                        key=lambda r: r["timestamp"])
    ad_recs = []
    for l in store.get("calcLogs") or []:
        if l.get("calc_type") != "ash_density":
            continue
        t = l.get("timestamp")
        if not t:
            continue
        try:
            import json
            v = json.loads(l.get("input_json") or "{}")
        except Exception:
            continue
        d = v.get("density")
        density = d if (isinstance(d, (int, float)) and 1.3 <= d <= 1.6) else None
        a = v.get("ash_content")
        ash = a if (isinstance(a, (int, float))) else None
        ad_recs.append({"ts": t, "belt": str(v.get("belt") or ""), "ash": ash, "density": density})
    ad_recs.sort(key=lambda r: r["ts"])

    if not coarse_recs and not float_recs and not ad_recs:
        return {"headers": BRIEF_HEADERS, "rows": []}

    all_ts = [r["timestamp"] for r in coarse_recs] + [r["timestamp"] for r in float_recs] + [r["ts"] for r in ad_recs]
    h_min = _hour_of(min(all_ts))
    h_max = _hour_of(max(all_ts))

    hours = []
    h = h_min
    while True:
        hours.append(h)
        if h == h_max:
            break
        h = _next_hour(h)

    i_coarse = i_float = i_ad = 0
    cur_coarse = cur_float = None
    prev_coarse = None
    est_coarse = None
    prev_forecast = None
    cur_ash501 = cur_ash502 = cur_density = None

    # 模型：生产模型
    cm = store.get("coarseModel") or {}
    prod = cm.get("production") or "pls"
    model = cm.get(prod) or {}

    rows = []
    for hh in hours:
        h_end = _next_hour(hh)
        while i_coarse < len(coarse_recs) and coarse_recs[i_coarse]["timestamp"] < h_end:
            cur_coarse = coarse_recs[i_coarse]
            i_coarse += 1
        while i_float < len(float_recs) and float_recs[i_float]["timestamp"] < h_end:
            cur_float = float_recs[i_float]
            i_float += 1
        density_before = cur_density
        while i_ad < len(ad_recs) and ad_recs[i_ad]["ts"] < h_end:
            a = ad_recs[i_ad]
            if a["belt"] == "501" and a["ash"] is not None:
                cur_ash501 = a["ash"]
            if a["belt"] == "502" and a["ash"] is not None:
                cur_ash502 = a["ash"]
            if a["density"] is not None:
                cur_density = a["density"]
            i_ad += 1
        density_measured = cur_density if (cur_density is not None and cur_density != density_before) else None

        level = _num(cur_coarse.get("level")) if cur_coarse else None
        level = level if level is not None else DEF["level"]
        float_ash = _num(cur_float.get("ash_content")) if cur_float else None
        float_ash = float_ash if float_ash is not None else DEF["floatAsh"]
        float_amt = _num(cur_float.get("coal_amount")) if cur_float else None
        ash501 = cur_ash501 if cur_ash501 is not None else DEF["ash501"]
        ash502 = cur_ash502 if cur_ash502 is not None else DEF["ash502"]
        density = cur_density if cur_density is not None else DEF["density"]

        is_new_sample = cur_coarse is not None and cur_coarse is not prev_coarse
        coarse_measured = None
        if is_new_sample and cur_coarse is not None:
            cmv = _num(cur_coarse.get("ash_content"))
            if cmv is not None:
                coarse_measured = cmv

        forecast = None
        if cur_coarse is not None:
            p = predict_coarse_ash(cur_coarse, model)
            if p is not None and math.isfinite(p):
                forecast = round(p, 2)

        if is_new_sample and coarse_measured is not None:
            est_coarse = round(coarse_measured, 2)
        elif est_coarse is not None and forecast is not None and prev_forecast is not None:
            est_coarse = round(est_coarse + (forecast - prev_forecast), 2)
        coarse_model = est_coarse

        heavy_amt = round(TOTAL_AMT - float_amt - COARSE_AMT, 1) if float_amt is not None else None
        formula_ok = (heavy_amt is not None and heavy_amt > 0 and float_amt is not None
                      and float_ash is not None and coarse_model is not None)

        if formula_ok:
            total_ash = round(calc_total_ash(heavy_ash, heavy_amt, float_ash, float_amt, coarse_model, COARSE_AMT), 2)
        else:
            total_ash = round((ash501 * SCALE_501 + ash502 * SCALE_502) / (SCALE_501 + SCALE_502), 2)

        rho_new = None
        if formula_ok:
            if scheme == "heavy":
                total_amt = heavy_amt + float_amt + COARSE_AMT
                target_heavy = round((target * total_amt - float_ash * float_amt - coarse_model * COARSE_AMT) / heavy_amt, 3)
                delta_a_heavy = round(heavy_ash - target_heavy, 3)
                delta_a = round(delta_a_heavy * heavy_amt / total_amt, 3)
                if abs(delta_a) <= tol:
                    rho_new = density
                else:
                    rho_new = max(DENSITY_GUIDE["rhoMin"], min(DENSITY_GUIDE["rhoMax"],
                                  round(density - (1.0 if delta_a > 0 else -1.0) * expert_adjust(abs(delta_a)), 3)))
            else:
                delta_a = round(total_ash - target, 3)
                if abs(delta_a) <= tol:
                    rho_new = density
                else:
                    rho_new = max(DENSITY_GUIDE["rhoMin"], min(DENSITY_GUIDE["rhoMax"],
                                  round(density - (1.0 if delta_a > 0 else -1.0) * expert_adjust(abs(delta_a)), 3)))

        rows.append([
            hh,
            _fmt(SCALE_501, 1), _fmt(SCALE_502, 1), _fmt(COARSE_AMT, 1), _fmt(heavy_ash, 2), _fmt(TOTAL_AMT, 1),
            _fmt(ash501, 2), _fmt(ash502, 2), _fmt(level, 1), _fmt(float_ash, 2), _fmt(float_amt, 1), _fmt(total_ash, 2),
            _fmt(rho_new, 3), _fmt(density_measured, 3),
            _fmt(coarse_model, 2), _fmt(coarse_measured, 2),
        ])
        prev_forecast = forecast
        prev_coarse = cur_coarse

    brief_rows = [r for r in rows if r[12] != "" and r[14] != ""]
    return {"headers": BRIEF_HEADERS, "rows": brief_rows}
