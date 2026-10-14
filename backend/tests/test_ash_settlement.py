"""Delayed assay availability, immutable decisions, and minute/hour conservation."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.decision_hourly.ash_settlement import (build_settlement, get_settlement, latest_end, manual_basis,
    prepare_due_hours, read_settled_hours, read_settled_interval, save_prepared, settle_due_hours, settlement_tick)
from app.decision_hourly.archive import archive_old_hour
from app.decision_hourly.models import DecisionAshSettlement, DecisionCycleContext, DecisionCycleHour, DecisionCycleSample
from app.models import ManualMeasurement
from app.telemetry.domain import iso_time
from test_hourly_brief import sessions
from test_decision_sources import client
from test_decision_evidence import row as old_row
from test_ash_history import save as save_old_hour

LOCAL = timezone(timedelta(hours=8))
START = datetime(2026, 10, 14, 8, tzinfo=LOCAL)
END = START+timedelta(hours=1)
PUBLISH = END+timedelta(hours=1)
NOW = PUBLISH+timedelta(minutes=45)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr('app.decision_hourly.ash_settlement.utc_now', lambda: NOW)


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


def assay(db, value=8, observed=PUBLISH+timedelta(minutes=20), created=PUBLISH+timedelta(minutes=21)):
    r = ManualMeasurement(metric='heavy_clean_coal_ash', name='重介精煤灰分', value=value, unit='%',
                          observed_at=iso_time(observed), created_at=iso_time(created), request_hash=uuid4().hex)
    db.add(r)
    db.flush()
    return r


def test_sample_at_ten_publishes_eight_to_nine_and_midnight(sessions):
    assert latest_end(NOW) == END
    assert latest_end(PUBLISH) == END
    assert latest_end(datetime(2026,10,14,0,31,tzinfo=LOCAL)) == datetime(2026,10,13,23,tzinfo=LOCAL)
    with sessions() as db:
        before = read_settled_hours(db, now=PUBLISH-timedelta(seconds=1))
        assert business_stamp(before['items'][-1]['start']) == START-timedelta(hours=1)
        with pytest.raises(ValueError):
            get_settlement(db, START, PUBLISH-timedelta(microseconds=1))
        assert read_settled_hours(db, now=PUBLISH)['latest_end'] == iso_time(END)
        assert read_settled_hours(db, now=NOW)['items'][-1]['start'] == iso_time(START)


def business_stamp(value):
    return datetime.fromisoformat(value)


def test_late_assay_updates_estimate_and_preserves_original_decisions(sessions):
    with sessions.begin() as db:
        original = sample(db)
        assay(db, 7, observed=PUBLISH-timedelta(hours=1), created=PUBLISH-timedelta(minutes=30))
        db.add(DecisionCycleHour(start=iso_time(START), end=iso_time(END), generated_at=iso_time(END),
                                snapshot=dict(total_ash=deepcopy(original.snapshot['total_ash']), advice='unchanged')))
        db.flush()
        settle_due_hours(db, PUBLISH)
        estimated = get_settlement(db, START, PUBLISH)
        assert estimated['item']['value'] == pytest.approx(7.95)
        assert estimated['item']['provisional'] and '暂估' in estimated['item']['reason']
        lab = assay(db)
        # A read detects a new lab before the background cache update and never writes.
        settled = get_settlement(db, START, NOW)
        assert settled['item']['value'] == pytest.approx(8.8)
        assert not settled['item']['provisional'] and not settled['item']['cached']
        assert settled['heavy_ash']['records'][0]['id'] == lab.id
        assert settled['item']['recalculated_count'] == 1
        settle_due_hours(db, NOW)
        # Even next-day late entry revises its assigned hour; no fixed cutoff.
        assay(db, 10, observed=PUBLISH, created=NOW+timedelta(days=1))
        revised = get_settlement(db, START, NOW+timedelta(days=1))
        assert revised['item']['value'] == pytest.approx(9.65)
        assert db.get(DecisionCycleSample, original.slot).snapshot == original.snapshot
        assert db.get(DecisionCycleHour, iso_time(START)).snapshot['advice'] == 'unchanged'
        assert db.get(DecisionCycleHour, iso_time(START)).snapshot['total_ash']['value'] == 6.25
    with sessions() as db:
        before = list(db.connection().connection.driver_connection.iterdump())
        r = read_settled_hours(db, START, END, NOW)
        assert r['items'][0]['value'] == pytest.approx(8.8) and r['items'][0]['cached']
        assert list(db.connection().connection.driver_connection.iterdump()) == before


def test_non_round_sampling_membership_availability_and_record_mean(sessions):
    with sessions.begin() as db:
        sample(db)
        a = assay(db, 8, observed=PUBLISH)
        b = assay(db, 10, created=NOW)
        assay(db, 90, created=NOW+timedelta(milliseconds=1))
        assay(db, 90, observed=PUBLISH+timedelta(hours=1))
        assay(db, 90, observed=NOW+timedelta(minutes=1), created=NOW)  # Future prefilled sample.
        result = build_settlement(db, START)
        assert result['heavy_ash']['value'] == 9
        assert [r['id'] for r in result['heavy_ash']['records']] == [a.id,b.id]
        assert result['item']['value'] == pytest.approx(9.65)


def test_estimate_uses_latest_prior_sample_and_never_borrows_later_hour(sessions):
    with sessions.begin() as db:
        a = assay(db, 7, observed=PUBLISH-timedelta(minutes=1))
        assay(db, 99, observed=PUBLISH-timedelta(minutes=20), created=NOW)
        assay(db, 98, observed=PUBLISH-timedelta(seconds=1), created=NOW+timedelta(seconds=1))
        assay(db, 90, observed=PUBLISH+timedelta(hours=1), created=NOW)
        b = manual_basis(db, START, END)
        assert b['carried'] and b['value'] == 7 and b['records'][0]['id'] == a.id
        b = manual_basis(db, START, END, NOW+timedelta(days=1))
        assert b['carried'] and b['value'] == 98


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
        settle_due_hours(db, NOW)
    with sessions() as db:
        whole = read_settled_interval(db,START,END,NOW)
        left = read_settled_interval(db,START,START+timedelta(minutes=1),NOW)
        right = read_settled_interval(db,START+timedelta(minutes=1),END,NOW)
        assert left['summary']['valid_seconds'] == 1
        assert right['summary']['valid_seconds'] == 9
        assert whole['summary']['valid_seconds'] == 10  # No artificial fill across 29-minute gap.
        assert left['summary']['mass_t']+right['summary']['mass_t'] == pytest.approx(whole['summary']['mass_t'])
        combined = sum(r['summary']['value']*r['summary']['mass_t'] for r in (left,right))/whole['summary']['mass_t']
        assert combined == pytest.approx(whole['summary']['value'])
        assert read_settled_hours(db,START,END,NOW)['summary']['value'] == whole['summary']['value']


def test_restart_and_archives_keep_attribution_and_allow_later_revision(sessions):
    with sessions.begin() as db:
        sample(db)
        assay(db)
        assay(db, 99, created=NOW+timedelta(minutes=1))
        db.add(DecisionCycleHour(start=iso_time(START), end=iso_time(END), generated_at=iso_time(END),snapshot={}))
        db.flush()
        before = build_settlement(db, START)
        archive_old_hour(db, iso_time(END))
        db.flush()
        assert list(db.scalars(select(DecisionCycleSample))) == []
        assert build_settlement(db, START) == before
        settle_due_hours(db, NOW)
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
        assert read_settled_hours(db, START, END, NOW)['summary']['value'] == 8.5
        assert read_settled_interval(db, START, START+timedelta(minutes=1), NOW)['summary']['value'] is None


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
        minute = read_settled_interval(db, START, START+timedelta(minutes=1), NOW)
        assert minute['summary']['value'] == 6.25 and minute['summary']['legacy_reference']


def test_concurrent_settlement_workers_update_same_hour_once(sessions):
    with sessions.begin() as db:
        sample(db)
        assay(db)
    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(lambda _: settlement_tick(NOW, sessions), range(2)))
    with sessions.begin() as db:
        saved = list(db.scalars(select(DecisionAshSettlement)))
        assert len(saved) == 1 and saved[0].snapshot['item']['value'] == pytest.approx(8.8)
        frozen = deepcopy(saved[0].snapshot)
        assay(db, 99, created=NOW+timedelta(seconds=1))
    settlement_tick(NOW+timedelta(minutes=1), sessions)
    with sessions() as db:
        updated = db.get(DecisionAshSettlement, iso_time(START)).snapshot
        assert updated['revision'] != frozen['revision']
        assert updated['heavy_ash']['value'] == 53.5


def test_api_publishes_only_settled_hours_and_has_no_read_writes(client,sessions,monkeypatch):
    monkeypatch.setattr('app.decision_hourly.ash_settlement.utc_now', lambda: NOW)
    with sessions.begin() as db:
        sample(db)
        assay(db)
    params=dict(start=iso_time(START),end=iso_time(END))
    for route in ('total-ash-hours','total-ash-interval'):
        r=client.get('/api/v1/decision/'+route,params=params)
        assert r.status_code==200,r.text
        assert r.json()['summary']['value']==pytest.approx(8.8)
        assert r.json()['publication_delay_seconds']==3600
        invalid=client.get('/api/v1/decision/'+route,params=dict(start=iso_time(END),end=iso_time(END+timedelta(hours=1))))
        assert invalid.status_code==422
    with sessions() as db:
        assert list(db.scalars(select(DecisionAshSettlement)))==[]


def test_old_policy_cache_is_replaced_and_pending_source_data_is_not_frozen(sessions):
    with sessions.begin() as db:
        assay(db)
        db.add(DecisionAshSettlement(start=iso_time(START), end=iso_time(END), deadline=iso_time(END+timedelta(hours=2)),
            generated_at=iso_time(NOW), snapshot=dict(policy='gpt-ash-settlement-v1', item=dict(value=99))))
        db.flush()
        assert get_settlement(db,START,NOW)['item']['value'] is None
        sample(db)
        settle_due_hours(db,NOW)
        result=get_settlement(db,START,NOW)
        assert result['item']['value']==pytest.approx(8.8) and result['item']['cached']
        assert result['policy']=='gpt-ash-settlement-v2'


def test_assay_entered_during_replay_cannot_commit_stale_estimate(sessions):
    with sessions.begin() as db:
        sample(db)
    with sessions() as db:
        prepared=prepare_due_hours(db,NOW)
    with sessions.begin() as db:
        assay(db)
        save_prepared(db,prepared)
        assert db.get(DecisionAshSettlement,iso_time(START)) is None
    settlement_tick(NOW,sessions)
    with sessions() as db:
        assert get_settlement(db,START,NOW)['item']['value']==pytest.approx(8.8)


def test_midnight_lab_belongs_to_previous_day_twenty_two_to_twenty_three(sessions):
    lab_time=datetime(2026,10,14,0,20,tzinfo=LOCAL)
    start=datetime(2026,10,13,22,tzinfo=LOCAL)
    with sessions.begin() as db:
        sample(db,start)
        lab=assay(db,8,observed=lab_time,created=lab_time+timedelta(minutes=1))
        r=get_settlement(db,start,lab_time+timedelta(minutes=2))
        assert r['heavy_ash']['records'][0]['id']==lab.id
        assert r['item']['value']==pytest.approx(8.8)
        assert not r['item']['provisional']


def test_original_live_heavy_value_is_used_as_estimate_if_no_manual_archive(sessions):
    with sessions.begin() as db:
        sample(db)
        result=get_settlement(db,START,NOW)
        assert result['item']['value']==6.25
        assert result['item']['provisional']
        assert read_settled_interval(db,START,START+timedelta(minutes=1),NOW)['summary']['provisional']
