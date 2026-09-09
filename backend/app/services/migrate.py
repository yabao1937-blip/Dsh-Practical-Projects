"""localStorage dmcs_store JSON → 数据库迁移（P1）。

无损映射前端 App.store 到 11 张表。preview() 只规划不写库；apply() 写库。
"""
import json
from typing import Any

from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models import (
    Alert, AutoState, CalcLog, CoalRecord, CoarseModel, CoarseModelHistory,
    HeavySample, ImportLog, ManualEntry, RegressionModel, Setting,
)

# 单值标量 → settings 表
SETTING_KEYS = {
    "ashTarget": "ash_target",
    "ashTargetTol": "ash_target_tol",
    "guideScheme": "guide_scheme",
    "totalAshManualOn": "total_ash_manual_on",
    "densityGuide": "density_guide",
    "densityAutoOn": "density_auto_on",
    "coarseTolerance": "coarse_tolerance",
    "coarseTrainRange": "coarse_train_range",
}

# 对象键 → auto_state 表（完整 JSON 无损存储）
AUTO_STATE_KEYS = [
    "amountInputs", "ashInputs", "instrumentInputs",
    "heavyAshInput", "coarseAshInput", "floatAshInput",
    "coarseCalc", "coarseAshEma", "autoState", "heavyAshBackcalc",
]


def _j(v: Any) -> str:
    """安全序列化（calcLogs 的 input_json/output_json 存字符串）"""
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False)


def _plan(store: dict) -> dict:
    """把 store 规划为 {表名: [记录 dict]}，不写库。"""
    plan: dict[str, list[dict]] = {t: [] for t in (
        "coal_records", "calc_logs", "manual_entries", "heavy_samples",
        "import_logs", "alerts", "coarse_models", "coarse_model_history",
        "regression_models", "settings", "auto_state",
    )}

    # 表1 粗精煤泥多因素 → coal_records(category=coarse)
    for r in store.get("coarseCoal") or []:
        plan["coal_records"].append(dict(
            category="coarse", ts=r.get("timestamp"), system=r.get("system") or "default",
            source=r.get("source") or "import", ash_content=r.get("ash_content"),
            coal_amount=r.get("coal_amount"), level=r.get("level"),
            raw_ash=r.get("raw_ash"), moisture=r.get("moisture"),
            sysA=r.get("sysA") or 0, sysB=r.get("sysB") or 0,
            sys401=r.get("sys401") or 0, sys402=r.get("sys402") or 0,
            desliming473=r.get("desliming473") or 0, desliming474=r.get("desliming474") or 0,
            is_stoppage=r.get("is_stoppage") or 0, mining_face=r.get("mining_face"),
            influence_value=r.get("influence_value"), predicted_ash=r.get("predicted_ash"),
        ))

    # 表2 浮精 → coal_records(category=float)
    for r in store.get("floatCoal") or []:
        plan["coal_records"].append(dict(
            category="float", ts=r.get("timestamp"), system=r.get("system") or "default",
            source=r.get("source") or "import", ash_content=r.get("ash_content"),
            coal_amount=r.get("coal_amount"), filter_press_running=r.get("filter_press_running"),
            influence_value=r.get("influence_value"), annotation=r.get("annotation"),
        ))

    # 表3 calcLogs → 拆分：ash_density 进 coal_records；其余补录进 calc_logs
    for r in store.get("calcLogs") or []:
        ct = r.get("calc_type")
        raw = r.get("input_json") or "{}"
        try:
            inp = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception:
            inp = {}
        if ct == "ash_density":
            plan["coal_records"].append(dict(
                category="ash_density", ts=r.get("timestamp"),
                system=inp.get("system") or "default", source="import",
                belt=inp.get("belt"), ash_content=inp.get("ash_content"),
                density=inp.get("density"),
            ))
        else:
            plan["calc_logs"].append(dict(
                ts=r.get("timestamp"), calc_type=ct or "unknown",
                system=inp.get("system") or "default",
                input_json=_j(raw), output_json=_j(r.get("output_json") or "{}"),
            ))

    # 手工补录历史
    for r in store.get("manualEntries") or []:
        plan["manual_entries"].append(dict(
            ts=r.get("timestamp"), category=r.get("category"), values=r.get("values"),
            remark=r.get("remark"), operator=r.get("operator"), status=r.get("status"),
        ))

    # 重介采样
    for r in store.get("heavySamples") or []:
        plan["heavy_samples"].append(dict(
            ts=r.get("timestamp"), rho=r.get("rho"), ash_content=r.get("ash_content"),
            source=r.get("source") or "采样",
        ))

    # 导入日志
    for r in store.get("importLogs") or []:
        plan["import_logs"].append(dict(
            ts=r.get("timestamp"), category=r.get("category"), filename=r.get("fileName"),
            total=r.get("total") or 0, success=r.get("success") or 0,
            failed=r.get("failed") or 0, skipped=r.get("skipped") or 0,
            status=r.get("status"), errors=r.get("errors"),
        ))

    # 告警
    for r in store.get("alerts") or []:
        plan["alerts"].append(dict(
            system=r.get("system") or "合并", level=r.get("level") or "提示",
            message=r.get("message"), ts=r.get("time") or "",
        ))

    # 单因素模型
    for r in store.get("regressionModels") or []:
        plan["regression_models"].append(dict(
            model_type=r.get("model_type"), params=r.get("params"),
            r_squared=r.get("r_squared") or 0.0, rmse=r.get("rmse") or 0.0,
            data_points=r.get("data_points") or 0, created_at=r.get("created_at") or "",
        ))

    # 粗灰模型（mlr + pls 两行，production 标记 is_current）
    cm = store.get("coarseModel")
    if cm:
        prod = cm.get("production") or "pls"
        for method in ("mlr", "pls"):
            m = cm.get(method)
            if not m:
                continue
            metrics = m.get("metrics") or {}
            plan["coarse_models"].append(dict(
                method=method, is_current=(method == prod),
                feature_names=cm.get("featureNames"),  # 若存在则留；否则 None
                intercept=m.get("intercept") or 0.0,
                coefs=m.get("coefs"), means=m.get("means"), stds=m.get("stds"),
                std_coef=m.get("stdCoef"), metrics=metrics,
                impute_means=m.get("imputeMeans"),
                pls_A=m.get("A") if method == "pls" else None,
                n=m.get("n") or cm.get("n") or 0,
                tolerance=cm.get("tolerance", 0.8),
                train_range=cm.get("range") or store.get("coarseTrainRange") or "jun_jul",
                trained_at=cm.get("trainedAt") or "",
            ))

    # 重训练历史
    for r in store.get("coarseModelHistory") or []:
        plan["coarse_model_history"].append(dict(
            trained_at=r.get("trainedAt") or "", n=r.get("n") or 0,
            tolerance=r.get("tolerance", 0.8), train_range=r.get("range") or "jun_jul",
            production=r.get("production") or "pls",
            mlr_r2=(r.get("mlr") or {}).get("r2"), pls_r2=(r.get("pls") or {}).get("r2"),
            mlr_q2=(r.get("mlr") or {}).get("q2"), pls_q2=(r.get("pls") or {}).get("q2"),
            pass_rate=None,
        ))

    # 单值 → settings
    for sk, dbk in SETTING_KEYS.items():
        if sk in store:
            plan["settings"].append(dict(key=dbk, value=store.get(sk),
                                         value_type=_type_of(store.get(sk))))

    # 对象键 → auto_state（完整 JSON）
    for k in AUTO_STATE_KEYS:
        if k in store:
            plan["auto_state"].append(dict(key=k, value=store.get(k)))

    return plan


def _type_of(v: Any) -> str:
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    return "json"


def preview(store: dict) -> dict:
    """dry-run：返回各表规划条数 + 未识别键，不写库。"""
    p = _plan(store)
    known = set(SETTING_KEYS) | set(AUTO_STATE_KEYS) | {
        "coarseCoal", "floatCoal", "calcLogs", "manualEntries", "heavySamples",
        "importLogs", "alerts", "regressionModels", "coarseModelHistory", "coarseModel",
        "magneticTail", "rawSlime", "__merged",
    }
    unmapped = [k for k in store if k not in known]
    return {
        "counts": {t: len(p[t]) for t in p},
        "unmapped_keys": unmapped,
    }


def _bulk(session: Session, model: type, rows: list[dict]) -> None:
    if rows:
        session.add_all([model(**r) for r in rows])


def _apply_in_session(db: Session, p: dict) -> None:
    """在给定会话内写入全部规划结果（不 commit，由调用方决定）。"""
    _bulk(db, CoalRecord, p["coal_records"])
    _bulk(db, CalcLog, p["calc_logs"])
    _bulk(db, ManualEntry, p["manual_entries"])
    _bulk(db, HeavySample, p["heavy_samples"])
    _bulk(db, ImportLog, p["import_logs"])
    _bulk(db, Alert, p["alerts"])
    _bulk(db, CoarseModel, p["coarse_models"])
    _bulk(db, CoarseModelHistory, p["coarse_model_history"])
    _bulk(db, RegressionModel, p["regression_models"])
    for s in p["settings"]:
        db.merge(Setting(**s))
    for a in p["auto_state"]:
        db.merge(AutoState(**a))


def apply(store: dict) -> dict:
    """写库并返回报告。"""
    p = _plan(store)
    db: Session = SessionLocal()
    try:
        _apply_in_session(db, p)
        db.commit()
        return {"counts": {t: len(p[t]) for t in p}, "ok": True}
    except Exception as e:
        db.rollback()
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


def replace(store: dict) -> dict:
    """清空业务表后整体重写（PUT /state 整库快照用）。

    清表与写入在同一事务内：若写入失败回滚，清表也一并回滚，不会清库后丢数据。
    """
    p = _plan(store)
    db: Session = SessionLocal()
    try:
        for t in (CoalRecord, CalcLog, CoarseModelHistory, RegressionModel, HeavySample,
                  ManualEntry, Alert, ImportLog, CoarseModel, Setting, AutoState):
            db.query(t).delete()
        _apply_in_session(db, p)
        db.commit()
        return {"counts": {t: len(p[t]) for t in p}, "ok": True}
    except Exception as e:
        db.rollback()
        return {"ok": False, "error": str(e)}
    finally:
        db.close()
