"""Score the previously frozen EXPERT8.1 held-period predictions. No fitting."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

BACKEND=Path(__file__).resolve().parents[1]
ROOT=BACKEND.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0,str(BACKEND))
from scripts.gpt_expert81_experiment import score, paired_bootstrap
from app.gpt_coarse.experiments.expert81 import digest, inside, parse_time, BJ


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    out=ROOT/"output/gpt-expert81-holdout-20261102"
    expected={"prediction-lock.json":"bce7ff3e12010ecc27a0494829c6c9955aa1dc123d1868525528a844b7681e68",
              "prediction-lock-manifest.json":"c6f11685d24bf33197ce07544e4764fdee9d17aed5f466095e7ef23b0203ed0f",
              "feature-evidence.json":"e784f270921a788b5e0df03b581c0e292d4556ca2d2e83e13598bc85a3bc7757",
              "labels-first-capture.json":"61aedc6aa9a3de9fd8a011a0af880e870bc4a310c7289bf557df64e07a636b3b",
              "label-capture-manifest.json":"47a78df04e939a74a675a6f228081f205af7b56310450e8ab9147902f5b44183"}
    for name,expected_hash in expected.items():
        if file_hash(out/name)!=expected_hash:
            raise ValueError(f"frozen scoring input changed: {name}")
    lock=json.loads((out/"prediction-lock.json").read_text(encoding="utf-8"))
    if digest({k:v for k,v in lock.items() if k!="prediction_lock_hash"})!=lock["prediction_lock_hash"]:
        raise ValueError("prediction lock internal hash mismatch")
    capture=json.loads((out/"labels-first-capture.json").read_text(encoding="utf-8"))
    labels={int(r["id"]):r for r in capture["raw_rows"]}
    if set(labels)!={185,187,190,195,200,202}:
        raise ValueError("unexpected label IDs; only the authorized six may be scored")
    features=json.loads((out/"feature-evidence.json").read_text(encoding="utf-8"))
    feature_by_id={r["assay_id"]:r for r in features["rows"]}
    records=[]
    for row in lock["rows"]:
        assay_id=row["assay_id"]
        if assay_id not in labels:
            continue
        label=labels[assay_id];feature=feature_by_id[assay_id]
        if (parse_time(label["observed_at"])!=parse_time(row["sample_at"])
                or parse_time(label["created_at"])!=parse_time(feature["initial_created_at"])
                or label["time_basis"]!=feature["time_basis"]):
            raise ValueError("label metadata differs from its frozen input identity")
        actual=float(label["value"])
        if not 0<=actual<=100:
            raise ValueError("invalid assay value")
        date=parse_time(row["sample_at"]).astimezone(BJ).date().isoformat()
        for method,predicted in row["methods"].items():
            value=predicted["prediction"];raw=predicted["raw_prediction"]
            if value is None:
                raise ValueError("a frozen scored prediction is missing")
            records.append({"assay_id":assay_id,"sample_at":row["sample_at"],"date":date,
                            "scope":row["scope"],"feature_eligibility":row["feature_eligibility"],
                            "method":method,"prediction":value,"raw_prediction":raw,"actual":actual,
                            "error":value-actual,"abs_error":abs(value-actual),"raw_error":raw-actual,
                            "raw_abs_error":abs(raw-actual),"normal":inside(actual,row["training_normal_fence"]),
                            "normal_fence":row["training_normal_fence"],"input_sha256":row["input_sha256"],
                            "label_version_evidence":"first complete captured final-value snapshot; no backdating to initial created_at",
                            "label_row_hash":digest(label),"low_load_extrapolation":assay_id==190})
    methods=["C7","C0","frozen_expert8","last_observed"]
    summary={}
    for scope in ("strict_primary","approximate_diagnostic_only"):
        own=[r for r in records if r["scope"]==scope]
        summary[scope]={"methods":{method:{"all":score([r for r in own if r["method"]==method]),
                        "normal":score([r for r in own if r["method"]==method and r["normal"]]),
                        "raw_all":score([r for r in own if r["method"]==method],raw=True)} for method in methods},
                        "per_date":{date:{method:score([r for r in own if r["date"]==date and r["method"]==method]) for method in methods} for date in sorted({r["date"] for r in own})},
                        "paired_MAE_differences":{subset:{method:paired_bootstrap(own,"C7",method,subset=subset) for method in ("C0","frozen_expert8","last_observed")} for subset in ("all","normal")},
                        "C7_worst":max((r for r in own if r["method"]=="C7"),key=lambda r:r["abs_error"])}
        for subset in summary[scope]["paired_MAE_differences"].values():
            for item in subset.values():
                item["note"]="reconstructed held-period evidence; n is very small; approximate scope is separate diagnostic, not primary acceptance"
    result={"schema":"expert81-reconstructed-holdout-score-v1","stage":"reconstructed_held_period_not_prospective_online",
            "predictions_preceded_label_reveal_in_this_task":True,"candidate_id":"C7","no_reselection_or_new_fit":True,
            "protocol_sha256":lock["protocol_sha256"],"prediction_lock_sha256":expected["prediction-lock.json"],
            "label_capture_sha256":expected["labels-first-capture.json"],"records":records,"summary":summary,
            "coverage":lock["coverage"],"excluded_coverage":lock["coverage_without_prediction"],
            "limitations":["two strict samples cannot establish 80 percent accuracy or stable superiority",
                           "ID190 low-load extrapolation remains in the strict denominator",
                           "initial created_at does not certify final-value historical availability",
                           "no new holdout labels were used to calibrate these already frozen historical predictions",
                           "issued predictions were reconstructed now; no true online sampling-time decision receipt exists for these six targets",
                           "approximate four samples are never pooled into strict primary acceptance"]}
    target=out/"holdout-score.json"
    if target.exists():
        raise ValueError("refusing to overwrite a holdout score")
    target.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    manifest={"schema":"expert81-holdout-score-manifest-v1","inputs":expected,"script_sha256":file_hash(__file__),
              "metric_source_sha256":file_hash(ROOT/"backend/scripts/gpt_expert81_experiment.py"),
              "files":{"holdout-score.json":{"bytes":target.stat().st_size,"sha256":file_hash(target)}}}
    (out/"holdout-score-manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"score_sha256":file_hash(target),"manifest_sha256":file_hash(out/"holdout-score-manifest.json"),
        "summary":{scope:{m:{k:v["all"][k] for k in ("n","MAE","hit1","max_absolute_error")} for m,v in item["methods"].items()} for scope,item in summary.items()},
        "per_target":[{"id":r["assay_id"],"scope":r["scope"],"actual":r["actual"],"prediction":r["prediction"],"abs_error":r["abs_error"],"normal":r["normal"]} for r in records if r["method"]=="C7"]},ensure_ascii=False))


if __name__=="__main__":
    main()
