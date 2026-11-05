"""Finite EX8.1 continuation; local exposed inputs, no activation or network."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import timedelta
import csv
import json
from pathlib import Path
import time

import numpy as np

from app.gpt_coarse.experiments import expert81_expanded as core
from scripts import gpt_expert81_expanded_experiment as previous
from scripts.gpt_expert81_experiment import score

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "output/gpt-expert81-expanded-20261102"
DEFAULT_OUT = ROOT / "output/gpt-expert81-continue-20261103"
METHODS = ("C7", "E5", "F0", "F1", "F2", "F3")


def seal(state):
    state.pop("state_hash", None)
    state["state_hash"] = core.digest(state)
    return state


def fit_candidate(events, at, method, *, group, target_id=None, mode="historical_entry_proxy"):
    kwargs = dict(group_key=group, exclude_record_id=target_id, config={"mode": mode})
    if method in ("C7", "E5"):
        return core.fit_state(events, at, "E2" if method == "C7" else "E5", **kwargs)
    if method == "F3":
        left = fit_candidate(events, at, "C7", group=group, target_id=target_id, mode=mode)
        right = fit_candidate(events, at, "E5", group=group, target_id=target_id, mode=mode)
        state = deepcopy(left)
        state.update(candidate_id=method, model={"kind": "fixed_blend", "weights": [.5, .5], "components": [left, right]})
        if right["status"] != "ok":
            state.update(status="unavailable", reason=right["reason"])
        return seal(state)
    base = "E0" if method in ("F0", "F1") else "E5"
    state = core.fit_state(events, at, base, **kwargs)
    state["candidate_id"] = method
    if state["status"] != "ok":
        return seal(state)
    current = core.pools(core.available_rows(events, at, exclude_record_id=target_id), group, state["config"])["current"]
    if method in ("F0", "F1"):
        state["config"].update(huber_delta_pp=.5, ridge_lambda=4.)
        state["model"] = core.weighted_huber(current, ["level"] if method == "F0" else ["level", "coal_amount"], state["config"])
    elif method == "F2":
        prior = state["model"]["legacy_response"]
        state["unshrunk_legacy_response"] = deepcopy(prior)
        prior["coefficients"] = [prior["coefficients"][0]] + [.5*v for v in prior["coefficients"][1:]]
        # The shrunken response is a deterministic transform, not a fresh Huber optimum.
        prior["fit_interpretation"] = "fixed half-slope transform; diagnostics refer to unshrunk response"
        residuals = [r["ash"]-core.linear_raw(prior, r["features"], "legacy") for r in current]
        state["model"]["calibration"] = {"kind": "constant", "value": float(np.median(residuals))}
    else:
        raise ValueError("undeclared continuation candidate")
    return seal(state)


def predict(state, features):
    core.verify_state(state)
    if state["status"] != "ok" or state["model"]["kind"] != "fixed_blend":
        return core.predict_state(state, features)
    parts = [predict(s, features) for s in state["model"]["components"]]
    if any(p["raw_prediction"] is None for p in parts):
        return {"prediction": None, "raw_prediction": None, "status": "unavailable", "reason": "blend_component_unavailable", "state_hash": state["state_hash"]}
    raw = core.finite(sum(w*p["raw_prediction"] for w, p in zip(state["model"]["weights"], parts)))
    lo, hi = state["config"]["bounds"]
    return {"prediction": min(hi, max(lo, raw)), "raw_prediction": raw, "status": "ok", "reason": None,
            "state_hash": state["state_hash"], "outside_current_ranges": sorted(set().union(*(p["outside_current_ranges"] for p in parts)))}


def metrics(rows):
    return {"eligible_n": len(rows), "predicted_n": sum(r["prediction"] is not None for r in rows),
            "all": score(rows), "normal": score([r for r in rows if r["normal"]])}


def summary(rows):
    return {phase: {m: metrics([r for r in rows if r["phase"] == phase and r["method"] == m]) for m in METHODS}
            for phase in sorted({r["phase"] for r in rows})}


def select(rows):
    segments = ("early_development", "main_development")
    own = [r for r in rows if r["phase"] in segments]
    candidates = []
    for method in METHODS[2:]:
        checks = {}
        for phase in segments:
            new = metrics([r for r in own if r["method"] == method and r["phase"] == phase])
            ref = metrics([r for r in own if r["method"] == "C7" and r["phase"] == phase])
            checks[phase] = new["predicted_n"] == ref["eligible_n"] and new["normal"]["hit1"] >= ref["normal"]["hit1"] and new["normal"]["MAE"] <= ref["normal"]["MAE"] + 1e-9
        combined = metrics([r for r in own if r["method"] == method])
        candidates.append({"method": method, "gates": checks, "eligible": all(checks.values()), "combined": combined})
    passing = sorted([c for c in candidates if c["eligible"]], key=lambda c: (-c["combined"]["normal"]["hit1"], c["combined"]["normal"]["MAE"], c["combined"]["all"]["MAE"], c["method"]))
    return {"selected": passing[0]["method"] if passing else "C7", "new_candidate_selected": bool(passing), "candidates": candidates,
            "interpretation": "exposed historical development selection only"}


def protected_hashes():
    old_protocol = previous.checked_json(previous.PROTOCOL_PATH, previous.LOCKED_PROTOCOL_SHA)
    result = previous.verify_inputs(old_protocol)
    manifest = previous.checked_json(SOURCE / "manifest.json")
    for name, spec in manifest["files"].items():
        p = SOURCE / name
        if previous.file_hash(p) != spec["sha256"]:
            raise ValueError(f"expanded artifact changed: {name}")
        result[str(p.relative_to(ROOT))] = spec["sha256"]
    for p in sorted((ROOT / "backend/models").rglob("*.json")):
        result[str(p.relative_to(ROOT))] = previous.file_hash(p)
    for p in (ROOT / "backend/data/train_js.json", SOURCE / "manifest.json"):
        result[str(p.relative_to(ROOT))] = previous.file_hash(p)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    out = args.output
    protocol_path = DEFAULT_OUT / "protocol.json"
    protocol = previous.checked_json(protocol_path)
    protocol_sha = previous.file_hash(protocol_path)
    if (out / "result.json").exists():
        raise ValueError("refusing to overwrite completed continuation")
    if previous.file_hash(SOURCE / "manifest.json") != protocol["source_manifest_sha256"]:
        raise ValueError("wrong expanded source manifest")
    if previous.file_hash(ROOT / "output/gpt-expert81-holdout-20261102/prediction-lock.json") != protocol["prediction_lock_sha256"]:
        raise ValueError("wrong original prediction lock")
    before = protected_hashes()
    out.mkdir(parents=True, exist_ok=True)
    previous.write_json(out / "protocol-lock.json", {"sha256": protocol_sha, "status": "sealed_before_numerical_fitting", "protected_inputs": before})
    roles = previous.checked_json(SOURCE / "normalized_roles.json")
    old = previous.checked_json(SOURCE / "result.json")
    old_model = previous.checked_json(SOURCE / "candidate.json")
    locked = previous.checked_json(ROOT / "output/gpt-expert81-holdout-20261102/prediction-lock.json")
    locked_features = {r["assay_id"]: r["features"] for r in locked["rows"]}
    historical = {r["assay_id"]: r for r in roles["historical_entry_proxy"] if r["version_id"].startswith("frozen68:")}
    db_current = {r["assay_id"]: r for r in roles["snapshot_diagnostic"] if r["domain"] == "current" and not r["version_id"].startswith("frozen68:")}
    target_specs = [r for r in old["predictions"] if r["method"] == "E5" and not r["phase"].startswith("two_system:")]
    predictions, strict_rows, descriptive, states = [], [], [], {}
    targets = {}
    started = time.monotonic()
    for spec in target_specs:
        identity, phase = spec["assay_id"], spec["phase"]
        diagnostic = phase.startswith("revealed_")
        target = deepcopy(db_current[identity] if diagnostic else historical[identity])
        if diagnostic:
            target.update(features=locked_features[identity], ash=spec["actual"], sample_at=spec["sample_at"])
        if identity in protocol["reserved_ids"]:
            raise ValueError("reserved target in training round")
        targets[str(identity)] = target
        events = roles["snapshot_diagnostic" if diagnostic else "historical_entry_proxy"]
        mode = "snapshot_diagnostic" if diagnostic else "historical_entry_proxy"
        for method in METHODS:
            if time.monotonic()-started > protocol["budget"]["maximum_training_seconds"]:
                raise TimeoutError("fixed matrix training budget exceeded; no retries")
            state = fit_candidate(events, target["sample_at"], method, group=target["group_key"], target_id=target["record_id"], mode=mode)
            states[state["state_hash"]] = state
            predictions.append(previous.record(target, method, predict(state, target["features"]), phase, core.inside(target["ash"], state["fence"])))
        if not diagnostic:
            # Actual first captures replace initial-created proxies for this audit.
            state = fit_candidate(roles["snapshot_diagnostic"], target["sample_at"], "F0", group=target["group_key"], target_id=target["record_id"], mode="snapshot_diagnostic")
            states[state["state_hash"]] = state
            strict_rows.append(previous.record(target, "F0", predict(state, target["features"]), "strict_capture_old_history_unassessable", core.inside(target["ash"], state["fence"])))
    # Validate full baseline replay before selection. No new parameter choice follows.
    differences = {}
    for method, legacy in (("C7", "E2"), ("E5", "E5")):
        ref = {(r["phase"], r["assay_id"]): r for r in old["predictions"] if r["method"] == legacy}
        diffs = []
        for r in predictions:
            if r["method"] != method:
                continue
            previous_row = ref[r["phase"], r["assay_id"]]
            if (r["prediction"] is None) != (previous_row["prediction"] is None) or r["normal"] != previous_row["normal"]:
                raise ValueError("baseline coverage/fence regression")
            if r["prediction"] is not None:
                diffs.append(abs(r["prediction"]-previous_row["prediction"]))
        differences[method] = max(diffs, default=0.)
        if differences[method] > 1e-10:
            raise ValueError("baseline prediction regression")
    selection = select(predictions)
    formal_at = core.parse_time(old_model["state"]["cutoff"])
    final = {}
    for method in METHODS:
        state = fit_candidate(roles["formal"], formal_at, method, group=previous.GROUP, mode="formal")
        final[method] = state
        states[state["state_hash"]] = state
    current = [r for r in core.available_rows(roles["formal"], formal_at) if r["domain"] == "current" and r["quality"] == "strict" and r["group_key"] == previous.GROUP]
    for target in current:
        for method, state in final.items():
            descriptive.append(previous.record(target, method, predict(state, target["features"]), "current_descriptive_in_sample", core.inside(target["ash"], state["fence"])))
    combined = {}
    for name, phases in (("development23", ["early_development", "main_development"]), ("historical29", ["early_development", "main_development", "later_diagnostic"])):
        combined[name] = {m: metrics([r for r in predictions if r["method"] == m and r["phase"] in phases]) for m in METHODS}
    reconstruction_max = 0.
    for r in predictions:
        rebuilt = predict(states[r["state_hash"]], targets[str(r["assay_id"])]["features"])["prediction"]
        if r["prediction"] is not None:
            reconstruction_max = max(reconstruction_max, abs(rebuilt-r["prediction"]))
        elif rebuilt is not None:
            raise ValueError("reconstruction changed rejection")
    after = protected_hashes()
    if before != after:
        raise ValueError("protected baseline changed during training")
    elapsed = time.monotonic()-started
    result = {"protocol_sha256": protocol_sha, "selection": selection, "summary": summary(predictions), "combined": combined,
              "descriptive": summary(descriptive), "strict_capture_replay": metrics(strict_rows),
              "predictions": predictions, "strict_capture_predictions": strict_rows, "descriptive_predictions": descriptive,
              "elapsed_seconds": elapsed, "canonical_source_n": 283, "historical_target_n": len(historical),
              "warnings": ["historical initial-created replay is an uncertified development proxy", "no independent new validation", "204/206 labels unread", "two-system separate low support unchanged", "legacy if_prod groups are observed signals, not physical shutdown"]}
    model = {"schema": "expert81-continue-offline-v1", "status": "offline_not_activated", "selected": selection["selected"],
             "state": final[selection["selected"]], "fixed_candidate_states": final, "protocol_sha256": protocol_sha,
             "formal_training_available_at": core.stamp(formal_at), "source_manifest_sha256": protocol["source_manifest_sha256"],
             "two_system_reference": "unchanged output/gpt-expert81-expanded-20261102/candidate.json#two_system_states"}
    model["artifact_hash"] = core.digest(model)
    validation = {"protected_inputs_unchanged": before == after, "baseline_max_prediction_difference": differences,
                  "saved_state_reconstruction_max_difference": reconstruction_max, "strict_capture_old_history_predicted_n": sum(r["prediction"] is not None for r in strict_rows),
                  "strict_capture_old_history_eligible_n": len(strict_rows), "matrix_runs": 1, "new_candidate_count": 4,
                  "reserved_label_ids_present": sorted({r["assay_id"] for r in roles["formal"] if r.get("assay_id") in protocol["reserved_ids"]}),
                  "network_calls": 0, "protected_hashes_before": before, "protected_hashes_after": after}
    for name, value in (("result.json", result), ("candidate.json", model), ("states.json", states), ("targets.json", targets), ("validation.json", validation)):
        previous.write_json(out / name, value)
    keys = list(predictions[0])
    with (out / "predictions.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, keys)
        writer.writeheader()
        writer.writerows(predictions)
    for source in (Path(__file__), protocol_path, ROOT / "backend/tests/test_gpt_expert81_continue.py"):
        dest = out / "source_snapshot" / source.relative_to(ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(source.read_bytes())
    previous.write_json(out / "manifest.json", {"protocol_sha256": protocol_sha, "inputs": before,
        "files": {str(p.relative_to(out)): {"sha256": previous.file_hash(p), "bytes": p.stat().st_size} for p in sorted(out.rglob("*")) if p.is_file()}})
    print(json.dumps({"selected": selection["selected"], "elapsed_seconds": elapsed, "development23": combined["development23"], "validation": {k:v for k,v in validation.items() if not k.startswith("protected_hashes")}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
