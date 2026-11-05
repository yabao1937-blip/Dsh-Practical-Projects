"""Synthetic leakage, source-mass and independent-system contracts."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json

import numpy as np
import pytest
from scipy.optimize import minimize

from app.gpt_coarse.experiments import expert81 as first_core
from app.gpt_coarse.experiments.expert81_expanded import (
    CANDIDATES,DEFAULT_CONFIG,FEATURES,available_rows,design,digest,fit_state,
    fit_two_system,group_of,predict_state,weighted_huber,
)

UTC=timezone.utc
AT=datetime(2026,11,2,4,tzinfo=UTC)
GROUP="0,1,1,1,0,0"


def row(i,value,*,domain="current",quality="strict",sample=None,available=None,old177=True,features=None):
    values=features or {"level":40.+i,"coal_amount":550.+i,"sysA":0,"sysB":1,"sys401":1,"sys402":1,"desliming473":0,"desliming474":0}
    sampled=sample or AT-timedelta(hours=55-8*i)
    ready=available or sampled+timedelta(hours=1)
    return {"record_id":f"{domain}:{i}","assay_id":i,"version_id":f"{domain}:v{i}","source":domain,
            "domain":domain,"quality":quality,"sample_at":sampled.isoformat(),"available_at":ready.isoformat(),
            "group_key":group_of(values),"system_count":sum(values[k] for k in ("sysA","sysB","sys401","sys402")),
            "ash":value,"features":values,"old177":old177,"frozen_prediction":13.,"provenance":{"kind":"immutable_synthetic_version"}}


@pytest.fixture
def current():
    return [row(i,9.+i) for i in range(1,6)]


@pytest.fixture
def legacy():
    records=[]
    for i in range(20):
        values={"level":30.+2*i,"coal_amount":0. if i<2 else 450.+10*i,
                "sysA":1,"sysB":1,"sys401":1,"sys402":i%2,"desliming473":(i//2)%2,"desliming474":(i//3)%2}
        records.append(row(i,13.-.03*i,domain="legacy",quality="legacy_complete",old177=i>=2,
                           sample=AT-timedelta(days=30),available=AT-timedelta(days=20),features=values))
    return records


def fitted(rows,candidate="E3",at=AT,**kwargs):
    return fit_state(rows,at,candidate,group_key=GROUP,**kwargs)


def test_legacy_total_mass_is_one_quarter_current_and177_sensitivity_keeps_equal_mass(current,legacy):
    full=fitted(current+legacy,"E3");old177=fitted(current+legacy,"E4")
    assert full["status"]==old177["status"]=="ok"
    assert full["weight_sums"]["legacy"]==old177["weight_sums"]["legacy"]==1.25
    assert full["weight_sums"]["current_strict"]==5
    assert full["legacy_fences"]==old177["legacy_fences"]
    for state in (full,old177):
        actual=sum(w for r,w in zip(state["used_rows"],state["model"]["row_weights"]) if r["domain"]=="legacy")
        assert actual==pytest.approx(1.25)
    assert len(full["used_rows"])==25 and len(old177["used_rows"])==23


def test_approximate_mass_is_capped_and_never_redefines_shared_strict_fence(current,legacy):
    approximate=[row(50+i,12.,domain="recent_excel",quality="minute_approximation",sample=AT-timedelta(days=3),available=AT-timedelta(days=2)) for i in range(30)]
    states=[fitted(current+legacy+approximate,method) for method in CANDIDATES]
    assert all(s["fence"]==states[0]["fence"] and s["current_normal_ids"]==states[0]["current_normal_ids"] for s in states)
    enhanced=states[-1]
    assert enhanced["weight_sums"]["approximate"]==1.25
    assert "recent_excel" in enhanced["model"]["source_domains"]
    assert len(enhanced["model"]["source_domains"])==2
    assert "source:current" not in enhanced["model"]["columns"]


@pytest.mark.parametrize("candidate",CANDIDATES)
def test_current_target_future_label_and_future_revision_do_not_change_earlier_state(current,legacy,candidate):
    future=row(80,18.,sample=AT-timedelta(hours=1),available=AT+timedelta(hours=1))
    target=row(99,12.,sample=AT-timedelta(hours=3),available=AT-timedelta(hours=2))
    before=fitted(current+legacy+[future,target],candidate,exclude_record_id=target["record_id"])
    future["ash"]=float("nan");target["ash"]=-999.
    after=fitted(current+legacy+[future,target],candidate,exclude_record_id=target["record_id"])
    assert before==after==fitted(current+legacy,candidate,exclude_record_id=target["record_id"])


def test_latest_group_or_bad_quality_revision_cannot_revive_an_old_eligible_version(current):
    moved=deepcopy(current[0]);moved.update(version_id="moved",available_at=(AT-timedelta(minutes=1)).isoformat(),quality="label_review")
    result=available_rows(current+[moved],AT)
    assert current[0]["record_id"] not in [r["record_id"] for r in result]
    deleted={"record_id":current[0]["record_id"],"version_id":"deleted","available_at":(AT-timedelta(seconds=1)).isoformat(),"deleted":True,"provenance":{"kind":"deletion"}}
    assert current[0]["record_id"] not in [r["record_id"] for r in available_rows(current+[deleted],AT)]


def test_legacy_base_is_fixed_without_current_tail_labels_for_both_two_stage_models(current,legacy):
    first=fitted(current+legacy,"E5");second=fitted(current+legacy,"E6")
    assert first["model"]["legacy_response"]==second["model"]["legacy_response"]
    changed=deepcopy(current)
    for r in changed:r["ash"]+=.5
    later=fitted(changed+legacy,"E6")
    assert first["model"]["legacy_response"]==later["model"]["legacy_response"]
    assert first["model"]["calibration"]["value"]!=pytest.approx(fitted(changed+legacy,"E5")["model"]["calibration"]["value"])


def test_E2_replays_chronological_current_C7_rather_than_full_data_artifact(current,legacy):
    state=fitted(current+legacy,"E2")
    events=[{"assay_id":r["assay_id"],"version_id":r["version_id"],"sample_at":r["sample_at"],"version_available_at":r["available_at"],"value":r["ash"],"group_key":GROUP,"features":r["features"],"frozen_prediction":13.,"provenance":r["provenance"]} for r in current]
    prior=first_core.fit_state(events,AT,"C7",{"receipt_mode":"historical_entry_proxy"},group_key=GROUP)
    assert predict_state(state,current[0]["features"])["prediction"]==first_core.predict_state(prior,current[0]["features"])["prediction"]


def test_weighted_huber_domain_intercepts_match_an_independent_convex_objective(current,legacy):
    rows=current+legacy;cfg=deepcopy(DEFAULT_CONFIG)
    weights=[1.]*len(current)+[.25*len(current)/len(legacy)]*len(legacy)
    model=weighted_huber(rows,["level","coal_amount"],cfg,weights=weights,source_domains=True)
    x=design(rows,["level","coal_amount"],model["source_domains"],cfg);y=np.array([r["ash"] for r in rows]);w=np.array(weights)
    def objective(beta):
        residual=y-x@beta
        return np.sum(w*np.where(np.abs(residual)<=1,.5*residual**2,np.abs(residual)-.5))+.5*np.dot(beta[1:3],beta[1:3])
    independent=minimize(objective,np.zeros(x.shape[1]),method="BFGS",options={"gtol":1e-7})
    assert objective(np.array(model["coefficients"]))==pytest.approx(independent.fun,abs=1e-7)
    assert model["gradient_max"]<1e-6


def test_two_system_branch_uses_only_own_group_and_never_loo_future_labels(current,legacy):
    values={**current[0]["features"],"sys402":0}
    group="0,1,1,0,0,0"
    own=[row(100+i,14.+.1*i,sample=AT-timedelta(hours=8-i),features={**values,"level":30.+.1*i}) for i in range(3)]
    assert fit_two_system(current+legacy+own,AT,group_key=group,method="T0")["status"]=="ok"
    diagnostic=fit_two_system(current+legacy+own,AT,group_key=group,method="T1")
    assert diagnostic["status"]=="unavailable"
    forward=fit_two_system(current+legacy+own,AT,group_key=group,method="T0",exclude_record_id=own[-1]["record_id"])
    assert forward["status"]=="unavailable"
    state=fit_two_system(current+legacy+own,AT,group_key=group,method="T0")
    assert {r["record_id"] for r in state["used_rows"]}=={r["record_id"] for r in own}
    assert fitted(current+legacy,"E3")["candidate_id"]=="E3"
    assert fit_state(current+legacy,AT,"E3",group_key=group)["reason"]=="two_system_requires_independent_branch"


def test_missing_factors_are_never_filled_and_nonfinite_raw_is_not_clipped(current,legacy):
    broken=deepcopy(current);broken[0]["features"]["level"]=None
    with pytest.raises((ValueError,TypeError)):
        fitted(broken+legacy)
    state=fitted(current+legacy,"E1");state.pop("state_hash");state["model"]["coefficients"]=[1e308,1e308];state["state_hash"]=digest(state)
    result=predict_state(state,{**current[0]["features"],"level":1e308})
    assert result["prediction"] is None and result["status"]=="unavailable"
    decoded=json.loads(json.dumps(fitted(current+legacy,"E3")))
    assert predict_state(decoded,current[0]["features"])["prediction"] is not None
