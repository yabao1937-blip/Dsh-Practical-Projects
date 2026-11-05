"""Fixed second-round capacity matrix, declared after seeing the first86."""
from __future__ import annotations

import numpy as np
from sklearn.ensemble import ExtraTreesRegressor, GradientBoostingRegressor, RandomForestRegressor
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVR

from .expert81_db_primary_predict import PUBLIC_FEATURES

SEED = 20261103


def candidate_specs():
    specs = []
    for c in (64., 256.):
        for gamma in (.1, .3, 1.):
            specs.append({"id": f"R2__SVR_{c:g}_{gamma:g}_0.1", "family": "svr", "params": {"C": c, "gamma": gamma, "epsilon": .1}, "complexity": 5})
    for c in (64., 256.):
        for gamma in (.3, 1.):
            specs.append({"id": f"R2__SVR_{c:g}_{gamma:g}_0.3", "family": "svr", "params": {"C": c, "gamma": gamma, "epsilon": .3}, "complexity": 5})
    for loss in ("squared_error", "huber"):
        for depth in (2, 3):
            for trees in (200, 400):
                specs.append({"id": f"R2__GB_{loss}_{depth}_{trees}", "family": "boost", "params": {"loss": loss, "max_depth": depth, "n_estimators": trees, "min_samples_leaf": 2, "learning_rate": .05}, "complexity": 8})
    for family in ("forest", "extra"):
        for depth in (4, 6):
            specs.append({"id": f"R2__{family.upper()}_{depth}_2", "family": family, "params": {"max_depth": depth, "min_samples_leaf": 2, "n_estimators": 256}, "complexity": 7})
    specs.append({"id": "R2__BLEND_SVR_GB", "family": "blend", "members": ["R2__SVR_256_0.3_0.1", "R2__GB_huber_3_200"], "weights": [.5, .5], "complexity": 9})
    specs.append({"id": "R2__BLEND_SVR_RF", "family": "blend", "members": ["R2__SVR_64_0.3_0.1", "R2__FOREST_6_2"], "weights": [.5, .5], "complexity": 9})
    return specs


def fit_candidates(rows):
    models = {}
    x = np.asarray([[r["features"][k] for k in PUBLIC_FEATURES] for r in rows], dtype=float)
    y = np.asarray([r["actual"] for r in rows], dtype=float)
    weights = np.asarray([r["training_weight"] for r in rows], dtype=float)
    for spec in candidate_specs():
        family = spec["family"]
        if family == "blend":
            models[spec["id"]] = {"kind": "mixture", "parts": [models[k] for k in spec["members"]], "weights": spec["weights"]}
            continue
        if family == "svr":
            estimator = SVR(**spec["params"], kernel="rbf")
        elif family == "boost":
            estimator = GradientBoostingRegressor(**spec["params"], random_state=SEED)
        else:
            cls = RandomForestRegressor if family == "forest" else ExtraTreesRegressor
            estimator = cls(**spec["params"], max_features=1., n_jobs=1, random_state=SEED)
        pipeline = Pipeline([("scale", StandardScaler()), ("model", estimator)])
        pipeline.fit(x, y, scale__sample_weight=weights, model__sample_weight=weights)
        models[spec["id"]] = {"kind": "estimator", "features": list(PUBLIC_FEATURES), "estimator": pipeline,
                              "db_training_ids": [r["record_id"] for r in rows], "db_training_weights": weights.tolist(),
                              "db_total_weight": float(weights.sum()), "reference_training_ids": [], "reference_total_weight": 0.}
    return models
