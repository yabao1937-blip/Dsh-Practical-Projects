from copy import deepcopy
from datetime import timedelta
import pytest
from sqlalchemy import select, text, func

from test_decision_evidence import client, sessions, HOUR, START, seed_anchor
from app.decision_hourly.models import DecisionCycleHour, DensityAction, GptHourlyDensityAdvice
from app.decision_hourly.advice_archive import save_result
from app.telemetry.domain import iso_time


def result(value=1.49):
    return dict(advice_policy='gpt-hourly-reference-v4', target={'min':8.5,'max':9},
        suggested_density=dict(value=value,status='calculated',reason='fixed basis'),
        advice_inputs=dict(ash=dict(value=8.4,revision='r1',start=iso_time(START-timedelta(hours=1)),end=iso_time(START)),
            density=dict(value=1.48,captured_at=iso_time(START),execution_basis=dict(components={'A304':{}}))))


def action(db, identifier, at, channel='A304', before=1.48, after=1.5):
    db.add(DensityAction(id=identifier,detected_at=iso_time(at),channel=channel,evidence=dict(continuous=True,
        before=dict(sampled_at=iso_time(at-timedelta(minutes=1)),pair=dict(setpoint=dict(value=before))),
        after=dict(sampled_at=iso_time(at),pair=dict(setpoint=dict(value=after))))))


def test_real_revisions_not_legacy_means_and_all_changes_remain(client,sessions,monkeypatch):
    now=HOUR+timedelta(minutes=27,seconds=8)
    monkeypatch.setattr('app.decision_hourly.router.utc_now',lambda:now)
    with sessions.begin() as db:
        seed_anchor(db,START,1.48)
        seed_anchor(db,HOUR,1.51,channel='A303')
        db.add(DecisionCycleHour(start=iso_time(START),end=iso_time(HOUR),generated_at=iso_time(HOUR),
            snapshot=dict(suggested_density=dict(value=1.99,status='calculated'))))
        action(db,'up',START+timedelta(minutes=20))
        action(db,'down',START+timedelta(minutes=40),before=1.5,after=1.47)
        action(db,'other',HOUR+timedelta(minutes=10),channel='A303')
        save_result(db,HOUR+timedelta(minutes=1),result())
        save_result(db,HOUR+timedelta(minutes=2),result(1.50))
    with sessions() as db:
        before=list(db.connection().connection.driver_connection.iterdump())
        r=client.get('/api/v1/decision/evidence/density-trend',params={'limit':168})
        assert r.status_code==200,r.text
        data=r.json()
        assert data['period']==dict(start=iso_time(now-timedelta(days=7)),end=iso_time(now))
        assert data['total_actions']==2
        changes=[p['value'] for p in data['channels']['A304'] if p['origin']=='change']
        assert changes==[1.5,1.47]
        assert set(data['channels'])=={'A304'}
        assert [p['value'] for p in data['suggestions']]==[1.49,1.5]
        assert all(p['apply_start']==iso_time(HOUR) for p in data['suggestions'])
        assert list(db.connection().connection.driver_connection.iterdump())==before
    month=client.get('/api/v1/decision/evidence/density-trend',params={'limit':720}).json()
    assert month['period']['start']==iso_time(now-timedelta(days=30))


def test_precise_custom_range_and_table_agree(client,sessions,monkeypatch):
    monkeypatch.setattr('app.decision_hourly.router.utc_now',lambda:HOUR)
    with sessions.begin() as db:
        seed_anchor(db,START,1.48)
        action(db,'outside',START+timedelta(minutes=10))
        action(db,'inside',START+timedelta(minutes=47))
    start=START+timedelta(minutes=31)
    r=client.get('/api/v1/decision/evidence/density-trend',params={'start':iso_time(start),'end':iso_time(HOUR+timedelta(minutes=30))})
    assert r.status_code==200,r.text
    data=r.json()
    assert data['period']==dict(start=iso_time(start),end=iso_time(HOUR))
    assert [a['id'] for a in data['actions']]==['inside']
    assert all(p['origin']!='anchor' for p in data['channels']['A304'])
    assert data['suggestions']==[]


@pytest.mark.parametrize('params', [dict(start='2026-10-08T01:00:00Z'),
    dict(start='2026-10-08T01:00:00',end='2026-10-08T02:00:00Z'),
    dict(start='2026-10-08T01:00:01Z',end='2026-10-08T02:00:00Z'),
    dict(start='2026-10-08T03:00:00Z',end='2026-10-08T02:00:00Z'),
    dict(start='2025-10-08T01:00:00Z',end='2026-10-08T02:00:00Z')])
def test_invalid_range(client,params):
    assert client.get('/api/v1/decision/evidence/density-trend',params=params).status_code==422


def test_old_host_missing_table_is_explicit_not_fake(client,sessions):
    with sessions.begin() as db:
        db.execute(text('DROP TABLE gpt_hourly_density_advice'))
    r=client.get('/api/v1/decision/evidence/density-trend').json()
    assert not r['archive_available'] and r['suggestions']==[]


def test_archive_deduplicates_but_preserves_return_to_previous_value_and_null(sessions):
    with sessions.begin() as db:
        save_result(db,HOUR,result());db.flush()
        assert save_result(db,HOUR+timedelta(minutes=1),result()) is None
        save_result(db,HOUR+timedelta(minutes=2),result(1.50));db.flush()
        save_result(db,HOUR+timedelta(minutes=3),result());db.flush()
        missing=result(None);missing['suggested_density']['status']='missing'
        save_result(db,HOUR+timedelta(minutes=4),missing);db.flush()
        assert db.scalar(select(func.count()).select_from(GptHourlyDensityAdvice))==4
        save_result(db,HOUR+timedelta(hours=1),result());db.flush()
        assert db.scalar(select(func.count()).select_from(GptHourlyDensityAdvice))==5


def test_readonly_archive_guard(monkeypatch):
    import app.decision_hourly.advice_archive as archive
    monkeypatch.setattr(archive,'REMOTE_READONLY',True)
    archive.archive_tick(lambda:pytest.fail('readonly must not even open a write session'))


def test_next_before_point_preserves_gap_and_change_marker(client,sessions,monkeypatch):
    monkeypatch.setattr('app.decision_hourly.router.utc_now',lambda:HOUR)
    at=START+timedelta(minutes=20)
    with sessions.begin() as db:
        action(db,'gap',at)
        action(db,'next',at+timedelta(minutes=1),before=1.5,after=1.49)
        db.flush()
        first=db.get(DensityAction,'gap')
        first.evidence={**first.evidence,'continuous':False}
    data=client.get('/api/v1/decision/evidence/density-trend').json()
    point=next(p for p in data['channels']['A304'] if p['at']==iso_time(at))
    assert point['origin']=='change' and point['break_before'] and point['action_id']=='gap'


def test_other_route_advice_does_not_compare_with_a304(client,sessions,monkeypatch):
    monkeypatch.setattr('app.decision_hourly.router.utc_now',lambda:HOUR)
    other=result()
    other['advice_inputs']['density']['execution_basis']['components']={'A303':{}}
    with sessions.begin() as db:
        save_result(db,START,other)
    r=client.get('/api/v1/decision/evidence/density-trend').json()
    assert set(r['channels'])=={'A304'}
    assert r['suggestions'][0]['value'] is None
