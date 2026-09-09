"""DB → 前端 store 结构 桥接（供 resolver / dashboard 复用）。

把 SQLAlchemy 各表反向还原为前端 App.store 的字典结构，使 services.resolvers /
services.density 等纯函数能直接消费。
"""
import json

from ..models import (
    AutoState, CalcLog, CoalRecord, CoarseModel, Setting,
)

# settings 表 key → store 字段
SETTING_TO_STORE = {
    "ash_target": "ashTarget",
    "ash_target_tol": "ashTargetTol",
    "guide_scheme": "guideScheme",
    "total_ash_manual_on": "totalAshManualOn",
    "density_guide": "densityGuide",
    "density_auto_on": "densityAutoOn",
    "coarse_tolerance": "coarseTolerance",
    "coarse_train_range": "coarseTrainRange",
}

# auto_state 表 key → store 顶层字段（原样）
AUTO_STATE_KEYS = [
    "amountInputs", "ashInputs", "instrumentInputs",
    "heavyAshInput", "coarseAshInput", "floatAshInput",
    "coarseCalc", "coarseAshEma", "autoState", "heavyAshBackcalc",
]


def _coarse_record_to_store(r: CoalRecord) -> dict:
    return {
        "timestamp": r.ts, "system": r.system, "source": r.source,
        "ash_content": r.ash_content, "coal_amount": r.coal_amount, "level": r.level,
        "raw_ash": r.raw_ash, "moisture": r.moisture,
        "sysA": r.sysA, "sysB": r.sysB, "sys401": r.sys401, "sys402": r.sys402,
        "desliming473": r.desliming473, "desliming474": r.desliming474,
        "is_stoppage": r.is_stoppage, "mining_face": r.mining_face,
        "influence_value": r.influence_value, "predicted_ash": r.predicted_ash,
    }


def _float_record_to_store(r: CoalRecord) -> dict:
    return {
        "timestamp": r.ts, "system": r.system, "source": r.source,
        "ash_content": r.ash_content, "coal_amount": r.coal_amount,
        "filter_press_running": r.filter_press_running,
        "influence_value": r.influence_value, "annotation": r.annotation,
    }


def load_store(db) -> dict:
    store: dict = {
        "coarseCoal": [], "floatCoal": [], "calcLogs": [], "magneticTail": [],
        "amountInputs": {}, "ashInputs": {}, "instrumentInputs": {},
        "heavyAshInput": {}, "coarseAshInput": {}, "floatAshInput": {},
        "coarseCalc": {}, "coarseAshEma": None, "autoState": {}, "coarseModel": None,
    }
    # settings → 单值
    for s in db.query(Setting).all():
        sk = SETTING_TO_STORE.get(s.key)
        if sk:
            store[sk] = s.value
    # auto_state → 对象键
    for a in db.query(AutoState).all():
        if a.key in AUTO_STATE_KEYS:
            store[a.key] = a.value
    # coal_records → 三表
    for r in db.query(CoalRecord).filter(CoalRecord.category == "coarse"):
        store["coarseCoal"].append(_coarse_record_to_store(r))
        store["magneticTail"].append({"timestamp": r.ts, "level": r.level,
                                      "ash_content": r.ash_content, "coal_amount": r.coal_amount})
    for r in db.query(CoalRecord).filter(CoalRecord.category == "float"):
        store["floatCoal"].append(_float_record_to_store(r))
    for r in db.query(CoalRecord).filter(CoalRecord.category == "ash_density"):
        store["calcLogs"].append({
            "timestamp": r.ts, "calc_type": "ash_density",
            "input_json": json.dumps({"system": r.system, "belt": r.belt,
                                      "ash_content": r.ash_content, "density": r.density}, ensure_ascii=False),
            "output_json": "{}",
        })
    # calc_logs（补录）→ calcLogs
    for l in db.query(CalcLog).all():
        store["calcLogs"].append({
            "timestamp": l.ts, "calc_type": l.calc_type,
            "input_json": l.input_json, "output_json": l.output_json,
        })
    # 按时间排序，使 _latest_calc_value 的 reversed() 取到真正的"最新"（时间序）
    store["calcLogs"].sort(key=lambda x: x.get("timestamp") or "")
    # coarse_models → coarseModel
    rows = db.query(CoarseModel).all()
    if rows:
        cm: dict = {"production": "pls"}
        prod = None
        for m in rows:
            node = {
                "type": m.method, "intercept": m.intercept, "coefs": m.coefs,
                "means": m.means, "stds": m.stds, "stdCoef": m.std_coef,
                "metrics": m.metrics, "imputeMeans": m.impute_means,
                "n": m.n, "tolerance": m.tolerance, "range": m.train_range,
            }
            if m.method == "pls":
                node["A"] = m.pls_A
            cm[m.method] = node
            if m.is_current:
                prod = m.method
            cm["trainedAt"] = m.trained_at
            cm["n"] = m.n
            cm["tolerance"] = m.tolerance
            cm["range"] = m.train_range
        cm["production"] = prod or "pls"
        store["coarseModel"] = cm
    return store
