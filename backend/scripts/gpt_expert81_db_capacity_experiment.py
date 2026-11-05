"""One fixed capacity extension after first86; same DB70 and ranking."""
from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import time

import joblib
from threadpoolctl import threadpool_limits

from app.gpt_coarse.experiments.expert81_db_capacity_search import candidate_specs, fit_candidates
from app.gpt_coarse.experiments.expert81_db_primary_search import candidate_specs as first_specs
from scripts.gpt_expert81_db_primary_experiment import ROOT, OUT as FIRST_OUT, normalized_rows, prediction_rows, scores, sha, shared_training, write
from scripts.gpt_expert81_continue_experiment import protected_hashes

OUT = FIRST_OUT/"capacity-round2"


def choose_combined(rows):
    complexities = {v+"__"+s["id"]:s["complexity"] for v in ("S", "M") for s in first_specs()}
    complexities.update({s["id"]:s["complexity"] for s in candidate_specs()})
    ranking = [{"method": method, "scores": scores([r for r in rows if r["method"] == method]), "complexity": complexities[method]}
               for method in sorted({r["method"] for r in rows})]
    ranking.sort(key=lambda r: (-r["scores"]["normal"]["hit1"], r["scores"]["normal"]["MAE"], r["scores"]["all"]["MAE"], r["complexity"], r["method"]))
    mae = min(ranking, key=lambda r: (r["scores"]["normal"]["MAE"], -r["scores"]["normal"]["hit1"], r["scores"]["all"]["MAE"], r["complexity"], r["method"]))
    return {"selected": ranking[0]["method"], "normal_MAE_winner": mae["method"], "ranking": ranking}


def conflicts(rows):
    grouped = defaultdict(list)
    keys = ("level", "coal_amount", "sysA", "sysB", "sys401", "sys402")
    for row in rows:
        grouped[tuple(row["features"][k] for k in keys)].append(row)
    output = []
    unavoidable_misses = 0
    for values, group in grouped.items():
        if len(group) <= 1:
            continue
        labels = sorted(r["actual"] for r in group)
        maximum_hits = max(sum(abs(y-c) <= 1.+1e-12 for y in labels) for c in [v-1 for v in labels]+[v+1 for v in labels])
        unavoidable_misses += len(labels)-maximum_hits
        output.append({"factors": dict(zip(keys, values)), "ids": [r["record_id"] for r in group], "actual": labels, "max_possible_hit1": maximum_hits})
    return {"exact_factor_duplicate_groups": output, "exact_factor_conflict_unavoidable_misses": unavoidable_misses,
            "max_possible_hit1_upper_bound": len(rows)-unavoidable_misses, "warning": "Exact duplicates only; absence does not prove factors identify coal quality or future accuracy"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--execute-locked", action="store_true"); args = parser.parse_args(argv)
    if not args.execute_locked:
        print(json.dumps({"status": "prepared_no_fitting", "new_candidates": len(candidate_specs())})); return
    protocol_path = OUT/"protocol.json"; protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    if protocol["status"] != "locked_before_fitting" or protocol["candidate_specs"] != candidate_specs():
        raise ValueError("capacity protocol differs from executable24")
    if (OUT/"results.json").exists():
        raise ValueError("refusing to overwrite capacity results")
    for name,wanted in protocol["input_files"].items():
        if sha(ROOT/name) != wanted:
            raise ValueError("capacity input/source changed: "+name)
    protected = protected_hashes()
    write(OUT/"search-lock.json", {"protocol_sha256": sha(protocol_path), "locked_at_utc": datetime.now(timezone.utc).isoformat(), "protected_inputs": protected, "input_files": protocol["input_files"]})
    first = json.loads((FIRST_OUT/"results.json").read_text(encoding="utf-8"))
    rows,_ = normalized_rows(FIRST_OUT/"data/training_rows.jsonl")
    if sorted(r["assay_id"] for r in rows) != protocol["common_target_assay_ids"]:
        raise ValueError("capacity target identities changed")
    predictions, fold_log, full_models, choices = [], [], {}, {}
    started = time.monotonic()
    with threadpool_limits(limits=1):
        for branch in ("2", "3"):
            own = [r for r in rows if str(r["system_count"]) == branch]
            bounds, strict, mixed = shared_training(own)
            models = fit_candidates(mixed); full_models[branch] = models
            predictions.extend(prediction_rows(models, own, bounds, "DB_resubstitution_selection", "full", branch))
            dates = sorted({r["date"] for r in own})
            for phase in ("leave_one_date_out", "forward_date_current_version_diagnostic"):
                for day in dates:
                    if time.monotonic()-started > protocol["budget"]["maximum_seconds"]:
                        raise TimeoutError("finite capacity budget exceeded")
                    raw_train = [r for r in own if (r["date"] != day if phase == "leave_one_date_out" else r["date"] < day)]
                    targets = [r for r in own if r["date"] == day]
                    local, strict_train, mixed_train = shared_training(raw_train)
                    minimum_n, minimum_dates = (10, 3) if branch == "2" else (5, 2)
                    if len(strict_train) < minimum_n or len({r["date"] for r in strict_train}) < minimum_dates:
                        fold_log.append({"branch": branch, "phase": phase, "date": day, "status": "unavailable_insufficient_branch_support", "test_ids": [r["record_id"] for r in targets]}); continue
                    fitted = fit_candidates(mixed_train)
                    predictions.extend(prediction_rows(fitted, targets, local, phase, day, branch))
                    fold_log.append({"branch": branch, "phase": phase, "date": day, "status": "ok", "strict_train_ids": [r["record_id"] for r in strict_train],
                                     "mixed_train_ids": [r["record_id"] for r in mixed_train], "mixed_training_weights": [r["training_weight"] for r in mixed_train],
                                     "test_ids": [r["record_id"] for r in targets], "fence": local})
    combined = first["predictions"]+predictions
    for branch in ("2", "3"):
        choices[branch] = choose_combined([r for r in combined if r["system_count"] == branch and r["phase"] == "DB_resubstitution_selection"])
    summaries = {b: {phase: {method: scores([r for r in combined if r["system_count"] == b and r["phase"] == phase and r["method"] == method])
                            for method in [r["method"] for r in choices[b]["ranking"]]}
                     for phase in ("DB_resubstitution_selection", "leave_one_date_out", "forward_date_current_version_diagnostic")} for b in ("2", "3")}
    if protected != protected_hashes():
        raise ValueError("protected older baseline changed")
    for name,wanted in protocol["input_files"].items():
        if sha(ROOT/name) != wanted:
            raise ValueError("capacity source/input changed during fitting")
    result = {"selections": choices, "summary": summaries, "new_predictions": predictions, "combined_predictions": combined, "folds": fold_log,
              "conflicts": {b:conflicts([r for r in rows if str(r["system_count"]) == b]) for b in ("2", "3")},
              "elapsed_seconds": time.monotonic()-started, "new_candidate_count": 24, "total_comparison_count": 110,
              "warning": "After seeing first86, a separate predeclared capacity extension on the same exposed DB; all selection scores resubstitution only"}
    write(OUT/"results.json", result)
    for filename, records in (("new-predictions.csv", predictions), ("combined-predictions.csv", combined)):
        with (OUT/filename).open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, list(records[0])); writer.writeheader(); writer.writerows(records)
    joblib.dump(full_models, OUT/"search-models.joblib", compress=3)
    old_models = joblib.load(FIRST_OUT/"search-models.joblib")
    selected = {b:(full_models[b] if choices[b]["selected"].startswith("R2__") else old_models[b])[choices[b]["selected"]] for b in choices}
    joblib.dump(selected, OUT/"selected-models.joblib", compress=3)
    for relative in protocol["input_files"]:
        if relative.endswith(".py"):
            destination = OUT/"source_snapshot"/relative; destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((ROOT/relative).read_bytes())
    write(OUT/"manifest.json", {"protocol_sha256": sha(protocol_path), "protected_inputs_unchanged": True, "input_files": protocol["input_files"],
          "files": {str(p.relative_to(OUT)):{"sha256": sha(p), "bytes": p.stat().st_size} for p in sorted(OUT.rglob("*")) if p.is_file()}})
    print(json.dumps({"selected": {b:c["selected"] for b,c in choices.items()}, "MAE_winner": {b:c["normal_MAE_winner"] for b,c in choices.items()}, "elapsed_seconds": result["elapsed_seconds"]}))


if __name__ == "__main__":
    main()
