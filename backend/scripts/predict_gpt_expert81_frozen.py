"""Batch factor-only prediction against a hash-verified frozen bundle; never fit."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path

import joblib


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze", type=Path, required=True)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--input-manifest", type=Path)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise ValueError("refusing to overwrite a prediction ledger")
    manifest = json.loads((args.freeze/"freeze-manifest.json").read_text(encoding="utf-8"))
    for name, expected in manifest["files"].items():
        if sha(args.freeze/name) != expected["sha256"]:
            raise ValueError(f"frozen member hash mismatch: {name}")
    provenance = {"provenance_verified": False, "input_mode": "unverified_factor_file"}
    if args.input_manifest:
        input_manifest = json.loads(args.input_manifest.read_text(encoding="utf-8"))
        if input_manifest.get("schema") != "expert81-future-input-manifest-v1" or input_manifest.get("status") != "complete" or input_manifest.get("labels_read") is not False:
            raise ValueError("not a complete label-free future collection manifest")
        for name, expected in input_manifest["files"].items():
            member = args.input_manifest.parent/name
            if member.resolve().parent != args.input_manifest.parent.resolve():
                raise ValueError("manifest member must be a direct collection sibling")
            if sha(member) != expected["sha256"] or member.stat().st_size != expected["bytes"]:
                raise ValueError(f"collection evidence hash/size mismatch: {name}")
        for name in ("inputs.json", "input-evidence.json", "query-receipts.json", "http-receipts.json", "collection-plan.json"):
            if name not in input_manifest["files"]:
                raise ValueError(f"collection evidence missing {name}")
        checks = {"input_file_sha256": sha(args.inputs), "freeze_manifest_sha256": sha(args.freeze/"freeze-manifest.json"),
                  "model_file_sha256": sha(args.freeze/"model.joblib"), "final_seal_sha256": sha(args.freeze/"final-seal.json"),
                  "source_contract_sha256": sha(args.freeze/"source_contract.json")}
        if any(input_manifest.get(key) != value for key,value in checks.items()):
            raise ValueError("collection does not match exact input/freeze/model/seal/source contract")
        seal = json.loads((args.freeze/"final-seal.json").read_text(encoding="utf-8"))
        if seal["freeze_manifest_sha256"] != checks["freeze_manifest_sha256"] or seal["input_source_contract_sha256"] != checks["source_contract_sha256"]:
            raise ValueError("final seal differs from collection contract")
        provenance = {"provenance_verified": True, "input_mode": "collection_receipt_chain_verified_raw_evidence_rechecked_at_reveal",
                      "input_manifest_sha256": sha(args.input_manifest), "source_contract_sha256": checks["source_contract_sha256"],
                      "final_seal_sha256": checks["final_seal_sha256"]}
    module_spec = importlib.util.spec_from_file_location("frozen_expert81_inference", args.freeze/"inference_core.py")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    bundle = joblib.load(args.freeze/"model.joblib")
    if args.inputs.suffix.lower() == ".csv":
        with args.inputs.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    else:
        payload = json.loads(args.inputs.read_text(encoding="utf-8-sig"))
        rows = payload["rows"] if isinstance(payload, dict) else payload
    predictions = module.predict_rows(bundle, rows)
    if args.input_manifest and sorted(r.get("assay_id") for r in rows) != sorted(input_manifest["assay_ids"]):
        raise ValueError("collection assay IDs differ from input rows")
    ledger = {"schema": "expert81-frozen-label-free-prediction-lock-v1", "predicted_at_utc": datetime.now(timezone.utc).isoformat(),
              "input_file_sha256": sha(args.inputs), "freeze_manifest_sha256": sha(args.freeze/"freeze-manifest.json"),
              "model_file_sha256": sha(args.freeze/"model.joblib"), "model_version": bundle["model_version"],
              "frozen_training_cutoff": bundle["db_exposure_cutoff"], "labels_read": False, "fits_performed": 0,
              "rows": predictions, **provenance}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(ledger, ensure_ascii=False, indent=2, allow_nan=False)+"\n", encoding="utf-8")
    print(json.dumps({"rows": len(predictions), "predicted": sum(r["prediction"] is not None for r in predictions),
                      "prediction_lock_sha256": sha(args.output), "fits_performed": 0}))


if __name__ == "__main__":
    main()
