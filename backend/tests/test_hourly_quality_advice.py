"""Two independently timed inputs, authoritative coverage, revision and protection."""
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import delete, select

from app.decision_hourly.models import DecisionCycleContext, DecisionCycleHour
from app.decision_hourly.quality_advice import density_mean, recommendation
from app.decision_hourly.service import context_definition, read_hour
from app.decision_hourly.statistics import digest
from app.services.decision_sources import shared_snapshot
from app.services.state import load_store
from app.telemetry.domain import iso_time, normalize
from app.telemetry.models import TelemetryReading
from test_hourly_brief import HOUR, sessions
from test_decision_sources import seeded, client

START = HOUR-timedelta(hours=1)
ASH_START = START-timedelta(hours=1)


@pytest.fixture
def prepared(sessions, seeded, monkeypatch):
    cfg, _, _ = seeded
    with sessions.begin() as db:
        current = shared_snapshot(db)
        definition = context_definition(current, load_store(db))
        key = digest(definition)
        db.add(DecisionCycleContext(key=key, definition=definition))
        base = dict(policy='decision-hourly-v2', period=dict(start=iso_time(START), end=iso_time(HOUR),
            apply_start=iso_time(HOUR), apply_end=iso_time(HOUR+timedelta(hours=1)), label='09:00–10:00', apply_label='10:00–11:00'),
            sources=current['sources'], model=current['model'], target=current['target'], density_label='A304',
            context_key=key, evidence=dict(segments=[dict(state=dict(context_key=key))]),
            cells=dict(density=dict(value=1.48, status='average'), total_ash=dict(value=99, status='average'),
                suggested_density=dict(value=None, status='missing', reason='旧即时建议缺数')),
            total_ash=dict(value=99, status='average'), suggested_density=dict(value=None),
            coverage=dict(ratio=0), guidance=dict(mean_delta=.03), notes=[])
        for start in (ASH_START, START):
            db.add(DecisionCycleHour(start=iso_time(start), end=iso_time(start+timedelta(hours=1)),
                generated_at=iso_time(HOUR), snapshot=deepcopy(base)))
        db.execute(delete(TelemetryReading))
        point = cfg.points[4]
        for i in range(720):
            when = START+timedelta(seconds=i*5)
            sample = normalize(point, 1.47 if i<360 else 1.49, 0, when, when, when)
            db.add(TelemetryReading(source_id=cfg.source_id, point_key=point.key, batch_id=str(i),
                received_at=iso_time(when), sample=sample))
    settlement = dict(revision='first', heavy_ash=dict(value=8, records=[]),
        item=dict(value=9.3, coverage=1., valid_seconds=3600, sample_count=720, provisional=True,
                  aggregation='output-mass-v1', reason='按原时段实际产煤量加权'))
    def settled(db, start, now):
        assert start == ASH_START
        return deepcopy(settlement)
    monkeypatch.setattr('app.decision_hourly.quality_advice.get_settlement', settled)
    return current, settlement, base, cfg


@pytest.mark.parametrize('ash,expected', [(8.8,1.48),(8.5,1.48),(9,1.48),(9.05,1.48),
    (8.45,1.48),(9.3,1.46),(8.2,1.5),(20,1.45),(2,1.51)])
def test_range_deadband_direction_and_cap(ash, expected):
    g = recommendation(ash, 1.48, dict(min=8.5,max=9))
    assert g['rhoNew'] == expected
    assert abs(g['deltaRho']) <= .03


def test_prior_mean_not_last_reading_or_old_offset_and_is_read_only(sessions, prepared):
    current, _, base, _ = prepared
    current['inputs']['density']['value'] = 1.53
    current['suggested_density'] = dict(value=None, reason='即时粗精预测缺数')
    with sessions() as db:
        before = list(db.connection().connection.driver_connection.iterdump())
        r = read_hour(db, HOUR, current)
        assert r['suggested_density']['value'] == 1.46
        assert r['guidance']['rhoCur'] == pytest.approx(1.48)
        assert r['advice_inputs']['ash']['start'] == iso_time(ASH_START)
        assert r['advice_inputs']['density']['start'] == iso_time(START)
        assert r['advice_inputs']['ash']['provisional']
        assert r['cells']['total_ash']['value'] == 99  # Ordinary statistics keep their original hour.
        assert r['cells']['suggested_density'] == r['suggested_density']
        assert not r['advice_frozen'] and r['frozen']
        assert db.get(DecisionCycleHour, iso_time(START)).snapshot == base
        assert list(db.connection().connection.driver_connection.iterdump()) == before


def test_late_assay_revision_changes_advice_without_replaying_hour(sessions, prepared):
    current, settlement, _, _ = prepared
    with sessions() as db:
        assert read_hour(db, HOUR, current)['suggested_density']['value'] == 1.46
        settlement['item'].update(value=8.7, provisional=False)
        settlement['revision'] = 'corrected'
        current['inputs']['density']['observed_at'] = iso_time(HOUR+timedelta(minutes=20))
        r = read_hour(db, HOUR+timedelta(minutes=20), current)
        assert r['suggested_density']['value'] == 1.48
        assert r['advice_inputs']['ash']['revision'] == 'corrected'
        assert not r['advice_inputs']['ash']['provisional']


@pytest.mark.parametrize('which,ratio,allowed', [('ash',.9,False),('ash',.901,True),
    ('density',.9,False),('density',649/720,True)])
def test_each_input_requires_more_than_90_percent(sessions, prepared, which, ratio, allowed):
    current, settlement, _, _ = prepared
    if which == 'ash':
        settlement['item']['coverage'] = ratio
    else:
        with sessions.begin() as db:
            db.execute(delete(TelemetryReading).where(TelemetryReading.received_at >=
                iso_time(START+timedelta(seconds=round(ratio*720)*5))))
    with sessions() as db:
        r = read_hour(db, HOUR, current)
        assert (r['suggested_density']['value'] is not None) == allowed
        assert (which in r['advice_blockers']) == (not allowed)
        # A missing final five minutes does not create the old 30s/latest gate.
        assert '小时末' not in r['suggested_density']['reason']


def test_missing_latest_ash_does_not_borrow_older_hour(sessions, prepared):
    current, settlement, _, _ = prepared
    settlement['item'].update(value=None, coverage=0)
    with sessions() as db:
        r = read_hour(db, HOUR, current)
        assert r['suggested_density']['value'] is None
        assert r['advice_blockers'] == ['ash']


@pytest.mark.parametrize('change', ['source','model','target','parameter','definition','mid_hour'])
def test_real_configuration_changes_are_still_protected(sessions, prepared, change):
    current, _, base, _ = prepared
    with sessions.begin() as db:
        if change == 'mid_hour':
            altered = deepcopy(db.get(DecisionCycleContext, base['context_key']).definition)
            altered['density_guide']['maxStep'] = .01
            key = digest(altered)
            db.add(DecisionCycleContext(key=key, definition=altered))
            saved = db.get(DecisionCycleHour, iso_time(ASH_START))
            saved.snapshot = {**base,'evidence':dict(segments=[dict(state=dict(context_key=key)),
                                                         dict(state=dict(context_key=base['context_key']))])}
        elif change == 'source':
            current['sources']['resolved_choices']['density-current'] = '["other", "point"]'
        elif change == 'model':
            current['model']['version'] = 'different'
        elif change == 'target':
            current['target']['min'] = 8.6
        elif change == 'parameter':
            from app.models import Setting
            db.add(Setting(key='density_guide',value=dict(maxStep=.01)))
        else:
            current['cells']['belt501']['sample']['definition_id'] = 'changed'
    with sessions() as db:
        r = read_hour(db, HOUR, current)
        assert r['suggested_density']['value'] is None
        assert '变化' in r['suggested_density']['reason']


def test_missing_definition_only_does_not_invent_configuration_change(sessions, prepared):
    current, _, _, _ = prepared
    current['cells']['belt501']['sample'].pop('definition_id')
    with sessions() as db:
        assert read_hour(db,HOUR,current)['suggested_density']['value'] == 1.46


@pytest.mark.parametrize('cause', ['stop','density','hold','stale','unit'])
def test_current_protections_do_not_depend_on_instant_suggestion(sessions, prepared, cause):
    current, _, _, _ = prepared
    if cause == 'stop':
        current['inputs']['is_stoppage'] = dict(value=1,status='calculated')
    elif cause == 'density':
        current['inputs']['density'] = dict(value=1.48,status='held')
    elif cause == 'hold':
        current['guidance'] = dict(hold=True,reason='等待动作反馈')
    elif cause == 'stale':
        current['inputs']['density']['observed_at'] = iso_time(HOUR-timedelta(seconds=60))
    else:
        current['inputs']['density']['unit'] = 'wrong'
    with sessions() as db:
        assert read_hour(db,HOUR,current)['suggested_density']['value'] is None


def test_burst_samples_and_invalid_density_cannot_manufacture_coverage(sessions, prepared):
    current, _, _, cfg = prepared
    with sessions.begin() as db:
        db.execute(delete(TelemetryReading))
        for i in range(720):
            when = START+timedelta(milliseconds=i)
            sample = normalize(cfg.points[4], 1.48, 0, when, when, when)
            db.add(TelemetryReading(source_id=cfg.source_id,point_key=cfg.points[4].key,
                batch_id=str(i),received_at=iso_time(when),sample=sample))
    with sessions() as db:
        r = density_mean(db,START,HOUR,current['sources'])
        assert r['coverage'] < .002
        assert r['value'] == pytest.approx(1.48)


def test_bad_sample_truncates_coverage_and_placeholder_is_excluded(sessions, prepared):
    current, _, _, cfg = prepared
    with sessions.begin() as db:
        db.execute(delete(TelemetryReading))
        for seconds, value, code in [(0,1.48,0),(1,1.49,0x80000000),(5,1,0),(10,0,0),(15,1.5,0)]:
            when=START+timedelta(seconds=seconds)
            db.add(TelemetryReading(source_id=cfg.source_id,point_key=cfg.points[4].key,batch_id=str(seconds),
                received_at=iso_time(when),sample=normalize(cfg.points[4],value,code,when,when,when)))
    with sessions() as db:
        r=density_mean(db,START,HOUR,current['sources'])
        assert r['valid_seconds']==6 and r['sample_count']==2
        assert r['value']==pytest.approx(1.49)


def test_real_settlement_and_manual_revision_feed_hour_advice(sessions, prepared, monkeypatch):
    from app.decision_hourly.ash_settlement import get_settlement
    from app.decision_hourly.models import DecisionCycleSample
    from test_decision_evidence import row
    from test_ash_settlement import assay
    current, _, base, _ = prepared
    monkeypatch.setattr('app.decision_hourly.quality_advice.get_settlement',get_settlement)
    with sessions.begin() as db:
        assay(db,8,observed=HOUR-timedelta(minutes=1),created=HOUR-timedelta(seconds=30))
        for i in range(720):
            r=row(i)
            when=ASH_START+timedelta(seconds=5*i)
            stamp=iso_time(when)
            r.slot,r.sampled_at=int(when.timestamp())//5,stamp
            for c in (r.snapshot['cells']['belt501'],r.snapshot['cells']['belt502']):
                c.update(observed_at=stamp,received_at=stamp)
            r.snapshot['cells'].update(float_ash=dict(value=10,status='manual',unit='%'),
                float_amount=dict(value=40,status='manual'),coarse_amount=dict(value=20,status='calculated'))
            r.snapshot.update(heavy_amount=dict(value=340,status='calculated'),coarse_ash=dict(value=20,status='predicted'),
                              sources=dict(resolved_choices={'heavy-ash':None}))
            db.add(DecisionCycleSample(slot=r.slot,sampled_at=stamp,fingerprint=str(i),
                context_key=base['context_key'],snapshot=r.snapshot))
    with sessions() as db:
        r=read_hour(db,HOUR,current)
        assert r['advice_inputs']['ash']['value']==pytest.approx(8.8)
        assert r['suggested_density']['value']==1.48
        assert r['advice_inputs']['ash']['provisional']
    with sessions.begin() as db:
        assay(db,10,observed=HOUR,created=HOUR+timedelta(seconds=1))
    with sessions() as db:
        r=read_hour(db,HOUR+timedelta(seconds=2),current)
        assert r['advice_inputs']['ash']['value']==pytest.approx(10.5)
        assert r['suggested_density']['value']==1.45
        assert not r['advice_inputs']['ash']['provisional']
        assert db.get(DecisionCycleHour,iso_time(START)).snapshot==base


def test_hourly_api_returns_new_policy_and_two_bases(sessions, prepared, client, monkeypatch):
    monkeypatch.setattr('app.decision_hourly.service.utc_now',lambda:HOUR)
    r = client.get('/api/v1/decision/hourly')
    assert r.status_code == 200
    assert r.json()['advice_policy'] == 'gpt-hourly-quality-density-v1'
    assert r.json()['advice_inputs']['ash']['end'] == iso_time(START)
    assert r.json()['advice_inputs']['density']['end'] == iso_time(HOUR)
