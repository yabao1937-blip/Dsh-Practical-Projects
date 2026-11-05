"""Label-free frozen EX8.1 inference. No fitting, calibration or mutable caches."""
from __future__ import annotations

import math

import numpy as np
from threadpoolctl import threadpool_limits

PUBLIC_FEATURES = ("level", "coal_amount", "sysA", "sysB", "sys401", "sys402")
SEMANTIC_FEATURES = PUBLIC_FEATURES + ("desliming473", "desliming474")
LABEL_KEYS = {"ash", "actual", "target", "label", "value", "ash_value", "manual_value"}


def numeric_features(raw):
    values = raw.get("features", raw)
    result = {}
    for name in PUBLIC_FEATURES:
        value = values.get(name)
        if isinstance(value, bool):
            value = int(value)
        if value is None:
            raise ValueError(f"missing public factor {name}")
        value = float(value)
        if not math.isfinite(value):
            raise ValueError(f"nonfinite public factor {name}")
        if name.startswith("sys") and value not in (0., 1.):
            raise ValueError("system feedback must be explicitly binary")
        result[name] = value
    if not 0 <= result["level"] <= 100:
        raise ValueError("level outside sensor percentage range")
    if result["coal_amount"] <= 10:
        raise ValueError("coal_amount_le10_outside_training_contract")
    return result


def branch_of(features):
    # Public routing depends only on trusted four-system feedback, never desliming.
    return str(int(sum(features[k] for k in PUBLIC_FEATURES[2:])))


def raw_predict(model, rows):
    if model["kind"] == "estimator":
        with threadpool_limits(limits=1):
            return np.asarray(model["estimator"].predict([[r["features"][k] for k in model["features"]] for r in rows]), dtype=float)
    if model["kind"] == "mixture":
        predictions = [raw_predict(part, rows) for part in model["parts"]]
        return sum(w*p for w, p in zip(model["weights"], predictions))
    if model["kind"] == "semantic_overlay":
        result = raw_predict(model["public"], rows)
        for i, row in enumerate(rows):
            namespace = row.get("feature_namespace")
            semantic = model["namespaces"].get(namespace)
            values = row["features"]
            if semantic is not None and row.get("semantic_quality") in ("strict", "good", "minute_approximation") and all(values.get(k) in (0, 1, False, True) for k in SEMANTIC_FEATURES[6:]):
                result[i] = raw_predict(semantic, [row])[0]
        return result
    raise ValueError("unsupported frozen model kind")


def predict_one(bundle, row):
    identity = row.get("record_id", row.get("assay_id", row.get("id")))
    result = {"record_id": identity, "assay_id": row.get("assay_id"), "sample_at": row.get("sample_at"), "public_quality": row.get("public_quality", row.get("quality")), "prediction": None, "raw_prediction": None,
              "status": "unavailable", "reason": None, "outside_training_ranges": [], "route": None}
    try:
        if LABEL_KEYS.intersection(row) or LABEL_KEYS.intersection(row.get("features", {})):
            raise ValueError("label-bearing input refused; provide factors only")
        if row.get("public_quality", row.get("quality")) not in ("strict", "good", "public_strict", "minute_approximation"):
            raise ValueError("public feature quality not eligible")
        values = numeric_features(row)
        branch = branch_of(values)
        state = bundle["branches"].get(branch)
        if state is None or state["status"] != "ok":
            raise ValueError("unsupported_system_count_or_insufficient_own_branch_support")
        factors = dict(row.get("features", row)); factors.update(values)
        model = state["model"]
        if model["kind"] == "semantic_overlay" and row.get("feature_namespace") in model["namespaces"] and row.get("semantic_quality") in ("strict", "good", "minute_approximation"):
            for key in SEMANTIC_FEATURES[6:]:
                try:
                    parsed = float(factors[key])
                    factors[key] = parsed if parsed in (0., 1.) else None
                except (ValueError, TypeError, KeyError):
                    factors[key] = None
        frame = {"features": factors, "feature_namespace": row.get("feature_namespace"), "semantic_quality": row.get("semantic_quality")}
        raw = float(raw_predict(state["model"], [frame])[0])
        if not math.isfinite(raw):
            raise ValueError("nonfinite frozen prediction")
        result.update(status="ok", prediction=min(100., max(0., raw)), raw_prediction=raw,
                      route=f"independent_system_count:{branch}",
                      outside_training_ranges=[k for k, bounds in state["ranges"].items() if not bounds["min"] <= values[k] <= bounds["max"]])
    except (ValueError, KeyError, TypeError) as exc:
        result["reason"] = str(exc)
    return result


def predict_rows(bundle, rows):
    return [predict_one(bundle, row) for row in rows]
