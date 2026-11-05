"""Freeze EXPERT8.1 held-period inputs/predictions before reading new assays.

Loads the actual selection-time core snapshot; no network, labels or tuning.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
from app.gpt_coarse.expert8 import predict as expert_predict

FEATURE_SHA = "e784f270921a788b5e0df03b581c0e292d4556ca2d2e83e13598bc85a3bc7757"
FEATURE_MANIFEST_SHA = "402988f0f5171a0120dafbca4c7eae479efee4ca1b9b15e5b4660fc54be2a486"
CANDIDATE_SHA = "ee03b8ea7a701a498715cc9b413ac03136fe1412a0207229ad7d39415a208e64"
FIT_CORE_SHA = "2a92112eef2cf2460c9d4923c6a18e2905b2dc838a9c396eebfd6d8ce3b44a98"
FIT_PROTOCOL_SHA = "79afa9387be716b65a00a7cd672d8d8b7664a503a1db1cbb403407d151869374"


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path=Path(path)
    if path.exists():
        raise ValueError(f"refusing to overwrite frozen file: {path.name}")
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory",type=Path,default=ROOT/"output/gpt-expert81-holdout-20261102")
    args=parser.parse_args(argv);out=args.directory
    original=ROOT/"output/gpt-expert81-20261102"
    core_path=original/"source_snapshot/backend/app/gpt_coarse/experiments/expert81.py"
    protocol_path=original/"source_snapshot/docs/verification/gpt-expert81-20261102.protocol.json"
    expected={out/"feature-evidence.json":FEATURE_SHA,out/"feature-capture-manifest.json":FEATURE_MANIFEST_SHA,
              original/"candidate.json":CANDIDATE_SHA,core_path:FIT_CORE_SHA,protocol_path:FIT_PROTOCOL_SHA}
    for path,hash_value in expected.items():
        if file_hash(path)!=hash_value:
            raise ValueError(f"frozen input changed: {path}")
    spec=importlib.util.spec_from_file_location("expert81_selection_core",core_path)
    core=importlib.util.module_from_spec(spec);spec.loader.exec_module(core)
    artifact=json.loads((original/"candidate.json").read_text(encoding="utf-8"))
    if core.digest({k:v for k,v in artifact.items() if k!="artifact_hash"})!=artifact["artifact_hash"]:
        raise ValueError("candidate internal hash mismatch")
    protocol=json.loads(protocol_path.read_text(encoding="utf-8"))
    frozen_path=ROOT/protocol["source"]["frozen_expert8_model"]
    if file_hash(frozen_path)!=protocol["source"]["frozen_expert8_model_sha256"] or file_hash(ROOT/"backend/app/gpt_coarse/expert8.py")!=protocol["source"]["frozen_expert8_implementation_sha256"]:
        raise ValueError("frozen EXPERT8 changed")
    frozen=json.loads(frozen_path.read_text(encoding="utf-8"))["model"]
    evidence=json.loads((out/"feature-evidence.json").read_text(encoding="utf-8"))
    if evidence["label_values_read"] is not False:
        raise ValueError("prediction lock must precede assay reveal")
    results=[];states={}
    for row in evidence["rows"]:
        sample=core.parse_time(row["sample_at"])
        values=row["features"]
        level_evidence=row["evidence"]["level"]
        if core.parse_time(level_evidence["target_at"]) != sample-core.timedelta(seconds=90):
            raise ValueError("unexpected level target time")
        for sensor in row["evidence"].values():
            if sensor.get("received_at") and core.parse_time(sensor["received_at"])>sample:
                raise ValueError("future target input evidence")
        quality=row["feature_eligibility"]
        strict=quality=="strict"
        approximate=quality=="minute_approximation"
        entry={"assay_id":row["assay_id"],"sample_at":row["sample_at"],
               "feature_eligibility":quality,"primary_eligible":strict,"label_validation":"pending_unrevealed",
               "group_key":row["six_switch_group"],"input_sha256":row["input_sha256"],
               "feature_values_hash":core.digest(values),"features":values,
               "input_evidence_sha256":core.digest(row["evidence"]),"methods":{},
               "scope":"strict_primary" if strict else ("approximate_diagnostic_only" if approximate else "quality_rejected"),
               "rejection_reason":None if strict or approximate else row["coverage_reason"]}
        if strict or approximate:
            for candidate in (artifact["candidate_id"],"C0"):
                state=core.fit_state(artifact["history_events"],sample,candidate,artifact["config"],
                                     group_key=row["six_switch_group"],exclude_assay_id=row["assay_id"])
                states[state["state_hash"]]=state
                estimate=core.predict_state(state,values,expert_predict(values,frozen))
                entry["methods"][candidate]=estimate
            old=expert_predict(values,frozen)
            c0=states[entry["methods"]["C0"]["state_hash"]]
            entry["methods"]["frozen_expert8"]={"prediction":old,"raw_prediction":old,"status":"ok" if old is not None else "unavailable","reason":None}
            last=core.last_observed_reference(c0)
            entry["methods"]["last_observed"]={"prediction":last,"raw_prediction":last,"status":"ok" if last is not None else "unavailable","reason":None}
            entry["training_normal_fence"]=c0["fence"]
            entry["formal_receipt_count"]=len(states[entry["methods"][artifact["candidate_id"]]["state_hash"]]["residual_receipt_ids"])
        else:
            entry["methods"]={m:{"prediction":None,"raw_prediction":None,"status":"unavailable","reason":row["coverage_reason"]} for m in (artifact["candidate_id"],"C0","frozen_expert8","last_observed")}
        results.append(entry)
    lock={"schema":"expert81-holdout-prediction-lock-v1","status":"predictions_frozen_before_label_reveal",
          "candidate_id":artifact["candidate_id"],"candidate_file_sha256":CANDIDATE_SHA,
          "candidate_internal_artifact_hash":artifact["artifact_hash"],"core_selection_source_sha256":FIT_CORE_SHA,
          "protocol_sha256":FIT_PROTOCOL_SHA,"feature_evidence_sha256":FEATURE_SHA,
          "feature_manifest_sha256":FEATURE_MANIFEST_SHA,"rows":results,
          "coverage_without_prediction":evidence["coverage_without_feature_queries"],
          "coverage":{"new_members":9,"user_time_error_excluded":1,"seed_not_available":1,
                      "features_queried":len(results),"strict":sum(x["primary_eligible"] for x in results),
                      "approximate":sum(x["scope"]=="approximate_diagnostic_only" for x in results),
                      "incomplete":sum(x["scope"]=="quality_rejected" for x in results)},
          "label_values_read":False,"new_labels_used_in_calibration":False,
          "interpretation":"reconstructed held-period predictions; old seed labels only, no later label updates; not archived production output or prospective online evidence; notebook snapshot/host sample clock limitation remains",
          "no_reselection":True,"no_eligibility_relaxation":True}
    lock["prediction_lock_hash"]=core.digest(lock)
    write_json(out/"prediction-lock.json",lock)
    write_json(out/"prediction-states.json",states)
    outputs=[out/"prediction-lock.json",out/"prediction-states.json"]
    manifest={"schema":"expert81-holdout-prediction-lock-manifest-v1","status":"labels_still_locked",
              "sources":{str(path.relative_to(ROOT)):value for path,value in expected.items()},
              "script_sha256":file_hash(__file__),"files":{p.name:{"sha256":file_hash(p),"bytes":p.stat().st_size} for p in outputs},
              "prediction_lock_hash":lock["prediction_lock_hash"],"coverage":lock["coverage"]}
    write_json(out/"prediction-lock-manifest.json",manifest)
    print(json.dumps({"manifest_sha256":file_hash(out/"prediction-lock-manifest.json"),"prediction_file_sha256":file_hash(out/"prediction-lock.json"),"lock_hash":lock["prediction_lock_hash"],"coverage":lock["coverage"],"predictions":[{"id":x["assay_id"],"scope":x["scope"],"methods":{k:v["prediction"] for k,v in x["methods"].items()}} for x in results]},ensure_ascii=False))


if __name__=="__main__":
    main()
