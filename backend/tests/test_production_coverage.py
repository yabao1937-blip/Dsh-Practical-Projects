"""Zero production is evidence, while absent or ambiguous flow remains a gap."""
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import delete

from app.decision_hourly.production_coverage import stopped_intervals, fields
from app.decision_hourly.ash_settlement import contribution_item, LEGACY
from app.decision_hourly.evidence import MASS
from app.decision_hourly.quality_advice import density_mean
from app.telemetry.domain import iso_time, normalize
from app.telemetry.models import TelemetryReading
from test_hourly_brief import HOUR, sessions
from test_decision_sources import seeded

START=HOUR-timedelta(hours=1)
END=START+timedelta(seconds=20)


def raw(db,cfg,sec,values=(0,0,0),codes=None,batches=None,duplicate=False):
    for index,value,code,batch in zip((1,5,6), values,codes or (0,0,0),batches or (str(sec),)*3):
        p=cfg.points[index]
        when=START+timedelta(seconds=sec)
        sample=normalize(p,value,code,when,when,when)
        db.add(TelemetryReading(source_id=cfg.source_id,point_key=p.key,
            batch_id=batch+('-dup' if duplicate else ''),received_at=iso_time(when),sample=sample))
    db.flush()


def test_half_production_half_stopped_preserves_mass_and_ash(sessions,seeded):
    cfg,_,_=seeded
    with sessions.begin() as db:
        raw(db,cfg,10);raw(db,cfg,15)
    with sessions() as db:
        stopped=stopped_intervals(db,START,END)
        c=[dict(sampled_at=iso_time(START+timedelta(seconds=s)),seconds=5,
                aggregation=MASS,mass_t=1,ash_mass_t=.09) for s in (0,5)]
        item=contribution_item(c,START,END,stopped)
        assert item['value']==9 and item['mass_t']==2
        assert item['valid_seconds']==10 and item['stopped_seconds']==10
        assert item['coverage']==1 and item['wall_coverage']==.5
        assert item['expected_production_seconds']==10


@pytest.mark.parametrize('case',['output','bad','different-batch','missing','duplicate','boolean','wrong-definition'])
def test_unconfirmed_stop_remains_in_denominator(sessions,seeded,case):
    cfg,_,_=seeded
    with sessions.begin() as db:
        raw(db,cfg,0,values=(0,1,0) if case=='output' else (0,0,0),
            codes=(0,0x80000000,0) if case=='bad' else None,
            batches=('a','b','a') if case=='different-batch' else None)
        if case=='missing':
            db.execute(delete(TelemetryReading).where(TelemetryReading.point_key==cfg.points[5].key,
                TelemetryReading.received_at==iso_time(START)))
        if case=='duplicate': raw(db,cfg,0,values=(0,1,0),duplicate=True)
        if case in ('boolean','wrong-definition'):
            from sqlalchemy import select
            r=db.scalar(select(TelemetryReading).where(TelemetryReading.point_key==cfg.points[1].key,
                TelemetryReading.received_at==iso_time(START)))
            r.sample={**r.sample, **({'value':False} if case=='boolean' else {'definition_id':'different'})}
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(r,'sample')  # Python dict equality treats False == 0.
    with sessions() as db:
        assert stopped_intervals(db,START,END)==[]


def test_no_bridge_over_outage_and_bad_next_batch_truncates(sessions,seeded):
    cfg,_,_=seeded
    with sessions.begin() as db:
        raw(db,cfg,0)
        assert fields(START,HOUR,stopped_intervals(db,START,HOUR),0)['stopped_seconds']==5
        raw(db,cfg,2,codes=(0x80000000,0,0))
        assert fields(START,HOUR,stopped_intervals(db,START,HOUR),0)['stopped_seconds']==2


def test_full_stop_has_no_coverage_or_ash(sessions,seeded):
    cfg,_,_=seeded
    with sessions.begin() as db:
        for s in (0,5,10,15):raw(db,cfg,s)
    with sessions() as db:
        r=contribution_item([],START,END,stopped_intervals(db,START,END))
        assert r['expected_production_seconds']==0 and r['coverage']==0
        assert r['value'] is None


def test_density_mean_excludes_stopped_period_and_minutes_only_deduct_accepted(sessions,seeded):
    cfg,_,_=seeded
    with sessions.begin() as db:
        raw(db,cfg,10);raw(db,cfg,15)
        for s,v in [(0,1.48),(5,1.48),(10,1.55),(15,1.55)]:
            p=cfg.points[4];when=START+timedelta(seconds=s)
            db.add(TelemetryReading(source_id=cfg.source_id,point_key=p.key,batch_id=str(s),
                received_at=iso_time(when),sample=normalize(p,v,0,when,when,when)))
    with sessions() as db:
        r=density_mean(db,START,END,dict(resolved_choices={'density-current':'["kepserver-main","point4"]'}))
        assert r['value']==1.48 and r['coverage']==1 and r['valid_seconds']==10
    c=[dict(sampled_at=iso_time(START+timedelta(seconds=s)),seconds=5,aggregation=LEGACY,value=9) for s in (59,70)]
    start,end=START+timedelta(minutes=1),START+timedelta(minutes=2)
    r=contribution_item(c,start,end,[[start.timestamp(),start.timestamp()+4]])
    assert r['valid_seconds']==5


def test_late_assay_does_not_reinterpret_saved_production_evidence(sessions,seeded):
    from app.decision_hourly.ash_settlement import build_settlement
    from app.decision_hourly.models import DecisionAshSettlement
    from app.telemetry.models import TelemetryPoint
    cfg,_,_=seeded
    with sessions.begin() as db:
        raw(db,cfg,0)
        first=build_settlement(db,START,HOUR+timedelta(hours=2))
        db.add(DecisionAshSettlement(start=iso_time(START),end=iso_time(HOUR),deadline=iso_time(HOUR),
            generated_at=iso_time(HOUR),snapshot=first))
        db.flush()
        db.get(TelemetryPoint,(cfg.source_id,cfg.points[1].key)).enabled=False
        db.flush()
        second=build_settlement(db,START,HOUR+timedelta(hours=3))
        assert second['stopped_intervals']==first['stopped_intervals']
        assert second['item']['stopped_seconds']==5
