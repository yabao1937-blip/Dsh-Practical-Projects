from copy import deepcopy
from datetime import timedelta

import pytest

from app.decision_hourly import density_candidates
from app.telemetry.domain import iso_time
from test_hourly_quality_advice import prepared, sessions, seeded, HOUR
from test_density_trend import action, result
from app.decision_hourly.advice_archive import save_result
from app.decision_hourly.models import DensityAction
from app.gpt_density.candidate import timestamp
from test_density_candidate import evidence
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.database import get_db
from app.routers.decision import router as decision_router
from app.decision_hourly.router import router as evidence_router


@pytest.fixture
def client(sessions):
    app=FastAPI()
    app.include_router(decision_router,prefix='/api/v1')
    app.include_router(evidence_router,prefix='/api/v1')
    def database():
        with sessions() as db:
            yield db
    app.dependency_overrides[get_db]=database
    with TestClient(app) as client:
        yield client


def test_default_production_gate_does_not_calculate(client, monkeypatch):
    monkeypatch.delenv('DMCS_DENSITY_EXPERIMENT',raising=False)
    monkeypatch.setattr('app.config.REMOTE_READONLY',None)
    monkeypatch.setattr(density_candidates,'latest_action',lambda *a,**k:pytest.fail('disabled must not query'))
    for path in ('/api/v1/decision/experimental-density','/api/v1/decision/evidence/density-candidates'):
        response=client.get(path)
        assert response.status_code == 404


def test_current_get_preserves_reference_and_database(client,sessions,prepared,monkeypatch):
    monkeypatch.setenv('DMCS_DENSITY_EXPERIMENT','1')
    monkeypatch.setattr(density_candidates,'utc_now',lambda:HOUR)
    monkeypatch.setattr('app.services.decision_sources.shared_snapshot',lambda *a,**k:deepcopy(prepared[0]))
    with sessions() as db:
        before=list(db.connection().connection.driver_connection.iterdump())
        response=client.get('/api/v1/decision/experimental-density')
        assert response.status_code==200,response.text
        body=response.json()
        assert body['reference']['value']==1.46
        assert body['setting']['value']==1.48
        assert body['experimental'] and body['policy']=='density-candidate-v1'
        assert body['candidate']['value']==1.475,body  # prepared ash is provisional
        assert body['formal_execution']['rhoNew']==1.46
        assert list(db.connection().connection.driver_connection.iterdump())==before


def test_history_replay_uses_before_only_and_excludes_backfill(client,sessions,monkeypatch):
    monkeypatch.setenv('DMCS_DENSITY_EXPERIMENT','1')
    monkeypatch.setattr('app.decision_hourly.router.utc_now',lambda:HOUR+timedelta(minutes=20))
    with sessions.begin() as db:
        row=save_result(db,HOUR,result())
        row.snapshot={**row.snapshot,'origin':'advice-backfill-v1'}
        action(db,'no-before-context',HOUR+timedelta(minutes=5))
    with sessions() as db:
        before=list(db.connection().connection.driver_connection.iterdump())
        response=client.get('/api/v1/decision/evidence/density-candidates')
        assert response.status_code==200,response.text
        body=response.json()
        row=body['rows'][0]
        assert row['reference']['identity']=='unknown'
        assert row['candidate']['status']=='unavailable'
        assert row['replay_provenance']['ignored_replay_count']==1
        assert body['summary']['unknown']['unavailable']==1
        assert list(db.connection().connection.driver_connection.iterdump())==before


def test_history_success_uses_paired_before_snapshot_not_after(client,sessions,monkeypatch):
    monkeypatch.setenv('DMCS_DENSITY_EXPERIMENT','1')
    monkeypatch.setattr('app.decision_hourly.router.utc_now',lambda:HOUR+timedelta(minutes=20))
    e=evidence()
    before_at=HOUR+timedelta(minutes=4)
    setting=deepcopy(e['current_setpoint'])
    actual=deepcopy(e['actual_density'])
    setting['observed_at']=actual['observed_at']=iso_time(before_at)
    with sessions.begin() as db:
        row=save_result(db,HOUR,result(1.46))
        row.snapshot={**row.snapshot,'ash':e['reference']['ash'],'basis':e['reference']['basis']}
        action(db,'paired',HOUR+timedelta(minutes=5),after=1.60)
        db.flush()
        a=db.get(DensityAction,'paired')
        content=deepcopy(a.evidence)
        content['before'].update(pair=dict(setpoint=setting,actual=actual),
            sampled_at=iso_time(before_at), decision_at=iso_time(before_at+timedelta(seconds=5)),
            operating_state=dict(stopped=0))
        a.evidence=content
    with sessions() as db:
        before=list(db.connection().connection.driver_connection.iterdump())
        response=client.get('/api/v1/decision/evidence/density-candidates')
        assert response.status_code==200,response.text
        row=response.json()['rows'][0]
        assert row['setting']['value']==1.48 and row['candidate']['value']==1.47
        assert row['reference']['value']==1.46
        assert row['replay_provenance']['outcome_evaluation'] is False
        assert list(db.connection().connection.driver_connection.iterdump())==before


def test_history_read_failure_is_not_treated_as_no_actions():
    from sqlalchemy.exc import SQLAlchemyError
    from fastapi import HTTPException
    class FailedRead:
        def scalar(self,query):
            raise SQLAlchemyError('fixture failure')
    with pytest.raises(HTTPException) as error:
        density_candidates.latest_action(FailedRead(),HOUR)
    assert error.value.status_code==503
    assert error.value.detail['error_code']=='candidate_history_unavailable'
