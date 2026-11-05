"""Native EX8 baseline contract excludes semantic substitution."""
import json
from pathlib import Path

import pytest

from app.gpt_coarse.expert8 import predict
from scripts.predict_gpt_expert81_sealed import baseline, main, runtime_versions, write_durable_exclusive


def model():
    return json.loads((Path(__file__).resolve().parents[1]/"models/gpt_expert8/20261012/model.json").read_text(encoding="utf-8"))["model"]


@pytest.mark.parametrize("level,coal", [(20., 300.), (50., 600.), (90., 1200.)])
def test_saved_EX8_formula_equals_original_frozen_native(level, coal):
    values = dict(level=level, coal_amount=coal, sysA=0, sysB=1, sys401=1, sys402=1, desliming473=0, desliming474=0)
    row = dict(features=values, public_quality="strict", feature_namespace="legacy_if_prod", semantic_quality="strict")
    assert baseline(row, model())["ex8_native_prediction"] == pytest.approx(predict(values, model()), abs=1e-12)
    row["feature_namespace"] = "runs_v1"
    assert baseline(row, model())["ex8_native_prediction"] is None
    assert baseline(row, model())["ex8_native_reason"] == "native_legacy_if_prod_inputs_unavailable"


def test_two_system_original_production_is_separate14_15_lookup():
    row = dict(features=dict(level=31., coal_amount=500., sysA=0, sysB=1, sys401=1, sys402=0), public_quality="strict")
    value = baseline(row, model())
    assert value["ex8_native_prediction"] is None
    assert value["old_production_policy_prediction"] == 14.
    row["features"]["level"] = 29.
    assert baseline(row, model())["old_production_policy_prediction"] == 15.


def test_runtime_mismatch_refuses_before_any_model_load(tmp_path, monkeypatch):
    import joblib
    freeze = tmp_path/"freeze"; freeze.mkdir()
    wrong = runtime_versions(); wrong["sklearn"] = "0.0-wrong"
    (freeze/"environment.json").write_text(json.dumps(wrong), encoding="utf-8")
    monkeypatch.setattr(joblib, "load", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not load")))
    output = tmp_path/"prediction-lock.json"
    with pytest.raises(ValueError, match="runtime versions"):
        main(["--freeze", str(freeze), "--inputs", str(tmp_path/"never-opened.json"), "--output", str(output)])
    assert not output.exists()
    refusal = json.loads((tmp_path/"prediction-lock.runtime-refusal.json").read_text())
    assert refusal["status"] == "runtime_version_mismatch_refused_before_joblib_load"
    assert refusal["fits_performed"] == 0


def test_prediction_lock_creation_is_exclusive(tmp_path):
    output = tmp_path/"lock.json"
    write_durable_exclusive(output, {"immutable": 1})
    with pytest.raises(FileExistsError):
        write_durable_exclusive(output, {"immutable": 2})
    assert json.loads(output.read_text()) == {"immutable": 1}
