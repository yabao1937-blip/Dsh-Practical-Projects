"""Assemble selected saved estimators into a new provisional final runtime; no fit."""
from __future__ import annotations

from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import shutil

import joblib

from scripts.gpt_expert81_db_primary_experiment import ROOT, OUT, metadata, normalized_rows, sha, write
from app.gpt_coarse.experiments.expert81_db_primary_predict import PUBLIC_FEATURES


def main():
    destination = OUT/"final-freeze"
    if destination.exists():
        raise ValueError("refusing to overwrite assembled final runtime")
    result = json.loads((OUT/"capacity-round2/results.json").read_text(encoding="utf-8"))
    original = json.loads((OUT/"results.json").read_text(encoding="utf-8"))
    protocol = json.loads((OUT/"protocol.json").read_text(encoding="utf-8"))
    selected_models = joblib.load(OUT/"capacity-round2/selected-models.joblib")
    rows,_ = normalized_rows(OUT/"data/training_rows.jsonl")
    branches = {}
    for branch, choice in result["selections"].items():
        model = selected_models[branch]
        if model["kind"] == "estimator":
            trained_ids = set(model["db_training_ids"])
        else:
            trained_ids = set()
            def collect(part):
                if part["kind"] == "estimator":
                    trained_ids.update(part["db_training_ids"])
                elif part["kind"] == "mixture":
                    for item in part["parts"]: collect(item)
                elif part["kind"] == "semantic_overlay":
                    collect(part["public"])
                    for item in part["namespaces"].values(): collect(item)
            collect(model)
        train = [r for r in rows if r["record_id"] in trained_ids]
        branches[branch] = {"status": "ok", "selected": choice["selected"], "model": model,
                            "fence": original["coverage"][branch]["fence"], "low_support": branch == "2",
                            "normal_training_ids": sorted(trained_ids),
                            "ranges": {k:{"min": min(r["features"][k] for r in train), "max": max(r["features"][k] for r in train)} for k in PUBLIC_FEATURES}}
    bundle = {"model_version": "EX81-DB-PRIMARY-20261103-CAP2-v1", "db_exposure_cutoff": protocol["db_exposure_cutoff"],
              "protocol_sha256": sha(OUT/"capacity-round2/protocol.json"), "branches": branches,
              "feature_contract": list(PUBLIC_FEATURES), "inference_contract": "purepredict_no_labels_no_fit_no_state_updates"}
    destination.mkdir()
    joblib.dump(bundle, destination/"model.joblib", compress=3)
    write(destination/"model.json", {k:{**{n:v for n,v in state.items() if n != "model"}, "model": metadata(state["model"])} for k,state in branches.items()})
    environment = json.loads((OUT/"freeze/environment.json").read_text(encoding="utf-8"))
    environment["threadpoolctl"] = importlib.metadata.version("threadpoolctl")
    write(destination/"environment.json", environment)
    for source,name in ((OUT/"freeze/inference_core.py", "inference_core.py"), (OUT/"freeze/predict.py", "predict.py"),
                        (OUT/"protocol.json", "protocol.json"), (OUT/"capacity-round2/protocol.json", "capacity-protocol.json")):
        shutil.copyfile(source, destination/name)
    write(destination/"training-scope.json", {"status": "provisional_pending_independent_final_audit_seal", "assembled_at_utc": datetime.now(timezone.utc).isoformat(),
          "db_exposure_cutoff": protocol["db_exposure_cutoff"], "all_exposed_assay_ids": protocol["all_exposed_assay_ids"],
          "common_training_evaluation_target_ids": protocol["common_target_assay_ids"], "selection": {b:c["selected"] for b,c in result["selections"].items()},
          "source_fingerprints": {str(p.relative_to(ROOT)):sha(p) for p in (OUT/"data/manifest.json", OUT/"protocol.json", OUT/"search-manifest.json", OUT/"capacity-round2/protocol.json", OUT/"capacity-round2/manifest.json")},
          "warning": "Current DB resubstitution winner across110 after an explicitly disclosed second development round. Not independent future accuracy; seal timestamp generated only after final independent acceptance."})
    core_names = [p.name for p in destination.iterdir() if p.is_file()]
    write(destination/"freeze-manifest.json", {"model_version": bundle["model_version"], "db_exposure_cutoff": bundle["db_exposure_cutoff"],
          "files": {name:{"bytes": (destination/name).stat().st_size, "sha256": sha(destination/name)} for name in sorted(core_names)}})
    for source,name in ((ROOT/"backend/scripts/predict_gpt_expert81_sealed.py", "predict-sealed.py"),
                        (ROOT/"backend/scripts/score_gpt_expert81_frozen.py", "score.py"),
                        (ROOT/"backend/models/gpt_expert8/20261012/model.json", "ex8-native-model.json"),
                        (OUT/"future-source-contract.json", "source_contract.json")):
        shutil.copyfile(source, destination/name)
    extra_names = ["predict-sealed.py", "score.py", "ex8-native-model.json", "source_contract.json"]
    write(destination/"sealed-runtime-manifest.json", {"schema": "expert81-final-runtime-addendum-v1", "freeze_manifest_sha256": sha(destination/"freeze-manifest.json"),
          "files": {name:{"bytes": (destination/name).stat().st_size, "sha256": sha(destination/name)} for name in extra_names}})
    print(json.dumps({"status": "assembled_provisional_no_fit", "selected": {b:c["selected"] for b,c in result["selections"].items()},
                      "freeze_manifest_sha256": sha(destination/"freeze-manifest.json"), "sealed_runtime_manifest_sha256": sha(destination/"sealed-runtime-manifest.json")}))


if __name__ == "__main__":
    main()
