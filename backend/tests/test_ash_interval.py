"""Minute clipping of immutable decision evidence, including compressed history."""
from copy import deepcopy
from datetime import timedelta, timezone

import pytest
from sqlalchemy import event

from app.decision_hourly.ash_interval import read_ash_interval
from app.decision_hourly.archive import archive_old_hour
from app.decision_hourly.evidence import MASS
from app.decision_hourly.models import DecisionCycleContext, DecisionCycleSample, DecisionCycleHour
from app.telemetry.domain import iso_time
from test_hourly_brief import sessions, HOUR
from test_decision_sources import client
from test_decision_evidence import row, START
from test_ash_history import save

NOW = HOUR+timedelta(minutes=24,seconds=37)


def save_rows(db, rows):
    if not db.get(DecisionCycleContext,'ctx'):
        db.add(DecisionCycleContext(key='ctx',definition={}))
        db.flush()
    for r in rows:
        db.add(DecisionCycleSample(slot=r.slot,sampled_at=r.sampled_at,context_key=r.context_key,
            fingerprint=r.fingerprint,snapshot=deepcopy(r.snapshot)))


def test_exact_minute_range_uses_saved_samples_not_hour_average(sessions):
    with sessions.begin() as db:
        save_rows(db,[row(i,400 if i<360 else 100,8 if i<360 else 10) for i in range(720)])
        save(db,START,99,720)  # Deliberately different: cannot stand in for a partial hour.
    with sessions() as db:
        before=list(db.connection().connection.driver_connection.iterdump())
        r=read_ash_interval(db,START+timedelta(minutes=10),START+timedelta(minutes=40),NOW)
        assert r['summary']['value']==pytest.approx((8*400*20+10*100*10)/(400*20+100*10))
        assert r['summary']['mass_t']==pytest.approx(150)
        assert r['summary']['valid_seconds']==1800
        assert r['items'][0]['sample_count']==360
        minute=read_ash_interval(db,START+timedelta(minutes=30),START+timedelta(minutes=31),NOW)
        assert minute['summary']['value']==10
        assert minute['items'][0]['sample_count']==12
        assert minute['summary']['coverage']==1
        assert read_ash_interval(db,START,HOUR,NOW)['summary']['value']==99
        assert list(db.connection().connection.driver_connection.iterdump())==before


def test_clips_both_edges_without_bridging_gaps(sessions):
    a,b=row(11,400,8),row(23,100,10)
    a.sampled_at=iso_time(START+timedelta(seconds=58))
    b.sampled_at=iso_time(START+timedelta(seconds=118))
    with sessions.begin() as db:
        save_rows(db,[a,b])
    with sessions() as db:
        r=read_ash_interval(db,START+timedelta(minutes=1),START+timedelta(minutes=2),NOW)
        assert r['summary']['mass_t']==pytest.approx((400*3+100*2)/3600)
        assert r['summary']['value']==pytest.approx((8*400*3+10*100*2)/(400*3+100*2))
        assert r['summary']['valid_seconds']==5
        assert r['summary']['coverage']==pytest.approx(5/60)


def test_batch_seen_before_range_does_not_become_new_production(sessions):
    a,b=row(11),row(12)
    for key in ('belt501','belt502'):
        b.snapshot['cells'][key]=deepcopy(a.snapshot['cells'][key])
    with sessions.begin() as db:
        save_rows(db,[a,b])
    with sessions() as db:
        r=read_ash_interval(db,START+timedelta(minutes=1),START+timedelta(minutes=2),NOW)
        assert r['summary']['value'] is None and r['summary']['mass_t']==0


def test_hot_and_compressed_boundary_records_match(sessions):
    with sessions.begin() as db:
        save_rows(db,[row(i) for i in range(36)])
        save(db,START,8,36)
    with sessions() as db:
        expected=read_ash_interval(db,START+timedelta(minutes=1),START+timedelta(minutes=3),NOW)
    with sessions.begin() as db:
        archive_old_hour(db,iso_time(HOUR+timedelta(days=8)))
    with sessions() as db:
        assert read_ash_interval(db,START+timedelta(minutes=1),START+timedelta(minutes=3),NOW)==expected


def test_only_hour_summary_cannot_supply_minutes_and_complete_hour_stays_frozen(sessions):
    with sessions.begin() as db:
        save(db,START,8.5,720)
    with sessions() as db:
        r=read_ash_interval(db,START+timedelta(minutes=1),START+timedelta(minutes=2),NOW)
        assert r['summary']['value'] is None
        assert '不能从小时均值' in r['items'][0]['reason']
        r=read_ash_interval(db,START,HOUR,NOW)
        assert r['summary']['value']==8.5


def test_legacy_and_mass_are_not_blended(sessions):
    old=row(0,ash=12)
    old.snapshot.pop('evidence_policy')
    with sessions.begin() as db:
        save_rows(db,[old,row(1,400,8)])
    with sessions() as db:
        r=read_ash_interval(db,START,START+timedelta(minutes=1),NOW)
        assert r['summary']['value'] is None and r['summary']['aggregation']=='mixed'
        assert r['summary']['groups']['output_mass']['value']==8
        assert r['summary']['groups']['legacy']['value']==12


def test_default_current_minute_timezone_and_hour_slices(sessions):
    with sessions() as db:
        r=read_ash_interval(db,now=NOW)
        assert r['period']['end']==iso_time(NOW.replace(second=0))
        assert len(r['items'])==25
        assert r['summary']['total_seconds']==86400
        start,end=START+timedelta(minutes=15),HOUR+timedelta(minutes=20)
        r=read_ash_interval(db,start,end,NOW)
        local=timezone(timedelta(hours=8))
        assert r==read_ash_interval(db,start.astimezone(local),end.astimezone(local),NOW)
        assert [(i['start'],i['end']) for i in r['items']]==[(iso_time(start),iso_time(HOUR)),(iso_time(HOUR),iso_time(end))]
        assert r['summary']['expected_count']==65*12


@pytest.mark.parametrize('start,end',[
    (None,HOUR),(HOUR,HOUR),(HOUR,HOUR-timedelta(minutes=1)),
    (HOUR-timedelta(days=32),HOUR),(HOUR,NOW+timedelta(minutes=1)),
    (HOUR,HOUR+timedelta(seconds=61)),(HOUR.replace(tzinfo=None),HOUR+timedelta(minutes=1))])
def test_minute_validation(sessions,start,end):
    with sessions() as db,pytest.raises(ValueError):
        read_ash_interval(db,start,end,NOW)


def test_interval_api_supports_current_hour_minutes_read_only(client,sessions,monkeypatch):
    monkeypatch.setattr('app.decision_hourly.ash_interval.utc_now',lambda:NOW)
    with sessions.begin() as db:
        save_rows(db,[row(720,400,8),row(721,100,10)])
    params={'start':iso_time(HOUR),'end':iso_time(HOUR+timedelta(minutes=1))}
    with sessions() as db:
        before=list(db.connection().connection.driver_connection.iterdump())
    response=client.get('/api/v1/decision/total-ash-interval',params=params)
    assert response.status_code==200,response.text
    assert response.json()['summary']['value']==pytest.approx(8.4)
    assert response.json()['summary']['aggregation']==MASS
    with sessions() as db:
        assert list(db.connection().connection.driver_connection.iterdump())==before


def test_frozen_mass_hour_and_two_minute_edges_conserve_mass(sessions):
    with sessions.begin() as db:
        save_rows(db,[row(i,400,8) for i in range(360,720)]+[row(i,100,10) for i in range(1440,1800)])
        db.add(DecisionCycleHour(start=iso_time(HOUR),end=iso_time(HOUR+timedelta(hours=1)),
            generated_at=iso_time(HOUR+timedelta(hours=1)),snapshot={'total_ash':dict(value=9,status='average',
                aggregation=MASS,mass_t=300,ash_mass_t=27,sample_count=720,valid_seconds=3600)}))
    with sessions() as db:
        r=read_ash_interval(db,START+timedelta(minutes=30),HOUR+timedelta(hours=1,minutes=30),HOUR+timedelta(hours=2))
        assert len(r['items'])==3 and r['items'][1]['frozen']
        assert r['summary']['mass_t']==pytest.approx(200+300+50)
        assert r['summary']['value']==pytest.approx(100*(16+27+5)/550)
        assert r['summary']['coverage']==1


def test_31_day_empty_range_does_not_query_once_per_hour(sessions):
    queries=[]
    with sessions() as db:
        connection=db.connection()
        def capture(conn,cursor,statement,parameters,context,many):
            queries.append(statement)
        event.listen(connection,'before_cursor_execute',capture)
        end=NOW.replace(second=0)
        r=read_ash_interval(db,end-timedelta(days=31),end,NOW)
        assert len(r['items'])==745 and r['summary']['value'] is None
        assert len(queries)<=4
