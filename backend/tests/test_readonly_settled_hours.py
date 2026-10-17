"""Exact, bounded hourly-table reads against isolated production-like storage."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import event

from app.decision_hourly.ash_settlement import (
    POLICY, build_settlement, latest_end, manual_basis,
    read_settled_hours as original_read, revision, settlement_tick, source_revision,
)
from app.decision_hourly.models import (
    DecisionAshSettlement, DecisionCycleHour, DecisionEvidenceArchive,
)
from app.models import ManualMeasurement
from app.remote_readonly.settled_hours import read_settled_hours
from app.telemetry.domain import iso_time
from test_ash_settlement import sample
from test_hourly_brief import sessions


LOCAL = timezone(timedelta(hours=8))
START = datetime(2026, 10, 17, 8, tzinfo=LOCAL)
NOW = START+timedelta(hours=5, minutes=1)


def lab(db, observed, value, *, created=NOW, basis='production-time-v1', **fields):
    row = ManualMeasurement(metric='heavy_clean_coal_ash', name='重介精煤灰分',
        value=value, unit='%', observed_at=iso_time(observed), created_at=iso_time(created),
        time_basis=basis, request_hash=uuid4().hex)
    for key, value in fields.items():
        setattr(row, key, value)
    db.add(row)
    db.flush()
    return row


def cache(db, start, *, now=NOW, completed=True, generated=None):
    stop = start+timedelta(hours=1)
    if completed:
        db.add(DecisionCycleHour(start=iso_time(start), end=iso_time(stop),
            generated_at=iso_time(stop) if generated is None else generated, snapshot={}))
        db.flush()
    snapshot = build_settlement(db, start, now)
    db.add(DecisionAshSettlement(start=iso_time(start), end=iso_time(stop),
        deadline=iso_time(stop+timedelta(hours=1)), generated_at=iso_time(now), snapshot=snapshot))
    db.flush()
    return snapshot


@contextmanager
def queries(db):
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(db.bind, 'before_cursor_execute', capture)
    try:
        yield statements
    finally:
        event.remove(db.bind, 'before_cursor_execute', capture)


def compare(db, start=None, end=None, now=NOW):
    expected = original_read(db, start, end, now)
    actual = read_settled_hours(db, start, end, now)
    assert actual == expected
    return actual


def test_empty_default_window_is_identical_and_five_selects(sessions):
    with sessions() as db:
        expected = original_read(db, now=NOW)
        with queries(db) as sql:
            result = read_settled_hours(db, now=NOW)
        assert result == expected
        assert len(result['items']) == 24 and result['pending']
        assert len(sql) == 5 and all(query.lstrip().upper().startswith('SELECT') for query in sql)
        assert not db.new and not db.dirty and not db.deleted


def test_completed_cached_window_is_three_selects_and_excludes_evidence_payloads(sessions):
    end = latest_end(NOW)
    with sessions.begin() as db:
        for index in range(24):
            stamp = end-timedelta(hours=index+1)
            cache(db, stamp)
        expected = original_read(db, now=NOW)
        # Fill the expensive field with a recognizable large value. The public
        # contract is solely the item, so it must not cross the bridge at all.
        saved = db.get(DecisionAshSettlement, iso_time(end-timedelta(hours=1)))
        saved.snapshot = dict(saved.snapshot, contributions=['not-read'*100_000])
        db.flush()
        with queries(db) as sql:
            result = read_settled_hours(db, now=NOW)
        assert result == expected and not result['pending']
        assert len(sql) == 3
        projection = next(query for query in sql if 'gpt_ash_settlements' in query)
        assert 'JSON_EXTRACT' in projection.upper()
        assert 'gpt_ash_settlements.snapshot \n' not in projection
        assert 'gpt_decision_evidence_archives' not in '\n'.join(sql)
        assert 'decision_cycle_samples' not in '\n'.join(sql)


def test_mixed_cached_stale_absent_policies_preserve_pending_contract(sessions):
    with sessions.begin() as db:
        for index in range(3):
            stamp = START+timedelta(hours=index)
            sample(db, stamp)
            if index < 2:
                cache(db, stamp)
        lab(db, START+timedelta(hours=1, minutes=20), 8)
        result = compare(db, START, START+timedelta(hours=3))
        assert [item.get('pending', False) for item in result['items']] == [False, True, True]
        assert result['items'][0]['cached']
        for item in result['items'][1:]:
            assert item['value'] is None and not item['cached'] and not item['frozen']
            assert item['settlement_policy'] == POLICY
        # An obsolete cached policy/hash is never served as a current value.
        first = db.get(DecisionAshSettlement, iso_time(START))
        first.snapshot = dict(first.snapshot, revision='old-policy')
        db.flush()
        assert compare(db, START, START+timedelta(hours=1))['pending']


def test_legacy_actual_time_latest_sample_and_future_filters_match(sessions):
    with sessions.begin() as db:
        sample(db, START)
        # Legacy observed10:15 means production08:15; actual08:47 must win.
        lab(db, START+timedelta(hours=2, minutes=15), 7, basis='legacy-lag-2h')
        lab(db, START+timedelta(minutes=47), 8)
        latest = lab(db, START+timedelta(minutes=47), 8.2)
        lab(db, START+timedelta(minutes=20), 99, created=NOW+timedelta(seconds=1))
        lab(db, NOW+timedelta(seconds=1), 99, created=NOW)
        lab(db, START+timedelta(minutes=58), 9, unit='wrong')
        lab(db, START+timedelta(minutes=57), 9, metric='coarse_slime_ash')
        snapshot = cache(db, START)
        assert snapshot['heavy_ash']['records'][0]['id'] == latest.id
        assert snapshot['heavy_ash']['value'] == 8.2
        assert not compare(db, START, START+timedelta(hours=1))['pending']


def test_late_sample_invalidates_own_and_carried_hours_only_then_tick_updates_both(sessions):
    begin, end = START-timedelta(hours=1), START+timedelta(hours=4)
    with sessions.begin() as db:
        lab(db, START-timedelta(hours=2), 7)
        lab(db, START+timedelta(minutes=47), 8)
        lab(db, START+timedelta(hours=3, minutes=10), 10)
        for index in range(5):
            stamp = begin+timedelta(hours=index)
            sample(db, stamp)
            cache(db, stamp)
    with sessions() as db:
        before = compare(db, begin, end)
        original_hours = {row.start: deepcopy(row.snapshot) for row in db.query(DecisionCycleHour)}
    with sessions.begin() as db:
        lab(db, START+timedelta(hours=1, minutes=20), 9)
    with sessions() as db:
        pending = compare(db, begin, end)
        assert [item.get('pending', False) for item in pending['items']] == [False, False, True, True, False]
    # At most two affected hours fit the unchanged worker budget on its next
    # five-second tick. Larger backlogs deliberately have no 30-second claim.
    settled_at = NOW+timedelta(seconds=5)
    settlement_tick(settled_at, sessions)
    with sessions() as db:
        after = compare(db, begin, end, settled_at)
        assert not after['pending']
        for index in (0, 1, 4):
            assert after['items'][index] == before['items'][index]
        for index in (2, 3):
            assert after['items'][index]['value'] != before['items'][index]['value']
        assert {row.start: row.snapshot for row in db.query(DecisionCycleHour)} == original_hours


@pytest.mark.parametrize('generated', [None, ''])
def test_missing_or_falsy_completed_source_uses_hot_and_archive_revision(sessions, generated):
    with sessions.begin() as db:
        sample(db, START)
        if generated == '':
            db.add(DecisionCycleHour(start=iso_time(START), end=iso_time(START+timedelta(hours=1)),
                generated_at='', snapshot={}))
            db.flush()
        snapshot = cache(db, START, completed=False)
        # A checksum participates even if the hour also has hot samples. Never
        # open/decompress this payload in a valid cached table read.
        db.add(DecisionEvidenceArchive(start=iso_time(START), payload=b'invalid-if-read',
            checksum='archive-hash', sample_count=0))
        db.flush()
        saved = db.get(DecisionAshSettlement, iso_time(START))
        saved.snapshot = dict(snapshot, revision=revision(manual_basis(db, START,
            START+timedelta(hours=1), NOW), source_revision(db, START)))
        db.flush()
        with queries(db) as sql:
            result = compare(db, START, START+timedelta(hours=1))
        assert not result['pending'] and result['items'][0]['cached']
        assert all('payload' not in query for query in sql)
        archive = db.get(DecisionEvidenceArchive, iso_time(START))
        archive.checksum = 'archive-revised'
        db.flush()
        assert compare(db, START, START+timedelta(hours=1))['pending']


def test_read_uses_sqlite_readonly_authorizer_without_any_mutation(sessions):
    from app.remote_readonly.server import _authorize

    with sessions.begin() as db:
        sample(db, START)
        lab(db, START+timedelta(minutes=47), 8)
        cache(db, START)
    with sessions() as db:
        expected = original_read(db, START, START+timedelta(hours=1), NOW)
        raw = db.connection().connection.driver_connection
        before = list(raw.iterdump())
        raw.set_authorizer(_authorize)
        try:
            assert read_settled_hours(db, START, START+timedelta(hours=1), NOW) == expected
        finally:
            raw.set_authorizer(None)
        assert list(raw.iterdump()) == before


def test_hot_source_buckets_keep_exact_hour_bounds_and_empty_counts(sessions):
    with sessions.begin() as db:
        sample(db, START)
        sample(db, START+timedelta(minutes=59, seconds=55))
        sample(db, START+timedelta(hours=1))
        for index in range(3):
            cache(db, START+timedelta(hours=index), completed=False)
        result = compare(db, START, START+timedelta(hours=3))
        assert not result['pending']
        assert [item['sample_count'] for item in result['items']] == [2, 1, 0]


@pytest.mark.parametrize('start,end', [
    (START, None), (START+timedelta(minutes=1), START+timedelta(hours=1)),
    (START, START), (START, NOW+timedelta(hours=1)),
])
def test_invalid_ranges_fail_before_queries(sessions, start, end):
    with sessions() as db, queries(db) as sql:
        with pytest.raises(ValueError):
            read_settled_hours(db, start, end, NOW)
        assert sql == []
