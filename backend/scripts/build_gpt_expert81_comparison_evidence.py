"""Export three comparison stages from saved models/records, without any fitting."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import csv
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT/"output/gpt-expert81-db-primary-20261103"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value, *, replace=False):
    if path.exists() and not replace:
        raise ValueError("refusing to overwrite evidence: "+str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+"\n", encoding="utf-8")


def metric(rows, field="candidate_prediction"):
    paired = [r for r in rows if r.get("actual") is not None and r.get(field) is not None]
    errors = [abs(r[field]-r["actual"]) for r in paired]
    return {"scope_n": len(rows), "n": len(paired), "MAE": sum(errors)/len(errors) if errors else None,
            "hit1": sum(e <= 1. for e in errors), "hit1_rate": sum(e <= 1. for e in errors)/len(errors) if errors else None,
            "normal": {"n": sum(r.get("normal") is True for r in paired),
                       "MAE": (sum(abs(r[field]-r["actual"]) for r in paired if r.get("normal") is True)/sum(r.get("normal") is True for r in paired)) if any(r.get("normal") is True for r in paired) else None,
                       "hit1": sum(abs(r[field]-r["actual"]) <= 1. for r in paired if r.get("normal") is True)}}


def refresh_metadata():
    evidence = OUT/"comparison-evidence"
    metadata = json.loads((evidence/"metadata.json").read_text(encoding="utf-8"))
    seal_path = OUT/"final-freeze/final-seal.json"
    if not seal_path.exists():
        raise ValueError("final seal does not yet exist")
    seal = json.loads(seal_path.read_text(encoding="utf-8"))
    if seal["freeze_manifest_sha256"] != sha(OUT/"final-freeze/freeze-manifest.json"):
        raise ValueError("seal does not match selected runtime")
    metadata["freeze"].update(status="accepted", sealed_at=seal["frozen_at_utc"], due_at=seal["due_at_utc"], final_seal_sha256=sha(seal_path))
    metadata["models"]["candidate"]["status"] = "accepted"
    write(evidence/"metadata.json", metadata, replace=True)
    write(evidence/"evidence-manifest.json", {"files": {name:sha(evidence/name) for name in ("metadata.json", "predictions.csv")}}, replace=True)
    print(json.dumps({"status": "accepted_seal_metadata_refreshed", "fits_performed": 0, "predictions_unchanged": True}))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--refresh-seal", action="store_true"); args = parser.parse_args(argv)
    if args.refresh_seal:
        refresh_metadata(); return
    raw = [json.loads(line) for line in (OUT/"data/training_rows.jsonl").read_text(encoding="utf-8").splitlines()]
    inputs = []
    for row in raw:
        quality = row["public_quality"]
        if row["exclusion_reasons"]:
            quality = "excluded_source_evidence"
        inputs.append({"record_id": row["record_id"], "assay_id": row["assay_id"], "sample_at": row["sample_at"],
                       "public_quality": quality, "features": row["features"], "feature_namespace": row["feature_namespace"],
                       "semantic_quality": row["quality_by_namespace"].get(row["feature_namespace"])})
    input_path = OUT/"history-all-inputs.json"; lock_path = OUT/"history-all-prediction-lock.json"
    write(input_path, {"rows": inputs})
    command = [sys.executable, str(OUT/"final-freeze/predict-sealed.py"), "--freeze", str(OUT/"final-freeze"), "--inputs", str(input_path), "--output", str(lock_path)]
    run = subprocess.run(command, capture_output=True, text=True, timeout=120)
    write(OUT/"history-all-inference-run.json", {"exit_code": run.returncode, "stdout": run.stdout, "stderr": run.stderr, "fits_performed": 0})
    if run.returncode:
        raise ValueError("pure final inference failed: "+run.stderr[-2000:])
    locked = json.loads(lock_path.read_text(encoding="utf-8"))
    by_id = {r["assay_id"]:r for r in raw}
    frozen = json.loads((OUT/"final-freeze/model.json").read_text(encoding="utf-8"))
    all_rows = []
    for prediction in locked["rows"]:
        source = by_id[prediction["assay_id"]]
        branch = str(source["system_count"]) if source["system_count"] is not None else "unknown"
        bounds = frozen.get(branch, {}).get("fence")
        actual = source["actual"]
        normal = bounds["lower"] <= actual <= bounds["upper"] if bounds is not None and actual is not None else None
        all_rows.append({"phase": "history_all", "record_id": source["record_id"], "assay_id": source["assay_id"], "date": source["date"], "system_count": branch,
                         "actual": actual, "current_prediction": prediction["old_production_policy_prediction"], "candidate_prediction": prediction["prediction"],
                         "current_ex8_algorithm": prediction["ex8_native_prediction"], "normal": normal, "public_quality": source["public_quality"],
                         "eligibility": source["eligibility"], "review_flags": source["review_flags"], "exclusion_reasons": source["exclusion_reasons"],
                         "candidate_status": prediction["status"], "candidate_reason": prediction["reason"], "EX8_reason": prediction["ex8_native_reason"],
                         "fold": "", "train_max_date": ""})
        all_rows[-1]["cohort"] = "label_review" if source["eligibility"] == "label_review" else "eligible" if source["eligibility"] in ("strict", "minute_approximation") else "rejected"
    if len(all_rows) != 81 or sum(r["candidate_prediction"] is not None for r in all_rows) != 76:
        raise ValueError("full DB replay does not account for expected81/76 coverage")
    first = json.loads((OUT/"results.json").read_text(encoding="utf-8"))
    capacity = json.loads((OUT/"capacity-round2/results.json").read_text(encoding="utf-8"))
    baselines = {(r["record_id"],r["method"]):r for r in first["baseline_predictions"]}
    fold_lookup = {(f["branch"], f["phase"], f["date"]):f for f in capacity["folds"] if f["status"] == "ok"}
    source_by_id = {r["record_id"]:r for r in raw}
    forward_rows = []
    for prediction in capacity["combined_predictions"]:
        branch = prediction["system_count"]
        if prediction["phase"] != "forward_date_current_version_diagnostic" or prediction["method"] != capacity["selections"][branch]["selected"]:
            continue
        fold = fold_lookup[branch, prediction["phase"], prediction["date"]]
        used_dates = [source_by_id[k]["date"] for k in fold["mixed_train_ids"]]
        train_max = max(used_dates)
        if not train_max < prediction["date"]:
            raise ValueError("forward fold used a target-day or future label")
        baseline = baselines[prediction["record_id"], "old_production_policy"]
        forward_rows.append({"phase": "history_forward", "record_id": prediction["record_id"], "assay_id": prediction["assay_id"], "date": prediction["date"],
                             "system_count": branch, "actual": prediction["actual"], "current_prediction": baseline["prediction"], "candidate_prediction": prediction["prediction"],
                             "current_ex8_algorithm": baselines[prediction["record_id"], "EX8_native_algorithm"]["prediction"], "normal": prediction["normal"],
                             "public_quality": prediction["public_quality"], "eligibility": source_by_id[prediction["record_id"]]["eligibility"], "review_flags": [], "exclusion_reasons": [],
                             "candidate_status": "ok", "candidate_reason": None, "EX8_reason": None,
                             "fold": branch+":"+prediction["fold"], "train_max_date": train_max})
        forward_rows[-1]["cohort"] = "eligible"
    write(OUT/"history-all-predictions.json", {"schema": "expert81-history-all-replay-v1", "rows": all_rows,
          "database_scope_n": 81, "candidate_predicted_n": 76, "prediction_lock_sha256": sha(lock_path), "labels_already_development_exposed": True, "fits_performed": 0})
    with (OUT/"history-all-predictions.csv").open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, list(all_rows[0])); writer.writeheader(); writer.writerows(all_rows)
    audit70 = [r for r in all_rows if r["eligibility"] in ("strict", "minute_approximation")]
    review6 = [r for r in all_rows if r["eligibility"] == "label_review"]
    analysis = {"schema": "expert81-history-three-metrics-v1", "history_all": {}, "history_forward": {}, "future": {"status": "pending", "n": 0, "MAE": None},
                "counts": {"database_n": 81, "audited_target_n": len(audit70), "review_n": len(review6), "excluded_n": 5},
                "quality": dict(Counter(r["public_quality"] for r in all_rows))}
    for name,rows in (("database_all81", all_rows), ("audited70", audit70), ("review6_diagnostic", review6)):
        analysis["history_all"][name] = {"candidate": metric(rows), "current": metric(rows, "current_prediction")}
    for branch in ("2", "3"):
        analysis["history_all"].setdefault("by_system", {})[branch] = {"candidate": metric([r for r in all_rows if r["system_count"] == branch]), "current": metric([r for r in all_rows if r["system_count"] == branch], "current_prediction")}
        own = [r for r in forward_rows if r["system_count"] == branch]
        analysis["history_forward"].setdefault("by_system", {})[branch] = {"candidate": metric(own), "current": metric(own, "current_prediction"),
             "prediction_min_date": min(r["date"] for r in own), "prediction_max_date": max(r["date"] for r in own),
             "train_max_date_range": [min(r["train_max_date"] for r in own), max(r["train_max_date"] for r in own)], "common_target_ids": [r["record_id"] for r in own]}
    analysis["history_forward"].update(candidate=metric(forward_rows), current=metric(forward_rows, "current_prediction"),
                                       note="Expanding earlier-date training using current captured labels; no same-day/future train dates, not legal historical report availability or independent test")
    write(OUT/"history-all-metrics.json", analysis)
    evidence = OUT/"comparison-evidence"; evidence.mkdir(exist_ok=True)
    paired = [r for r in all_rows if r["actual"] is not None and r["current_prediction"] is not None and r["candidate_prediction"] is not None]+forward_rows
    fields = ["phase", "record_id", "system_count", "date", "actual", "current_prediction", "candidate_prediction", "current_ex8_algorithm", "normal", "train_max_date", "fold", "cohort"]
    with (evidence/"predictions.csv").open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fields, extrasaction="ignore"); writer.writeheader(); writer.writerows(paired)
    metadata = {"scope": {"total_n": 81, "evaluable_n": 76, "audited_n": 70, "review_n": 6, "rejected_n": 5, "description": "全部81条已曝光DB；回算76含6待核单列，70审计训练资格；5输入/停机/时间原因拒绝", "data_cutoff": locked["frozen_training_cutoff"],
                           "by_system": {"2": {"total_n": 18, "evaluable_n": 18, "audited_n": 16, "review_n": 2, "rejected_n": 0}, "3": {"total_n": 60, "evaluable_n": 58, "audited_n": 54, "review_n": 4, "rejected_n": 2}, "unknown": {"total_n": 3, "evaluable_n": 0, "audited_n": 0, "review_n": 0, "rejected_n": 3}}},
                "freeze": {"status": "provisional", "model_version": locked["model_version"], "model_sha256": locked["model_file_sha256"], "sealed_at": None, "due_at": None},
                "models": {"current": {"id": "EX8", "name": "现用EX8", "version": "20261012", "status": "frozen", "method": {"summary": "三系统使用冻结EX8原生八因素公式；二系统为实际旧14/15规则", "details": ["三系统只接受真实legacy if_prod输入，不将RUNS代填", "二系统灰分规则：液位>=30取14，否则15；EX8原始参数算法仅额外列示"]}},
                           "candidate": {"id": "EX81", "name": "候选模型", "version": locked["model_version"], "status": "provisional", "method": {"summary": "Gradient Boosting Regression (GBR)，二/三系统独立参数", "details": ["400棵树，max_depth=3，min_samples_leaf=2，learning_rate=0.05，squared_error", "液位/煤量/可信四系统状态共6因素；不使用ID/日期/脱粉两位", "数据库strict权重1，合格分钟近似0.5；评分等权；按strict训练标签fence，旧表未用于最终赢家", "当前DB重代入拟合最优；日期回放明显退化，未证明未来准确率"]}}},
                "notes": {"history_all": "已曝光历史DB回算，含6待核诊断且未加入训练；默认全样本MAE，normal附属，非独立检验。", "history_forward": "仅更早采样日期训练→较新日期，使用当前捕获版本的历史时间回测；不是LODO，不保证当时报告已可用。", "future": "最终独立封存后七天输入先锁/标签后揭；尚无未来实测数据，不显示0误差。"}}
    metadata["models"]["current"]["method"]["by_system"] = {"2": {"summary": "实际旧生产14/15液位查表规则", "details": ["液位≥30取14，否则15；不是EX8三系统参数"]}, "3": {"summary": "冻结EX8原生八因素公式", "details": ["20261012版本；真实legacy if_prod输入，不以RUNS代填"]}}
    metadata["models"]["candidate"]["method"]["by_system"] = {b: {"summary": "Gradient Boosting Regression (GBR)，本系统数独立参数", "details": ["400树、depth3、leaf2、lr0.05；DB strict1/approx0.5", "拟合高分与日期退化同时披露，未证明未来能力"]} for b in ("2", "3")}
    write(evidence/"metadata.json", metadata)
    write(evidence/"evidence-manifest.json", {"files": {name:sha(evidence/name) for name in ("metadata.json", "predictions.csv")}})
    print(json.dumps({"history_all": analysis["history_all"], "history_forward": analysis["history_forward"], "future": analysis["future"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
