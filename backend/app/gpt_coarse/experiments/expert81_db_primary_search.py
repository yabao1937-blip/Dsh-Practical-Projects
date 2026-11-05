"""Bounded DB-primary EX8.1 estimator definitions; dates/IDs never enter X."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy

import numpy as np
from sklearn.dummy import DummyRegressor
from sklearn.ensemble import ExtraTreesRegressor, GradientBoostingRegressor, HistGradientBoostingRegressor, RandomForestRegressor
from sklearn.linear_model import HuberRegressor, Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import PolynomialFeatures, StandardScaler
from sklearn.svm import SVR
from sklearn.tree import DecisionTreeRegressor

from .expert81_db_primary_predict import PUBLIC_FEATURES, SEMANTIC_FEATURES, raw_predict

SEED = 20261103


def candidate_specs():
    specs = [{"id": "MEDIAN", "family": "median", "params": {}, "complexity": 0}]
    for alpha in (.1, 1., 10., 100.):
        specs.append({"id": f"RIDGE_{alpha:g}", "family": "ridge", "params": {"alpha": alpha}, "complexity": 1})
    for alpha in (.1, 1., 10.):
        specs.append({"id": f"HUBER_{alpha:g}", "family": "huber", "params": {"alpha": alpha, "epsilon": 1.35}, "complexity": 2})
    for alpha in (1., 10., 100.):
        specs.append({"id": f"QUADRATIC_{alpha:g}", "family": "quadratic", "params": {"alpha": alpha}, "complexity": 3})
    for c in (1., 4., 16.):
        for gamma in (.1, 1.):
            specs.append({"id": f"SVR_{c:g}_{gamma:g}", "family": "svr", "params": {"C": c, "gamma": gamma, "epsilon": .3}, "complexity": 4})
    for family, depth, leaves in ((f, d, n) for f in ("tree", "forest", "extra") for d in (2, 3) for n in (4, 6)):
        specs.append({"id": f"{family.upper()}_{depth}_{leaves}", "family": family, "params": {"max_depth": depth, "min_samples_leaf": leaves}, "complexity": 5 if family == "tree" else 6})
    for leaves in (4, 8):
        for penalty in (1., 10.):
            specs.append({"id": f"HIST_{leaves}_{penalty:g}", "family": "hist", "params": {"max_leaf_nodes": leaves, "l2_regularization": penalty}, "complexity": 7})
    for loss in ("huber", "absolute_error"):
        for depth in (1, 2):
            specs.append({"id": f"BOOST_{loss}_{depth}", "family": "boost", "params": {"loss": loss, "max_depth": depth}, "complexity": 7})
    for identity, members in (("BLEND_RIDGE_SVR", ["RIDGE_10", "SVR_4_0.1"]),
                              ("BLEND_HUBER_FOREST", ["HUBER_1", "FOREST_3_4"]),
                              ("BLEND_QUAD_BOOST", ["QUADRATIC_10", "BOOST_huber_1"])):
        specs.append({"id": identity, "family": "blend", "members": members, "weights": [.5, .5], "complexity": 8})
    for member in ("RIDGE_10", "SVR_4_0.1"):
        specs.append({"id": "SEMANTIC_"+member, "family": "semantic_overlay", "member": member, "minimum_normal_n": 10, "minimum_dates": 3, "complexity": 9})
    specs.append({"id": "REFERENCE_HUBER_10PCT", "family": "reference", "member": "HUBER_1", "reference_total_weight_ratio": .1, "complexity": 4})
    return specs


def make_estimator(spec):
    family, params = spec["family"], spec["params"]
    if family == "median":
        estimator = DummyRegressor(strategy="median")
    elif family in ("ridge", "quadratic"):
        estimator = Ridge(**params)
    elif family == "huber":
        estimator = HuberRegressor(**params, max_iter=5000, tol=1e-8)
    elif family == "svr":
        estimator = SVR(**params, kernel="rbf")
    elif family == "tree":
        estimator = DecisionTreeRegressor(**params, random_state=SEED)
    elif family in ("forest", "extra"):
        cls = RandomForestRegressor if family == "forest" else ExtraTreesRegressor
        estimator = cls(**params, n_estimators=128, random_state=SEED, n_jobs=1, max_features=1.)
    elif family == "hist":
        estimator = HistGradientBoostingRegressor(**params, learning_rate=.05, max_iter=80, max_depth=3, min_samples_leaf=6,
                                                  early_stopping=False, random_state=SEED)
    elif family == "boost":
        estimator = GradientBoostingRegressor(**params, learning_rate=.05, n_estimators=80, min_samples_leaf=5, random_state=SEED)
    else:
        raise ValueError("not a primitive estimator")
    steps = [("scale", StandardScaler())]
    if family == "quadratic":
        steps.append(("poly", PolynomialFeatures(degree=2, include_bias=False)))
    steps.append(("model", estimator))
    return Pipeline(steps)


def fit_primitive(spec, rows, features=PUBLIC_FEATURES, reference=None):
    estimator = make_estimator(spec)
    x = np.asarray([[r["features"][k] for k in features] for r in rows], dtype=float)
    y = np.asarray([r["actual"] for r in rows], dtype=float)
    db_weights = np.asarray([r.get("training_weight", 1.) for r in rows], dtype=float)
    db_mass = float(db_weights.sum())
    if reference:
        # Scale using DB training rows only. Reference cannot redefine DB scaling.
        estimator.named_steps["scale"].fit(x, sample_weight=db_weights)
        weights = np.r_[db_weights, np.repeat(.1*db_mass/len(reference), len(reference))]
        train = rows+reference
        expanded = np.asarray([[r["features"][k] for k in features] for r in train])
        transformed = estimator.named_steps["scale"].transform(expanded)
        if "poly" in estimator.named_steps:
            transformed = estimator.named_steps["poly"].fit_transform(transformed)
        estimator.named_steps["model"].fit(transformed, [r["actual"] for r in train], sample_weight=weights)
    else:
        estimator.fit(x, y, scale__sample_weight=db_weights, model__sample_weight=db_weights)
    return {"kind": "estimator", "features": list(features), "estimator": estimator,
            "db_training_ids": [r["record_id"] for r in rows], "reference_training_ids": [r["record_id"] for r in reference or []],
            "reference_total_weight": .1*db_mass if reference else 0., "db_total_weight": db_mass,
            "db_training_weights": db_weights.tolist()}


def fit_candidates(rows, reference=None):
    specs = candidate_specs()
    lookup = {s["id"]: s for s in specs}
    models = {}
    for spec in specs:
        identity, family = spec["id"], spec["family"]
        if family == "blend":
            model = {"kind": "mixture", "weights": spec["weights"], "parts": [models[k] for k in spec["members"]]}
        elif family == "semantic_overlay":
            model = {"kind": "semantic_overlay", "public": models[spec["member"]], "namespaces": {}}
            for namespace in sorted({r.get("feature_namespace", "unspecified") for r in rows}):
                own = [r for r in rows if r.get("feature_namespace") == namespace and r.get("semantic_quality") in ("strict", "good", "minute_approximation") and all(r["features"].get(k) in (0, 1, False, True) for k in SEMANTIC_FEATURES[6:])]
                if len(own) >= spec["minimum_normal_n"] and len({r["date"] for r in own}) >= spec["minimum_dates"]:
                    model["namespaces"][namespace] = fit_primitive(lookup[spec["member"]], own, SEMANTIC_FEATURES)
        elif family == "reference":
            model = fit_primitive(lookup[spec["member"]], rows, reference=reference)
        else:
            model = fit_primitive(spec, rows)
        models[identity] = model
    return models


def fence(values):
    q1, q3 = np.quantile(values, [.25, .75], method="linear")
    spread = max(1., float(q3-q1))
    return {"lower": float(q1-1.5*spread), "upper": float(q3+1.5*spread), "q1": float(q1), "q3": float(q3)}


def normal(value, bounds):
    return bounds["lower"] <= value <= bounds["upper"]
