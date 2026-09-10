"""总览页聚合接口（dashboard / trend）"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..services import resolvers
from ..services.density import compute_density_guidance
from ..services.density_model import fit_density_gain
from ..services.state import load_store

router = APIRouter(prefix="/overview", tags=["总览"])


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db)):
    store = load_store(db)
    target = store.get("ashTarget", 8.50)
    tol = store.get("ashTargetTol", 0.1)
    scheme = store.get("guideScheme", "total")
    heavy_ash = resolvers.get_heavy_ash(store)
    rho_cur = resolvers.resolve_density(store)
    total_ex = resolvers.resolve_total_ash_ex(store)
    actual_total = total_ex["value"]
    # 总灰分若落到"501 常量"（501 未接入、无任何真实数据源）→ 标记为常量，
    # 供 compute_density_guidance 拒绝据此给出密度建议
    total_is_constant = (total_ex["source"] == "ash501"
                         and resolvers.ash501_layer(store) == "none")
    density_k = fit_density_gain(store)          # 数据驱动 K（表3 配对，分系统）

    g_state = {"rho_cur": rho_cur, "heavy_ash": heavy_ash, "actual_total": actual_total,
               "scheme": scheme, "tol": tol, "actual_total_is_constant": total_is_constant}
    if density_k["valid"]:
        g_state["k"] = density_k["k"]
    if scheme == "heavy":
        g_state.update({
            "heavy_amt": resolvers.resolve_amount(store, "denseAmount"),
            "float_amt": resolvers.resolve_amount(store, "floatAmount"),
            "coarse_amt": resolvers.resolve_amount(store, "coarseAmount"),
            "float_ash": resolvers.resolve_float_ash(store),
            "coarse_ash": resolvers.resolve_coarse_ash(store),
        })
    guide = compute_density_guidance(g_state, target)

    return {
        "ashTarget": target,
        "ashTargetTol": tol,
        "guideScheme": scheme,
        "heavyAsh": heavy_ash,
        "density": rho_cur,
        "totalAsh": actual_total,
        "amounts": {
            "denseAmount": resolvers.resolve_amount(store, "denseAmount"),
            "floatAmount": resolvers.resolve_amount(store, "floatAmount"),
            "coarseAmount": resolvers.resolve_amount(store, "coarseAmount"),
            "totalAmount": resolvers.resolve_amount(store, "totalAmount"),
        },
        "floatAsh": resolvers.resolve_float_ash(store),
        "coarseAsh": resolvers.resolve_coarse_ash(store),
        "guidance": guide,
        "densityK": density_k,
    }


@router.get("/trend")
def trend(db: Session = Depends(get_db)):
    """灰分趋势：ash_density 记录的时间序列（供总览页趋势图）"""
    store = load_store(db)
    series = []
    for l in store.get("calcLogs") or []:
        if l.get("calc_type") != "ash_density":
            continue
        try:
            import json as _json
            v = _json.loads(l.get("input_json") or "{}")
            ash = v.get("ash_content")
            if isinstance(ash, (int, float)):
                series.append({"timestamp": l.get("timestamp"), "ash_content": ash})
        except Exception:
            continue
    series.sort(key=lambda x: x["timestamp"])
    return {"series": series}
