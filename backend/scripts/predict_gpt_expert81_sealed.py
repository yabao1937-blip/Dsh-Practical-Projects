"""Append native EX8 baseline predictions before labels, preserving original locks."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import importlib.util
import importlib.metadata
import json
import math
import os
from pathlib import Path
import sys


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def runtime_versions():
    packages = {"numpy": "numpy", "scipy": "scipy", "sklearn": "scikit-learn", "joblib": "joblib", "threadpoolctl": "threadpoolctl"}
    return {"python": sys.version, **{name: importlib.metadata.version(package) for name,package in packages.items()}}


def write_durable_exclusive(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n"); stream.flush(); os.fsync(stream.fileno())


def baseline(row, model):
    output = {"ex8_native_prediction": None, "ex8_native_reason": "native_legacy_if_prod_inputs_unavailable", "old_production_policy_prediction": None}
    values = row.get("features", row)
    try:
        systems = [float(values[k]) for k in ("sysA", "sysB", "sys401", "sys402")]
        level, coal = float(values["level"]), float(values["coal_amount"])
        if row.get("public_quality", row.get("quality")) not in ("strict", "minute_approximation", "good", "public_strict"):
            return output
        if not all(v in (0., 1.) for v in systems) or not math.isfinite(level) or not math.isfinite(coal) or not 0 <= level <= 100 or coal <= 10:
            return output
        count = int(sum(systems))
        if count == 2:
            output.update(old_production_policy_prediction=14. if level >= 30. else 15., ex8_native_reason="two_system_actual_old_production_used14_15_lookup")
            return output
        if count != 3 or row.get("feature_namespace") != "legacy_if_prod" or row.get("semantic_quality") not in ("strict", "minute_approximation", "good"):
            return output
        desliming = [float(values[k]) for k in ("desliming473", "desliming474")]
        if not all(v in (0., 1.) for v in desliming):
            return output
        raw = [coal, *systems, *desliming]
        used = [min(bounds[1], max(bounds[0], value)) for value,bounds in zip(raw, model["bounds"])]
        correction = 2.*math.tanh(sum(c*(value-reference)/scale for c,value,reference,scale in zip(model["coefs"], used, model["reference"], model["scale"]))/2.)
        prediction = 30.5-.3*min(75., max(25., level))+correction
        output.update(ex8_native_prediction=prediction, old_production_policy_prediction=prediction, ex8_native_reason=None)
    except (KeyError, ValueError, TypeError):
        pass
    return output


def production_baseline(row, model):
    """Faithful archived production contract, separate from legacy diagnostics."""
    output={"ex8_production_prediction":None,"ex8_production_reason":row.get("production_reason") or "production_inputs_unavailable", "current_production_prediction":None,
            "production_policy_route":None,"production_system_count":row.get("production_system_count")}
    if row.get("baseline_id")!="EX8_PRODUCTION_RUNS" or row.get("feature_namespace")!="runs_v1" or row.get("production_quality")!="eligible":return output
    values=row.get("features",{});valid=row.get("production_features_valid",{});status=row.get("production_feature_status",{})
    try:
        switches=("sysA","sysB","sys401","sys402")
        required=(*switches,"level")
        if any(valid.get(k) is not True or status.get(k)!="good" for k in required):
            output["ex8_production_reason"]="production_required_systems_or_lag90_level_not_good";return output
        system_values=[float(values[k]) for k in switches]
        if not all(v in (0.,1.) for v in system_values):raise ValueError("production_feedback_not_binary")
        count=int(sum(system_values));declared=row.get("production_system_count")
        if type(declared) is not int or declared!=count:raise ValueError("production_system_count_does_not_match_feedback")
        level=float(values["level"])
        if not math.isfinite(level) or not 0<=level<=100:raise ValueError("production_level_outside_sensor_contract")
        if count==2:
            prediction=14. if level>=30. else 15.
            route="two_system_fixed14_15"
        else:
            all_keys=("coal_amount",*switches,"desliming473","desliming474","level")
            if any(valid.get(k) is not True or status.get(k)!="good" for k in all_keys):raise ValueError("production_non_two_system_requires_all8_good")
            coal=float(values["coal_amount"]);desliming=[float(values[k]) for k in ("desliming473","desliming474")]
            if not math.isfinite(coal) or coal<=10 or not all(v in (0.,1.) for v in desliming):raise ValueError("production_EX8_numeric_engine_contract_failed")
            raw=[coal,*system_values,*desliming]
            used=[min(bounds[1],max(bounds[0],value)) for value,bounds in zip(raw,model["bounds"])]
            correction=2.*math.tanh(sum(c*(value-reference)/scale for c,value,reference,scale in zip(model["coefs"],used,model["reference"],model["scale"]))/2.)
            prediction=30.5-.3*min(75.,max(25.,level))+correction
            route="non_two_system_EX8_RUNS"
        if not math.isfinite(prediction) or not 0<=prediction<=100:raise ValueError("production_output_outside_0_100_no_final_clipping")
        output.update(ex8_production_prediction=prediction,current_production_prediction=prediction,ex8_production_reason=None,production_policy_route=route)
    except (ValueError,TypeError,KeyError) as exc:output["ex8_production_reason"]=str(exc)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze", type=Path, required=True)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--input-manifest", type=Path)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise ValueError("refusing to overwrite a final prediction lock")
    actual_runtime = runtime_versions()
    expected_runtime = json.loads((args.freeze/"environment.json").read_text(encoding="utf-8"))
    mismatches = {name:{"expected": expected_runtime.get(name), "actual": actual} for name,actual in actual_runtime.items() if expected_runtime.get(name) != actual}
    if mismatches:
        refusal = {"status": "runtime_version_mismatch_refused_before_joblib_load", "runtime": actual_runtime, "mismatches": mismatches, "fits_performed": 0}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_durable_exclusive(args.output.with_name(args.output.stem+".runtime-refusal.json"), refusal)
        raise ValueError("runtime versions do not match frozen environment")
    if args.inputs.suffix.lower() == ".csv":
        with args.inputs.open(encoding="utf-8-sig", newline="") as stream:
            source_rows = list(csv.DictReader(stream))
    else:
        source_payload = json.loads(args.inputs.read_text(encoding="utf-8-sig"))
        source_rows = source_payload["rows"] if isinstance(source_payload, dict) else source_payload
    label_keys = {"actual", "ash", "ash_value", "value", "label", "target", "manual_value"}
    if any(label_keys.intersection(row) or label_keys.intersection(row.get("features", {})) for row in source_rows):
        raise ValueError("label-bearing file refused before any inference lock is written")
    runtime = json.loads((args.freeze/"sealed-runtime-manifest.json").read_text(encoding="utf-8"))
    for name, expected in runtime["files"].items():
        path = args.freeze/name
        if sha(path) != expected["sha256"] or path.stat().st_size != expected["bytes"]:
            raise ValueError("sealed runtime member mismatch: "+name)
    if args.input_manifest:
        seal = json.loads((args.freeze/"final-seal.json").read_text(encoding="utf-8"))
        if seal["sealed_runtime_manifest_sha256"] != sha(args.freeze/"sealed-runtime-manifest.json"):
            raise ValueError("final seal does not protect this exact runtime")
    source = importlib.util.spec_from_file_location("original_frozen_predict_cli", args.freeze/"predict.py")
    original = importlib.util.module_from_spec(source); source.loader.exec_module(original)
    selected_path = args.output.with_name(args.output.stem+".selected-only.json")
    forwarded = ["--freeze", str(args.freeze), "--inputs", str(args.inputs), "--output", str(selected_path)]
    if args.input_manifest:
        forwarded += ["--input-manifest", str(args.input_manifest)]
    original.main(forwarded)
    with selected_path.open("r+b") as selected_stream:
        selected_stream.flush(); os.fsync(selected_stream.fileno())
    lock = json.loads(selected_path.read_text(encoding="utf-8"))
    if args.inputs.suffix.lower() == ".csv":
        with args.inputs.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
    else:
        payload = json.loads(args.inputs.read_text(encoding="utf-8-sig")); rows = payload["rows"] if isinstance(payload, dict) else payload
    model = json.loads((args.freeze/"ex8-native-model.json").read_text(encoding="utf-8"))["model"]
    native_file = args.inputs
    native_rows = rows
    if args.input_manifest:
        collection = json.loads(args.input_manifest.read_text(encoding="utf-8"))
        if "baseline-ex8-inputs.json" not in collection["files"]:
            raise ValueError("collection manifest does not protect independent native EX8 inputs")
        native_file = args.input_manifest.parent/"baseline-ex8-inputs.json"
        native_payload = json.loads(native_file.read_text(encoding="utf-8"))
        native_rows = native_payload["rows"] if isinstance(native_payload, dict) else native_payload
    key = lambda row: (str(row.get("record_id")), row.get("assay_id"), row.get("sample_at"))
    native_by_key = {key(row): row for row in native_rows}
    if len(native_by_key) != len(native_rows) or set(native_by_key) != {key(row) for row in rows}:
        raise ValueError("native EX8 input identity/time coverage differs from candidate inputs")
    production_rows=None;production_file=None;production_model=None;production_availability=None
    if args.input_manifest:
        for name in ("production-ex8-inputs.json","production-ex8-lock.json"):
            if name not in collection["files"]:raise ValueError("production EX8 collection evidence missing "+name)
        production_file=args.input_manifest.parent/"production-ex8-inputs.json"
        payload=json.loads(production_file.read_text(encoding="utf-8"));production_rows=payload["rows"] if isinstance(payload,dict) else payload
        production_by_key={key(row):row for row in production_rows}
        if len(production_by_key)!=len(production_rows) or set(production_by_key)!={key(row) for row in rows}:raise ValueError("production EX8 identity/time coverage differs from candidate inputs")
        production_model_path=args.freeze/"ex8-production-model.json"
        production_model=json.loads(production_model_path.read_text(encoding="utf-8"))["model"]
        availability_path=args.input_manifest.parent/"production-ex8-lock.json"
        production_availability=json.loads(availability_path.read_text(encoding="utf-8"))
        if production_availability.get("schema")!="expert81-production-EX8-RUNS-availability-lock-v1" or production_availability.get("labels_read") is not False or production_availability.get("model_prediction_performed") is not False:raise ValueError("production availability lock is not label-free pre-prediction evidence")
        required_hashes={"input_file_sha256":sha(production_file),"parameter_sha256":sha(production_model_path),"input_evidence_sha256":sha(args.input_manifest.parent/"input-evidence.json"),"source_contract_sha256":sha(args.freeze/"source_contract.json")}
        if any(production_availability.get(k)!=v for k,v in required_hashes.items()):raise ValueError("production availability lock hash mismatch")
        availability_keys=[key(row) for row in production_availability["rows"]]
        if len(availability_keys)!=len(set(availability_keys)) or set(availability_keys)!=set(production_by_key):raise ValueError("production availability lock target coverage differs")
    for prediction,row in zip(lock["rows"], rows):
        prediction.update(baseline(native_by_key[key(row)], model))
        if production_rows is not None:prediction.update(production_baseline(production_by_key[key(row)],production_model))
        try:
            factors = row.get("features", row)
            system_values = [float(factors[k]) for k in ("sysA", "sysB", "sys401", "sys402")]
            prediction["system_count"] = int(sum(system_values)) if all(v in (0., 1.) for v in system_values) else None
        except (ValueError, KeyError, TypeError):
            prediction["system_count"] = None
        if production_rows is not None:
            left_count=prediction.get("production_system_count");right_count=prediction.get("system_count")
            prediction["comparison_system_count_consistent"]=(left_count==right_count) if left_count is not None and right_count is not None else None
            prediction["comparison_system_count_reason"]="candidate_and_production_feedback_counts_differ" if prediction["comparison_system_count_consistent"] is False else None
    lock.update(selected_only_lock_sha256=sha(selected_path), native_EX8_input_file_sha256=sha(native_file), native_EX8_model_sha256=sha(args.freeze/"ex8-native-model.json"),
                sealed_runtime_manifest_sha256=sha(args.freeze/"sealed-runtime-manifest.json"),
                runtime=actual_runtime, final_lock_write_contract="exclusive_create_flush_fsync", selected_only_lock_file=selected_path.name,
                baseline_contract="EX8 native legacy_if_prod three-system only; no RUNS substitution; two-system old production14/15 lookup separately")
    if production_rows is not None:
        lock.update(production_EX8_input_file_sha256=sha(production_file),production_EX8_model_sha256=sha(args.freeze/"ex8-production-model.json"),
                    production_EX8_availability_lock_sha256=sha(args.input_manifest.parent/"production-ex8-lock.json"),
                    production_baseline_contract_id="EX8_PRODUCTION_RUNS",comparison_baseline_field="current_production_prediction",
                    production_comparison_note="Actual non2 EX8 RUNS branch;2 fixed14/15. Archived input reconstruction, not proof of an on-site issued prediction; separate side coverage.")
    else:lock.update(comparison_baseline_field="old_production_policy_prediction",production_baseline_contract_id=None)
    lock["selected_predicted_at_utc"]=lock["predicted_at_utc"]
    lock["predicted_at_utc"]=datetime.now(timezone.utc).isoformat()
    write_durable_exclusive(args.output, lock)
    print(json.dumps({"prediction_lock_sha256": sha(args.output), "rows": len(lock["rows"]), "fits_performed": 0, "EX8_native_n": sum(r["ex8_native_prediction"] is not None for r in lock["rows"])}))


if __name__ == "__main__":
    main()
