"""Natural-hour windows, real events, forward archives and no repeated control accumulation."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, delete

from app.decision_hourly.models import DecisionCycleSample, DecisionCycleHour, DecisionCycleContext
from app.decision_hourly.service import build_hour, record, read_hour, tick, window, context_definition
from app.decision_hourly.statistics import hour_advice, mean_cell, EXPECTED, refresh_coverage_policy, digest
from app.decision_hourly.worker import next_delay
from app.services.decision_sources import shared_snapshot
from app.telemetry.domain import iso_time
from app.telemetry.models import TelemetryPoint, TelemetryReading
from app.models import AutoState
from app.services.state import load_store
from test_hourly_brief import sessions, HOUR, add_manual, send
from test_decision_sources import seeded, client

START = HOUR-timedelta(hours=1)


def reset_readings(sessions,cfg):
    cfg.history_interval_seconds = 5
    with sessions.begin() as db:
        db.execute(delete(TelemetryReading))
        for p in db.scalars(select(TelemetryPoint)):
            p.latest=None
            p.last_history_at=None


def fake_rows(deltas=None, omit=(), same_event=False):
    result = []
    for i in range(EXPECTED):
        if i in omit:
            continue
        density = 1.45 if i<600 else 1.5
        delta = (deltas or [.02])[i % len(deltas or [.02])]
        result.append(SimpleNamespace(slot=int(START.timestamp())//5+i,context_key='one',
            fingerprint='same' if same_event else str(i),snapshot=dict(
                suggested_density=dict(value=density+delta if delta is not None else None,status='calculated'),
                cells={'density':dict(value=density,status='good')})))
    return result


def advice(rows, basis=None):
    return hour_advice(rows,START,HOUR,basis or dict(value=1.5,status='good',observed_at=iso_time(HOUR-timedelta(seconds=2))))


def test_signed_mean_is_not_sum_or_absolute_recommendation():
    output,coverage,guide = advice(fake_rows([.02,-.01,0]))
    assert output['value'] == 1.503
    assert guide['mean_delta'] == pytest.approx(.01/3)
    assert coverage['valid_count'] == 720
    assert [coverage[k] for k in ('up','down','stable')] == [240,240,240]
    assert advice(fake_rows([.02]))[0]['value'] == 1.52
    assert advice(fake_rows([.05]))[0]['value'] == 1.53
    assert advice(fake_rows([-.05]))[0]['value'] == 1.47
    assert advice(fake_rows([.02,-.02]))[0]['value'] == 1.5


@pytest.mark.parametrize('omitted,allowed', [(range(0,720,10),False), (list(range(0,720,10))+[1],False),
    (range(12),True),(range(13),True),(range(708,720),True),(range(707,720),True),
    (range(71),True),(range(72),False),(range(73),False),(range(360),False)])
def test_coverage_requires_over_90_percent_without_continuous_gap_limit(omitted,allowed):
    output,coverage,_ = advice(fake_rows(omit=omitted))
    assert (output['value'] is not None) == allowed
    assert coverage['max_gap_seconds'] is None
    assert coverage['required_ratio_exclusive']
    if len(omitted)==71:
        assert coverage['longest_gap_seconds']==355
        assert coverage['valid_count']==649


def test_duplicate_reading_does_not_manufacture_hour_coverage():
    output,coverage,_ = advice(fake_rows(same_event=True))
    assert output['value'] is None and coverage['valid_count']==1


def legacy_hour(omit=71, other_reason=''):
    basis=dict(value=1.5,status='good',observed_at=iso_time(HOUR-timedelta(seconds=2)))
    output,coverage,guide=advice(fake_rows(omit=range(omit)),basis)
    coverage.pop('policy'); coverage.pop('required_ratio_exclusive'); coverage['max_gap_seconds']=60
    old_reason=(f'有效建议覆盖 {720-omit}/720（{(720-omit)/720:.1%}），连续缺口 {omit*5} 秒；'
        '需覆盖至少 90% 且缺口不超过 60 秒')
    guide.update(reason=old_reason+('；'+other_reason if other_reason else ''),rhoNew=None)
    output.update(value=None,status='missing',reason=guide['reason']+'；专家经验建议')
    return dict(period={'start':iso_time(START),'end':iso_time(HOUR)},coverage=coverage,
        guidance=guide,suggested_density=output,cells={'suggested_density':output},density_basis=basis,
        corrections=[{'version':'previous-correction'}])


def test_legacy_gate_is_rechecked_without_replaying_or_mutating_frozen_records():
    saved=legacy_hour()
    response=deepcopy(saved)
    refresh_coverage_policy(response)
    assert response['suggested_density']['value']==1.52
    assert response['coverage']['longest_gap_seconds']==355
    assert response['coverage']['max_gap_seconds'] is None
    assert response['corrections']==saved['corrections']
    assert saved['suggested_density']['value'] is None
    assert saved['coverage']['max_gap_seconds']==60
    assert response['guidance']['mean_delta']==saved['guidance']['mean_delta']
    assert response['guidance']['deltaRho']==saved['guidance']['deltaRho']
    again=deepcopy(response)
    refresh_coverage_policy(response)
    assert response==again


@pytest.mark.parametrize('reason',['小时内数据来源、模型或调密参数发生变化，暂不合并建议',
                                 '小时末 30 秒内没有有效实测密度','未知保护条件'])
def test_policy_refresh_cannot_remove_other_blockers(reason):
    result=legacy_hour(other_reason=reason)
    refresh_coverage_policy(result)
    assert result['suggested_density']['value'] is None
    assert reason in result['suggested_density']['reason']


def test_policy_refresh_requires_more_than_90_and_valid_frozen_basis():
    result=legacy_hour(omit=72)
    result['suggested_density'].update(value=1.52,status='calculated',reason='previously valid')
    result['guidance'].update(reason='previously valid',rhoNew=1.52)
    refresh_coverage_policy(result)
    assert result['suggested_density']['value'] is None
    assert '需超过 90%' in result['suggested_density']['reason']
    result=legacy_hour()
    result['density_basis']['value']=1
    refresh_coverage_policy(result)
    assert result['suggested_density']['value'] is None


def test_new_policy_does_not_resurrect_old_offsets_without_its_two_inputs(sessions,seeded):
    with sessions.begin() as db:
        current=shared_snapshot(db)
        summary=legacy_hour()
        summary.update(sources=current['sources'],target=current['target'])
        summary['context_key']=digest(context_definition(current,load_store(db)))
        db.add(DecisionCycleHour(start=iso_time(START),end=iso_time(HOUR),generated_at=iso_time(HOUR),snapshot=summary))
    with sessions() as db:
        response=read_hour(db,HOUR,live=current)
        assert response['suggested_density']['value'] is None
        assert response['advice_policy']=='gpt-hourly-quality-density-v1'
        assert 'ash' in response['advice_blockers']
        stopped=deepcopy(current)
        stopped['inputs']['is_stoppage']=dict(value=1,status='calculated')
        assert '设备停机' in read_hour(db,HOUR,live=stopped)['suggested_density']['reason']
        assert db.get(DecisionCycleHour,iso_time(START)).snapshot==summary


def test_context_change_bad_last_density_and_missing_suggestions_withhold():
    rows = fake_rows()
    rows[20].context_key = 'different-meter'
    assert '变化' in advice(rows)[0]['reason']
    assert advice(fake_rows(),dict(value=1,status='good',observed_at=iso_time(HOUR-timedelta(seconds=2))))[0]['value'] is None
    assert advice(fake_rows(),dict(value=1.5,status='good',observed_at=iso_time(HOUR-timedelta(seconds=31))))[0]['value'] is None
    rows = fake_rows()
    for row in rows[:73]:
        row.snapshot['suggested_density']['value'] = None
    assert advice(rows)[1]['valid_count']==647
    assert advice(rows)[0]['value'] is None


def test_manual_event_mean_instead_of_poll_weight_and_carry():
    a=dict(value=8,status='manual',observed_at=iso_time(START+timedelta(minutes=1)),record_id=1)
    b=dict(value=10,status='manual',observed_at=iso_time(START+timedelta(minutes=59)),record_id=2)
    output=mean_cell('heavy_ash',[a]*700+[b]*20,b,START,HOUR)
    assert output['value']==9 and output['sample_count']==2
    old=dict(a,value=8.5,observed_at=iso_time(START-timedelta(minutes=5)))
    output=mean_cell('heavy_ash',[old]*720,old,START,HOUR)
    assert output['value']==8.5 and output['carried'] and output['sample_count']==0
    assert output['observed_at']==old['observed_at']


def test_end_boundary_and_held_measurements_are_excluded():
    samples=[dict(value=v,status=status,observed_at=iso_time(at)) for v,status,at in [
        (30,'good',START),(40,'good',START+timedelta(minutes=1)),(50,'good',HOUR-timedelta(seconds=1)),
        (99,'good',HOUR),(0,'good',HOUR-timedelta(minutes=2)),(40,'held',HOUR-timedelta(minutes=3))]]
    output=mean_cell('raw_ash',samples,{},START,HOUR)
    assert output['value']==40 and output['sample_count']==3 and output['excluded_count']==2
    assert window(HOUR+timedelta(minutes=24))==(START,HOUR)
    assert window(HOUR)==(START,HOUR)


def test_raw_history_mean_manual_samples_and_no_fabricated_past_model(sessions,seeded):
    cfg,repo,values=seeded
    reset_readings(sessions,cfg)
    for seconds,ash in [(55,30),(30,40),(2,50)]:
        send(repo,cfg,HOUR-timedelta(seconds=seconds),[ash,*values[1:]])
    with sessions.begin() as db:
        add_manual(db,9.5,HOUR-timedelta(seconds=20),HOUR-timedelta(seconds=19),'heavy_clean_coal_ash')
        add_manual(db,99,HOUR-timedelta(seconds=10),HOUR+timedelta(seconds=1),'heavy_clean_coal_ash')
    with sessions() as db:
        result=build_hour(db,START,HOUR,HOUR)
        assert result['cells']['raw_ash']['value']==40
        assert result['cells']['heavy_ash']['value']==9
        assert result['cells']['heavy_ash']['sample_count']==2
        assert result['coarse_ash']['value'] is None
        assert result['suggested_density']['value'] is None
        assert result['coverage']['recorded_count']==0


def test_archived_results_mean_dedup_state_and_freeze(sessions,seeded,monkeypatch):
    cfg,repo,values=seeded
    reset_readings(sessions,cfg)
    # Each event is both in raw telemetry and in a decision; it must count only once.
    for seconds,ash,active in [(55,30,True),(30,40,False),(2,50,True)]:
        when=HOUR-timedelta(seconds=seconds)
        values[0],values[7]=ash,active
        send(repo,cfg,when,values)
        monkeypatch.setattr('app.services.decision_sources.utc_now',lambda:when)
        with sessions.begin() as db:
            snap=shared_snapshot(db)
            snap['cells']['coarse_ash']=dict(value=ash/2,status='predicted',unit='%')
            snap['coarse_ash']=snap['cells']['coarse_ash']
            assert record(db,when,snap)
            db.flush()
            assert not record(db,when,snap)
    with sessions() as db:
        result=build_hour(db,START,HOUR,HOUR)
        assert result['cells']['raw_ash']['value']==40
        assert result['cells']['raw_ash']['sample_count']==3
        assert result['coarse_ash']['value']==20
        assert result['cells']['systems']['components']['401']['value']==pytest.approx(2/3)
        assert result['cells']['heavy_ash']['sample_count']==1
    monkeypatch.setattr('app.services.decision_sources.utc_now',lambda:HOUR)
    tick(HOUR,sessions)
    with sessions() as db:
        saved=deepcopy(db.get(DecisionCycleHour,iso_time(START)).snapshot)
        assert len(list(db.scalars(select(DecisionCycleSample))))==4
    with sessions.begin() as db:
        add_manual(db,88,HOUR-timedelta(seconds=1),HOUR+timedelta(seconds=1),'heavy_clean_coal_ash')
    tick(HOUR+timedelta(seconds=1),sessions)
    with sessions() as db:
        assert db.get(DecisionCycleHour,iso_time(START)).snapshot==saved
        response=read_hour(db,HOUR)
        assert {k:v for k,v in response['cells'].items() if k not in ('density','suggested_density')} == {
            k:v for k,v in saved['cells'].items() if k not in ('density','suggested_density')}
        assert response['cells']['density']['value']==pytest.approx(saved['cells']['density']['value'])


def test_hourly_endpoint_is_read_only(sessions,seeded,client,monkeypatch):
    monkeypatch.setattr('app.decision_hourly.service.utc_now',lambda:HOUR)
    monkeypatch.setattr('app.decision_hourly.service.build_hour',lambda *args:pytest.fail('Polling launched an hour calculation'))
    response=client.get('/api/v1/decision/hourly')
    assert response.status_code==200,response.text
    assert response.json()['status']=='pending'
    assert response.json()['period']['end']==iso_time(HOUR)
    with sessions() as db:
        assert db.scalar(select(DecisionCycleSample)) is None
        assert db.scalar(select(DecisionCycleHour)) is None


def test_actual_snapshot_timestamp_wins_over_pre_lock_clock(sessions,seeded):
    with sessions.begin() as db:
        current=shared_snapshot(db)
        current['generated_at']=iso_time(HOUR+timedelta(seconds=1))
        record(db,HOUR-timedelta(seconds=1),current)
    with sessions() as db:
        row=db.scalar(select(DecisionCycleSample))
        assert row.slot==int(HOUR.timestamp())//5
        assert row.sampled_at==current['generated_at']


def test_definition_changes_invalidate_context_and_current_guard(sessions,seeded):
    with sessions.begin() as db:
        current=shared_snapshot(db)
        original=context_definition(current,load_store(db))
        changed=deepcopy(current)
        changed['cells']['belt501']['sample']['definition_id']='new-node-or-scale'
        assert context_definition(changed,load_store(db))!=original
        record(db,HOUR,current)
        db.flush()
        row=db.scalar(select(DecisionCycleSample))
        summary=build_hour(db,START,HOUR,HOUR)
        summary['context_key']=row.context_key
        summary['suggested_density']=dict(value=1.5,status='calculated',reason='test')
        summary['cells']['suggested_density']=summary['suggested_density']
        db.add(DecisionCycleHour(start=iso_time(START),end=iso_time(HOUR),generated_at=iso_time(HOUR),snapshot=summary))
        db.add(DecisionCycleHour(start=iso_time(START-timedelta(hours=1)),end=iso_time(START),
                                 generated_at=iso_time(START),snapshot=deepcopy(summary)))
    with sessions() as db:
        assert read_hour(db,HOUR,live=current)['suggested_density']['value'] is None  # No new ash coverage.
        assert '变化' in read_hour(db,HOUR,live=changed)['suggested_density']['reason']
        stopped=deepcopy(current)
        stopped['inputs']['is_stoppage']=dict(value=1,status='calculated')
        assert '设备停机' in read_hour(db,HOUR,live=stopped)['suggested_density']['reason']
        assert db.get(DecisionCycleHour,iso_time(START)).snapshot['suggested_density']['value']==1.5


def test_retention_keeps_hour_and_model_context(sessions,seeded):
    old=HOUR-timedelta(days=9)
    with sessions.begin() as db:
        snap=shared_snapshot(db)
        snap['generated_at']=iso_time(old)
        record(db,old,snap)
        db.flush()
        old_sample=db.scalar(select(DecisionCycleSample))
        key=old_sample.context_key
        db.add(DecisionCycleHour(start=iso_time(old),end=iso_time(old+timedelta(hours=1)),
                                generated_at=iso_time(old+timedelta(hours=1)),snapshot={'context_key':key}))
    tick(HOUR,sessions)
    with sessions() as db:
        assert db.get(DecisionCycleSample,int(old.timestamp())//5) is None
        assert db.get(DecisionCycleContext,key) is not None
        assert db.get(DecisionCycleHour,iso_time(old)).snapshot=={'context_key':key}


def test_full_hour_integrates_saved_outputs_with_last_meter_basis(sessions,seeded):
    with sessions.begin() as db:
        template=shared_snapshot(db)
        store=load_store(db)
        for i in range(720):
            snap=deepcopy(template)
            stamp=iso_time(START+timedelta(seconds=5*i+1))
            snap['generated_at']=stamp
            for key,c in snap['cells'].items():
                if c.get('status')=='good' and not isinstance(c.get('value'),dict):
                    c['observed_at']=stamp
                    if c.get('sample'):
                        c['sample'].update(batch_id=str(i),received_at=stamp)
            for c in snap['inputs'].values():
                if c.get('status')=='good':
                    c['observed_at']=stamp
            snap['cells']['density']['value']=1.4 if i<719 else 1.5
            snap['inputs']['density']['value']=snap['cells']['density']['value']
            # Each new batch is real authority now, not a fabricated archive-only event.
            for c in snap['cells'].values():
                sample=c.get('sample') or {}
                if sample.get('batch_id')==str(i):
                    sample['value']=c['value']
                    sample['raw_value']=c['value']
                    db.add(TelemetryReading(source_id=c['source_id'],point_key=c['point_key'],
                        batch_id=str(i),received_at=stamp,sample=deepcopy(sample)))
            db.flush()
            snap['suggested_density']=dict(value=snap['cells']['density']['value']-.02,status='calculated',reason='test')
            snap['cells']['suggested_density']=snap['suggested_density']
            snap['cells']['total_ash']=dict(value=9 if i%2 else 10,status='calculated',unit='%')
            snap['total_ash']=snap['cells']['total_ash']
            record(db,START+timedelta(seconds=5*i+1),snap,store)
        db.flush()
        result=build_hour(db,START,HOUR,HOUR)
        assert result['coverage']['valid_count']==720
        # The raw meter at :58 is newer than the last archived decision at :56.
        assert result['suggested_density']['value']==1.46
        assert result['guidance']['rhoCur']==1.48
        assert result['cells']['density']['value']<1.401
        # Equal flow weighted by observed duration; final sample covers only four seconds.
        assert result['total_ash']['value']==pytest.approx((10*1800+9*1799)/3599)


def test_boolean_state_and_real_zero_are_distinct_from_numeric_missing():
    stamp=iso_time(START+timedelta(minutes=1))
    off=dict(value=False,status='good',observed_at=stamp)
    assert mean_cell('systems:401',[off],{},START,HOUR)['value']==0
    assert mean_cell('raw_ash',[off],{},START,HOUR)['value'] is None
    assert mean_cell('float_amount',[dict(off,value=0)],{},START,HOUR)['value']==0


@pytest.mark.parametrize('now',[100.01,104.99,105,109.991])
def test_windows_timer_early_wakeup_still_lands_in_next_slot(now):
    next_wakeup=now+next_delay(now)-.016
    assert int(next_wakeup)//5==int(now)//5+1
