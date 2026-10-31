"""Offline normal-assay study. All adaptation is restricted to previously available labels."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
import numpy as np
from scipy.optimize import least_squares

from .level_relationship import LOCAL, cohort, date_of, design, digest, midnight, ols, predict as baseline_predict

EPS = 1e-12


def available(rows, cutoff):
    return [r for r in rows if datetime.fromisoformat(r["observed_at"]) < cutoff
            and datetime.fromisoformat(r["created_at"]) < cutoff]


def fences(rows, protocol):
    values = np.array([r["value"] for r in rows], dtype=float)
    q1, q3 = np.quantile(values, [.25, .75], method="linear")
    spread = max(float(q3 - q1), protocol["iqr_floor_pp"])
    margin = protocol["iqr_multiplier"] * spread
    return {"lower": float(q1 - margin), "upper": float(q3 + margin),
            "q1": float(q1), "q3": float(q3), "raw_iqr": float(q3 - q1), "effective_iqr": spread}


def inside(value, bounds):
    return bounds["lower"] <= value <= bounds["upper"]


def densest_center(values, tolerance):
    """Maximize +/-tolerance coverage, then minimize MAE; deterministic, training-only."""
    y = np.sort(np.asarray(values, dtype=float))
    if not len(y):
        raise ValueError("Cannot fit an empty coverage interval")
    median = float(np.median(y))
    choices = []
    right = 0
    for left in range(len(y)):
        right = max(right, left)
        while right + 1 < len(y) and y[right + 1] - y[left] <= 2 * tolerance + EPS:
            right += 1
        lo, hi = y[right] - tolerance, y[left] + tolerance
        center = float(np.clip(median, min(lo, hi), max(lo, hi)))
        errors = abs(y - center)
        hits = int(np.sum(errors <= tolerance + EPS))
        choices.append((-hits, float(np.mean(errors)), abs(center - median), center))
    return min(choices)[-1]


def constant(value, fallback=None):
    return {"kind": "constant", "coef": [float(value)], "fallback": fallback}


def linear(rows, *, robust=True, coal=False, scale=1.0):
    y = np.array([r["value"] for r in rows], dtype=float)
    x = design(rows, coal=coal)
    if len(rows) < x.shape[1] + 1 or np.linalg.matrix_rank(x) < x.shape[1]:
        return constant(np.median(y), "insufficient or rank-deficient regression; clean median")
    coefficients = ols(x, y)
    if robust:
        fitted = least_squares(lambda b: x @ b - y, coefficients, loss="huber", f_scale=scale, max_nfev=2000)
        if not fitted.success:
            raise ValueError("Huber optimizer failed: " + fitted.message)
        coefficients = fitted.x
    return {"kind": "linear", "coal": coal, "coef": coefficients.tolist(), "fallback": None,
            "basis": "level centered at50; coal centered at500 and scaled by100"}


def value_of(model, rows):
    if model["kind"] == "constant":
        return np.full(len(rows), model["coef"][0])
    if model["kind"] == "neighbors":
        output = []
        for row in rows:
            def distance(sample):
                d = ((sample["level"] - row["features"]["level"]) / model["level_scale"]) ** 2
                if model["coal"]:
                    d += ((sample["coal"] - row["features"]["coal_amount"]) / model["coal_scale"]) ** 2
                return d, -datetime.fromisoformat(sample["observed_at"]).timestamp(), sample["id"]
            nearest = sorted(model["samples"], key=distance)[:model["count"]]
            values = [s["value"] for s in nearest]
            output.append(densest_center(values, model["tolerance"]) if model["majority"] else float(np.median(values)))
        return np.array(output)
    return design(rows, coal=model.get("coal", False)) @ np.array(model["coef"])


def shifted(model, offset):
    result = {**model, "coef": list(model["coef"]), "intercept_shift": float(offset)}
    result["coef"][0] += float(offset)
    return result


def recent_rows(clean, prediction_at, protocol):
    lower = prediction_at - timedelta(hours=protocol["recent_max_age_hours"])
    ordered = sorted((r for r in clean if lower <= datetime.fromisoformat(r["observed_at"]) < prediction_at),
                     key=lambda r: (datetime.fromisoformat(r["observed_at"]), r["id"]))
    return ordered[-protocol["recent_count"]:]


def fit_snapshot(training, prediction_at, protocol):
    bounds = fences(training, protocol)
    clean = [r for r in training if inside(r["value"], bounds)]
    if not clean:
        raise ValueError("No normal training labels")
    y = np.array([r["value"] for r in clean])
    recent = recent_rows(clean, prediction_at, protocol)
    huber = linear(clean, scale=protocol["huber_new_scale_pp"])
    offset = densest_center(y - value_of(huber, clean), protocol["tolerance_pp"])
    recent_bias = float(np.median([r["value"] for r in recent] - value_of(huber, recent))) if recent else 0.
    recent_bias *= len(recent) / (len(recent) + protocol["recent_bias_pseudocount"])
    models = {
        "raw_huber_level": linear(training, scale=protocol["huber_previous_scale_pp"]),
        "clean_huber_level": huber,
        "clean_majority_level": shifted(huber, offset),
        "clean_recent_bias5": shifted(huber, recent_bias),
        "clean_huber_level_coal": linear(clean, coal=True, scale=protocol["huber_new_scale_pp"]),
        "clean_ols_level": linear(clean, robust=False),
        "clean_median": constant(np.median(y)),
        "clean_recent_median5": constant(np.median([r["value"] for r in recent]) if recent else np.median(y),
                                         None if recent else "no assay within24h; clean median"),
        "clean_mode": constant(densest_center(y, protocol["tolerance_pp"]))}
    if "neighbor_count" in protocol:
        samples = [{"id": r["id"], "observed_at": r["observed_at"], "value": r["value"],
                    "level": r["features"]["level"], "coal": r["features"]["coal_amount"]} for r in clean]
        for name, coal, majority in (("clean_local_median_level", False, False),
                                     ("clean_local_median_level_coal", True, False),
                                     ("clean_local_majority_level_coal", True, True)):
            models[name] = {"kind": "neighbors", "coal": coal, "majority": majority,
                            "count": protocol["neighbor_count"], "level_scale": protocol["neighbor_level_scale"],
                            "coal_scale": protocol["neighbor_coal_scale"], "tolerance": protocol["tolerance_pp"],
                            "samples": samples, "fallback": None}
    return {"bounds": bounds, "training_ids": [r["id"] for r in training],
            "normal_training_ids": [r["id"] for r in clean],
            "excluded_training_ids": [r["id"] for r in training if not inside(r["value"], bounds)],
            "recent_ids": [r["id"] for r in recent], "models": models,
            "low_history": len(training) < protocol["low_history_warning_n"]}


def score(rows, protocol):
    if not rows:
        return {"n": 0, "dates": 0}
    errors = np.array([abs(r["prediction"] - r["actual"]) for r in rows])
    by_date = defaultdict(list)
    for r in rows:
        by_date[r["date"]].append(abs(r["prediction"] - r["actual"]))
    tol = protocol["tolerance_pp"]
    return {"n": len(rows), "dates": len(by_date), "hit1": int(np.sum(errors <= tol + EPS)),
            "hit1_rate": float(np.mean(errors <= tol + EPS)),
            "hit2": int(np.sum(errors <= protocol["secondary_tolerance_pp"] + EPS)),
            "mae": float(np.mean(errors)), "median_error": float(np.median(errors)),
            "p90_error": float(np.quantile(errors, .9)), "max_error": float(np.max(errors)),
            "date_hit1_rate": float(np.mean([np.mean(np.array(e) <= tol + EPS) for e in by_date.values()])),
            "date_mae": float(np.mean([np.mean(e) for e in by_date.values()]))}


def choose_candidate(scores, order):
    valid = [s for s in scores if s["model"] in order and s.get("n", 0)]
    if not valid:
        return None
    return min(valid, key=lambda s: (-s["hit1_rate"], s["mae"], order.index(s["model"])))["model"]


def run(audit, protocol, frozen):
    if audit["dataset_sha256"] != protocol["dataset_sha256"] or digest(audit["rows"]) != protocol["dataset_sha256"]:
        raise ValueError("Audit does not match locked data hash")
    all_rows = audit["rows"]
    predictions, snapshots, skipped, summaries, coverage = [], {}, [], [], []
    selected = None
    for systems in (3, 2):
        for approx in (False, True):
            source = [r for r in cohort(all_rows, systems, approx) if protocol["training_start"] <= date_of(r) < protocol["replay_end_exclusive"]]
            targets = [r for r in cohort(all_rows, systems, True) if protocol["evaluation_start"] <= date_of(r) < protocol["replay_end_exclusive"]]
            for availability in ("preceding_day", "entry_time"):
                group = f"systems{systems}_" + ("strict_plus_approx" if approx else "strict") + "_" + availability
                group_records = []
                for row in targets:
                    at = datetime.fromisoformat(row["observed_at"])
                    cutoff = midnight(date_of(row)) if availability == "preceding_day" else at
                    training = available(source, cutoff)
                    phase = "selection" if date_of(row) < protocol["selection_end_exclusive"] else "later_replay"
                    common = {"group": group, "id": row["id"], "observed_at": row["observed_at"],
                              "date": date_of(row), "phase": phase, "quality": row["eligibility"],
                              "actual": row["value"], "level": row["features"]["level"],
                              "coal": row["features"]["coal_amount"], "cutoff": cutoff.isoformat()}
                    if len(training) < protocol["minimum_available_labels"]:
                        skipped.append({**common, "reason": "insufficient available training labels", "available_n": len(training)})
                        continue
                    snapshot = fit_snapshot(training, at, protocol)
                    snapshot_id = group + ":" + str(row["id"])
                    snapshots[snapshot_id] = {**snapshot, "prediction_at": at.isoformat(), "cutoff": cutoff.isoformat()}
                    normal = inside(row["value"], snapshot["bounds"])
                    names = protocol["baselines"] + protocol["candidate_order"] + (["two_system_lookup"] if systems == 2 else [])
                    for name in names:
                        if name in snapshot["models"]:
                            prediction = value_of(snapshot["models"][name], [row])[0]
                        else:
                            prediction = baseline_predict([row], name, None, frozen)[0]
                        if not np.isfinite(prediction):
                            raise ValueError("Nonfinite prediction")
                        record = {**common, "snapshot": snapshot_id, "model": name,
                                  "prediction": float(prediction), "normal": normal,
                                  "lower": snapshot["bounds"]["lower"], "upper": snapshot["bounds"]["upper"],
                                  "error": float(prediction - row["value"])}
                        group_records.append(record)
                predictions.extend(group_records)
                names = sorted({r["model"] for r in group_records})
                for quality in ("strict", "minute_approximation"):
                    for phase in ("selection", "later_replay", "all_replay"):
                        eligible = [r for r in targets if r["eligibility"] == quality and
                                    (phase == "all_replay" or ("selection" if date_of(r) < protocol["selection_end_exclusive"] else "later_replay") == phase)]
                        rr = [r for r in group_records if r["quality"] == quality and (phase == "all_replay" or r["phase"] == phase)]
                        predicted_ids = {r["id"] for r in rr}
                        coverage.append({"group": group, "quality": quality, "phase": phase,
                                         "eligible": len(eligible), "predicted": len(predicted_ids),
                                         "skipped": len(eligible) - len(predicted_ids),
                                         "normal": len({r["id"] for r in rr if r["normal"]})})
                        for name in names:
                            for subset in ("all", "normal"):
                                records = [r for r in rr if r["model"] == name and (subset == "all" or r["normal"])]
                                metric = score(records, protocol)
                                summaries.append({"group": group, "quality": quality, "phase": phase,
                                                  "model": name, "subset": subset, **metric,
                                                  "hit1_over_all_eligible": metric.get("hit1", 0) / len(eligible) if eligible else None})
                if systems == 3 and not approx and availability == "preceding_day":
                    selection = [s for s in summaries if s["group"] == group and s["quality"] == "strict"
                                 and s["phase"] == "selection" and s["subset"] == "normal"]
                    selected = choose_candidate(selection, protocol["candidate_order"])
    final = {}
    final_at = midnight(protocol["replay_end_exclusive"])
    for systems in (3, 2):
        training = available(cohort(all_rows, systems), final_at)
        snapshot = fit_snapshot(training, final_at, protocol)
        rr = []
        for name, model in snapshot["models"].items():
            for row, prediction in zip(training, value_of(model, training)):
                rr.append({"date": date_of(row), "model": name, "actual": row["value"], "prediction": float(prediction),
                           "normal": inside(row["value"], snapshot["bounds"])})
        fit_metrics = [{"model": name, "subset": subset,
                        **score([r for r in rr if r["model"] == name and (subset == "all" or r["normal"])], protocol)}
                       for name in snapshot["models"] for subset in ("all", "normal")]
        final[str(systems)] = {**snapshot, "as_of": final_at.isoformat(), "fit_metrics": fit_metrics,
                               "not_activated": True, "warning": "fitting scores are not predictive validation"}
    return {"schema": "gpt-normal-ash-study-v1", "status": "offline_only_not_activated",
            "dataset_sha256": protocol["dataset_sha256"], "protocol_sha256": digest(protocol),
            "selected_algorithm": selected, "selected_using": "systems3 strict preceding_day selection dates only",
            "metrics": summaries, "coverage": coverage, "predictions": predictions,
            "snapshots": snapshots, "skipped": skipped, "final_candidates": final}
