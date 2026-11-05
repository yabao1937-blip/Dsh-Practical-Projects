"""Score an existing prediction lock after label reveal; never fit or infer anew."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
from statistics import mean


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def identity(row):
    if row.get("assay_id") is not None:
        return int(row["assay_id"])
    if row.get("id") is not None:
        return int(row["id"])
    return int(str(row["record_id"]).split(":")[-1])


def metric(rows):
    predicted = [r for r in rows if r["prediction"] is not None]
    errors = [abs(r["prediction"]-r["actual"]) for r in predicted]
    return {"eligible_n": len(rows), "predicted_n": len(predicted), "hit1": sum(e <= 1. for e in errors),
            "hit1_rate": sum(e <= 1. for e in errors)/len(predicted) if predicted else None,
            "MAE": mean(errors) if errors else None,
            "rejected_ids": [r["assay_id"] for r in rows if r["prediction"] is None]}


def score(frozen_metadata, lock, labels):
    by_id = {identity(r): r for r in labels}
    if len(by_id) != len(labels):
        raise ValueError("duplicate revealed label identities")
    expected = {identity(r) for r in lock["rows"]}
    if expected != set(by_id):
        raise ValueError("revealed labels must exactly match already locked target IDs")
    comparison_field=lock.get("comparison_baseline_field","old_production_policy_prediction")
    if comparison_field not in ("old_production_policy_prediction","current_production_prediction"):raise ValueError("unknown prelocked comparison baseline")
    output = []
    for prediction in lock["rows"]:
        assay_id = identity(prediction); label = by_id[assay_id]
        actual = label.get("actual", label.get("value", label.get("ash")))
        actual = float(actual)
        if not math.isfinite(actual) or not 0 < actual <= 100:
            raise ValueError("invalid revealed label")
        route = prediction.get("route") or "unsupported"
        branch = frozen_metadata.get(str(prediction.get("system_count")) if prediction.get("system_count") is not None else route.split(":")[-1], {})
        bounds = branch.get("fence")
        normal = None if bounds is None else bounds["lower"] <= actual <= bounds["upper"]
        current_group=prediction.get("production_system_count") if comparison_field=="current_production_prediction" else prediction.get("system_count")
        current_bounds=frozen_metadata.get(str(current_group),{}).get("fence")
        current_normal=None if current_bounds is None else current_bounds["lower"]<=actual<=current_bounds["upper"]
        # First capture or created_at is not proof of actual report availability.
        report_at = label.get("actual_report_available_at")
        proven = bool(report_at and label.get("report_clock_comparable") is True and label.get("immutable_report_version_proven") is True
                      and datetime.fromisoformat(report_at.replace("Z", "+00:00")) > datetime.fromisoformat(lock["predicted_at_utc"].replace("Z", "+00:00")))
        output.append({**prediction, "assay_id": assay_id, "actual": actual, "normal": normal,
                       "current_prediction": prediction.get(comparison_field),
                       "current_system_count":current_group,"current_normal":current_normal,
                       "abs_error": None if prediction["prediction"] is None else abs(prediction["prediction"]-actual),
                       "prospective_report_time_proven": proven})
    summaries = {"combined": {"all": metric(output), "normal": metric([r for r in output if r["normal"] is True])}}
    for quality in ("strict", "minute_approximation"):
        own = [r for r in output if r.get("public_quality") == quality]
        summaries[quality] = {"all": metric(own), "normal": metric([r for r in own if r["normal"] is True])}
    baseline_summary = {}
    for field in ("ex8_native_prediction", "old_production_policy_prediction", "ex8_production_prediction", "current_production_prediction"):
        common = [r for r in output if r["prediction"] is not None and r.get(field) is not None and (field not in ("ex8_production_prediction","current_production_prediction") or r.get("comparison_system_count_consistent") is not False)]
        baseline_summary[field] = {"common_target_ids": [r["assay_id"] for r in common],
                                   "selected_all": metric(common), "selected_normal": metric([r for r in common if r["normal"] is True]),
                                   "baseline_all": metric([{**r, "prediction": r[field]} for r in common]),
                                   "baseline_normal": metric([{**r, "prediction": r[field]} for r in common if r["normal"] is True])}
    current_summary={"all":metric([{**r,"prediction":r["current_prediction"]} for r in output]),"normal":metric([{**r,"prediction":r["current_prediction"]} for r in output if r["current_normal"] is True])}
    for branch in sorted({str(r.get("current_system_count")) for r in output}):
        own=[r for r in output if str(r.get("current_system_count"))==branch]
        current_summary.setdefault("by_system",{})[branch]={"all":metric([{**r,"prediction":r["current_prediction"]} for r in own]),"normal":metric([{**r,"prediction":r["current_prediction"]} for r in own if r["current_normal"] is True])}
    return {"schema": "expert81-frozen-post-reveal-score-v1", "summary": summaries, "current_summary":current_summary,"comparison_baseline_field":comparison_field,
            "current_baseline_id":lock.get("production_baseline_contract_id") or "EX8_NATIVE_LEGACY","rows": output,
            "comparison_system_count_mismatch_ids":[r["assay_id"] for r in output if r.get("comparison_system_count_consistent") is False],
            "fits_performed": 0, "new_predictions_performed": 0,
            "evaluation_kind": "frozen time-holdout replay; prospective only where actual report/version clock evidence independently proves prediction earlier",
            "prospective_proven_n": sum(r["prospective_report_time_proven"] for r in output), "baseline_common_coverage": baseline_summary}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze", type=Path, required=True)
    parser.add_argument("--prediction-lock", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise ValueError("refusing to overwrite an existing score")
    lock = json.loads(args.prediction_lock.read_text(encoding="utf-8"))
    if lock["freeze_manifest_sha256"] != sha(args.freeze/"freeze-manifest.json") or lock["model_file_sha256"] != sha(args.freeze/"model.joblib"):
        raise ValueError("prediction lock does not match exact frozen version")
    if lock.get("provenance_verified") is not True:
        raise ValueError("formal score requires verified collection manifest; bare factor-file predictions are diagnostic only")
    labels = json.loads(args.labels.read_text(encoding="utf-8"))
    if isinstance(labels, dict):
        labels = labels.get("rows", labels.get("labels", labels.get("records")))
    metadata = json.loads((args.freeze/"model.json").read_text(encoding="utf-8"))
    result = score(metadata, lock, labels)
    result.update(prediction_lock_sha256=sha(args.prediction_lock), label_file_sha256=sha(args.labels), freeze_manifest_sha256=sha(args.freeze/"freeze-manifest.json"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+"\n", encoding="utf-8")
    print(json.dumps(result["summary"]))


if __name__ == "__main__":
    main()
