"""总览页聚合接口（dashboard / trend）"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..services import resolvers
from ..services.guidance import build_density_guidance
from ..services.state import load_store

router = APIRouter(prefix="/overview", tags=["总览"])


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db)):
    store = load_store(db)
    target = store.get("ashTarget", 8.50)
    tol = store.get("ashTargetTol", 0.1)
    scheme = store.get("guideScheme", "total")
    guide, density_k = build_density_guidance(store)

    return {
        "ashTarget": target,
        "ashTargetTol": tol,
        "guideScheme": scheme,
        "heavyAsh": guide["heavyAsh"],
        "density": guide["rhoCur"],
        "totalAsh": guide["actualTotal"],
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
