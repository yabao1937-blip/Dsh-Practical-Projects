"""DB-primary finite search and immutable freeze, requiring an approved protocol."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import csv
from datetime import datetime, timezone, timedelta
import hashlib
import json
import math
from pathlib import Path
import platform
import shutil
import sys
import time

import joblib
import numpy as np
import scipy
import sklearn
from threadpoolctl import threadpool_limits

from app.gpt_coarse.experiments.expert81_db_primary_predict import PUBLIC_FEATURES, branch_of, numeric_features, raw_predict
from app.gpt_coarse.experiments.expert81_db_primary_search import candidate_specs, fence, fit_candidates, fit_primitive, normal
from scripts.gpt_expert81_continue_experiment import protected_hashes
from app.gpt_coarse.expert8 import predict as expert8_predict
from app.gpt_coarse.experiments import expert81 as first_core, expert81_expanded as expanded_core

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT/"output/gpt-expert81-db-primary-20261103"
BJ = timezone(timedelta(hours=8))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, data):
    if path.exists():
        raise ValueError(f"refusing to overwrite {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)+"\n", encoding="utf-8")


def statistics(rows):
    own = [r for r in rows if r["prediction"] is not None]
    errors = [abs(r["prediction"]-r["actual"]) for r in own]
    n = len(own); hits = sum(v <= 1. for v in errors)
    if n:
        z = 1.959963984540054; p = hits/n; denominator = 1+z*z/n
        center = (p+z*z/(2*n))/denominator
        radius = z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/denominator
        interval = [center-radius, center+radius]
    else:
        interval = None
    return {"eligible_n": len(rows), "n": n, "dates": len({r["date"] for r in own}), "hit1": hits,
            "hit1_rate": hits/n if n else None, "hit1_wilson95": interval,
            "MAE": float(np.mean(errors)) if n else None,
            "bias": float(np.mean([r["prediction"]-r["actual"] for r in own])) if n else None,
            "max_absolute_error": max(errors) if n else None,
            "rejected_ids": [r["record_id"] for r in rows if r["prediction"] is None]}


def scores(rows):
    result = {"all": statistics(rows), "normal": statistics([r for r in rows if r["normal"]])}
    for quality in ("strict", "minute_approximation"):
        own = [r for r in rows if r.get("public_quality") == quality]
        result[quality] = {"all": statistics(own), "normal": statistics([r for r in own if r["normal"]])}
    return result


def normalized_rows(path):
    payload = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    output, rejected = [], []
    seen = set()
    for raw in payload:
        identity = str(raw["record_id"])
        if identity in seen:
            raise ValueError("multiple current DB versions for one record")
        seen.add(identity)
        reason = ";".join(raw.get("exclusion_reasons", [])+raw.get("review_flags", [])) or raw.get("rejection_reason")
        if raw.get("source_type", "manual_db") not in ("manual_db", "manual_db_81", "manual_db_77", "db", "production_database"):
            raise ValueError("non-DB source in DB main table")
        if raw.get("public_quality") not in ("strict", "public_strict", "good", "minute_approximation"):
            reason = reason or "public_pairing_not_eligible"
        if not (raw.get("training_eligible_strict_default") or raw.get("training_eligible_approx_default")):
            reason = reason or "source_audited_eligibility_rejected"
        try:
            features = numeric_features(raw)
            actual = float(raw["actual"])
            if not math.isfinite(actual) or not 0 < actual <= 100:
                raise ValueError("invalid ash label")
            sample_at = datetime.fromisoformat(raw["sample_at"].replace("Z", "+00:00"))
            if sample_at.tzinfo is None:
                raise ValueError("sample clock domain not declared")
            for key in ("label_version_id", "available_at", "provenance"):
                if not raw.get(key):
                    raise ValueError(f"missing {key}")
            available_at = datetime.fromisoformat(raw["available_at"].replace("Z", "+00:00"))
            if available_at.tzinfo is None:
                raise ValueError("availability clock domain not declared")
            if raw.get("label_status", "captured") in ("label_review", "excluded", "invalid", "missing"):
                raise ValueError("label status not eligible")
        except (ValueError, KeyError, TypeError) as exc:
            reason = reason or str(exc)
        if reason:
            rejected.append({"record_id": identity, "assay_id": raw.get("assay_id"), "reason": reason})
            continue
        combined_features = dict(raw["features"]); combined_features.update(features)
        output.append({**raw, "record_id": identity, "features": combined_features, "actual": actual,
                       "sample_at": sample_at.astimezone(timezone.utc).isoformat(), "available_at": available_at.astimezone(timezone.utc).isoformat(),
                       "semantic_quality": raw.get("quality_by_namespace", {}).get(raw.get("feature_namespace")),
                       "date": sample_at.astimezone(BJ).date().isoformat(), "system_count": int(branch_of(features))})
    return sorted(output, key=lambda r: (r["sample_at"], r["record_id"])), rejected


def reference_rows(branch):
    source = ROOT/"output/gpt-expert81-expanded-20261102/normalized_roles.json"
    events = json.loads(source.read_text(encoding="utf-8"))["formal"]
    own = [r for r in events if r["domain"] == "legacy" and r["system_count"] == int(branch)]
    if not own:
        return []
    bounds = fence([r["ash"] for r in own])
    return [{**r, "actual": r["ash"]} for r in own if normal(r["ash"], bounds)]


def prediction_rows(models, targets, bounds, phase, fold, branch):
    output = []
    for method, model in models.items():
        raw = raw_predict(model, targets)
        if len(raw) != len(targets) or not np.isfinite(raw).all():
            raise ValueError(f"nonfinite/incomplete predictions in {method}")
        for target, value in zip(targets, raw):
            predicted = min(100., max(0., float(value)))
            output.append({"phase": phase, "fold": fold, "system_count": branch, "method": method,
                           "record_id": target["record_id"], "assay_id": target.get("assay_id"), "sample_at": target["sample_at"],
                           "date": target["date"], "actual": target["actual"], "normal": normal(target["actual"], bounds),
                           "prediction": predicted, "raw_prediction": float(value), "error": predicted-target["actual"],
                           "abs_error": abs(predicted-target["actual"]), "feature_namespace": target.get("feature_namespace")})
            output[-1]["public_quality"] = target["public_quality"]
    return output


def ranked(rows):
    specs = {s["id"]: s for s in candidate_specs()}
    table = [{"method": method, "scores": scores([r for r in rows if r["method"] == method]), "complexity": specs[method.split("__")[-1]]["complexity"]}
             for method in sorted({r["method"] for r in rows})]
    table.sort(key=lambda r: (-r["scores"]["normal"]["hit1"], r["scores"]["normal"]["MAE"], r["scores"]["all"]["MAE"], r["complexity"], r["method"]))
    mae = min(table, key=lambda r: (r["scores"]["normal"]["MAE"], -r["scores"]["normal"]["hit1"], r["scores"]["all"]["MAE"], r["complexity"], r["method"]))
    return {"selected": table[0]["method"], "normal_MAE_winner": mae["method"], "ranking": table}


def shared_training(train_raw):
    strict = [r for r in train_raw if r["public_quality"] == "strict"]
    if not strict:
        return None, [], []
    bounds = fence([r["actual"] for r in strict])
    strict_normal = [{**r, "training_weight": 1.} for r in strict if normal(r["actual"], bounds)]
    mixed_normal = strict_normal+[{**r, "training_weight": .5} for r in train_raw if r["public_quality"] == "minute_approximation" and normal(r["actual"], bounds)]
    return bounds, strict_normal, mixed_normal


def fit_variants(strict, mixed, reference):
    output = {}
    for variant, training in (("S", strict), ("M", mixed)):
        output.update({variant+"__"+key: value for key,value in fit_candidates(training, reference=reference).items()})
    return output


def metadata(model):
    if model["kind"] == "estimator":
        estimator = model["estimator"]
        return {**{k:v for k,v in model.items() if k != "estimator"},
                "estimator_class": type(estimator.named_steps["model"]).__name__,
                "parameters": {k: repr(v) for k,v in estimator.get_params(deep=True).items()},
                "scale_mean": estimator.named_steps["scale"].mean_.tolist(),
                "scale_scale": estimator.named_steps["scale"].scale_.tolist()}
    if model["kind"] == "mixture":
        return {"kind": "mixture", "weights": model["weights"], "parts": [metadata(p) for p in model["parts"]]}
    return {"kind": "semantic_overlay", "public": metadata(model["public"]), "namespaces": {k:metadata(v) for k,v in model["namespaces"].items()}}


def baseline_predictions(rows, coverage):
    ex8 = json.loads((ROOT/"backend/models/gpt_expert8/20261012/model.json").read_text(encoding="utf-8"))["model"]
    c7 = json.loads((ROOT/"output/gpt-expert81-20261102/candidate.json").read_text(encoding="utf-8"))["state"]
    e5 = json.loads((ROOT/"output/gpt-expert81-expanded-20261102/candidate.json").read_text(encoding="utf-8"))["state"]
    output = []
    for row in rows:
        branch = str(row["system_count"])
        native = row.get("feature_namespace") == "legacy_if_prod" and row.get("semantic_quality") in ("strict", "minute_approximation", "good")
        values = row["features"]
        ex8_value = expert8_predict(values, ex8) if native else None
        references = {"EX8_native_algorithm": ex8_value,
                      "old_production_policy": (14. if values["level"] >= 30. else 15.) if branch == "2" else ex8_value,
                      "C7_frozen": first_core.predict_state(c7, values)["prediction"] if native and branch == "3" else None,
                      "E5_frozen": expanded_core.predict_state(e5, values)["prediction"] if native and branch == "3" else None}
        for method,value in references.items():
            output.append({"phase": "current_DB_frozen_baseline_comparison", "method": method, "record_id": row["record_id"],
                           "assay_id": row["assay_id"], "sample_at": row["sample_at"], "date": row["date"], "system_count": branch,
                           "public_quality": row["public_quality"], "actual": row["actual"], "prediction": value,
                           "normal": normal(row["actual"], coverage[branch]["fence"]),
                           "interpretation": "two-system EX8 algorithm only; actual old production used14/15 lookup" if branch == "2" and method == "EX8_native_algorithm" else "frozen original baseline"})
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, default=OUT/"protocol.json")
    parser.add_argument("--execute-locked", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute_locked:
        print(json.dumps({"status": "prepared_no_data_read_no_fitting", "candidate_count": 2*len(candidate_specs())})); return
    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    if protocol["status"] != "locked_before_fitting" or protocol["candidate_specs"] != candidate_specs():
        raise ValueError("protocol not approved/locked or candidate definitions differ")
    if (OUT/"results.json").exists() or (OUT/"freeze").exists():
        raise ValueError("completed search/freeze exists; use an explicitly versioned new protocol instead")
    for relative, wanted in protocol["input_files"].items():
        if sha(ROOT/relative) != wanted:
            raise ValueError(f"DB/reference/source fingerprint differs: {relative}")
    before = protected_hashes()
    write(OUT/"search-lock.json", {"protocol_sha256": sha(args.protocol), "written_at_utc": datetime.now(timezone.utc).isoformat(),
                                 "protected_inputs": before, "input_files": protocol["input_files"], "candidate_count": 2*len(candidate_specs())})
    rows, rejected = normalized_rows(ROOT/protocol["training_rows_path"])
    if sorted(r["assay_id"] for r in rows) != protocol["common_target_assay_ids"]:
        raise ValueError("fixed70 common target pool differs from protocol")
    for row in rows:
        if row["assay_id"] == 197:
            raise ValueError("197 exclusion lost in normalized adapter")
    started = time.monotonic()
    predictions, folds, final_models, choices, branches, coverage = [], [], {}, {}, {}, {}
    with threadpool_limits(limits=1):
        for branch in sorted({str(r["system_count"]) for r in rows}):
            own = [r for r in rows if str(r["system_count"]) == branch]
            bounds, training, mixed_training = shared_training(own)
            dates = sorted({r["date"] for r in own}); train_dates = {r["date"] for r in training}
            coverage[branch] = {"eligible_n": len(own), "normal_n": len(training), "dates": dates,
                                "ids": [r["record_id"] for r in own], "normal_ids": [r["record_id"] for r in training], "fence": bounds,
                                "strict_training_normal_n": len(training), "mixed_training_normal_n": len(mixed_training),
                                "strict_target_n": sum(r["public_quality"] == "strict" for r in own),
                                "approximate_target_n": sum(r["public_quality"] == "minute_approximation" for r in own),
                                "namespace_counts": dict(Counter(r.get("feature_namespace", "unspecified") for r in own))}
            enough = branch == "3" and len(training) >= 5 and len(train_dates) >= 2
            enough = enough or branch == "2" and len(training) >= 10 and len(train_dates) >= 3
            if not enough:
                if branch == "2" and len(training) >= 3 and len(train_dates) >= 2:
                    model = fit_primitive(candidate_specs()[0], training)
                    branches[branch] = {"status": "ok", "model": model, "selected": "own_two_system_median_low_support", "low_support": True,
                                        "ranges": {k:{"min": min(r["features"][k] for r in training), "max": max(r["features"][k] for r in training)} for k in PUBLIC_FEATURES}}
                else:
                    branches[branch] = {"status": "unavailable", "reason": "insufficient_independent_system_branch_support", "low_support": True}
                continue
            reference = reference_rows(branch)
            models = fit_variants(training, mixed_training, reference)
            current = prediction_rows(models, own, bounds, "DB_resubstitution_selection", "full", branch)
            predictions.extend(current); selection = ranked(current); choices[branch] = selection
            final_models[branch] = models
            branches[branch] = {"status": "ok", "selected": selection["selected"], "model": models[selection["selected"]],
                                "fence": bounds, "normal_training_ids": [r["record_id"] for r in (mixed_training if selection["selected"].startswith("M__") else training)], "low_support": branch == "2",
                                "ranges": {k:{"min": min(r["features"][k] for r in (mixed_training if selection["selected"].startswith("M__") else training)), "max": max(r["features"][k] for r in (mixed_training if selection["selected"].startswith("M__") else training))} for k in PUBLIC_FEATURES}}
            for phase in ("leave_one_date_out", "forward_date_current_version_diagnostic"):
                for day in dates:
                    if time.monotonic()-started > protocol["budget"]["maximum_seconds"]:
                        raise TimeoutError("finite experiment exceeded predeclared budget; no automatic retry")
                    train_raw = [r for r in own if (r["date"] != day if phase == "leave_one_date_out" else r["date"] < day)]
                    test = [r for r in own if r["date"] == day]
                    if not train_raw:
                        folds.append({"branch": branch, "phase": phase, "date": day, "status": "unavailable_no_past_training", "test_ids": [r["record_id"] for r in test]}); continue
                    local_bounds, train, mixed_train = shared_training(train_raw)
                    minimum_n, minimum_dates = (10, 3) if branch == "2" else (5, 2)
                    if len(train) < minimum_n or len({r["date"] for r in train}) < minimum_dates:
                        folds.append({"branch": branch, "phase": phase, "date": day, "status": "unavailable_insufficient_training", "test_ids": [r["record_id"] for r in test]}); continue
                    fitted = fit_variants(train, mixed_train, reference)
                    predictions.extend(prediction_rows(fitted, test, local_bounds, phase, day, branch))
                    folds.append({"branch": branch, "phase": phase, "date": day, "status": "ok", "train_ids": [r["record_id"] for r in train],
                                  "mixed_train_ids": [r["record_id"] for r in mixed_train], "mixed_training_weights": [r["training_weight"] for r in mixed_train],
                                  "test_ids": [r["record_id"] for r in test], "fence": local_bounds,
                                  "available_labels_before_target": {r["record_id"]: sum(t["available_at"] < r["sample_at"] for t in train) for r in test}})
    if "3" not in choices:
        raise ValueError("main three-system branch lacks enough audited DB support")
    summaries = {branch: {phase: {method: scores([r for r in predictions if r["system_count"] == branch and r["phase"] == phase and r["method"] == method])
                                for method in [v+"__"+s["id"] for v in ("S", "M") for s in candidate_specs()]}
                         for phase in sorted({r["phase"] for r in predictions if r["system_count"] == branch})} for branch in choices}
    after = protected_hashes()
    if before != after:
        raise ValueError("frozen older inputs changed during search")
    for relative, wanted in protocol["input_files"].items():
        if sha(ROOT/relative) != wanted:
            raise ValueError("DB/source data changed during search")
    bundle = {"model_version": protocol["model_version"], "db_exposure_cutoff": protocol["db_exposure_cutoff"],
              "protocol_sha256": sha(args.protocol), "branches": branches, "feature_contract": list(PUBLIC_FEATURES),
              "inference_contract": "purepredict_no_labels_no_fit_no_state_updates"}
    baselines = baseline_predictions(rows, coverage)
    baseline_summary = {branch: {method: scores([r for r in baselines if r["system_count"] == branch and r["method"] == method]) for method in sorted({r["method"] for r in baselines})} for branch in coverage}
    write(OUT/"results.json", {"selections": choices, "summary": summaries, "coverage": coverage, "rejected_rows": rejected,
                               "folds": folds, "predictions": predictions, "elapsed_seconds": time.monotonic()-started,
                               "baseline_predictions": baselines, "baseline_summary": baseline_summary,
                               "selection_interpretation": "current DB resubstitution fit only; CV/current-version forward diagnostics, no new independent verification"})
    keys = list(predictions[0])
    with (OUT/"predictions.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, keys); writer.writeheader(); writer.writerows(predictions)
    with (OUT/"baseline-predictions.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, list(baselines[0])); writer.writeheader(); writer.writerows(baselines)
    joblib.dump(final_models, OUT/"search-models.joblib", compress=3)
    freeze = OUT/"freeze"; freeze.mkdir()
    joblib.dump(bundle, freeze/"model.joblib", compress=3)
    write(freeze/"model.json", {k:{**{n:v for n,v in branch.items() if n != "model"}, "model": metadata(branch["model"]) if branch["status"] == "ok" else None} for k,branch in branches.items()})
    env = {"python": sys.version, "platform": platform.platform(), "sklearn": sklearn.__version__, "numpy": np.__version__, "scipy": scipy.__version__, "joblib": joblib.__version__}
    write(freeze/"environment.json", env)
    shutil.copyfile(ROOT/"backend/app/gpt_coarse/experiments/expert81_db_primary_predict.py", freeze/"inference_core.py")
    shutil.copyfile(ROOT/"backend/scripts/predict_gpt_expert81_frozen.py", freeze/"predict.py")
    shutil.copyfile(args.protocol, freeze/"protocol.json")
    write(freeze/"training-scope.json", {"db_exposure_cutoff": protocol["db_exposure_cutoff"], "frozen_at_utc": datetime.now(timezone.utc).isoformat(),
                                         "coverage": coverage, "input_files": protocol["input_files"], "protected_baselines_unchanged": before == after,
                                         "selection_current_fit_only": True, "status": "provisional_candidate_pending_independent_final_seal"})
    write(freeze/"freeze-manifest.json", {"model_version": bundle["model_version"], "db_exposure_cutoff": bundle["db_exposure_cutoff"],
        "files": {p.name:{"bytes": p.stat().st_size, "sha256": sha(p)} for p in sorted(freeze.iterdir()) if p.is_file()}})
    for relative in protocol["input_files"]:
        if relative.startswith("backend/") and relative.endswith(".py"):
            destination = OUT/"source_snapshot"/relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT/relative, destination)
    write(OUT/"search-manifest.json", {"protocol_sha256": sha(args.protocol), "protected_inputs_before": before, "protected_inputs_after": after,
        "files": {str(p.relative_to(OUT)):{"bytes": p.stat().st_size, "sha256": sha(p)} for p in sorted(OUT.rglob("*")) if p.is_file() and "data" not in p.relative_to(OUT).parts}})
    print(json.dumps({"selected": {k:v["selected"] for k,v in choices.items()}, "elapsed_seconds": time.monotonic()-started,
                      "freeze_manifest_sha256": sha(freeze/"freeze-manifest.json"), "selections": choices}, ensure_ascii=False))


if __name__ == "__main__":
    main()
