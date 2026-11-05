"""Package saved forward-selected3sys and unchanged2sys policy; zero refits."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

import joblib

from scripts.gpt_expert81_db_primary_experiment import ROOT, OUT, metadata, normalized_rows, sha, write
from app.gpt_coarse.experiments.expert81_db_primary_predict import PUBLIC_FEATURES


def main():
    destination = OUT/"production-freeze"
    if destination.exists(): raise ValueError("production runtime already assembled")
    choice = json.loads((OUT/"production-selection/results.json").read_text(encoding="utf-8"))
    if choice["selected_three_system"] != "S__BOOST_absolute_error_2": raise ValueError("independently reviewed expected choice changed")
    current = json.loads((OUT/"results.json").read_text(encoding="utf-8"))
    protocol = json.loads((OUT/"protocol.json").read_text(encoding="utf-8"))
    models = joblib.load(OUT/"search-models.joblib")
    model = models["3"][choice["selected_three_system"]]
    rows,_ = normalized_rows(OUT/"data/training_rows.jsonl")
    branches = {}
    for branch in ("2", "3"):
        own = [r for r in rows if str(r["system_count"]) == branch]
        used_ids = set(model["db_training_ids"]) if branch == "3" else set()
        support = [r for r in own if r["record_id"] in used_ids] if branch == "3" else own
        branches[branch] = {"status": "ok", "selected": choice["selected_three_system"] if branch == "3" else "retain_current14_15_lookup",
                           "model": model if branch == "3" else {"kind": "two_system_production_lookup", "level_threshold": 30., "at_or_above": 14., "below": 15., "fits_performed": 0},
                           "fence": current["coverage"][branch]["fence"], "normal_training_ids": sorted(used_ids),
                           "fence_source_strict_ids": [r["record_id"] for r in own if r["public_quality"] == "strict"],
                           "route_interpretation": "saved full strict DB model, no refit" if branch == "3" else "unchanged current fixed policy, not a learned new2sys model",
                           "ranges": {k:{"min": min(r["features"][k] for r in support), "max": max(r["features"][k] for r in support)} for k in PUBLIC_FEATURES}}
        if branch == "3":
            branches[branch]["supported_system_masks"] = [list(values) for values in sorted({tuple(int(r["features"][k]) for k in PUBLIC_FEATURES[2:]) for r in support})]
    bundle = {"model_version": "EX81-PRODUCTION-SELECT-20261103-v1", "db_exposure_cutoff": protocol["db_exposure_cutoff"],
              "protocol_sha256": sha(OUT/"production-selection/protocol.json"), "branches": branches,
              "feature_contract": list(PUBLIC_FEATURES), "inference_contract": "purepredict_no_labels_no_fit_no_state_updates"}
    destination.mkdir()
    joblib.dump(bundle, destination/"model.joblib", compress=3)
    write(destination/"model.json", {k:{**{n:v for n,v in state.items() if n != "model"}, "model": metadata(state["model"]) if k == "3" else state["model"]} for k,state in branches.items()})
    source_core = (OUT/"freeze/inference_core.py").read_text(encoding="utf-8")
    needle = '    if model["kind"] == "estimator":\n'
    policy = '    if model["kind"] == "two_system_production_lookup":\n        return np.asarray([14. if r["features"]["level"] >= 30. else 15. for r in rows], dtype=float)\n'
    if source_core.count(needle) != 1: raise ValueError("unexpected frozen pure inference structure")
    source_core = source_core.replace(needle,policy+needle)
    guard_at = '        factors = dict(row.get("features", row)); factors.update(values)\n'
    guard = '        if branch == "3" and [int(values[k]) for k in PUBLIC_FEATURES[2:]] not in state["supported_system_masks"]:\n            raise ValueError("unsupported_three_system_feedback_combination")\n'
    if source_core.count(guard_at) != 1: raise ValueError("unexpected scope guard insertion point")
    (destination/"inference_core.py").write_text(source_core.replace(guard_at,guard+guard_at), encoding="utf-8")
    for source,name in ((OUT/"final-freeze/environment.json", "environment.json"), (OUT/"freeze/predict.py", "predict.py"),
                        (OUT/"production-selection/protocol.json", "protocol.json")):
        shutil.copyfile(source,destination/name)
    write(destination/"training-scope.json", {"status": "provisional_production_oriented_candidate_pending_audit_seal", "assembled_at_utc": datetime.now(timezone.utc).isoformat(),
          "db_exposure_cutoff": protocol["db_exposure_cutoff"], "all_exposed_assay_ids": protocol["all_exposed_assay_ids"],
          "all_DB_target_ids": protocol["common_target_assay_ids"], "selection": {"3": choice["selected_three_system"], "2": "retain_current14_15_lookup"},
          "fit_calls_during_assembly": 0, "three_system_training_ids": model["db_training_ids"], "two_system_training_ids": [],
          "selection_evidence": {str(p.relative_to(ROOT)):sha(p) for p in (OUT/"production-selection/protocol.json", OUT/"production-selection/results.json", OUT/"search-models.joblib", OUT/"data/manifest.json")},
          "warning": "Selected on already exposed forward-date development diagnosis, not prospective. Prior GB400 fitting winner remains a research artifact and is not this production-oriented candidate."})
    core_names = [p.name for p in destination.iterdir() if p.is_file()]
    write(destination/"freeze-manifest.json", {"model_version": bundle["model_version"], "db_exposure_cutoff": bundle["db_exposure_cutoff"],
          "files": {name:{"bytes": (destination/name).stat().st_size, "sha256": sha(destination/name)} for name in sorted(core_names)}})
    extra = [(ROOT/"backend/scripts/predict_gpt_expert81_sealed.py", "predict-sealed.py"), (ROOT/"backend/scripts/score_gpt_expert81_frozen.py", "score.py"),
             (ROOT/"backend/models/gpt_expert8/20261012/model.json", "ex8-native-model.json"), (OUT/"future-source-contract.json", "source_contract.json"),
             (OUT/"input-runtime-dependencies.json", "input-runtime-dependencies.json")]
    for source,name in extra: shutil.copyfile(source,destination/name)
    write(destination/"sealed-runtime-manifest.json", {"schema": "expert81-production-runtime-addendum-v1", "freeze_manifest_sha256": sha(destination/"freeze-manifest.json"),
          "files": {name:{"bytes": (destination/name).stat().st_size, "sha256": sha(destination/name)} for _,name in extra}})
    print(json.dumps({"status": "production_candidate_assembled_no_fit", "model_sha256": sha(destination/"model.joblib"),
                      "freeze_manifest_sha256": sha(destination/"freeze-manifest.json"), "sealed_runtime_manifest_sha256": sha(destination/"sealed-runtime-manifest.json"),
                      "input_runtime_dependencies_sha256": sha(destination/"input-runtime-dependencies.json")}))


if __name__ == "__main__": main()
