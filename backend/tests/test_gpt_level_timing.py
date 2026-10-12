"""Plant-confirmed 90s level transport delay; no training-row shift or live DB writes."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.gpt_coarse.timing import POLICY, level_mean
from app.services.decision_sources import shared_snapshot, identity
from app.services.state import load_store
from app.hourly_brief.service import build_snapshot
from app.telemetry.domain import iso_time, normalize
from app.telemetry.models import TelemetryReading, TelemetryPoint
from app.decision_hourly.models import DecisionCycleSample
from app.decision_hourly.service import record, context_definition, build_hour
from app.decision_hourly.statistics import mean_cell
from app.models import DecisionSourceRevision
from test_hourly_brief import sessions, HOUR
from test_decision_sources import seeded


def levels(sessions, cfg, events):
    """Keep the latest t-2s reading, replace only the liquid-level raw history."""
    with sessions.begin() as db:
        db.execute(delete(TelemetryReading).where(TelemetryReading.point_key=='point2',
            TelemetryReading.received_at < iso_time(HOUR-timedelta(seconds=2))))
        for seconds, value, code in events:
            when=HOUR-timedelta(seconds=seconds)
            sample=normalize(cfg.points[2],value,code,when,when,when)
            db.add(TelemetryReading(source_id=cfg.source_id,point_key='point2',batch_id=uuid4().hex,
                received_at=iso_time(when),sample=sample))


@pytest.mark.parametrize('seconds,expected',[(90,75),(90.001,75),(120,75),(120.001,None),(89.999,None)])
def test_target_and_tolerance_are_past_only(sessions,seeded,seconds,expected):
    levels(sessions,seeded[0],[(seconds,75,0)])
    with sessions() as db:
        before=list(db.connection().connection.driver_connection.iterdump())
        snap=shared_snapshot(db)
        c=snap['cells']['level']
        assert c['value']==expected
        assert all(snap['inputs']['level'][k]==v for k,v in c.items())
        assert snap['level_observation']['value']==50
        assert c['alignment']['target_at']==iso_time(HOUR-timedelta(seconds=90))
        if expected is not None:
            assert c['observed_at']==iso_time(HOUR-timedelta(seconds=seconds))
            assert snap['coarse_ash']['value']==pytest.approx(9.7)
        else:
            assert snap['coarse_ash']['value'] is None
            assert snap['suggested_density']['value'] is None
        assert build_snapshot(db,HOUR)['cells']['level']==c
        assert list(db.connection().connection.driver_connection.iterdump())==before


def test_bad_latest_is_not_replaced_with_older_good(sessions,seeded):
    levels(sessions,seeded[0],[(95,75,0),(91,30,0x80000000)])
    with sessions() as db:
        c=shared_snapshot(db)['cells']['level']
        assert c['value'] is None and '质量异常' in c['reason']
        assert c['sample']['received_at']==iso_time(HOUR-timedelta(seconds=91))


def test_old_point_definition_and_manual_default_cannot_supply_level(sessions,seeded):
    levels(sessions,seeded[0],[(90,75,0)])
    with sessions.begin() as db:
        row=db.scalar(select(TelemetryReading).where(TelemetryReading.point_key=='point2',
            TelemetryReading.received_at <= iso_time(HOUR-timedelta(seconds=90))))
        row.sample={**row.sample,'definition_id':'old-definition'}
    with sessions() as db:
        assert shared_snapshot(db)['cells']['level']['value'] is None
    with sessions.begin() as db:
        db.add(DecisionSourceRevision(effective_at=iso_time(HOUR-timedelta(days=1)),
            config=dict(choices={'tail-level':''},target_min=8.5,target_max=9.0)))
    with sessions() as db:
        assert shared_snapshot(db)['cells']['level']['value'] is None  # No implicit 55% fallback.


def test_decision_and_latest_cache_cannot_replace_missing_raw_history(sessions,seeded):
    cfg=seeded[0]
    at=HOUR-timedelta(seconds=91)
    with sessions.begin() as db:
        snap=shared_snapshot(db)
        record(db,HOUR,snap)
        # Simulate a legacy inline decision; it remains readable but is not raw history.
        row=db.scalar(select(DecisionCycleSample))
        row.sampled_at=iso_time(at)
        row.snapshot=dict(cells={'level':dict(snap['level_observation'],value=65)},
            level_observation=dict(snap['level_observation'],value=65))
        point=db.get(TelemetryPoint,(cfg.source_id,'point2'))
        point.latest=normalize(cfg.points[2],75,0,at,at,at)
    levels(sessions,cfg,[])
    with sessions() as db:
        before=deepcopy(db.scalar(select(DecisionCycleSample)).snapshot)
        assert shared_snapshot(db)['cells']['level']['value'] is None
        assert db.scalar(select(DecisionCycleSample)).snapshot==before


def test_level_hour_uses_decision_time_and_deduplicates_raw_event(sessions,seeded):
    start=HOUR-timedelta(hours=1)
    def row(at,observed,value):
        c=dict(value=value,status='good',source='KEPServer',source_id='kep',point_key='level',
               observed_at=iso_time(observed),alignment=POLICY)
        return SimpleNamespace(sampled_at=iso_time(at),snapshot={'cells':{'level':c}})
    first_at=start-timedelta(seconds=91)
    rows=[row(start,first_at,75),row(start+timedelta(seconds=5),first_at,75),
          row(HOUR-timedelta(seconds=1),HOUR-timedelta(seconds=91),25)]
    result=level_mean(rows,start,HOUR,mean_cell)
    assert result['value']==50 and result['sample_count']==2
    assert result['first_at']==iso_time(first_at)
    rows[-1].snapshot['cells']['level'].pop('alignment')
    assert level_mean(rows,start,HOUR,mean_cell)['value'] is None


@pytest.mark.parametrize('field,new_value',[('source_id','other'),('point_key','other'),('definition_id','changed')])
def test_level_hour_rejects_mixed_point_identity(sessions,seeded,field,new_value):
    start=HOUR-timedelta(hours=1)
    c=dict(value=50,status='good',source_id='kep',point_key='level',definition_id='original',
           observed_at=iso_time(start),alignment=POLICY)
    rows=[SimpleNamespace(sampled_at=iso_time(start+timedelta(seconds=90)),snapshot={'cells':{'level':c}}),
          SimpleNamespace(sampled_at=iso_time(start+timedelta(seconds=95)),snapshot={'cells':{'level':dict(c,**{field:new_value})}})]
    assert '定义变化' in level_mean(rows,start,HOUR,mean_cell)['reason']


def test_hour_summary_does_not_mix_unshifted_raw_level_and_context_changes(sessions,seeded):
    start=HOUR-timedelta(hours=1)
    with sessions.begin() as db:
        snap=shared_snapshot(db)
        old=deepcopy(snap)
        old.pop('input_timing')
        assert context_definition(old,load_store(db))!=context_definition(snap,load_store(db))
        snap['generated_at']=iso_time(start+timedelta(seconds=2))
        snap['cells']['level'].update(value=75,observed_at=iso_time(start-timedelta(seconds=90)))
        record(db,start,snap)
    with sessions() as db:
        result=build_hour(db,start,HOUR,HOUR)
        assert result['cells']['level']['value']==75  # raw telemetry still says 50.
        assert result['cells']['level']['sample_count']==1
