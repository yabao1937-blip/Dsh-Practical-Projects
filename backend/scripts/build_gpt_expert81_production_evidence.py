"""Recompute historical comparison for saved forward-selected runtime, no fit."""
from __future__ import annotations

import csv
import json
from pathlib import Path
import shutil
import subprocess
import sys

from scripts.build_gpt_expert81_comparison_evidence import ROOT, OUT, metric, sha, write


def main():
    destination = OUT/"production-selection"
    raw = [json.loads(s) for s in (OUT/"data/training_rows.jsonl").read_text(encoding="utf-8").splitlines()]
    by_id = {r["assay_id"]:r for r in raw}; by_record = {r["record_id"]:r for r in raw}
    freeze = OUT/"production-freeze"
    lock_path = destination/"history-all-prediction-lock.json"
    run = subprocess.run([sys.executable, str(freeze/"predict-sealed.py"), "--freeze", str(freeze), "--inputs", str(OUT/"history-all-inputs.json"), "--output", str(lock_path)], capture_output=True, text=True, timeout=120)
    write(destination/"history-inference-run.json", {"exit_code": run.returncode, "stdout": run.stdout, "stderr": run.stderr, "fits_performed": 0})
    if run.returncode: raise ValueError(run.stderr[-2500:])
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    model = json.loads((freeze/"model.json").read_text(encoding="utf-8"))
    history = []
    for prediction in lock["rows"]:
        raw_row = by_id[prediction["assay_id"]]; actual = raw_row["actual"]
        branch = str(raw_row["system_count"]) if raw_row["system_count"] is not None else "unknown"
        bounds = model.get(branch, {}).get("fence")
        history.append({"phase": "history_all", "record_id": raw_row["record_id"], "assay_id": raw_row["assay_id"], "system_count": branch, "date": raw_row["date"],
                        "actual": actual, "current_prediction": prediction["old_production_policy_prediction"], "candidate_prediction": prediction["prediction"],
                        "current_ex8_algorithm": prediction["ex8_native_prediction"], "normal": bounds["lower"] <= actual <= bounds["upper"] if bounds and actual is not None else None,
                        "public_quality": raw_row["public_quality"], "cohort": "label_review" if raw_row["eligibility"] == "label_review" else "eligible" if raw_row["eligibility"] in ("strict", "minute_approximation") else "rejected",
                        "review_flags": raw_row["review_flags"], "exclusion_reasons": raw_row["exclusion_reasons"], "candidate_status": prediction["status"], "candidate_reason": prediction["reason"],
                        "training_mode": "fixed_policy" if branch == "2" else "learned", "fold": "", "train_min_date": "", "train_max_date": ""})
    if len(history) != 81 or sum(r["candidate_prediction"] is not None for r in history) != 76: raise ValueError("production historical81/76 coverage changed")
    first = json.loads((OUT/"results.json").read_text(encoding="utf-8"))
    all_results = json.loads((OUT/"capacity-round2/results.json").read_text(encoding="utf-8"))
    choice = json.loads((destination/"results.json").read_text(encoding="utf-8"))["selected_three_system"]
    folds = {(f["branch"], f["phase"], f["date"]):f for f in first["folds"] if f["status"] == "ok"}
    baselines = {(r["record_id"],r["method"]):r for r in first["baseline_predictions"]}
    phase = "forward_date_current_version_diagnostic"
    forward = []
    for prediction in all_results["combined_predictions"]:
        if prediction["system_count"] != "3" or prediction["phase"] != phase or prediction["method"] != choice: continue
        fold = folds["3",phase,prediction["date"]]
        train_dates = [by_record[key]["date"] for key in fold["train_ids"]]
        minimum,maximum = min(train_dates),max(train_dates)
        if maximum >= prediction["date"]: raise ValueError("production forward used same/future date")
        forward.append({"phase": "history_forward", "record_id": prediction["record_id"], "assay_id": prediction["assay_id"], "system_count": "3", "date": prediction["date"],
                        "actual": prediction["actual"], "current_prediction": baselines[prediction["record_id"],"EX8_native_algorithm"]["prediction"], "candidate_prediction": prediction["prediction"],
                        "current_ex8_algorithm": baselines[prediction["record_id"],"EX8_native_algorithm"]["prediction"], "normal": prediction["normal"], "public_quality": prediction["public_quality"], "cohort": "eligible",
                        "training_mode": "learned", "fold": "3:"+prediction["date"], "train_min_date": minimum, "train_max_date": maximum})
    # A fixed2sys rule is independent of training support; score all16 qualified targets.
    for row in history:
        if row["system_count"] == "2" and row["cohort"] == "eligible":
            if row["candidate_prediction"] != row["current_prediction"] or row["candidate_prediction"] not in (14.,15.): raise ValueError("2sys retained policy differs")
            forward.append({**{k:v for k,v in row.items() if k not in ("review_flags", "exclusion_reasons", "candidate_status", "candidate_reason")},
                            "phase": "history_forward", "training_mode": "fixed_policy", "fold": "fixed-policy:no-training", "train_min_date": "", "train_max_date": ""})
    if len(forward) != 64: raise ValueError("expected forward48 learned plus16 unchanged policy")
    write(destination/"history-all-predictions.json", {"schema": "expert81-production-history-all-v1", "rows": history, "scope_n":81, "predicted_n":76, "fits_performed":0, "prediction_lock_sha256":sha(lock_path)})
    write(destination/"history-forward-predictions.json", {"rows":forward,"attempted_n":70,"learned_three_system_n":48,"fixed_policy_two_system_n":16,"startup_rejected_three_system_n":6,"fits_performed_during_export":0})
    metrics = {"history_all": {}, "history_forward": {}, "future": {"status":"pending","n":0,"MAE":None}, "model_version":lock["model_version"]}
    for scope,rows in (("database_all81",history),("audited70",[r for r in history if r["cohort"]=="eligible"]),("review6",[r for r in history if r["cohort"]=="label_review"])):
        metrics["history_all"][scope] = {"candidate":metric(rows),"current":metric(rows,"current_prediction")}
    for stage,rows in (("history_all",history),("history_forward",forward)):
        metrics[stage]["by_system"] = {b:{"candidate":metric([r for r in rows if r["system_count"]==b]),"current":metric([r for r in rows if r["system_count"]==b],"current_prediction")} for b in ("2","3")}
    metrics["history_forward"].update(candidate=metric(forward),current=metric(forward,"current_prediction"),attempted_n=70,learned3_attempted_n=54,learned3_predicted_n=48,fixed2_attempted_predicted_n=16)
    write(destination/"history-metrics.json",metrics)
    evidence = OUT/"comparison-evidence"
    archive = destination/"prior-gb-comparison-evidence"
    if archive.exists(): raise ValueError("prior GB comparison already archived")
    shutil.copytree(evidence,archive)
    metadata = json.loads((evidence/"metadata.json").read_text(encoding="utf-8"))
    metadata["freeze"] = {"status":"provisional","model_version":lock["model_version"],"model_sha256":lock["model_file_sha256"],"sealed_at":None,"due_at":None}
    metadata["models"]["candidate"].update(version=lock["model_version"],status="provisional",method={"summary":"生产导向：三系统按向前日期回测选GBR；二系统保留现用14/15", "details":["三系统从110个已保存候选按forward全MAE选S__BOOST_absolute_error_2，不重训", "前五全MAE差不足0.019pp，不能声称可靠唯一最优", "历史拟合只作诊断；未来时间留出尚待验收"],"by_system":{
        "3":{"summary":"Gradient Boosting Regression (GBR)，绝对误差损失","details":["80棵树，max_depth=2，min_samples_leaf=5，learning_rate=0.05，absolute_error", "仅36条strict常规DB完整模型；近似样本只评分不参与此候选训练", "110候选共同forward48条/16日期全MAE最低，当前版本历史开发选择，非独立盲测"]},
        "2":{"summary":"保留现用14/15固定液位规则，零训练","details":["液位≥30取14，否则15；和current相同", "新2系GB仅保留研究产物，不进入生产候选路由或指标", "16条仅固定规则历史回算，不声称新2系学习模型泛化"]}}})
    metadata["notes"].update(history_all="生产导向候选对81已曝光DB回算，76输入合法含6待核单列；拟合仅诊断，默认全MAE。",history_forward="三系统仅更早日期训练→较新日期48/54；二系统无需训练的现用固定规则16/16，合计64/70；非独立盲测。",future="独立终审封存后才开始168小时，输入先锁/标签后揭；当前pending。")
    metadata["evaluation_pools"] = {
      "history_all":{"total_n":81,"normal_n":None,"audited_n":70,"review_n":6,"by_system":{"2":{"total_n":18,"normal_n":18,"audited_n":16,"review_n":2},"3":{"total_n":60,"normal_n":sum(r["normal"] is True for r in history if r["system_count"]=="3"),"audited_n":54,"review_n":4},"unknown":{"total_n":3,"normal_n":None}},"description":"全部81身份含5输入/时间/停机拒绝，76合法配对；待核6未训练","fold_description":"保存的full模型纯predict，无fit","eval_date_range":{"start":min(r["date"] for r in history),"end":max(r["date"] for r in history)}},
      "history_forward":{"total_n":70,"normal_n":None,"audited_n":70,"review_n":0,"by_system":{"2":{"total_n":16,"normal_n":16,"audited_n":16,"review_n":0},"3":{"total_n":54,"normal_n":None,"audited_n":54,"review_n":0}},"description":"三系统54尝试、48有过去支持、6冷启动拒绝；二系统固定规则16无需训练，组合64/70","fold_description":"3sys expanding sample dates with train-only strict fence/scaler;2sys zero-training retained policy","train_date_range":{"start":min(r["train_min_date"] for r in forward if r["training_mode"]=="learned"),"end":max(r["train_max_date"] for r in forward if r["training_mode"]=="learned")},"eval_date_range":{"start":min(r["date"] for r in forward),"end":max(r["date"] for r in forward)}},
      "future":{"total_n":0,"normal_n":None,"by_system":{"2":{"total_n":0},"3":{"total_n":0}},"description":"尚无封存后的实测样本","fold_description":"pending"}}
    paired = [r for r in history if r["actual"] is not None and r["current_prediction"] is not None and r["candidate_prediction"] is not None]+forward
    fields = ["phase","record_id","system_count","date","actual","current_prediction","candidate_prediction","current_ex8_algorithm","normal","cohort","training_mode","fold","train_min_date","train_max_date"]
    with (evidence/"predictions.csv").open("w",encoding="utf-8-sig",newline="") as stream:
        writer=csv.DictWriter(stream,fields,extrasaction="ignore");writer.writeheader();writer.writerows(paired)
    write(evidence/"metadata.json",metadata,replace=True)
    write(evidence/"evidence-manifest.json",{"files":{name:sha(evidence/name) for name in ("metadata.json","predictions.csv")}},replace=True)
    print(json.dumps({"history_all":metrics["history_all"],"history_forward":metrics["history_forward"],"evidence_rows":len(paired),"future":metrics["future"]},ensure_ascii=False))


if __name__ == "__main__":main()
