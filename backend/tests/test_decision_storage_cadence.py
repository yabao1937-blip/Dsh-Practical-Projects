"""Thirty-second archive intervals coexist with immutable five-second evidence."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, event

from app.decision_hourly.evidence import summarize, weights
from app.decision_hourly.models import DecisionCycleSample
from app.decision_hourly.production_coverage import stopped_intervals, fields
from app.decision_hourly.quality_advice import density_mean
from app.decision_hourly.service import prepare_record, save_record, observation_tick
from app.decision_hourly.statistics import ARCHIVE_CADENCE, hour_advice, mean_cell, cadence_seconds
from app.decision_hourly.ash_settlement import settlement_snapshot
from app.telemetry.domain import iso_time, normalize
from app.telemetry.models import TelemetryReading
from test_decision_evidence import row, START
from test_decision_sources import seeded
from test_hourly_brief import HOUR, sessions


def archived(second, cadence=30, rate=400, ash=8):
    result = row(second//5,rate=rate,ash=ash)
    result.snapshot['cadence_seconds'] = cadence
    result.snapshot['suggested_density'] = dict(value=1.49,status='calculated')
    for key in ('belt501','belt502'):
        result.snapshot['cells'][key]['cadence_seconds'] = cadence
    return result


def test_complete_thirty_second_hour_has_full_mass_and_coverage():
    rows = [archived(i) for i in range(0,3600,30)]
    result = summarize(rows,HOUR)
    cell = result['total_ash']
    assert cell['valid_seconds'] == 3600
    assert cell['sample_count'] == 120
    assert cell['mass_t'] == pytest.approx(400)
    assert len(result['segments']) == 1
    _,coverage,_ = hour_advice(rows,START,HOUR,
        dict(value=1.47,status='good',observed_at=iso_time(HOUR-timedelta(seconds=1))))
    assert coverage['ratio'] == 1
    assert coverage['valid_seconds'] == 3600
    assert coverage['valid_sample_count'] == 120
    assert coverage['longest_gap_seconds'] == 0


def test_lost_thirty_second_batch_remains_thirty_second_hole():
    rows = [archived(i) for i in range(0,3600,30) if i != 900]
    result = summarize(rows,HOUR)['total_ash']
    assert result['valid_seconds'] == 3570
    _,coverage,_ = hour_advice(rows,START,HOUR,
        dict(value=1.47,status='good',observed_at=iso_time(HOUR-timedelta(seconds=1))))
    assert coverage['ratio'] == pytest.approx(3570/3600)
    assert coverage['longest_gap_seconds'] == 30


def test_mixed_five_and_thirty_second_hour_weights_actual_duration():
    rows = [archived(i,5,rate=400,ash=8) for i in range(0,1800,5)]
    rows += [archived(i,30,rate=400,ash=10) for i in range(1800,3600,30)]
    for r in rows[:360]:
        r.snapshot['suggested_density']['value'] = 1.48
    before = deepcopy([r.snapshot for r in rows])
    result = summarize(rows,HOUR)
    assert result['total_ash']['value'] == pytest.approx(9)
    assert result['total_ash']['valid_seconds'] == 3600
    assert len(result['segments']) == 1
    _,coverage,guide = hour_advice(rows,START,HOUR,
        dict(value=1.47,status='good',observed_at=iso_time(HOUR-timedelta(seconds=1))))
    assert coverage['ratio'] == 1
    assert guide['mean_delta'] == pytest.approx(.015)
    assert before == [r.snapshot for r in rows]


def test_legacy_metadata_never_infers_thirty_seconds_from_large_spacing():
    rows = [row(0),row(6)]
    result = summarize(rows,HOUR)['total_ash']
    assert result['valid_seconds'] == 10
    assert cadence_seconds({'cadence_seconds':True}) == 5
    assert cadence_seconds({'cadence_seconds':600}) == 5


def test_bad_event_truncates_prior_good_period_and_hour_end_clips():
    a,b,c = archived(0),archived(10,5),archived(3590)
    b.snapshot['cells']['belt501']['quality'] = 'bad'
    result = summarize([a,b,c],HOUR)['total_ash']
    assert result['valid_seconds'] == 20
    assert result['mass_t'] == pytest.approx(400*20/3600)
    assert settlement_snapshot(c.snapshot)['cadence_seconds'] == 30


def test_cell_means_weight_auto_events_by_cadence_but_not_manual_assays():
    a = dict(value=8,status='good',observed_at=iso_time(START),cadence_seconds=5)
    b = dict(value=10,status='good',observed_at=iso_time(START+timedelta(seconds=5)),cadence_seconds=30)
    result = mean_cell('raw_ash',[a,b],{},START,HOUR)
    assert result['value'] == pytest.approx((8*5+10*30)/35)
    result = mean_cell('heavy_ash',[dict(a,status='manual'),dict(b,status='manual')],{},START,HOUR)
    assert result['value'] == 9


def raw_density(db,cfg,sec,value,cadence):
    when = START+timedelta(seconds=sec)
    p = cfg.points[4]
    sample = normalize(p,value,0,when,when,when)
    sample['cadence_seconds'] = cadence
    db.add(TelemetryReading(source_id=cfg.source_id,point_key=p.key,batch_id=str(sec),
        received_at=iso_time(when),sample=sample))


def test_raw_density_weights_mixed_cadence_and_does_not_fill_missing_batch(sessions,seeded):
    cfg,_,_ = seeded
    with sessions.begin() as db:
        raw_density(db,cfg,0,1.48,5)
        raw_density(db,cfg,5,1.50,30)
        raw_density(db,cfg,65,1.50,30)
    with sessions() as db:
        result = density_mean(db,START,START+timedelta(seconds=95),
            dict(resolved_choices={'density-current':'["kepserver-main","point4"]'}))
        assert result['valid_seconds'] == 65
        assert result['coverage'] == pytest.approx(65/95)
        assert result['value'] == pytest.approx((1.48*5+1.5*60)/65)


def test_stop_intervals_accept_thirty_seconds_only_with_explicit_raw_metadata(sessions,seeded):
    cfg,_,_ = seeded
    with sessions.begin() as db:
        for sec,cadence in [(0,30),(60,5)]:
            when = START+timedelta(seconds=sec)
            for idx in (1,5,6):
                p = cfg.points[idx]
                sample = normalize(p,0,0,when,when,when)
                sample['cadence_seconds'] = cadence
                db.add(TelemetryReading(source_id=cfg.source_id,point_key=p.key,batch_id=str(sec),
                    received_at=iso_time(when),sample=sample))
    with sessions() as db:
        result = fields(START,START+timedelta(seconds=90),stopped_intervals(db,START,HOUR),0)
        assert result['stopped_seconds'] == 35


def test_storage_metadata_preserves_slot_and_historical_record(sessions,seeded):
    from app.services.decision_sources import shared_snapshot
    from app.services.state import load_store
    with sessions.begin() as db:
        result = shared_snapshot(db,START)
        old = prepare_record(db,START,result,load_store(db))
        assert old['snapshot']['cadence_seconds'] == 5
        assert save_record(db,old)
        now = START+timedelta(seconds=30)
        result['generated_at'] = iso_time(now)
        new = prepare_record(db,now,result,load_store(db),cadence_seconds=30)
        assert new['slot'] == int(now.timestamp())//5
        assert new['snapshot']['cadence_seconds'] == 30
        assert save_record(db,new)
    with sessions() as db:
        saved = list(db.scalars(select(DecisionCycleSample).order_by(DecisionCycleSample.slot)))
        assert [r.snapshot['cadence_seconds'] for r in saved] == [5,30]
        assert saved[0].slot == int(START.timestamp())//5


def test_bad_quality_event_inside_period_truncates_mass_with_one_batched_query(sessions,seeded):
    cfg,_,_ = seeded
    rows = [archived(i) for i in range(0,3600,30)]
    for r in rows:
        for key,index in [('belt501',5),('belt502',6)]:
            r.snapshot['cells'][key].update(source_id=cfg.source_id,point_key=cfg.points[index].key)
    p = cfg.points[5]
    when = START+timedelta(seconds=15)
    sample = normalize(p,200,0x80000000,when,when,when)
    sample.update(cadence_seconds=5,storage_kind='quality-event')
    with sessions.begin() as db:
        db.add(TelemetryReading(source_id=cfg.source_id,point_key=p.key,batch_id='bad-at-15',
            received_at=iso_time(when),sample=sample))
    with sessions() as db:
        statements = []
        def count(conn,cursor,statement,parameters,context,executemany):
            statements.append(statement)
        event.listen(db.bind,'before_cursor_execute',count)
        try:
            result = summarize(rows,HOUR,db=db)['total_ash']
        finally:
            event.remove(db.bind,'before_cursor_execute',count)
        assert result['valid_seconds'] == 3585
        assert len(statements) == 1


def test_lagged_quality_event_uses_level_clock_not_current_clock(sessions,seeded):
    cfg,_,_ = seeded
    r = archived(0)
    p = cfg.points[3]
    r.snapshot['quality_sources'] = [dict(source_id=cfg.source_id,point_key=p.key,lag_seconds=90)]
    when = START-timedelta(seconds=75)  # Affects predictions at START+15s.
    sample = normalize(p,50,0x80000000,when,when,when)
    sample.update(cadence_seconds=5,storage_kind='quality-event')
    with sessions.begin() as db:
        db.add(TelemetryReading(source_id=cfg.source_id,point_key=p.key,batch_id='lagged-bad',
            received_at=iso_time(when),sample=sample))
    with sessions() as db:
        assert weights([r],HOUR,db=db)[r.slot]['seconds'] == 15


def test_unrelated_quality_event_with_good_selected_point_does_not_cut_archive(sessions,seeded):
    cfg,_,_ = seeded
    r = archived(0)
    p = cfg.points[5]
    for key,index in [('belt501',5),('belt502',6)]:
        r.snapshot['cells'][key].update(source_id=cfg.source_id,point_key=cfg.points[index].key)
    when = START+timedelta(seconds=15)
    sample = normalize(p,200,0,when,when,when)
    sample.update(cadence_seconds=5,storage_kind='quality-event')
    with sessions.begin() as db:
        db.add(TelemetryReading(source_id=cfg.source_id,point_key=p.key,batch_id='other-point-went-bad',
            received_at=iso_time(when),sample=sample))
    with sessions() as db:
        assert weights([r],HOUR,db=db)[r.slot]['seconds'] == 30


def test_legacy_five_second_advice_keeps_arithmetic_mean_at_clipped_hour_end():
    a,b = row(0),row(719)
    a.snapshot['suggested_density'] = dict(value=1.48,status='calculated')
    b.snapshot['suggested_density'] = dict(value=1.50,status='calculated')
    b.sampled_at = iso_time(HOUR-timedelta(seconds=1))
    _,_,guide = hour_advice([a,b],START,HOUR,
        dict(value=1.47,status='good',observed_at=iso_time(HOUR-timedelta(seconds=1))))
    assert guide['mean_delta'] == pytest.approx(.02)


def test_archive_retries_after_durable_batch_phase_and_saves_once_per_bucket(sessions,seeded,monkeypatch):
    cfg,_,_ = seeded
    first = START+timedelta(seconds=.1)
    assert observation_tick(first,sessions,cadence_seconds=30) is None
    when = START+timedelta(seconds=1)
    p = cfg.points[0]
    sample = normalize(p,40,0,when,when,when)
    sample.update(cadence_seconds=30,storage_kind='periodic')
    with sessions.begin() as db:
        db.add(TelemetryReading(source_id=cfg.source_id,point_key=p.key,batch_id='periodic-new',
            received_at=iso_time(when),sample=sample))
    now = START+timedelta(seconds=5.1)
    monkeypatch.setattr('app.services.decision_sources.utc_now',lambda:now)
    assert observation_tick(now,sessions,cadence_seconds=30) == now
    assert observation_tick(now+timedelta(seconds=5),sessions,cadence_seconds=30) is None
    with sessions() as db:
        assert len(list(db.scalars(select(DecisionCycleSample)))) == 1


@pytest.mark.parametrize('size',[6,12])
def test_thirty_second_maintenance_visits_entire_backlog(monkeypatch,size):
    from app.decision_hourly import ash_settlement as settlement
    now = HOUR+timedelta(hours=3,minutes=10)
    starts = [iso_time(HOUR-timedelta(hours=12+i)) for i in range(size)]
    visited = set()
    class MetadataOnly:
        def execute(self,query):
            return SimpleNamespace(all=lambda:[(key,'generated') for key in starts])
        def scalars(self,query):
            return []
        def get(self,model,key):
            return None if key in starts else SimpleNamespace(snapshot={'revision':key})
    monkeypatch.setattr(settlement,'available_assays',lambda *a:[])
    monkeypatch.setattr(settlement,'manual_revision',lambda *a:'manual-revision')
    monkeypatch.setattr(settlement,'manual_basis',lambda db,start,*a:{'key':iso_time(start)})
    monkeypatch.setattr(settlement,'source_revision',lambda *a:'source')
    monkeypatch.setattr(settlement,'revision',lambda basis,source:basis['key'])
    def build(db,start,*args):
        visited.add(iso_time(start))
        return {}
    monkeypatch.setattr(settlement,'build_settlement',build)
    for step in range(size):
        settlement.prepare_due_hours(MetadataOnly(),now+timedelta(seconds=ARCHIVE_CADENCE*step))
    assert visited == set(starts)
