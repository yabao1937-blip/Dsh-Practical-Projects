"""Polling cannot replay cold historical evidence or expose incompatible caches."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.decision_hourly import ash_settlement as settlement
from app.decision_hourly.models import DecisionAshSettlement, DecisionCycleHour
from app.telemetry.domain import iso_time
from test_ash_settlement import START, END, NOW, assay, sample
from test_hourly_brief import sessions


def no_replay(*args, **kwargs):
    raise AssertionError('Polling must leave historical replay to the worker')


@pytest.mark.parametrize('policy', [None, 'gpt-ash-settlement-v3'])
def test_hour_poll_is_pending_without_replay_or_old_snapshot_validation(sessions, monkeypatch, policy):
    with sessions.begin() as db:
        sample(db)
        assay(db)
        if policy:
            db.add(DecisionAshSettlement(start=iso_time(START), end=iso_time(END), deadline=iso_time(END),
                generated_at=iso_time(NOW), snapshot=dict(policy=policy, revision='incompatible', item=dict(value=99))))
    monkeypatch.setattr(settlement, 'build_settlement', no_replay)
    monkeypatch.setattr(settlement, 'read_ash_hours', no_replay)
    with sessions() as db:
        before = list(db.connection().connection.driver_connection.iterdump())
        for _ in range(3):
            result = settlement.read_settled_hours(db, START, END, NOW)
            assert result['pending']
            assert result['items'][0]['pending']
            assert result['items'][0]['status'] == 'pending'
            assert result['items'][0]['value'] is None
            assert result['summary']['value'] is None
            assert result['summary']['sample_count'] == 0
            assert result['items'][0]['settlement_policy'] == settlement.POLICY
        assert list(db.connection().connection.driver_connection.iterdump()) == before


def test_worker_warms_cache_and_late_assay_revises_carried_hours(sessions, monkeypatch):
    previous = START-timedelta(hours=1)
    with sessions.begin() as db:
        sample(db, previous)
        sample(db, START)
        first = assay(db, 7, observed=previous, created=previous)
        first.time_basis = 'production-time-v1'
    settlement.settlement_tick(NOW, sessions)
    with sessions() as db:
        before = settlement.read_settled_hours(db, previous, END, NOW)
        assert not before['pending']
        assert [i['value'] for i in before['items']] == pytest.approx([7.95, 7.95])
    with sessions.begin() as db:
        late = assay(db, 9, observed=previous+timedelta(minutes=47), created=NOW+timedelta(seconds=1))
        late.time_basis = 'production-time-v1'
    revised_at = NOW+timedelta(seconds=2)
    with monkeypatch.context() as patch:
        patch.setattr(settlement, 'build_settlement', no_replay)
        with sessions() as db:
            pending = settlement.read_settled_hours(db, previous, END, revised_at)
            assert pending['pending']
            assert all(i['value'] is None and i['pending'] for i in pending['items'])
    settlement.settlement_tick(revised_at, sessions)
    with sessions() as db:
        after = settlement.read_settled_hours(db, previous, END, revised_at)
        assert not after['pending']
        assert [i['value'] for i in after['items']] == pytest.approx([9.65, 9.65])
        assert not after['items'][0]['provisional']
        assert after['items'][1]['provisional']


def test_worker_prioritizes_visible_ten_hours_and_bounds_each_tick(sessions, monkeypatch):
    old_start = START-timedelta(hours=15)
    with sessions.begin() as db:
        for hours in range(20):
            start = START-timedelta(hours=hours)
            db.add(DecisionCycleHour(start=iso_time(start), end=iso_time(start+timedelta(hours=1)),
                generated_at=iso_time(NOW), snapshot={}))
        late = assay(db, 8, observed=old_start, created=NOW)
        late.time_basis = 'production-time-v1'
    original, built = settlement.build_settlement, []
    def replay(db, start, *args, **kwargs):
        built.append(start)
        return original(db, start, *args, **kwargs)
    monkeypatch.setattr(settlement, 'build_settlement', replay)
    for tick in range(5):
        before = len(built)
        settlement.settlement_tick(NOW+timedelta(seconds=5*tick), sessions)
        assert len(built)-before == 2
    assert built == [START-timedelta(hours=i) for i in range(10)]
    settlement.settlement_tick(NOW+timedelta(seconds=25), sessions)
    assert built[10] == old_start  # Late historical entries still get priority over rotated backlog.
    assert len(built) <= 12
    with sessions() as db:
        assert len(list(db.scalars(select(DecisionAshSettlement)))) == len(built)

