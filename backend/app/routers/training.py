"""粗精煤泥灰分 DS（原版）/ GPT（按日分组时间验证）训练与持久化。

POST /api/v1/training/coarse-model?range=jun_jul&engine=ds|gpt
- 从 coal_records(category=coarse) 取训练数据
- DS 使用原版留一验证及时间选型；GPT 使用十因素/专家候选的嵌套时间验证
- 单事务：替换 coarse_models（两行，train_run_id 关联）+ 写 coarse_model_history(detail 全量快照) + 回填 predicted_ash
- 进程内锁串行化，防并发重训竞态
"""
import math
import threading
from datetime import datetime
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..auth import require_write
from ..database import get_db
from ..models import AutoState, CoalRecord, CoarseModel, CoarseModelHistory, Setting
from ..services.coarse_training import train_models
from ..services.modeling import predict_coarse_ash
from ..services.training import MLR_FEATURES, _js_round, train_coarse_model as train_ds
from ..services.state import load_store, state_revision

router = APIRouter(prefix="/training", tags=["模型训练"])

_train_lock = threading.Lock()


def _record_to_dict(r: CoalRecord) -> dict:
    return {
        "timestamp": r.ts,
        "ash_content": r.ash_content,
        "coal_amount": r.coal_amount,
        "level": r.level,
        "raw_ash": r.raw_ash,
        "moisture": r.moisture,
        "sysA": r.sysA, "sysB": r.sysB, "sys401": r.sys401, "sys402": r.sys402,
        "desliming473": r.desliming473, "desliming474": r.desliming474,
        "is_stoppage": r.is_stoppage,
        "mining_face": r.mining_face,
        "influence_value": r.influence_value,
        "predicted_ash": r.predicted_ash,
        "system": r.system, "source": r.source,
    }


def _get_setting(db: Session, key: str, default):
    row = db.query(Setting).filter(Setting.key == key).first()
    return row.value if row is not None and row.value is not None else default


@router.post("/coarse-model", dependencies=[Depends(require_write)])
def train_coarse_model(
    train_range: Literal["jun_jul", "30d", "all"] = Query("jun_jul", alias="range"),
    engine: Literal["ds", "gpt"] = Query("ds"),
    expected_revision: str | None = Header(None, alias="X-DMCS-Revision"),
    db: Session = Depends(get_db),
):
    """训练并保存所选版本；两种算法分别逐值对齐前端，保留另一版本。"""
    with _train_lock:
        if db.bind.dialect.name == "sqlite":
            db.execute(text("BEGIN IMMEDIATE"))
        if expected_revision is not None and state_revision(db) != expected_revision:
            raise HTTPException(409, "数据版本已变化，请同步后重新训练")
        # 数据快照在锁内取，避免「旧快照训练后覆盖新数据」的 lost update
        recs = (db.query(CoalRecord)
                .filter(CoalRecord.category == "coarse")
                .order_by(CoalRecord.ts, CoalRecord.id).all())
        if not recs:
            raise HTTPException(422, "无粗精煤泥(coarse)记录，无法训练")

        store_records = [_record_to_dict(r) for r in recs]
        tol_raw = _get_setting(db, "coarse_tolerance", 0.8)
        try:
            tol = float(tol_raw)
        except (TypeError, ValueError):
            raise HTTPException(422, f"coarse_tolerance 非法: {tol_raw!r}")
        if not math.isfinite(tol) or tol <= 0:
            raise HTTPException(422, "coarse_tolerance 必须为有限正数")

        result = (train_models if engine == "gpt" else train_ds)(store_records, train_range, tol)
        if result is None:
            raise HTTPException(422, "训练数据不足：DS需至少12条有效采样；GPT还需覆盖8个日期")

        trained_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        run_id = uuid4().hex

        # 两个版本分别保留；coarse_models 两行仍是当前启用版本，兼容取值链。
        before = load_store(db)
        variants = dict(before.get("coarseModelVariants") or {})
        previous = before.get("coarseModel")
        if previous:
            old_engine = "gpt" if (previous.get("mlr", {}).get("metrics") or {}).get("version") == 2 else "ds"
            saved = variants.get(old_engine)
            same = saved and all(saved.get(k) == previous.get(k) for k in
                                 ("trainedAt", "n", "range", "tolerance", "production"))
            same = same and all(saved[m].get(k) == previous[m].get(k)
                                for m in ("mlr", "pls") for k in ("intercept", "coefs", "imputeMeans"))
            if not same:  # 兼容表没有 lambda 等完整字段，同一模型保留已有快照。
                variants[old_engine] = previous
        coarse_model = {
            "mlr": {k: v for k, v in result["mlr"].items() if k != "yhat"},
            "pls": {k: v for k, v in result["pls"].items() if k != "yhat"},
            "production": result["production"], "trainedAt": trained_at, "engine": engine,
            "n": result["n"], "tolerance": result["tolerance"], "range": result["range"],
        }
        variants[engine] = coarse_model
        bank = db.query(AutoState).filter(AutoState.key == "coarseModelVariants").first()
        if bank is None:
            db.add(AutoState(key="coarseModelVariants", value=variants))
        else:
            bank.value = variants

        # 回填 predicted_ash（全部 coarse 记录，round4，与前端同口径）
        for r, d in zip(recs, store_records):
            r.predicted_ash = _js_round(predict_coarse_ash(d, result[result["production"]]), 4)

        # 替换当前模型（mlr + pls 两行，production 标 is_current，train_run_id 关联）
        db.query(CoarseModel).delete()
        for method in ("mlr", "pls"):
            m = result[method]
            metrics = dict(m.get("metrics") or {})
            metrics["drop"] = m.get("drop")           # 持久化被剔除特征列
            db.add(CoarseModel(
                method=method, is_current=(method == result["production"]),
                train_run_id=run_id, feature_names=MLR_FEATURES,
                intercept=m.get("intercept") if m.get("intercept") is not None else 0.0,
                coefs=m.get("coefs"), means=m.get("means"), stds=m.get("stds"),
                std_coef=m.get("stdCoef"), metrics=metrics,
                impute_means=m.get("imputeMeans"),
                pls_A=m.get("A") if method == "pls" else None,
                n=m.get("n") if m.get("n") is not None else result.get("n") or 0,
                tolerance=result.get("tolerance", 0.8),
                train_range=result.get("range") or train_range,
                trained_at=trained_at,
            ))

        h = result["history"]
        h["engine"] = engine
        detail = dict(h)
        detail["lambda"] = result["mlr"]["lambda"]
        detail["drop"] = result["mlr"]["drop"]
        db.add(CoarseModelHistory(
            train_run_id=run_id, trained_at=trained_at, n=h["n"], tolerance=h["tolerance"],
            train_range=h["range"], production=h["production"],
            mlr_r2=h["mlr"]["r2"], pls_r2=h["pls"]["r2"],
            mlr_q2=h["mlr"]["q2"], pls_q2=h["pls"]["q2"],
            pass_rate=h[h["production"]]["passRate"], detail=detail,
        ))
        db.flush()
        revision = state_revision(db)
        db.commit()

    return {
        "ok": True, "revision": revision, "production": result["production"], "n": result["n"],
        "range": result["range"], "tolerance": tol, "trainedAt": trained_at,
        "trainRunId": run_id,
        "coarseModel": coarse_model,
        "engine": engine, "coarseModelVariants": variants,
        "history": {**h, "trainedAt": trained_at},
        "mlr": {"r2": result["mlr"]["metrics"]["r2"], "q2": result["mlr"]["metrics"]["q2"],
                "q2Time": result["mlr"]["metrics"]["q2Time"], "lambda": result["mlr"]["lambda"],
                "rmse": result["mlr"]["metrics"]["rmse"], "mae": result["mlr"]["metrics"]["mae"]},
        "pls": {"r2": result["pls"]["metrics"]["r2"], "q2": result["pls"]["metrics"]["q2"],
                "q2Time": result["pls"]["metrics"]["q2Time"], "A": result["pls"]["A"],
                "rmse": result["pls"]["metrics"]["rmse"], "mae": result["pls"]["metrics"]["mae"]},
    }
