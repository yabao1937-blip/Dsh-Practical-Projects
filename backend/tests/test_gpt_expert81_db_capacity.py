"""Finite capacity definitions and duplicate-input information bound."""
import numpy as np
from threadpoolctl import threadpool_limits

from app.gpt_coarse.experiments.expert81_db_capacity_search import candidate_specs, fit_candidates
from app.gpt_coarse.experiments.expert81_db_primary_predict import raw_predict
from scripts.gpt_expert81_db_capacity_experiment import conflicts


def test_fixed24_capacity_models_have_no_leaf1_or_zero_epsilon():
    specs = candidate_specs()
    assert len(specs) == len({s["id"] for s in specs}) == 24
    rows = [{"record_id": str(i), "actual": 11.+np.sin(i)*.5, "training_weight": .5 if i%3 == 0 else 1.,
             "features": dict(level=35.+i, coal_amount=550.+10*(i%4), sysA=0, sysB=1, sys401=1, sys402=1)} for i in range(16)]
    for spec in specs:
        if spec["family"] == "svr":
            assert spec["params"]["epsilon"] >= .1
        elif spec["family"] != "blend":
            assert spec["params"]["min_samples_leaf"] >= 2
    with threadpool_limits(limits=1):
        models = fit_candidates(rows)
        assert all(np.isfinite(raw_predict(model, rows)).all() for model in models.values())
    assert len(models) == 24


def test_identical_public_factors_with_conflicting_labels_bound_hits():
    features = dict(level=50., coal_amount=600., sysA=0, sysB=1, sys401=1, sys402=1)
    rows = [{"record_id": str(i), "actual": value, "features": features} for i,value in enumerate([10., 10.5, 13.])]
    result = conflicts(rows)
    assert result["exact_factor_conflict_unavoidable_misses"] == 1
    assert result["max_possible_hit1_upper_bound"] == 2
