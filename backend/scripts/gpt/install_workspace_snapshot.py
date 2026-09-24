"""将重训结果装入本 GPT 工作区的全新数据库；拒绝覆盖已有数据库。"""
import argparse
import copy
import json
import math
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))


def validate_bundle(records, bundle):
    """在打开数据库前核对记录与模型，防止目录内文件来自不同批次。"""
    from scripts.gpt.retrain_from_xlsx import digest
    from app.services.coarse_training import aggregate_daily, prepare_rows
    from app.services.modeling import MLR_FEATURES, predict_coarse_ash
    from app.services.training import _metrics

    clean = prepare_rows(records)
    audit = bundle["audit"]
    if len(clean) != len(records) or digest(clean) != audit["sha256"]:
        raise ValueError("训练记录哈希或有效记录数不匹配")
    daily = aggregate_daily(clean)
    if audit["totalN"] != len(clean) or audit["totalDays"] != len(daily):
        raise ValueError("训练记录数量与审计不匹配")
    for mode, rows in (("sample", clean), ("daily", daily)):
        model = bundle["models"][mode]
        if model["engine"] != "gpt" or model["n"] != len(rows) or model["range"] != "all":
            raise ValueError(f"{mode} 模型身份或训练范围不匹配")
        if model["production"] not in ("mlr", "pls") or model["tolerance"] != .8:
            raise ValueError(f"{mode} 模型选型或容差无效")
        for kind in ("mlr", "pls"):
            node = model[kind]
            if (node["feature_names"] != MLR_FEATURES or node["inputPolicy"] != "clip-training-range"
                    or node["metrics"]["trainingRevision"] != bundle["revision"]):
                raise ValueError(f"{mode}/{kind} 特征或模型版本不匹配")
            if not math.isfinite(node["intercept"]):
                raise ValueError("截距非有限数")
            for field in ("coefs", "means", "stds", "imputeMeans"):
                values = node[field]
                if len(values) != len(MLR_FEATURES) or not all(math.isfinite(v) for v in values):
                    raise ValueError(f"{mode}/{kind}/{field} 参数无效")
            bounds = node["inputBounds"]
            if (len(bounds) != len(MLR_FEATURES) or any(len(b) != 2 or not all(math.isfinite(v) for v in b)
                    or b[0] > b[1] for b in bounds) or bounds != node["metrics"]["inputBounds"]):
                raise ValueError(f"{mode}/{kind} 输入边界无效")
            actual = _metrics([r["ash_content"] for r in rows],
                              [predict_coarse_ash(r, node) for r in rows], 0, .8)
            if any(not math.isclose(actual[k], node["metrics"][k], abs_tol=1e-8)
                   for k in ("r2", "mae", "rmse", "passRate")):
                raise ValueError(f"{mode}/{kind} 参数预测与训练指标不一致")


def install(baseline_state, model_directory):
    destination = ROOT / "backend/data/dense_medium.db"
    if destination.exists():
        raise FileExistsError(f"拒绝覆盖已有数据库：{destination}")
    state = json.loads(baseline_state.read_text(encoding="utf-8"))
    bundle = json.loads((model_directory / "models.json").read_text(encoding="utf-8"))
    records = json.loads((model_directory / "records.json").read_text(encoding="utf-8"))
    validate_bundle(records, bundle)
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.database import Base
    from app.models import CoarseModelHistory
    from app.services import migrate
    from app.services.modeling import MLR_FEATURES, predict_coarse_ash
    from app.services.state import load_store
    from app.services.training import _js_round
    # 独立连接，不能复用其他已导入模块的全局 engine / SessionLocal。
    engine = create_engine("sqlite:///" + destination.as_posix())
    session_factory = sessionmaker(bind=engine)
    saved_ds = copy.deepcopy(state["coarseModelVariants"].get("ds"))
    current = bundle["models"]["sample"]
    state["coarseCoal"] = records
    state["coarseModel"] = current
    state["coarseModelVariants"]["gpt"] = current
    state["coarseTrainRange"] = "all"
    state["coarseTolerance"] = .8
    current["featureNames"] = MLR_FEATURES
    active = current[current["production"]]
    for record in records:
        record["predicted_ash"] = _js_round(predict_coarse_ash(record, active), 4)
    state.pop("_revision", None)
    Base.metadata.create_all(bind=engine)
    with session_factory() as session:
        plan = migrate._plan(state)
        run_id = uuid4().hex
        for row in plan["coarse_models"]:
            row["train_run_id"] = run_id
        migrate._apply_in_session(session, plan)
        detail = {"engine": "gpt", "n": current["n"], "range": "all", "tolerance": .8,
                  "production": current["production"],
                  **{kind: current[kind]["metrics"] for kind in ("mlr", "pls")}}
        session.add(CoarseModelHistory(
            train_run_id=run_id, trained_at=current["trainedAt"], n=current["n"],
            tolerance=.8, train_range="all", production=current["production"],
            mlr_r2=current["mlr"]["metrics"]["r2"], pls_r2=current["pls"]["metrics"]["r2"],
            mlr_q2=current["mlr"]["metrics"]["q2"], pls_q2=current["pls"]["metrics"]["q2"],
            pass_rate=active["metrics"]["passRate"], detail=detail))
        session.flush()
        loaded = load_store(session)
        if loaded["coarseModelVariants"].get("ds") != saved_ds:
            raise ValueError("DS 快照发生变化，回滚安装")
        if len(loaded["coarseCoal"]) != bundle["audit"]["totalN"]:
            raise ValueError("落库记录数量不符，回滚安装")
        restored = loaded["coarseModel"][current["production"]]
        if restored["inputBounds"] != active["inputBounds"]:
            raise ValueError("落库输入边界不符，回滚安装")
        for record in records:
            if abs(predict_coarse_ash(record, restored) - predict_coarse_ash(record, active)) >= 1e-9:
                raise ValueError("落库预测不一致，回滚安装")
        session.commit()
    print(json.dumps({"database": str(destination), "n": current["n"], "engine": "gpt",
                      "production": current["production"], "dsSnapshotUnchanged": True}, ensure_ascii=False))
    engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-state", type=Path, required=True)
    parser.add_argument("--model-directory", type=Path, required=True)
    args = parser.parse_args()
    install(args.baseline_state, args.model_directory)
