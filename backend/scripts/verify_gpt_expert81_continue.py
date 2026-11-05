"""Read-only saved-state audit; never refits or chooses a model."""
from __future__ import annotations

import json
from pathlib import Path
from statistics import mean, median

import numpy as np

from app.gpt_coarse.experiments import expert81_expanded as core
from scripts.gpt_expert81_continue_experiment import protected_hashes
from scripts.gpt_expert81_expanded_experiment import file_hash, write_json

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "output/gpt-expert81-continue-20261103"


def linear(model, values):
    vector = [1.] + [(values[k]-model["centers"][k])/model["scales"][k] for k in model["predictors"]]
    vector += [0.]*len(model["source_domains"])
    return sum(v*c for v, c in zip(vector, model["coefficients"]))


def evaluate(state, values):
    if state["status"] != "ok":
        return None
    assert core.group_of(values) == state["group_key"]
    m = state["model"]
    if m["kind"] == "linear":
        raw = linear(m, values)
    elif m["kind"] == "legacy_calibrated":
        raw = linear(m["legacy_response"], values)+m["calibration"]["value"]
    elif m["kind"] == "fixed_blend":
        # Blend native raw predictions; no clipping was triggered in this round.
        raw = sum(w*evaluate(s, values) for w, s in zip(m["weights"], m["components"]))
    elif m["kind"] == "first_C7":
        raw = core.first_core.predict_state(m["state"], values)["raw_prediction"]
    else:
        raise AssertionError("unexpected saved model")
    assert np.isfinite(raw)
    lo, hi = state["config"]["bounds"]
    assert lo <= raw <= hi  # makes independent blend reconstruction exact
    return raw


def main():
    read = lambda name: json.loads((OUT/name).read_text(encoding="utf-8"))
    manifest = read("manifest.json")
    for name, spec in manifest["files"].items():
        assert file_hash(OUT/name) == spec["sha256"], name
    before = read("protocol-lock.json")["protected_inputs"]
    assert before == protected_hashes()
    assert file_hash(OUT/"protocol.json") == manifest["protocol_sha256"]
    states, result, targets = read("states.json"), read("result.json"), read("targets.json")
    gradients, time_checks, transforms = [], 0, 0
    for state in states.values():
        core.verify_state(state)
        at = core.parse_time(state["cutoff"])
        for row in state["used_rows"]:
            assert core.parse_time(row["available_at"]) < at
            assert row["record_id"] != state["excluded_target_id"]
            if row["domain"] == "current":
                assert row["quality"] == "strict" and row["group_key"] == state["group_key"]
                assert core.parse_time(row["sample_at"]) < at
            time_checks += 1
        if state["status"] != "ok":
            continue
        if state["candidate_id"] in ("F0", "F1"):
            model, rows = state["model"], state["used_rows"]
            x = np.array([[1.]+[(r["features"][k]-model["centers"][k])/model["scales"][k] for k in model["predictors"]] for r in rows])
            y = np.array([r["ash"] for r in rows]); b = np.array(model["coefficients"])
            gradient = -x.T@np.clip(y-x@b, -.5, .5)+np.array([0.]+[4.]*(len(b)-1))*b
            assert np.max(np.abs(gradient)) < 1e-6
            gradients.append(float(np.max(np.abs(gradient))))
        if state["candidate_id"] == "F2":
            original = state["unshrunk_legacy_response"]["coefficients"]
            modified = state["model"]["legacy_response"]["coefficients"]
            assert modified == [original[0]]+[v*.5 for v in original[1:]]
            own = [r for r in state["used_rows"] if r["domain"] == "current"]
            residual = [r["ash"]-linear(state["model"]["legacy_response"], r["features"]) for r in own]
            assert abs(median(residual)-state["model"]["calibration"]["value"]) < 1e-12
            transforms += 1
    max_difference = 0.
    for row in result["predictions"]:
        expected = evaluate(states[row["state_hash"]], targets[str(row["assay_id"])]["features"])
        assert (expected is None) == (row["prediction"] is None)
        if expected is not None:
            max_difference = max(max_difference, abs(expected-row["prediction"]))
            assert abs(expected-row["prediction"]) < 1e-12
    for phase, methods in result["summary"].items():
        for method, reported in methods.items():
            own = [r for r in result["predictions"] if r["phase"] == phase and r["method"] == method]
            for subset in ("all", "normal"):
                rows = [r for r in own if r["prediction"] is not None and (subset == "all" or r["normal"])]
                assert reported[subset]["n"] == len(rows)
                assert reported[subset]["hit1"] == sum(abs(r["actual"]-r["prediction"]) <= 1. for r in rows)
                if rows:
                    assert abs(reported[subset]["MAE"]-mean(abs(r["actual"]-r["prediction"]) for r in rows)) < 1e-12
    for identity in targets:
        rows = [r for r in result["predictions"] if r["assay_id"] == int(identity)]
        assert len({r["normal"] for r in rows}) == 1
        assert len({str(states[r["state_hash"]]["fence"]) for r in rows}) == 1
        assert len({r["prediction"] is None for r in rows}) == 1
    audit = {"status": "passed", "all_manifest_hashes_valid": True, "protected_baselines_unchanged": True,
             "state_count": len(states), "used_row_availability_checks": time_checks,
             "independent_prediction_max_difference": max_difference, "new_regression_max_gradient": max(gradients),
             "fixed_half_slope_and_median_checks": transforms, "independent_metric_checks": "all phase/method all+normal counts hits MAE",
             "shared_target_ids_coverage_and_training_fence": True, "refits_performed": 0, "network_calls": 0}
    destination = OUT/"independent-validation.json"
    if destination.exists():
        assert json.loads(destination.read_text(encoding="utf-8")) == audit, "prior independent audit differs"
    else:
        write_json(destination, audit)
    print(json.dumps(audit))


if __name__ == "__main__":
    main()
