"""服务端统一密度决策入口；总览和助手共用取值、来源与动作保护。

时间戳沿用前端毫秒值。驱动键与 App._densityDriveKey 保持一致，
本函数不修改 store，也不推进动作状态。
"""
import math
import time
from datetime import datetime

from . import resolvers as R
from .density import DENSITY_GUIDE, compute_density_guidance
from .density_model import fit_density_gain

DENSITY_DWELL_MS = 30 * 60_000
PLACEHOLDER_STALE_MS = 24 * 3600_000


def density_guard_state(store, actual_total, heavy_ash, scheme, now_ms=None):
    now = time.time() * 1000 if now_ms is None else now_ms
    total_manual = (store.get("ashInputs") or {}).get("totalAsh") or {}
    heavy_manual = store.get("heavyAshInput") or {}
    heavy_is_manual = R.heavy_ash_source(store) == "manual"
    target = store.get("ashTarget", 8.50)
    cfg = heavy_manual if scheme == "heavy" else total_manual
    manual_on = heavy_is_manual if scheme == "heavy" else bool(store.get("totalAshManualOn"))
    value = cfg.get("manual")
    placeholder = bool(manual_on and R._is_num(value) and abs(value - target) < 1e-9
                       and (not cfg.get("manualAt") or now - cfg["manualAt"] > PLACEHOLDER_STALE_MS))

    stamp, source = 0, "calc"
    if scheme == "heavy" and heavy_is_manual:
        source, stamp = "heavyManual", heavy_manual.get("manualAt") or 0
    elif scheme != "heavy" and store.get("totalAshManualOn") and R._is_num(total_manual.get("manual")):
        source, stamp = "totalManual", total_manual.get("manualAt") or 0
    if not stamp:
        ts = max((r.get("timestamp") or "" for r in store.get("coarseCoal") or []), default="")
        if ts:
            try:
                stamp = int(datetime.fromisoformat(ts).timestamp() * 1000)
            except (ValueError, OverflowError, OSError):
                stamp = 0
    if isinstance(stamp, float) and stamp.is_integer():
        stamp = int(stamp)
    driver = heavy_ash if scheme == "heavy" else actual_total
    value_key = "na" if driver is None else f"{driver:.3f}"
    drive_key = f"{scheme}|{source}|{stamp}|{value_key}"

    epochs = [0]
    if store.get("totalAshManualOn") and R._is_num(total_manual.get("manual")):
        epochs.append(total_manual.get("manualAt") or 0)
    if heavy_is_manual:
        epochs.append(heavy_manual.get("manualAt") or 0)
    for name, key in (("coarseAshInput", "coarseAsh"), ("floatAshInput", "floatAsh")):
        manual = store.get(name) or {}
        if R.manual_valid(store, manual, key):
            epochs.append(manual.get("manualAt") or 0)
    lab_epoch = max(v for v in epochs if R._is_num(v))
    last_move = store.get("densityLastMoveAt") or 0
    latch = store.get("densityActionLatch") or {}
    newer = bool(last_move and lab_epoch > last_move)
    since_move = math.floor((now - last_move) / 60_000 + 0.5) if last_move else None
    hold, reason = False, ""
    if latch.get("key") == drive_key:
        hold = True
        reason = ("已按当前这份数据调整过密度（同一份化验只动作一次）；"
                  "请等新的化验/在线数据后再看下一步建议")
    elif last_move and now - last_move < DENSITY_DWELL_MS and not newer:
        hold = True
        reason = (f"刚调整过密度（{since_move} 分钟前），此后还没有新的化验/在线数据 —— "
                  "过程到位需要时间，等新数据来了再动下一步（最长等 30 分钟）")
    return {"placeholder": placeholder, "hold": hold, "holdReason": reason,
            "driveKey": drive_key, "dataIsNewer": newer, "sinceMoveMin": since_move,
            "labEpoch": lab_epoch}


def build_density_guidance(store, now_ms=None):
    """返回 (建议, K 拟合信息)，调用方不能省略来源或重介方案所需参数。"""
    scheme = "heavy" if store.get("guideScheme") == "heavy" else "total"
    total = R.resolve_total_ash_ex(store)
    heavy = R.get_heavy_ash(store)
    guard = density_guard_state(store, total["value"], heavy, scheme, now_ms)
    max_step = (store.get("densityGuide") or {}).get("maxStep")
    if not R._is_num(max_step) or max_step <= 0:
        max_step = DENSITY_GUIDE["maxStep"]
    state = {
        "rho_cur": R.resolve_density(store), "heavy_ash": heavy,
        "actual_total": total["value"], "scheme": scheme,
        "tol": store.get("ashTargetTol", 0.1), "max_step": max_step,
        "actual_total_is_constant": total["source"] == "ash501" and R.ash501_layer(store) == "none",
        "placeholder_manual": guard["placeholder"], "hold": guard["hold"],
        "hold_reason": guard["holdReason"], "drive_key": guard["driveKey"],
    }
    if scheme == "heavy":
        state.update(heavy_amt=R.resolve_amount(store, "denseAmount"),
                     float_amt=R.resolve_amount(store, "floatAmount"),
                     coarse_amt=R.resolve_amount(store, "coarseAmount"),
                     float_ash=R.resolve_float_ash(store), coarse_ash=R.resolve_coarse_ash(store))
    density_k = fit_density_gain(store)
    if density_k["valid"]:
        state["k"] = density_k["k"]
    guide = compute_density_guidance(state, store.get("ashTarget", 8.50))
    return guide, density_k
