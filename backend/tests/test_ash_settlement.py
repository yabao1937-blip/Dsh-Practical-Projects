"""Delayed assay availability, immutable decisions, and minute/hour conservation."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.decision_hourly.ash_settlement import (build_settlement, get_settlement, latest_end, manual_basis,
    read_settled_hours, read_settled_interval, settle_due_hours, settlement_tick)
from app.decision_hourly.archive import archive_old_hour
from app.decision_hourly.models import DecisionAshSettlement, DecisionCycleContext, DecisionCycleHour, DecisionCycleSample
from app.models import ManualMeasurement
from app.telemetry.domain import iso_time
from test_hourly_brief import sessions
from test_decision_sources import client
from test_decision_evidence import row as old_row
from test_ash_history import save as save_old_hour

LOCAL = timezone(timedelta(hours=8))
START = datetime(2026, 10, 14, 6, tzinfo=LOCAL)
END = START+timedelta(hours=1)
DEADLINE = END+timedelta(hours=2)


def sample(db, when=START, rate=400, choice=None, coarse_status='predicted'):
    r = old_row(0, rate=rate, ash=6.25)
    r.slot, r.sampled_at, r.fingerprint = int(when.timestamp())//5, iso_time(when), str(uuid4())
    for c in (r.snapshot['cells']['belt501'], r.snapshot['cells']['belt502']):
        c.update(observed_at=iso_time(when), received_at=iso_time(when), batch_id=r.fingerprint)
    r.snapshot['cells'].update(heavy_ash=dict(value=5, status='manual', unit='%'),
        float_ash=dict(value=10, status='manual', unit='%'),
        float_amount=dict(value=40, status='manual', unit='t/h'),
        coarse_amount=dict(value=20, status='calculated', unit='t/h'))
    r.snapshot.update(heavy_amount=dict(value=rate-60, status='calculated'),
        coarse_ash=dict(value=20, status=coarse_status), sources=dict(resolved_choices={'heavy-ash':choice}))
    r.snapshot['total_ash']['value'] = (5*(rate-60)+10*40+20*20)/rate
    if not db.get(DecisionCycleContext, 'ctx'):
        db.add(DecisionCycleContext(key='ctx', definition={}))
        db.flush()
    db.add(DecisionCycleSample(slot=r.slot, sampled_at=r.sampled_at, context_key='ctx',
                               fingerprint=r.fingerprint, snapshot=r.snapshot))
    db.flush()
    return r


def assay(db, value=8, observed=START+timedelta(minutes=30), created=DEADLINE-timedelta(minutes=1)):
    r = ManualMeasurement(metric='heavy_clean_coal_ash', name='重介精煤灰分', value=value, unit='%',
                          observed_at=iso_time(observed), created_at=iso_time(created), request_hash=uuid4().hex)
    db.add(r)
    db.flush()
    return r


def test_two_hour_publication_boundary_and_midnight(sessions):
    assert latest_end(datetime(2026,10,14,8,31,tzinfo=LOCAL)) == START
    assert latest_end(DEADLINE) == END
    assert latest_end(datetime(2026,10,14,1,31,tzinfo=LOCAL)) == datetime(2026,10,13,23,tzinfo=LOCAL)
    with sessions() as db:
        before = read_settled_hours(db, now=datetime(2026,10,14,8,31,tzinfo=LOCAL))
        assert business_stamp(before['items'][-1]['start']) == START-timedelta(hours=1)
        with pytest.raises(ValueError):
            get_settlement(db, START, DEADLINE-timedelta(microseconds=1))
        assert read_settled_hours(db, now=DEADLINE)['latest_end'] == iso_time(END)


def business_stamp(value):
    return datetime.fromisoformat(value)


def test_late_assay_revises_only_settlement_and_freezes_at_fixed_deadline(sessions):
    with sessions.begin() as db:
        original = sample(db)
        lab = assay(db)
        db.add(DecisionCycleHour(start=iso_time(START), end=iso_time(END), generated_at=iso_time(END),
                                snapshot=dict(total_ash=deepcopy(original.snapshot['total_ash']), advice='unchanged')))
        db.flush()
        settled = build_settlement(db, START)
        assert settled['item']['value'] == pytest.approx(8.8)
        assert settled['heavy_ash']['records'][0]['id'] == lab.id
        assert settled['item']['recalculated_count'] == 1
        settle_due_hours(db, DEADLINE)
        frozen = deepcopy(db.get(DecisionAshSettlement, iso_time(START)).snapshot)
        assay(db, 30, created=DEADLINE+timedelta(seconds=1))
        settle_due_hours(db, DEADLINE+timedelta(minutes=10))
        assert db.get(DecisionAshSettlement, iso_time(START)).snapshot == frozen
        assert db.get(DecisionCycleSample, original.slot).snapshot == original.snapshot
        assert db.get(DecisionCycleHour, iso_time(START)).snapshot['advice'] == 'unchanged'
        assert db.get(DecisionCycleHour, iso_time(START)).snapshot['total_ash']['value'] == 6.25
    with sessions() as db:
        before = list(db.connection().connection.driver_connection.iterdump())
        r = read_settled_hours(db, START, END, DEADLINE+timedelta(hours=4))
        assert r['items'][0]['value'] == pytest.approx(8.8) and r['items'][0]['frozen']
        assert list(db.connection().connection.driver_connection.iterdump()) == before


def test_sampling_membership_deadline_inclusive_and_record_mean(sessions):
    with sessions.begin() as db:
        sample(db)
        a = assay(db, 8, observed=START)
        b = assay(db, 10, created=DEADLINE)
        assay(db, 90, created=DEADLINE+timedelta(milliseconds=1))
        assay(db, 90, observed=END)
        result = build_settlement(db, START)
        assert result['heavy_ash']['value'] == 9
        assert [r['id'] for r in result['heavy_ash']['records']] == [a.id,b.id]
        assert result['item']['value'] == pytest.approx(9.65)


def test_no_in_hour_assay_carries_latest_prior_sample_not_latest_entered(sessions):
    with sessions.begin() as db:
        a = assay(db, 7, observed=START-timedelta(minutes=1))
        assay(db, 99, observed=START-timedelta(minutes=20), created=DEADLINE)
        assay(db, 98, observed=START-timedelta(seconds=1), created=DEADLINE+timedelta(seconds=1))
        b = manual_basis(db, START, END)
        assert b['carried'] and b['value'] == 7 and b['records'][0]['id'] == a.id


def test_each_frozen_source_choice_is_respected(sessions):
    with sessions.begin() as db:
        sample(db, choice=json.dumps(['kep', 'online-heavy-ash']))
        sample(db, START+timedelta(seconds=5), choice=json.dumps(['manual-measurements','heavy_clean_coal_ash']))
        assay(db)
        r = build_settlement(db, START)
        assert r['item']['recalculated_count'] == 1
        assert r['item']['value'] == pytest.approx((6.25+8.8)/2)


@pytest.mark.parametrize('status', ['held','stopped','missing'])
def test_manual_assay_does_not_rescue_invalid_coarse_predictions(sessions,status):
    with sessions.begin() as db:
        sample(db, coarse_status=status)
        assay(db)
        r = build_settlement(db, START)
        assert r['item']['value'] is None and not r['contributions']


def test_bad_flow_and_stale_float_ash_are_not_rescued(sessions):
    with sessions.begin() as db:
        r = sample(db)
        r.snapshot['cells']['belt502']['quality'] = 'bad'
        db.get(DecisionCycleSample,r.slot).snapshot = deepcopy(r.snapshot)
        other = sample(db, START+timedelta(seconds=5))
        other.snapshot['cells']['float_ash'].update(status='good', observed_at=iso_time(START-timedelta(minutes=1)))
        db.get(DecisionCycleSample,other.slot).snapshot = deepcopy(other.snapshot)
        assay(db)
        db.flush()
        assert build_settlement(db,START)['item']['value'] is None


def test_minute_slices_use_same_hour_assays_and_conserve_mass(sessions):
    with sessions.begin() as db:
        sample(db, START+timedelta(seconds=59), rate=400)
        sample(db, START+timedelta(minutes=30), rate=100)
        assay(db)
        settle_due_hours(db, DEADLINE)
    with sessions() as db:
        whole = read_settled_interval(db,START,END,DEADLINE)
        left = read_settled_interval(db,START,START+timedelta(minutes=1),DEADLINE)
        right = read_settled_interval(db,START+timedelta(minutes=1),END,DEADLINE)
        assert left['summary']['valid_seconds'] == 1
        assert right['summary']['valid_seconds'] == 9
        assert whole['summary']['valid_seconds'] == 10  # No artificial fill across 29-minute gap.
        assert left['summary']['mass_t']+right['summary']['mass_t'] == pytest.approx(whole['summary']['mass_t'])
        combined = sum(r['summary']['value']*r['summary']['mass_t'] for r in (left,right))/whole['summary']['mass_t']
        assert combined == pytest.approx(whole['summary']['value'])
        assert read_settled_hours(db,START,END,DEADLINE)['summary']['value'] == whole['summary']['value']


def test_restart_after_deadline_cannot_include_newer_assays_and_archives_work(sessions):
    with sessions.begin() as db:
        sample(db)
        assay(db)
        assay(db, 99, created=DEADLINE+timedelta(minutes=1))
        db.add(DecisionCycleHour(start=iso_time(START), end=iso_time(END), generated_at=iso_time(END),snapshot={}))
        db.flush()
        before = build_settlement(db, START)
        archive_old_hour(db, iso_time(END))
        db.flush()
        assert list(db.scalars(select(DecisionCycleSample))) == []
        assert build_settlement(db, START) == before
        settle_due_hours(db, DEADLINE+timedelta(days=2))
        saved = db.get(DecisionAshSettlement, iso_time(START))
        assert saved and saved.snapshot['item']['value'] == pytest.approx(8.8)


def test_missing_data_is_not_zero_or_repaired_by_an_assay(sessions):
    with sessions.begin() as db:
        assay(db)
        r=build_settlement(db,START)
        assert r['item']['value'] is None and r['item']['sample_count'] == 0
        assert '缺少原时段逐次明细' in r['item']['reason']


def test_old_frozen_value_survives_without_details_but_cannot_fill_minutes(sessions):
    with sessions.begin() as db:
        save_old_hour(db, START, 8.5, 720)
        assay(db, 9)
        db.flush()
        r = build_settlement(db, START)
        assert r['item']['value'] == 8.5 and r['item']['legacy_reference']
        assert r['item']['recalculated_count'] == 0 and r['contributions'] == []
        assert '未纳入晚补化验' in r['item']['reason']
        assert '采用重介化验' not in r['item']['reason']
        assert read_settled_hours(db, START, END, DEADLINE)['summary']['value'] == 8.5
        assert read_settled_interval(db, START, START+timedelta(minutes=1), DEADLINE)['summary']['value'] is None


def test_old_cycle_values_remain_marked_references_without_recalculation(sessions):
    with sessions.begin() as db:
        r = sample(db)
        r.snapshot.pop('evidence_policy')
        db.get(DecisionCycleSample, r.slot).snapshot = deepcopy(r.snapshot)
        assay(db, 9)
        db.flush()
        result = build_settlement(db, START)
        assert result['item']['value'] == 6.25 and result['item']['legacy_reference']
        assert result['item']['recalculated_count'] == 0
        assert result['contributions'][0]['manual_applied'] is False
        minute = read_settled_interval(db, START, START+timedelta(minutes=1), DEADLINE)
        assert minute['summary']['value'] == 6.25 and minute['summary']['legacy_reference']


def test_concurrent_settlement_workers_freeze_once(sessions):
    with sessions.begin() as db:
        sample(db)
        assay(db)
    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(lambda _: settlement_tick(DEADLINE, sessions), range(2)))
    with sessions.begin() as db:
        saved = list(db.scalars(select(DecisionAshSettlement)))
        assert len(saved) == 1 and saved[0].snapshot['item']['value'] == pytest.approx(8.8)
        frozen = deepcopy(saved[0].snapshot)
        assay(db, 99, created=DEADLINE+timedelta(seconds=1))
    settlement_tick(DEADLINE+timedelta(minutes=1), sessions)
    with sessions() as db:
        assert db.get(DecisionAshSettlement, iso_time(START)).snapshot == frozen


def test_api_publishes_only_settled_hours_and_has_no_read_writes(client,sessions,monkeypatch):
    monkeypatch.setattr('app.decision_hourly.ash_settlement.utc_now', lambda: DEADLINE)
    with sessions.begin() as db:
        sample(db)
        assay(db)
    params=dict(start=iso_time(START),end=iso_time(END))
    for route in ('total-ash-hours','total-ash-interval'):
        r=client.get('/api/v1/decision/'+route,params=params)
        assert r.status_code==200,r.text
        assert r.json()['summary']['value']==pytest.approx(8.8)
        assert r.json()['publication_delay_seconds']==7200
        invalid=client.get('/api/v1/decision/'+route,params=dict(start=iso_time(END),end=iso_time(END+timedelta(hours=1))))
        assert invalid.status_code==422
    with sessions() as db:
        assert list(db.scalars(select(DecisionAshSettlement)))==[]
