"""Minute archives coexist with unchanged five/30-second historical evidence."""
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.decision_hourly.evidence import summarize, weights
from app.decision_hourly.models import DecisionCycleSample
from app.decision_hourly.production_coverage import stopped_intervals, fields
from app.decision_hourly.quality_advice import density_mean
from app.decision_hourly.service import observation_tick, periodic_batch_ready, prepare_record, save_record
from app.decision_hourly.statistics import ARCHIVE_CADENCE, cadence_seconds, hour_advice, mean_cell
from app.telemetry.domain import iso_time, normalize
from app.telemetry.models import TelemetryReading
from test_decision_evidence import START
from test_decision_sources import seeded
from test_decision_storage_cadence import archived, raw_density
from test_hourly_brief import HOUR, sessions


def coverage(rows):
    return hour_advice(rows,START,HOUR,
        dict(value=1.47,status='good',observed_at=iso_time(HOUR-timedelta(seconds=1))))[1]


def test_default_is_sixty_seconds_without_reinterpreting_legacy_cadences():
    assert ARCHIVE_CADENCE == 60
    assert [cadence_seconds({'cadence_seconds':c}) for c in (5,30,60)] == [5,30,60]
    assert cadence_seconds({}) == 5
    assert cadence_seconds({'cadence_seconds':True}) == 5
    assert cadence_seconds({'cadence_seconds':600}) == 5


def test_sixty_samples_represent_full_hour_and_one_lost_batch_is_sixty_second_gap():
    rows = [archived(s,60) for s in range(0,3600,60)]
    result = summarize(rows,HOUR)
    assert result['total_ash']['sample_count'] == 60
    assert result['total_ash']['valid_seconds'] == 3600
    assert result['total_ash']['mass_t'] == pytest.approx(400)
    assert len(result['segments']) == 1
    assert coverage(rows)['ratio'] == 1
    missing = [r for r in rows if r.sampled_at != iso_time(START+timedelta(seconds=900))]
    result = summarize(missing,HOUR)['total_ash']
    assert result['valid_seconds'] == 3540
    assert coverage(missing)['ratio'] == pytest.approx(3540/3600)
    assert coverage(missing)['longest_gap_seconds'] == 60


def test_mixed_five_thirty_sixty_periods_use_each_actual_duration_and_preserve_rows():
    rows = [archived(s,5,ash=6) for s in range(0,1200,5)]
    rows += [archived(s,30,ash=9) for s in range(1200,2400,30)]
    rows += [archived(s,60,ash=12) for s in range(2400,3600,60)]
    for r in rows:
        r.snapshot['suggested_density']['value'] = 1.47+r.snapshot['total_ash']['value']/300
    before = deepcopy([vars(r) for r in rows])
    result = summarize(rows,HOUR)
    assert result['total_ash']['value'] == pytest.approx(9)
    assert result['total_ash']['valid_seconds'] == 3600
    assert result['total_ash']['mass_t'] == pytest.approx(400)
    assert coverage(rows)['storage_cadences_seconds'] == [5,30,60]
    assert coverage(rows)['ratio'] == 1
    assert before == [vars(r) for r in rows]


def test_explicit_thirty_second_weighting_remains_after_default_changes_to_sixty():
    a = dict(value=8,status='good',observed_at=iso_time(START),cadence_seconds=5)
    b = dict(value=10,status='good',observed_at=iso_time(START+timedelta(seconds=5)),cadence_seconds=30)
    assert mean_cell('raw_ash',[a,b],{},START,HOUR)['value'] == pytest.approx((8*5+10*30)/35)
    c = dict(value=12,status='good',observed_at=iso_time(START+timedelta(seconds=35)),cadence_seconds=60)
    assert mean_cell('raw_ash',[a,b,c],{},START,HOUR)['value'] == pytest.approx((8*5+10*30+12*60)/95)


def test_minute_period_clips_at_hour_end_and_never_reuses_previous_hour_flow():
    before = archived(3590,60)
    result = summarize([before],HOUR)['total_ash']
    assert result['valid_seconds'] == 10
    after = deepcopy(before)
    after.slot = int(HOUR.timestamp())//5
    after.sampled_at = iso_time(HOUR)
    result = summarize([after],HOUR+timedelta(hours=1))['total_ash']
    assert result['valid_seconds'] == 0
    assert result['excluded_reasons'] == {'跨小时旧皮带批次':1}


@pytest.mark.parametrize('lag',[0,90])
def test_bad_quality_event_truncates_minute_period_on_the_input_clock(sessions,seeded,lag):
    cfg,_,_ = seeded
    r = archived(0,60)
    p = cfg.points[3]
    r.snapshot['quality_sources'] = [dict(source_id=cfg.source_id,point_key=p.key,lag_seconds=lag)]
    when = START+timedelta(seconds=25-lag)
    sample = normalize(p,50,0x80000000,when,when,when)
    sample.update(cadence_seconds=5,storage_kind='quality-event')
    with sessions.begin() as db:
        db.add(TelemetryReading(source_id=cfg.source_id,point_key=p.key,batch_id='quality-limit',
            received_at=iso_time(when),sample=sample))
    with sessions() as db:
        assert weights([r],HOUR,db=db)[r.slot]['seconds'] == 25


def test_raw_minute_density_and_zero_production_preserve_real_gap(sessions,seeded,monkeypatch):
    cfg,_,_ = seeded
    with sessions.begin() as db:
        raw_density(db,cfg,0,1.48,5)
        raw_density(db,cfg,5,1.50,30)
        raw_density(db,cfg,35,1.52,60)
        for sec,cadence in [(0,5),(5,30),(35,60)]:
            when = START+timedelta(seconds=sec)
            for idx in (1,5,6):
                p = cfg.points[idx]
                sample = normalize(p,0,0,when,when,when)
                sample['cadence_seconds'] = cadence
                db.add(TelemetryReading(source_id=cfg.source_id,point_key=p.key,batch_id=str(sec),
                    received_at=iso_time(when),sample=sample))
    end = START+timedelta(seconds=155)
    with sessions() as db:
        stopped = fields(START,end,stopped_intervals(db,START,end),0)
        assert stopped['stopped_seconds'] == 95
        assert stopped['expected_production_seconds'] == 60
        # Exclude stop evidence from the density test to assess its raw intervals.
        with monkeypatch.context() as context:
            context.setattr('app.decision_hourly.quality_advice.stopped_intervals',lambda *a:[])
            density = density_mean(db,START,end,
                dict(resolved_choices={'density-current':'["kepserver-main","point4"]'}))
        assert density['valid_seconds'] == 95
        assert density['coverage'] == pytest.approx(95/155)
        assert density['value'] == pytest.approx((1.48*5+1.50*30+1.52*60)/95)


def test_minute_archive_waits_for_real_minute_batch_and_saves_once_per_minute(sessions,seeded,monkeypatch):
    cfg,_,_ = seeded
    now = START+timedelta(seconds=5.1)
    p = cfg.points[0]
    with sessions.begin() as db:
        for second,cadence,runtime in [(1,30,False),(2,5,True)]:
            when = START+timedelta(seconds=second)
            sample = normalize(p,40,0,when,when,when)
            sample.update(cadence_seconds=cadence,storage_kind='periodic',runtime_only=runtime)
            db.add(TelemetryReading(source_id=cfg.source_id,point_key=p.key,batch_id=str(second),
                received_at=iso_time(when),sample=sample))
    with sessions() as db:
        assert not periodic_batch_ready(db,now)
    assert observation_tick(now,sessions,cadence_seconds=60) is None
    when = START+timedelta(seconds=3)
    sample = normalize(p,40,0,when,when,when)
    sample.update(cadence_seconds=60,storage_kind='periodic')
    with sessions.begin() as db:
        db.add(TelemetryReading(source_id=cfg.source_id,point_key=p.key,batch_id='minute',
            received_at=iso_time(when),sample=sample))
    monkeypatch.setattr('app.services.decision_sources.utc_now',lambda:now)
    assert observation_tick(now,sessions,cadence_seconds=60) == now
    assert observation_tick(now+timedelta(seconds=30),sessions,cadence_seconds=60) is None
    with sessions() as db:
        rows = list(db.scalars(select(DecisionCycleSample)))
        assert len(rows) == 1
        assert rows[0].slot == int(now.timestamp())//5
        assert rows[0].snapshot['cadence_seconds'] == 60


def test_write_time_minute_bucket_rejects_prepared_duplicate_with_different_five_second_slot(sessions,seeded):
    from app.services.decision_sources import shared_snapshot
    from app.services.state import load_store
    with sessions() as db:
        store = load_store(db)
        result = shared_snapshot(db,START)
        first = prepare_record(db,START,result,store,cadence_seconds=60)
        later = deepcopy(result)
        later['generated_at'] = iso_time(START+timedelta(seconds=5))
        second = prepare_record(db,START+timedelta(seconds=5),later,store,cadence_seconds=60)
    with sessions.begin() as db:
        assert save_record(db,first)
    with sessions.begin() as db:
        assert not save_record(db,second)
