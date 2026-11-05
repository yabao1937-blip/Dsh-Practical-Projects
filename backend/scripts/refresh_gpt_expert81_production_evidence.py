"""Refresh mutable presentation metadata from exact production runtime/seal only."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT/"output/gpt-expert81-db-primary-20261103"


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--accepted-seal",action="store_true");args=parser.parse_args(argv)
    freeze=BASE/"production-freeze";evidence=BASE/"comparison-evidence"
    metadata=json.loads((evidence/"metadata.json").read_text(encoding="utf-8"));state=json.loads((freeze/"model.json").read_text(encoding="utf-8"))["3"]
    ids=set(state["normal_training_ids"])
    rows=[json.loads(s) for s in (BASE/"data/training_rows.jsonl").read_text(encoding="utf-8").splitlines()]
    dates=[r["date"] for r in rows if r["record_id"] in ids]
    training_range={"start":min(dates),"end":max(dates)}
    metadata["freeze"].update(model_sha256=sha(freeze/"model.joblib"),freeze_manifest_sha256=sha(freeze/"freeze-manifest.json"),sealed_runtime_manifest_sha256=sha(freeze/"sealed-runtime-manifest.json"))
    metadata["models"]["candidate"]["training_date_range"]=training_range
    metadata["models"]["current"]["training_date_range"]=None
    metadata["models"]["current"]["method"]["by_system"]["3"]={"summary":"177条旧样本：液位经验基线＋其余7因素有限修正，参数冻结","details":["冻结EX8版本20261012，177条原训练样本；不是近期DB重新训练", "液位基线30.5−0.3×液位，经验液位裁剪25–75；其余7因素修正幅度限定±2灰分百分点", "历史①②保留真实legacy；未来③独立EX8_PRODUCTION_RUNS使用真实RUNS、同一冻结参数，不回填旧历史", "原旧表日期含歧义，不虚构完整训练日期范围；未来严格归档重建不冒称当场已发出的生产预测"]}
    estimator=state["model"]["parameters"]
    expected={"model__n_estimators":"80","model__max_depth":"2","model__min_samples_leaf":"5","model__learning_rate":"0.05","model__loss":"'absolute_error'"}
    if len(ids)!=36 or any(estimator.get(k)!=v for k,v in expected.items()):raise ValueError("method description differs from actual frozen parameters/support")
    description={"summary":"36条strict常规DB：液位/煤量动态GBR，四反馈限定已覆盖工况","details":["Gradient Boosting Regression：80棵树，max_depth=2，min_samples_leaf=5，learning_rate=0.05，absolute_error", "实际full模型仅36条strict常规DB；本候选不以分钟近似或旧表训练", "四系统反馈在训练中恒为A0/B1/4011/4021，只作支持工况门槛，不宣称学会四开关变化效应", "未见三系统四反馈组合hard guard拒绝；液位/煤量出训练范围保留预测并标记超支持范围", "110候选共同forward48/16日期全MAE最低；前列差距不稳定，须待冻结后新数据验证"]}
    metadata["models"]["candidate"]["method"]["by_system"]["3"]=description
    metadata["models"]["candidate"]["method"]["summary"]="三系统36条strict常规DB的液位/煤量GBR＋已覆盖四反馈门槛；二系统保留14/15"
    metadata["evaluation_pools"]["history_all"]["candidate_train_date_range"]=training_range
    metadata["evaluation_pools"]["future"].update(baseline_id="EX8_PRODUCTION_RUNS",baseline_label="现用EX8生产RUNS口径（参数冻结）",candidate_baseline_id="EX81_PRODUCTION_CANDIDATE",candidate_baseline_label="EX8.1候选（冻结参数纯predict）",fold_description="冻结后参数直接推理，无fit、校准或scaler/fence/路由更新；两侧独立覆盖并另列共同交集",training_note="封存参数纯predict；不沿用历史TSCV各折训练。三系统候选36strict已覆工况，二系统固定14/15；当前EX8按真实生产RUNS合同独立判断可用性。")
    if args.accepted_seal:
        seal=json.loads((freeze/"final-seal.json").read_text(encoding="utf-8"))
        if seal["freeze_manifest_sha256"]!=sha(freeze/"freeze-manifest.json") or seal["sealed_runtime_manifest_sha256"]!=sha(freeze/"sealed-runtime-manifest.json"):
            raise ValueError("seal does not bind current exact production runtime")
        start=datetime.fromisoformat(seal["frozen_at_utc"].replace("Z","+00:00"));due=datetime.fromisoformat(seal["due_at_utc"].replace("Z","+00:00"))
        if due-start!=timedelta(hours=168):raise ValueError("seven-day due must be exactly168hours after accepted seal")
        metadata["freeze"].update(status="accepted",sealed_at=seal["frozen_at_utc"],due_at=seal["due_at_utc"],final_seal_sha256=sha(freeze/"final-seal.json"))
        metadata["models"]["candidate"]["status"]="accepted"
    (evidence/"metadata.json").write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (evidence/"evidence-manifest.json").write_text(json.dumps({"files":{name:sha(evidence/name) for name in ("metadata.json","predictions.csv")}},indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"status":metadata["freeze"]["status"],"candidate_training_date_range":training_range,"model_sha256":metadata["freeze"]["model_sha256"],"fits_performed":0}))


if __name__=="__main__":main()
