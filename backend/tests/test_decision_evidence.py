"""Mass conservation, traceability and independent GPT evidence writes."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import require_write
from app.database import get_db
from app.decision_hourly.evidence import POLICY, MASS, summarize
from app.decision_hourly.tracking import observe
from app.decision_hourly.models import DecisionCycleHour, DecisionCycleSample, DecisionCycleContext, DensityAction, DensityActionEntry
from app.decision_hourly.archive import archive_old_hour, read_sample
from app.decision_hourly.ash_history import read_ash_hours
from app.decision_hourly.router import router
from app.telemetry.domain import iso_time
from test_hourly_brief import sessions, HOUR

START=HOUR-timedelta(hours=1)


def row(i,rate=400,ash=8,level=50,on=True,setpoint=1.48):
    when=START+timedelta(seconds=i*5)
    cell=dict(value=rate/2,status='good',quality='good',unit='t/h',source_id='kep',
        observed_at=iso_time(when),received_at=iso_time(when),batch_id=str(i),definition_id='defined')
    c=dict(belt501=deepcopy(cell),belt502=deepcopy(cell),
        total_ash=dict(value=ash,status='calculated'),level=dict(value=level,status='good'),
        density=dict(value=1.47,status='good'),
        systems=dict(components={'401':dict(value=on,status='good')}))
    pair=dict(setpoint=dict(cell,value=setpoint,point_key='md-set'),actual=dict(cell,value=1.477,point_key='md'))
    return SimpleNamespace(slot=int(when.timestamp())//5,sampled_at=iso_time(when),context_key='ctx',fingerprint=str(i),
        snapshot=dict(evidence_policy=POLICY,cells=c,total_ash=c['total_ash'],
            inputs={'is_stoppage':dict(value=0)},density_channels={'A304':pair},suggested_density=dict(value=1.48,status='calculated')))


def test_actual_output_mass_conservation_and_time_coverage():
    rows=[row(i,400 if i<360 else 100,8 if i<360 else 10) for i in range(720)]
    r=summarize(rows,HOUR)['total_ash']
    assert r['value']==pytest.approx(8.4)
    assert r['mass_t']==pytest.approx(250)
    assert r['ash_mass_t']==pytest.approx(21)
    assert r['valid_seconds']==3600 and r['sample_count']==720


def test_duplicates_gaps_and_missing_ash_do_not_manufacture_tonnes():
    a,b,c,d=row(0),row(1),row(60),row(61,0)
    b.snapshot['cells']['belt501']=deepcopy(a.snapshot['cells']['belt501'])
    b.snapshot['cells']['belt502']=deepcopy(a.snapshot['cells']['belt502'])
    c.snapshot['total_ash']=dict(value=None,status='missing')
    r=summarize([a,b,c,d],HOUR)['total_ash']
    assert r['mass_t']==pytest.approx(400*5/3600)
    assert r['observed_mass_t']==pytest.approx(400*10/3600)
    assert r['flow_seconds']==15 and r['valid_seconds']==5
    assert r['excluded_reasons']=={'重复皮带批次':1,'无有效总灰分':1,'零产出':1}
    assert len(summarize([a,b,c,d],HOUR)['segments'])>=2


@pytest.mark.parametrize('field,value', [('batch_id','other'),('source_id','other'),('definition_id',None),
    ('quality','bad'),('status','held'),('unit',''),('value',-1),('value',float('nan'))])
def test_invalid_flow_pair_excluded(field,value):
    r=row(0);r.snapshot['cells']['belt502'][field]=value
    assert summarize([r],HOUR)['total_ash']['mass_t']==0


def test_equal_values_new_batches_count_but_old_policy_does_not():
    rows=[row(0),row(1),row(2)]
    rows[2].snapshot.pop('evidence_policy')
    result=summarize(rows,HOUR)['total_ash']
    assert result['sample_count']==2 and result['valid_seconds']==10


def test_hour_end_duration_clips_without_gap_fill():
    r=row(719);r.sampled_at=iso_time(HOUR-timedelta(seconds=2))
    result=summarize([r],HOUR)['total_ash']
    assert result['valid_seconds']==2
    assert result['mass_t']==pytest.approx(400*2/3600)


def test_old_batch_cannot_count_again_in_next_hour():
    a=row(719)
    b=deepcopy(a);b.sampled_at=iso_time(HOUR);b.slot+=1
    old=summarize([a],HOUR)['total_ash']
    new=summarize([b],HOUR+timedelta(hours=1))['total_ash']
    assert old['valid_seconds']==5 and new['valid_seconds']==0
    assert new['excluded_reasons']=={'跨小时旧皮带批次':1}


def test_segment_boundaries_cannot_overlap_on_jitter():
    a,b=row(0),row(1,on=False)
    a.sampled_at=iso_time(START+timedelta(seconds=4))
    result=summarize([a,b],HOUR)['segments']
    assert result[0]['end']==result[1]['start']
    assert result[0]['duration_seconds']==1


def test_segments_keep_binary_states_and_sustained_changes():
    rows=[row(i,level=50 if i<6 else 70,on=i<3) for i in range(12)]
    result=summarize(rows,HOUR)
    segments=result['segments']
    assert len(segments)==3
    assert [s['state']['switches']['systems']['401'] for s in segments]==[True,False,False]
    assert segments[0]['duration_seconds']==15
    assert '持续确认' in segments[-1]['start_reason']
    assert sum(s['sample_count'] for s in segments)==12
    assert result['response']['interval'] is None and result['response']['interval_width_limit'] is None
    assert result['response']['execution_step_limit']==.03
    rows=[row(i,level=70 if i==3 else 50) for i in range(8)]
    assert len(summarize(rows,HOUR)['segments'])==1


def test_mass_history_and_mixed_legacy_no_blended_mean(sessions):
    with sessions.begin() as db:
        for i,(rate,ash) in enumerate([(400,8),(100,10)]):
            start=START+timedelta(hours=i)
            c=summarize([row(0,rate,ash)],HOUR)['total_ash']
            db.add(DecisionCycleHour(start=iso_time(start),end=iso_time(start+timedelta(hours=1)),
                generated_at=iso_time(HOUR),snapshot={'total_ash':c}))
    with sessions() as db:
        r=read_ash_hours(db,START,HOUR+timedelta(hours=1),HOUR+timedelta(hours=2))
        assert r['summary']['value']==pytest.approx(8.4)
        assert r['summary']['aggregation']==MASS
    with sessions.begin() as db:
        db.add(DecisionCycleHour(start=iso_time(HOUR+timedelta(hours=1)),end=iso_time(HOUR+timedelta(hours=2)),
            generated_at=iso_time(HOUR),snapshot={'total_ash':dict(value=9,status='average',sample_count=720)}))
    with sessions() as db:
        r=read_ash_hours(db,START,HOUR+timedelta(hours=2),HOUR+timedelta(hours=2))
        assert r['summary']['value'] is None and r['summary']['aggregation']=='mixed'
        assert r['summary']['groups']['output_mass']['value']==pytest.approx(8.4)


def test_setpoint_change_is_observation_not_inferred_manual_action(sessions):
    with sessions.begin() as db:
        observe(db,row(0));db.flush()
        observe(db,row(1));db.flush()
        assert db.scalar(select(DensityAction)) is None
        observe(db,row(2,setpoint=1.49));db.flush()
        action=db.scalar(select(DensityAction))
        assert action.channel=='A304' and action.evidence['continuous']
        assert action.evidence['before']['pair']['setpoint']['value']==1.48
        assert list(db.scalars(select(DensityActionEntry)))==[]
        observe(db,row(10,setpoint=1.50));db.flush()
        later=list(db.scalars(select(DensityAction).order_by(DensityAction.detected_at)))[-1]
        assert not later.evidence['continuous']


def test_lossless_archive_then_hot_delete(sessions):
    r=row(0)
    with sessions.begin() as db:
        db.add(DecisionCycleContext(key='ctx',definition={}))
        db.add(DecisionCycleSample(**vars(r)))
        db.add(DecisionCycleHour(start=iso_time(START),end=iso_time(HOUR),generated_at=iso_time(HOUR),snapshot={'frozen':'original'}))
    with sessions.begin() as db:
        archive_old_hour(db,iso_time(HOUR))
    with sessions() as db:
        assert db.get(DecisionCycleSample,r.slot) is None
        assert read_sample(db,r.slot)['snapshot']==r.snapshot
        assert db.get(DecisionCycleHour,iso_time(START)).snapshot=={'frozen':'original'}


@pytest.fixture
def client(sessions):
    app=FastAPI();app.include_router(router,prefix='/api/v1')
    def database():
        with sessions() as db:yield db
    app.dependency_overrides[get_db]=database
    app.dependency_overrides[require_write]=lambda:None
    with TestClient(app) as c:yield c


def make_action(sessions):
    with sessions.begin() as db:
        observe(db,row(0));db.flush();observe(db,row(1,setpoint=1.5));db.flush()
        return db.scalar(select(DensityAction.id))


def test_entries_idempotent_revision_and_report_timing(client,sessions):
    identifier=make_action(sessions)
    url='/api/v1/decision/evidence/actions/'+identifier+'/entries'
    body=dict(client_id=str(uuid4()),revision=0,kind='confirmation',origin='manual',adoption='unknown')
    assert client.post(url,json=body).status_code==200
    assert client.post(url,json=body).json()['revision']==1
    assert client.post(url,json=dict(body,origin='automatic')).status_code==409
    assert client.post(url,json=dict(body,client_id=str(uuid4()))).status_code==409
    assay=dict(client_id=str(uuid4()),revision=1,kind='assay',phase='after',value=8.5,
        sample_start=iso_time(START+timedelta(minutes=1)),sample_end=iso_time(START+timedelta(minutes=1)),
        reported_at=iso_time(START+timedelta(minutes=30)),sample_location='重介皮带',sample_code='lab-1')
    assert client.post(url,json=dict(assay,reported_at=iso_time(START))).status_code==422
    assert client.post(url,json=dict(assay,sample_start=iso_time(START))).status_code==422
    response=client.post(url,json=assay)
    assert response.status_code==200,response.text
    assert response.json()['revision']==2
    assert client.post(url,json=dict(assay,client_id=str(uuid4()),revision=2)).status_code==409
    with sessions() as db:
        from app.models import ManualMeasurement,AutoState
        assert db.scalar(select(ManualMeasurement)) is None
        assert db.scalar(select(AutoState)) is None
        before=list(db.connection().connection.driver_connection.iterdump())
        assert client.get('/api/v1/decision/evidence/actions/'+identifier).status_code==200
        assert list(db.connection().connection.driver_connection.iterdump())==before


@pytest.mark.parametrize('stamp',['0001-01-01T00:00:00Z','9999-12-31T23:00:00Z','2026-10-08T01:00:00+08:30'])
def test_hour_query_bad_time_is_422_not_500(client,stamp):
    assert client.get('/api/v1/decision/evidence/samples',params={'start':stamp}).status_code==422


def test_samples_and_segments_queries_are_read_only(client,sessions):
    with sessions() as db:
        before=list(db.connection().connection.driver_connection.iterdump())
        assert client.get('/api/v1/decision/evidence/segments').status_code==200
        assert client.get('/api/v1/decision/evidence/hours').json()['current_start']
        assert client.get('/api/v1/decision/evidence/samples',params={'start':iso_time(START)}).json()['total']==0
        assert list(db.connection().connection.driver_connection.iterdump())==before
