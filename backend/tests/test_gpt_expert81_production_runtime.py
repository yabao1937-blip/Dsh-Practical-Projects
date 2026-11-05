"""Production scope and policy contracts against the actual saved bundle; no fit."""
import importlib.util
from pathlib import Path

import joblib


def runtime():
    root=Path(__file__).resolve().parents[2]
    freeze=root/"output/gpt-expert81-db-primary-20261103/production-freeze"
    spec=importlib.util.spec_from_file_location("production_test_core",freeze/"inference_core.py")
    core=importlib.util.module_from_spec(spec);spec.loader.exec_module(core)
    return core,joblib.load(freeze/"model.joblib")


def row(**features):
    return {"record_id":"synthetic:scope","public_quality":"strict","features":dict(level=50.,coal_amount=600.,sysA=0,sysB=1,sys401=1,sys402=1,**features)}


def test_known_feedback_scope_predicts_and_unseen_three_system_mask_refuses():
    core,bundle=runtime()
    before=joblib.hash(bundle)
    frame=row();assert core.predict_one(bundle,frame)["status"]=="ok"
    frame["features"].update(sysA=1,sys402=0)
    rejected=core.predict_one(bundle,frame)
    assert rejected["prediction"] is None
    assert rejected["reason"]=="unsupported_three_system_feedback_combination"
    assert joblib.hash(bundle)==before


def test_continuous_extrapolation_flags_without_new_filter_and_two_system_policy():
    core,bundle=runtime()
    frame=row();frame["features"]["coal_amount"]=700.
    result=core.predict_one(bundle,frame)
    assert result["status"]=="ok" and "coal_amount" in result["outside_training_ranges"]
    frame["features"].update(sysA=1,sysB=1,sys401=0,sys402=0,level=29.)
    assert core.predict_one(bundle,frame)["prediction"]==15.
    frame["features"]["level"]=31.
    assert core.predict_one(bundle,frame)["prediction"]==14.
