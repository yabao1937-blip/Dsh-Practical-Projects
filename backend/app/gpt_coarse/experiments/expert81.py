"""Pure, version-aware EXPERT8.1 fitting/prediction. No I/O or runtime activation.

Availability evidence is supplied by the caller; an initial database created_at
is not evidence that a subsequently edited value existed at that time.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math

import numpy as np

BJ = timezone(timedelta(hours=8))
UTC = timezone.utc
MODEL_VERSION = "EXPERT8.1"
CANDIDATE_IDS = tuple(f"C{i}" for i in range(8))
DEFAULT_CONFIG = {
    "allowed_groups": ["0,1,1,1,0,0"],
    "minimum_normal_labels": 5,
    "minimum_normal_sample_dates": 2,
    "iqr_floor_pp": 1.0,
    "iqr_multiplier": 1.5,
    "recent_max_age_hours": 168.0,
    "minimum_recent": 3,
    "recent_bias_pseudocount": 5.0,
    "half_life_hours": 72.0,
    "huber_delta_pp": 1.0,
    "ridge_lambda": 1.0,
    "feature_centers": {"level": 50.0, "coal_amount": 600.0},
    "feature_scales": {"level": 10.0, "coal_amount": 100.0},
    "maximum_iterations": 2000,
    "coefficient_tolerance": 1e-10,
    "output_bounds": [0.0, 100.0],
    "receipt_mode": "formal",
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def parse_time(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    return result.astimezone(UTC)


def stamp(value):
    return parse_time(value).isoformat(timespec="microseconds")


def finite(value, name="value"):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def configuration(config=None):
    result = deepcopy(DEFAULT_CONFIG)
    if config:
        unknown = set(config) - set(result)
        if unknown:
            raise ValueError(f"unknown EXPERT8.1 config keys: {sorted(unknown)}")
        result.update(deepcopy(config))
    positive = ["minimum_normal_labels", "minimum_normal_sample_dates", "iqr_floor_pp",
                "iqr_multiplier", "recent_max_age_hours", "minimum_recent", "recent_bias_pseudocount",
                "half_life_hours", "huber_delta_pp", "ridge_lambda", "maximum_iterations", "coefficient_tolerance"]
    if any(finite(result[k], k) <= 0 for k in positive):
        raise ValueError("configuration values must be positive")
    for key in ("level", "coal_amount"):
        finite(result["feature_centers"][key], key)
        if finite(result["feature_scales"][key], key) <= 0:
            raise ValueError("feature scales must be positive")
    lo, hi = map(finite, result["output_bounds"])
    if not 0 <= lo < hi <= 100:
        raise ValueError("invalid ash output bounds")
    if result["receipt_mode"] not in ("formal", "historical_entry_proxy", "historical_midnight_proxy"):
        raise ValueError("unknown receipt evidence mode")
    return result


def available_versions(history, prediction_at, *, group_key, exclude_assay_id=None):
    """Latest version actually available strictly before this prediction.

    Future values and the current target's value are never read. Tombstones
    supersede prior versions only when their own availability passes the gate.
    """
    cutoff = parse_time(prediction_at)
    latest = {}
    for raw in history:
        assay_id = str(raw["assay_id"])
        if exclude_assay_id is not None and assay_id == str(exclude_assay_id):
            continue
        available_at = parse_time(raw["version_available_at"])
        if available_at >= cutoff:
            continue
        if not raw.get("version_id") or not isinstance(raw.get("provenance"), dict) or not raw["provenance"].get("kind"):
            raise ValueError("label version ID and availability provenance required")
        row = deepcopy(raw)
        row.update(version_available_at=stamp(available_at), version_id=str(raw["version_id"]))
        if assay_id in latest:
            previous = latest[assay_id]
            previous_at = parse_time(previous["version_available_at"])
            if available_at == previous_at and digest(row) != digest(previous):
                raise ValueError("ambiguous simultaneous versions for one assay")
            if available_at < previous_at:
                continue
        latest[assay_id] = row
    selected = []
    for row in latest.values():
        # A later tombstone/group/time revision supersedes the old version
        # even if that new version is not eligible for the requested group.
        if row.get("deleted", False) or str(row.get("group_key")) != str(group_key):
            continue
        sample_at = parse_time(row["sample_at"])
        if sample_at >= cutoff:
            continue
        if parse_time(row["version_available_at"]) < sample_at:
            raise ValueError("label version cannot be available before its sample")
        row["sample_at"] = stamp(sample_at)
        row["value"] = finite(row["value"], "assay")
        if not 0 <= row["value"] <= 100:
            raise ValueError("assay outside 0..100")
        row["features"] = {key: finite(row["features"][key], key) for key in ("level", "coal_amount")}
        row["frozen_prediction"] = finite(row["frozen_prediction"], "frozen_prediction")
        selected.append(row)
    return sorted(selected, key=lambda r: (parse_time(r["sample_at"]), str(r["assay_id"]), r["version_id"]))


def label_fence(rows, config=None):
    cfg = configuration(config)
    if not rows:
        return None
    q1, q3 = map(float, np.quantile([r["value"] for r in rows], [.25, .75], method="linear"))
    span = max(q3-q1, cfg["iqr_floor_pp"])
    return {"q1": q1, "q3": q3, "iqr": q3-q1,
            "lower": q1-cfg["iqr_multiplier"]*span, "upper": q3+cfg["iqr_multiplier"]*span}


def inside(value, fence):
    return fence is not None and fence["lower"] <= value <= fence["upper"]


def recent_rows(rows, prediction_at, count, cfg):
    cutoff = parse_time(prediction_at)
    lower = cutoff-timedelta(hours=cfg["recent_max_age_hours"])
    return [r for r in rows if lower <= parse_time(r["sample_at"]) < cutoff][-count:]


def weighted_median(values, weights):
    pairs = sorted(zip(map(float, values), map(float, weights)))
    total = sum(w for _, w in pairs)
    if not total > 0:
        raise ValueError("weighted median requires positive total weight")
    cumulative = 0.0
    for i, (value, weight) in enumerate(pairs):
        cumulative += weight
        if abs(cumulative-total/2) <= 1e-12*total and i+1 < len(pairs):
            return (value+pairs[i+1][0])/2
        if cumulative > total/2:
            return value
    return pairs[-1][0]


def huber_ridge_fit(rows, predictors, cfg):
    """Minimize SUM Huber(data residual) + lambda/2 * squared slopes.

    Ridge is never a robustified pseudo-observation. Fixed physical scaling
    makes the declared lambda comparable across historical folds.
    """
    x = np.array([[1.0]+[(r["features"][k]-cfg["feature_centers"][k])/cfg["feature_scales"][k]
                         for k in predictors] for r in rows], dtype=float)
    y = np.array([r["value"] for r in rows], dtype=float)
    beta = np.zeros(x.shape[1]); beta[0] = np.median(y)
    penalty = np.diag([0.0]+[cfg["ridge_lambda"]]*len(predictors))
    delta = cfg["huber_delta_pp"]
    converged = False
    for iteration in range(1, int(cfg["maximum_iterations"])+1):
        residual = y-x@beta
        weights = np.minimum(1.0, delta/np.maximum(np.abs(residual), 1e-300))
        updated = np.linalg.solve(x.T@(weights[:, None]*x)+penalty, x.T@(weights*y))
        if not np.all(np.isfinite(updated)):
            raise ValueError("nonfinite Huber-ridge solution")
        step = float(np.max(np.abs(updated-beta)))
        beta = updated
        if step <= cfg["coefficient_tolerance"]:
            converged = True
            break
    residual = y-x@beta
    absolute = np.abs(residual)
    loss = np.where(absolute <= delta, .5*residual**2, delta*(absolute-.5*delta)).sum()
    gradient = -(x.T@np.clip(residual, -delta, delta))+penalty@beta
    if not converged or float(np.max(np.abs(gradient))) > 1e-6:
        raise ValueError("Huber-ridge solver did not converge")
    return {"kind": "linear", "predictors": list(predictors), "coefficients": beta.tolist(),
            "centers": {k: cfg["feature_centers"][k] for k in predictors},
            "scales": {k: cfg["feature_scales"][k] for k in predictors},
            "iterations": iteration, "objective": float(loss+.5*beta@penalty@beta),
            "gradient_max": float(np.max(np.abs(gradient))), "intercept_shift": 0.0}


def _seal(state):
    state["state_hash"] = digest(state)
    return state


def verify_state(state):
    content = {k: v for k, v in state.items() if k != "state_hash"}
    if state.get("state_hash") != digest(content):
        raise ValueError("EXPERT8.1 state hash mismatch")


def fit_state(history, prediction_at, candidate_id, config=None, *, group_key, exclude_assay_id=None,
              association_validator=None):
    """Fit a serializable state without reading the current target's label."""
    if candidate_id not in CANDIDATE_IDS:
        raise ValueError("undeclared EXPERT8.1 candidate")
    cfg = configuration(config)
    cutoff = parse_time(prediction_at)
    state = {"schema": "expert81-state-v1", "model_version": MODEL_VERSION,
             "candidate_id": candidate_id, "prediction_at": stamp(cutoff), "group_key": str(group_key),
             "excluded_target_id": None if exclude_assay_id is None else str(exclude_assay_id),
             "config": cfg, "status": "unavailable", "reason": None, "model": None,
             "training_ids": [], "training_versions": [], "normal_training_ids": [],
             "excluded_training_ids": [], "training_records": [], "recent_ids": [],
             "residual_receipt_ids": [], "residual_association_ids": [], "latest_label_available_at": None, "fence": None,
             "fallback": None, "ranges": {}}
    if str(group_key) not in cfg["allowed_groups"]:
        state["reason"] = "unsupported_group"
        return _seal(state)
    rows = available_versions(history, cutoff, group_key=group_key, exclude_assay_id=exclude_assay_id)
    bounds = label_fence(rows, cfg)
    normal = [r for r in rows if inside(r["value"], bounds)]
    state.update(training_ids=[r["assay_id"] for r in rows],
                 training_versions=[r["version_id"] for r in rows],
                 normal_training_ids=[r["assay_id"] for r in normal],
                 excluded_training_ids=[r["assay_id"] for r in rows if not inside(r["value"], bounds)],
                 training_records=[{k: r[k] for k in ("assay_id", "version_id", "sample_at", "version_available_at", "value", "features", "provenance")} for r in rows],
                 fence=bounds,
                 latest_label_available_at=max((r["version_available_at"] for r in rows), default=None))
    dates = {parse_time(r["sample_at"]).astimezone(BJ).date() for r in normal}
    if len(normal) < cfg["minimum_normal_labels"] or len(dates) < cfg["minimum_normal_sample_dates"]:
        state["reason"] = "insufficient_normal_history_or_dates"
        return _seal(state)
    state["ranges"] = {k: {"minimum": min(r["features"][k] for r in normal),
                           "maximum": max(r["features"][k] for r in normal)} for k in ("level", "coal_amount")}
    median = float(np.median([r["value"] for r in normal]))
    model = {"kind": "constant", "value": median}
    if candidate_id in ("C1", "C2", "C4", "C7"):
        count = len(normal) if candidate_id == "C7" else (10 if candidate_id in ("C2", "C4") else 5)
        recent = recent_rows(normal, cutoff, count, cfg)
        state["recent_ids"] = [r["assay_id"] for r in recent]
    else:
        recent = []
    if candidate_id in ("C1", "C2"):
        if len(recent) >= cfg["minimum_recent"]:
            model["value"] = float(np.median([r["value"] for r in recent]))
        else:
            state["fallback"] = "C0_insufficient_recent_history"
    elif candidate_id == "C3":
        weights = [2**(-(cutoff-parse_time(r["sample_at"])).total_seconds()/3600/cfg["half_life_hours"]) for r in normal]
        model["value"] = weighted_median([r["value"] for r in normal], weights)
    elif candidate_id == "C4":
        shift = 0.0
        if len(recent) >= cfg["minimum_recent"]:
            shift = float(np.median([r["value"]-r["frozen_prediction"] for r in recent]))*len(recent)/(len(recent)+cfg["recent_bias_pseudocount"])
        else:
            state["fallback"] = "frozen_expert8_zero_bias_insufficient_recent_history"
        model = {"kind": "frozen_expert8_shift", "intercept_shift": shift}
    elif candidate_id in ("C5", "C6", "C7"):
        predictors = ["level", "coal_amount"] if candidate_id == "C6" else ["level"]
        try:
            model = huber_ridge_fit(normal, predictors, cfg)
        except (ValueError, np.linalg.LinAlgError) as exc:
            state["reason"] = str(exc)
            return _seal(state)
        if candidate_id == "C7":
            model["base_clip_before_shift"] = True
            completed = []
            for row in recent:
                receipt = row.get("c5_receipt")
                if receipt is None:
                    continue
                original = validate_c5_receipt(receipt, row["sample_at"], assay_id=row["assay_id"],
                                    evidence_mode=cfg["receipt_mode"], features=row["features"],
                                    group_key=group_key, prediction_at=cutoff,
                                    label_version_id=row["version_id"],
                                    label_event_hash=row["provenance"].get("label_event_hash"),
                                    association_validator=association_validator)
                if original is not None:
                    completed.append((row, original))
            completed = completed[-5:]
            state["recent_ids"] = [row["assay_id"] for row, _ in completed]
            if len(completed) >= cfg["minimum_recent"]:
                residuals = [r["value"]-receipt["prediction"] for r, receipt in completed]
                n = len(completed)
                model["intercept_shift"] = float(np.median(residuals))*n/(n+cfg["recent_bias_pseudocount"])
                state["residual_receipt_ids"] = [receipt["receipt_hash"] for _, receipt in completed]
                state["residual_association_ids"] = [r["c5_receipt"]["association_hash"] for r, _ in completed
                                                     if r["c5_receipt"].get("schema") == "expert81-c5-association-v1"]
            else:
                state["fallback"] = "C5_zero_bias_insufficient_prequential_receipts"
    state.update(status="ok", reason=None, model=model)
    return _seal(state)


def predict_state(state, features, frozen_prediction=None):
    """Predict with a sealed state; no label, history mutation or re-fitting."""
    verify_state(state)
    result = {"model_version": MODEL_VERSION, "candidate_id": state["candidate_id"],
              "state_hash": state["state_hash"], "status": state["status"], "reason": state["reason"],
              "fallback": state["fallback"], "prediction": None, "value": None,
              "raw_prediction": None, "outside_predictors": []}
    if state["status"] != "ok":
        return result
    model = state["model"]
    try:
        values = {k: finite(features[k], k) for k in ("level", "coal_amount")}
        if model["kind"] == "constant":
            raw = model["value"]
        elif model["kind"] == "frozen_expert8_shift":
            raw = finite(frozen_prediction, "frozen_prediction")+model["intercept_shift"]
        else:
            base = model["coefficients"][0]+sum(coef*(values[k]-model["centers"][k])/model["scales"][k]
                       for k, coef in zip(model["predictors"], model["coefficients"][1:]))
            base = finite(base, "base prediction")
            lo, hi = state["config"]["output_bounds"]
            if model.get("base_clip_before_shift"):
                result["base_raw_prediction"] = base
                result["base_final_prediction"] = min(hi, max(lo, base))
                base = result["base_final_prediction"]
            raw = base+model["intercept_shift"]
        raw = finite(raw, "prediction")
        result["outside_predictors"] = [k for k in model.get("predictors", [])
                                         if not state["ranges"][k]["minimum"] <= values[k] <= state["ranges"][k]["maximum"]]
        lo, hi = state["config"]["output_bounds"]
        result.update(prediction=min(hi, max(lo, raw)), value=min(hi, max(lo, raw)), raw_prediction=raw)
    except (ValueError, TypeError, KeyError) as exc:
        result.update(status="unavailable", reason=f"invalid_prediction_input: {exc}")
    return result


def make_c5_receipt(state, features, *, sample_at, clock_domain=None, clock_epoch=None):
    """Create a prediction receipt now; caller must persist it before assay entry."""
    if state["candidate_id"] != "C5":
        raise ValueError("receipt requires a C5 state")
    if parse_time(state["prediction_at"]) > parse_time(sample_at):
        raise ValueError("a later C5 fit cannot create an earlier receipt")
    predicted = predict_state(state, features)
    if predicted["prediction"] is None:
        return None
    values = {k: finite(features[k], k) for k in ("level", "coal_amount")}
    receipt = {"schema": "expert81-c5-receipt-v1", "candidate_id": "C5",
               "evidence_mode": state["config"]["receipt_mode"],
               "group_key": state["group_key"], "clock_domain": clock_domain, "clock_epoch": clock_epoch,
               "sample_at": stamp(sample_at), "fit_cutoff_at": state["prediction_at"],
               "prediction": predicted["prediction"], "state_hash": state["state_hash"],
               "training_ids": state["training_ids"], "training_versions": state["training_versions"],
               "latest_label_available_at": state["latest_label_available_at"],
               "features": values, "features_hash": digest(values), "state": deepcopy(state)}
    receipt["receipt_hash"] = digest(receipt)
    return receipt


def make_c5_association(original_receipt, *, association_validator=None, **fields):
    """Bind a later actual sample without changing any earlier receipt bytes.

    The runtime registry must verify that referenced prediction/seal/label
    events were actually persisted in order and selected latest-eligible.
    """
    association = {"schema": "expert81-c5-association-v1", "original_receipt": deepcopy(original_receipt), **deepcopy(fields)}
    association["association_hash"] = digest(association)
    validate_c5_receipt(association, association["sample_at"], group_key=association["group_key"],
                        association_validator=association_validator)
    return association


def validate_c5_receipt(receipt, sample_at, *, assay_id=None, evidence_mode="formal",
                        features=None, group_key=None, prediction_at=None, label_version_id=None,
                        label_event_hash=None, association_validator=None):
    if receipt.get("schema") == "expert81-c5-association-v1":
        body = {k: v for k, v in receipt.items() if k != "association_hash"}
        if receipt.get("association_hash") != digest(body) or evidence_mode != "formal":
            raise ValueError("invalid formal C5 association hash or mode")
        original = receipt["original_receipt"]
        validated = validate_c5_receipt(original, original["sample_at"], assay_id=assay_id,
                                        evidence_mode=evidence_mode, group_key=group_key)
        sample = parse_time(sample_at); asof = parse_time(receipt["input_asof"])
        seal = parse_time(receipt["postseal_at"]); bound = parse_time(receipt["bound_at"])
        if (parse_time(receipt["sample_at"]) != sample or asof != parse_time(original["sample_at"])
                or not asof <= seal < sample <= asof+timedelta(seconds=30)
                or receipt.get("max_age_seconds") != 30 or receipt.get("matching_policy") != "latest-eligible"
                or bound < sample):
            raise ValueError("C5 association violates its predeclared sample window")
        if str(receipt.get("group_key")) != str(original["group_key"]):
            raise ValueError("C5 association group mismatch")
        for key in ("clock_domain", "clock_epoch"):
            if not receipt.get(key) or receipt[key] != original.get(key):
                raise ValueError("C5 association clock domain/epoch mismatch")
        for key in ("prediction_event_hash", "prediction_seal_hash", "label_event_hash", "association_seal_hash"):
            value = receipt.get(key, "")
            if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("C5 association lacks immutable event references")
        if not receipt.get("label_version_id"):
            raise ValueError("C5 association lacks label version identity")
        if label_version_id is not None and receipt["label_version_id"] != str(label_version_id):
            raise ValueError("C5 association belongs to a different label version")
        if label_version_id is not None and (not label_event_hash or receipt["label_event_hash"] != label_event_hash):
            raise ValueError("C5 association label event identity mismatch")
        if receipt.get("issued_features_hash") != original["features_hash"]:
            raise ValueError("C5 association issued feature identity mismatch")
        if features is not None and receipt.get("training_features_hash") != digest({k:finite(features[k],k) for k in ("level","coal_amount")}):
            raise ValueError("C5 association training feature identity mismatch")
        if prediction_at is not None and bound >= parse_time(prediction_at):
            return None
        if not callable(association_validator) or association_validator(receipt) is not True:
            raise ValueError("formal C5 association requires trusted runtime registry verification")
        return validated
    content = {k: v for k, v in receipt.items() if k != "receipt_hash"}
    if receipt.get("receipt_hash") != digest(content) or receipt.get("candidate_id") != "C5":
        raise ValueError("invalid C5 receipt hash or method")
    if receipt.get("evidence_mode") != evidence_mode:
        raise ValueError("exploratory/formal C5 receipt modes cannot be mixed")
    sample = parse_time(sample_at)
    if parse_time(receipt["sample_at"]) != sample or parse_time(receipt["fit_cutoff_at"]) > sample:
        raise ValueError("C5 receipt is not an earlier sampling-time prediction")
    if receipt.get("latest_label_available_at") is not None and parse_time(receipt["latest_label_available_at"]) >= parse_time(receipt["fit_cutoff_at"]):
        raise ValueError("C5 receipt includes an unavailable label")
    finite(receipt["prediction"], "receipt prediction")
    if not receipt.get("state_hash") or len(receipt["training_ids"]) != len(receipt["training_versions"]):
        raise ValueError("C5 receipt lacks fitted-state provenance")
    if assay_id is not None and str(assay_id) in {str(i) for i in receipt["training_ids"]}:
        raise ValueError("C5 receipt trained on its own target label")
    state = receipt.get("state")
    if not isinstance(state, dict):
        raise ValueError("C5 receipt lacks frozen fitted state")
    verify_state(state)
    if (state["candidate_id"] != "C5" or state["status"] != "ok"
            or state["state_hash"] != receipt["state_hash"]
            or state["training_ids"] != receipt["training_ids"]
            or state["training_versions"] != receipt["training_versions"]
            or state["prediction_at"] != receipt["fit_cutoff_at"]
            or state["latest_label_available_at"] != receipt["latest_label_available_at"]
            or state["group_key"] != receipt["group_key"]
            or state["config"]["receipt_mode"] != evidence_mode):
        raise ValueError("C5 receipt state/history/version/cutoff mismatch")
    if group_key is not None and str(group_key) != str(receipt["group_key"]):
        raise ValueError("C5 receipt group mismatch")
    values = {k: finite(receipt["features"][k], k) for k in ("level", "coal_amount")}
    if digest(values) != receipt["features_hash"]:
        raise ValueError("C5 receipt feature hash mismatch")
    if features is not None and digest({k: finite(features[k], k) for k in ("level", "coal_amount")}) != receipt["features_hash"]:
        raise ValueError("C5 receipt input differs from its exact sampling-time features")
    replay = predict_state(state, values)
    if replay["prediction"] is None or replay["prediction"] != receipt["prediction"]:
        raise ValueError("C5 receipt prediction does not replay from its frozen state/input")
    return receipt


def last_observed_reference(state):
    """Most recently sampled raw legal-time assay, without a normal filter."""
    verify_state(state)
    if state["status"] != "ok":
        return None
    return state["training_records"][-1]["value"] if state["training_records"] else None
