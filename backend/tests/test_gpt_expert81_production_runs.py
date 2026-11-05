"""Actual RUNS baseline routing and independent future coverage; zero fitting."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.gpt_coarse.expert8 import predict
from scripts.predict_gpt_expert81_sealed import production_baseline
from scripts.score_gpt_expert81_frozen import score

KEYS=("coal_amount","sysA","sysB","sys401","sys402","desliming473","desliming474","level")


def model():
    return json.loads((Path(__file__).resolve().parents[1]/"models/gpt_expert8/20261012/model.json").read_text(encoding="utf-8"))["model"]


def frame(count=3):
    flags=[int(i<count) for i in range(4)]
    values=dict(zip(KEYS,[600.,*flags,1,0,50.]))
    return dict(baseline_id="EX8_PRODUCTION_RUNS",feature_namespace="runs_v1",production_system_count=count,production_quality="eligible",production_reason=None,
                features=values,production_features_valid={k:True for k in KEYS},production_feature_status={k:"good" for k in KEYS})


@pytest.mark.parametrize("count",[0,1,3,4])
def test_actual_non_two_routes_use_frozen_EX8_with_real_RUNS(count):
    row=frame(count);result=production_baseline(row,model())
    assert result["ex8_production_prediction"]==pytest.approx(predict(row["features"],model()),abs=1e-12)
    assert result["production_policy_route"]=="non_two_system_EX8_RUNS"
    assert result["production_system_count"]==count


def test_actual_two_system_policy_ignores_missing_coal_and_desliming():
    row=frame(2)
    for key in ("coal_amount","desliming473","desliming474"):
        row["features"][key]=None;row["production_features_valid"][key]=False;row["production_feature_status"][key]="missing"
    row["features"]["level"]=29.
    assert production_baseline(row,model())["current_production_prediction"]==15.
    row["features"]["level"]=30.
    assert production_baseline(row,model())["current_production_prediction"]==14.


def test_non_two_minute_approximation_or_count_mismatch_never_sneaks_through():
    row=frame();row["production_feature_status"]["coal_amount"]="minute_approximation"
    assert production_baseline(row,model())["current_production_prediction"] is None
    row=frame();row["production_system_count"]=2
    assert production_baseline(row,model())["current_production_prediction"] is None


def test_old_only_other_system_is_scored_and_group_mismatch_not_paired():
    metadata={"2":{"fence":{"lower":4.,"upper":24.}},"3":{"fence":{"lower":6.,"upper":17.}}}
    lock={"predicted_at_utc":"2026-11-10T08:00:00+00:00","production_baseline_contract_id":"EX8_PRODUCTION_RUNS","comparison_baseline_field":"current_production_prediction","rows":[
      {"record_id":"db:1","assay_id":1,"system_count":4,"production_system_count":4,"route":None,"prediction":None,"current_production_prediction":14.,"ex8_production_prediction":14.,"comparison_system_count_consistent":True},
      {"record_id":"db:2","assay_id":2,"system_count":2,"production_system_count":3,"route":"independent_system_count:2","prediction":14.,"current_production_prediction":12.,"ex8_production_prediction":12.,"comparison_system_count_consistent":False}]}
    result=score(metadata,lock,[{"assay_id":1,"actual":14.5},{"assay_id":2,"actual":14.5}])
    assert result["summary"]["combined"]["all"]["eligible_n"]==2
    assert result["summary"]["combined"]["all"]["predicted_n"]==1
    assert result["current_summary"]["all"]["predicted_n"]==2
    assert result["current_summary"]["by_system"]["4"]["all"]["predicted_n"]==1
    assert result["baseline_common_coverage"]["current_production_prediction"]["baseline_all"]["predicted_n"]==0
    assert result["comparison_system_count_mismatch_ids"]==[2]
