"""DB primary contracts on synthetic data; no actual assay search here."""
from copy import deepcopy
import hashlib
import io

import joblib
import numpy as np
import pytest
from threadpoolctl import threadpool_limits

from app.gpt_coarse.experiments.expert81_db_primary_predict import PUBLIC_FEATURES, branch_of, predict_one, raw_predict
from app.gpt_coarse.experiments.expert81_db_primary_search import candidate_specs, fence, fit_candidates, normal
from scripts.gpt_expert81_db_primary_experiment import ranked, shared_training


@pytest.fixture(scope="module")
def fitted():
    rows = []
    for i in range(24):
        features = dict(level=35.+i, coal_amount=530.+(i%5)*13, sysA=0, sysB=1, sys401=1, sys402=1, desliming473=0, desliming474=0)
        rows.append(dict(record_id=f"db:{i}", actual=11.+.03*i+np.sin(i)*.4, date=f"2026-10-{15+i//3:02d}",
                         features=features, feature_namespace="legacy_if_prod", semantic_quality="strict", public_quality="strict"))
    with threadpool_limits(limits=1):
        models = fit_candidates(rows)
    return rows, models


def test_bounded43_definitions_no_ID_date_features_and_finite_predictions(fitted):
    rows, models = fitted
    specs = candidate_specs()
    assert len(specs) == len(models) == 43
    assert len({s["id"] for s in specs}) == 43
    for spec in specs:
        if spec["family"] in ("tree", "forest", "extra"):
            assert spec["params"]["max_depth"] <= 3 and spec["params"]["min_samples_leaf"] >= 4
    for model in models.values():
        assert np.isfinite(raw_predict(model, rows)).all()
    changed = deepcopy(rows)
    for row in changed:
        row.update(record_id="future-unrelated-ID", date="2039-01-01", actual=99.)
    for model in models.values():
        assert np.array_equal(raw_predict(model, rows), raw_predict(model, changed))


def test_public_prediction_routing_and_fences_ignore_desliming_namespace(fitted):
    rows, models = fitted
    changed = deepcopy(rows)
    for row in changed:
        row["feature_namespace"] = "runs_v1"
        row["features"].update(desliming473=1, desliming474=None)
    assert [branch_of(r["features"]) for r in rows] == [branch_of(r["features"]) for r in changed]
    for method, model in models.items():
        if not method.startswith("SEMANTIC_"):
            assert np.array_equal(raw_predict(model, rows), raw_predict(model, changed))
    assert fence([r["actual"] for r in rows]) == fence([r["actual"] for r in changed])
    # Unknown semantic namespace uses the saved public model and never false-fills.
    for method in ("SEMANTIC_RIDGE_10", "SEMANTIC_SVR_4_0.1"):
        assert np.array_equal(raw_predict(models[method], changed), raw_predict(models[method]["public"], changed))


def test_frozen_inference_refuses_labels_and_never_mutates_or_fits(fitted, monkeypatch):
    rows, models = fitted
    model = deepcopy(models["SEMANTIC_RIDGE_10"])
    estimators = [model["public"]["estimator"], *[p["estimator"] for p in model["namespaces"].values()]]
    for estimator in estimators:
        for component in estimator.named_steps.values():
            monkeypatch.setattr(type(component), "fit", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("forbidden fit")))
    bundle = {"branches": {"3": {"status": "ok", "model": model, "ranges": {k:{"min": min(r["features"][k] for r in rows), "max": max(r["features"][k] for r in rows)} for k in PUBLIC_FEATURES}}}}
    stream = io.BytesIO(); joblib.dump(bundle, stream); before = hashlib.sha256(stream.getvalue()).hexdigest()
    assert predict_one(bundle, rows[0])["prediction"] is None  # actual present
    factors = {k:v for k,v in rows[0].items() if k != "actual"}
    first = predict_one(bundle, factors)
    assert first["status"] == "ok"
    assert predict_one(bundle, factors) == first
    csv_factors = deepcopy(factors)
    csv_factors["features"] = {k:str(v) for k,v in csv_factors["features"].items()}
    assert predict_one(bundle, csv_factors)["prediction"] == pytest.approx(first["prediction"])
    stream = io.BytesIO(); joblib.dump(bundle, stream)
    assert hashlib.sha256(stream.getvalue()).hexdigest() == before
    factors["features"]["sys402"] = 0
    assert predict_one(bundle, factors)["prediction"] is None


def test_hit_first_ranking_and_explicit_MAE_tradeoff():
    rows = []
    for method, errors in (("MEDIAN", [.9, .9]), ("RIDGE_1", [0., 1.1])):
        for i, error in enumerate(errors):
            rows.append(dict(method=method, actual=10., prediction=10.+error, normal=True, date="2026-10-20", record_id=str(i)))
    result = ranked(rows)
    assert result["selected"] == "MEDIAN"
    assert result["normal_MAE_winner"] == "RIDGE_1"


def test_reference_total_weight_is_capped_and_scaler_only_uses_DB(fitted):
    rows, _ = fitted
    reference = deepcopy(rows[:12])
    for row in reference:
        row["record_id"] = "reference:"+row["record_id"]
        row["features"]["level"] += 20
    with threadpool_limits(limits=1):
        models = fit_candidates(rows, reference=reference)
    model = models["REFERENCE_HUBER_10PCT"]
    assert model["reference_total_weight"] == pytest.approx(.1*len(rows))
    assert model["db_total_weight"] == len(rows)
    assert np.allclose(model["estimator"].named_steps["scale"].mean_, [np.mean([r["features"][k] for r in rows]) for k in PUBLIC_FEATURES])


def test_public_inference_ignores_malformed_unused_desliming(fitted):
    rows, models = fitted
    bundle = {"branches": {"3": {"status": "ok", "model": models["RIDGE_10"], "ranges": {}}}}
    factors = {k:v for k,v in rows[0].items() if k != "actual"}
    before = predict_one(bundle, factors)
    factors = deepcopy(factors)
    factors["features"].update(desliming473="not-used", desliming474=object())
    assert predict_one(bundle, factors) == before


def test_shared_fence_uses_strict_training_only_and_mixed_weights(fitted):
    rows, _ = fitted
    strict = [{**r, "public_quality": "strict"} for r in rows[:12]]
    approximate = [{**rows[12], "public_quality": "minute_approximation", "actual": 10.9},
                   {**rows[13], "public_quality": "minute_approximation", "actual": 99.}]
    bounds, strict_normal, mixed = shared_training(strict+approximate)
    assert bounds == fence([r["actual"] for r in strict])
    assert len(mixed) == len(strict_normal)+1
    assert mixed[-1]["training_weight"] == .5
    assert all(r["training_weight"] == 1. for r in strict_normal)
