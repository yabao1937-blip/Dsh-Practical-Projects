"""Retention must use final usage, including held values and old zlib evidence."""
from datetime import timedelta
import hashlib
import json
import zlib

import pytest
from sqlalchemy import delete, event, select

from app.decision_hourly.models import (DecisionCycleContext, DecisionCycleSample,
                                       DecisionEvidenceArchive, DecisionTelemetryPin)
from app.decision_hourly.references import pin_readings
from app.schema_upgrade import upgrade_schema
from app.storage_retention.pins import backfill_hour_refs, prepare_hour_refs, commit_hour_refs
from app.telemetry.domain import iso_time
from app.telemetry.models import TelemetryReading
from test_hourly_brief import sessions, HOUR


def reading(db, identity='batch'):
    row = TelemetryReading(source_id='kepserver-main', point_key='level', batch_id=identity,
                           received_at=iso_time(HOUR-timedelta(days=80)), sample={})
    db.add(row)
    db.flush()
    return row.id


def decision(db, when, reading_id):
    row = DecisionCycleSample(slot=int(when.timestamp()//5), sampled_at=iso_time(when),
                              context_key='context', fingerprint='fingerprint',
                              snapshot={'cells': {'current': {'$reading': reading_id,
                                  'overlay': {'retained': {'$reading': reading_id}}}}})
    db.add(row)
    db.flush()
    return row


def archive(db, when, reading_id, *, corrupt=False, count=1):
    raw = json.dumps([dict(slot=int(when.timestamp()//5), sampled_at=iso_time(when),
                          snapshot={'cells': {'x': {'$reading': reading_id}}})]).encode()
    row = DecisionEvidenceArchive(start=iso_time(HOUR), payload=zlib.compress(raw),
                                  checksum='invalid' if corrupt else hashlib.sha256(raw).hexdigest(),
                                  sample_count=count)
    db.add(row)
    db.flush()
    return row


def test_pin_bounds_cover_late_and_out_of_order_uses_without_lowering_max(sessions):
    with sessions.begin() as db:
        key = reading(db)
        db.add(DecisionTelemetryPin(reading_id=key, first_slot=500, last_slot=None))
    with sessions.begin() as db:
        held = db.get(DecisionTelemetryPin, key)
        pin_readings(db, {key}, 900)
        assert (held.first_slot, held.last_slot) == (500, 900)
        pin_readings(db, {key}, 100)
        assert (held.first_slot, held.last_slot) == (100, 900)
        pin_readings(db, {key}, 1200)
        assert (held.first_slot, held.last_slot) == (100, 1200)
        pin_readings(db, {key}, 300)
    with sessions() as db:
        held = db.get(DecisionTelemetryPin, key)
        assert (held.first_slot, held.last_slot) == (100, 1200)


def test_pin_writes_chunk_under_old_sqlite_parameter_limit(sessions):
    with sessions.begin() as db:
        keys = {reading(db, 'batch'+str(i)) for i in range(250)}
        pin_readings(db, keys, 100)
        pin_readings(db, keys, 200)
        rows = list(db.scalars(select(DecisionTelemetryPin)))
        assert len(rows) == 250
        assert {(r.first_slot, r.last_slot) for r in rows} == {(100, 200)}


def test_hour_backfill_combines_hot_and_checked_archive_and_is_idempotent(sessions):
    with sessions.begin() as db:
        key = reading(db)
        db.add(DecisionCycleContext(key='context', definition={}))
        decision(db, HOUR+timedelta(seconds=30), key)
        latest = decision(db, HOUR+timedelta(minutes=50), key)
        archive(db, HOUR+timedelta(seconds=5), key)
        result = backfill_hour_refs(db, HOUR, HOUR+timedelta(hours=1))
        assert result == dict(start=iso_time(HOUR), end=iso_time(HOUR+timedelta(hours=1)),
                              hot_count=2, archived_count=1, reading_count=1)
        pin = db.get(DecisionTelemetryPin, key)
        assert pin.first_slot == int(HOUR.timestamp()//5)+1
        assert pin.last_slot == latest.slot
        assert backfill_hour_refs(db, iso_time(HOUR), iso_time(HOUR+timedelta(hours=1))) == result
        # A future decision arriving before an older cursor cannot be undone.
        pin_readings(db, {key}, latest.slot+5000)
        backfill_hour_refs(db, HOUR, HOUR+timedelta(hours=1))
        assert pin.last_slot == latest.slot+5000


@pytest.mark.parametrize('kind', ['checksum', 'count', 'missing', 'malformed'])
def test_backfill_corruption_fails_before_any_pin_write(sessions, kind):
    with sessions.begin() as db:
        key = reading(db)
        db.add(DecisionCycleContext(key='context', definition={}))
        decision(db, HOUR+timedelta(seconds=30), key)
        archive(db, HOUR+timedelta(seconds=5), True if kind == 'malformed' else key+1 if kind == 'missing' else key,
                corrupt=kind == 'checksum', count=2 if kind == 'count' else 1)
        with pytest.raises(ValueError):
            backfill_hour_refs(db, HOUR, HOUR+timedelta(hours=1))
        assert list(db.scalars(select(DecisionTelemetryPin))) == []


def test_empty_hour_does_not_guess_usage_of_unscanned_legacy_pin(sessions):
    with sessions.begin() as db:
        key = reading(db)
        db.add(DecisionTelemetryPin(reading_id=key, first_slot=123, last_slot=None))
    with sessions.begin() as db:
        result = backfill_hour_refs(db, HOUR, HOUR+timedelta(hours=1))
        assert result['reading_count'] == 0
        assert db.get(DecisionTelemetryPin, key).last_slot is None


@pytest.mark.parametrize('start,end', [(HOUR+timedelta(minutes=1), HOUR+timedelta(hours=1, minutes=1)),
                                      (HOUR, HOUR+timedelta(hours=2)),
                                      (HOUR.replace(tzinfo=None), HOUR+timedelta(hours=1))])
def test_backfill_rejects_partial_or_ambiguous_windows(sessions, start, end):
    with sessions() as db, pytest.raises(ValueError):
        backfill_hour_refs(db, start, end)


def test_startup_adds_nullable_bounds_without_scanning_legacy_pins(sessions):
    engine = sessions.kw['bind']
    with sessions.begin() as db:
        key = reading(db)
    with engine.begin() as conn:
        conn.exec_driver_sql('ALTER TABLE decision_telemetry_pins DROP COLUMN last_slot')
        conn.exec_driver_sql('INSERT INTO decision_telemetry_pins(reading_id,first_slot) VALUES (?,?)', (key, 123))
    statements = []
    listener = lambda _conn, _cursor, statement, *_args: statements.append(statement.lower())
    event.listen(engine, 'before_cursor_execute', listener)
    try:
        upgrade_schema(engine)
        upgrade_schema(engine)
    finally:
        event.remove(engine, 'before_cursor_execute', listener)
    assert not any(('update decision_telemetry_pins' in sql or
                    'from decision_telemetry_pins' in sql) for sql in statements)
    with sessions() as db:
        pin = db.get(DecisionTelemetryPin, key)
        assert pin.first_slot == 123 and pin.last_slot is None


def test_prepare_is_readonly_and_commit_never_decompresses_or_parses(sessions, monkeypatch):
    with sessions.begin() as db:
        key = reading(db)
        db.add(DecisionCycleContext(key='context', definition={}))
        decision(db, HOUR+timedelta(seconds=30), key)
        archive(db, HOUR+timedelta(seconds=5), key)
    with sessions() as db:
        statements = []
        listener = lambda _conn, _cursor, statement, *_args: statements.append(statement.lower())
        event.listen(db.bind, 'before_cursor_execute', listener)
        try:
            prepared = prepare_hour_refs(db, HOUR, HOUR+timedelta(hours=1))
        finally:
            event.remove(db.bind, 'before_cursor_execute', listener)
        assert statements and all(sql.lstrip().startswith('select') for sql in statements)
        assert not list(db.scalars(select(DecisionTelemetryPin)))
    with sessions.begin() as db:
        monkeypatch.setattr('app.storage_retention.pins.zlib.decompressobj',
                            lambda *a: pytest.fail('Writer must not decompress an archive'))
        # The writer's ORM may itself parse tiny JSON result values elsewhere;
        # the pin path requests only identity columns and raw snapshot equality.
        result = commit_hour_refs(db, prepared)
        assert result['hot_count'] == 1 and result['archived_count'] == 1
        assert db.get(DecisionTelemetryPin, key).last_slot == int(HOUR.timestamp()//5)+6


@pytest.mark.parametrize('change', ['new-hot', 'delete-hot', 'fingerprint', 'same-fingerprint-snapshot',
                                  'new-archive', 'changed-archive-payload', 'missing-raw'])
def test_changed_source_cannot_commit_stale_pin_bounds(sessions, change):
    with sessions.begin() as db:
        key, other = reading(db), reading(db, 'other')
        db.add(DecisionCycleContext(key='context', definition={}))
        row = decision(db, HOUR+timedelta(seconds=30), key)
        slot = row.slot
        if change == 'changed-archive-payload':
            archive(db, HOUR+timedelta(seconds=5), key)
    with sessions() as db:
        prepared = prepare_hour_refs(db, HOUR, HOUR+timedelta(hours=1))
    with sessions.begin() as db:
        if change == 'new-hot':
            decision(db, HOUR+timedelta(seconds=35), other)
        elif change == 'delete-hot':
            db.execute(delete(DecisionCycleSample).where(DecisionCycleSample.slot == slot))
        elif change == 'fingerprint':
            db.get(DecisionCycleSample, slot).fingerprint = 'changed'
        elif change == 'same-fingerprint-snapshot':
            db.get(DecisionCycleSample, slot).snapshot = {'cells': {'changed': {'$reading': other}}}
        elif change == 'new-archive':
            archive(db, HOUR+timedelta(seconds=5), other)
        elif change == 'changed-archive-payload':
            # Even corruption leaving the declared checksum untouched must block.
            db.get(DecisionEvidenceArchive, iso_time(HOUR)).payload = zlib.compress(b'[]')
        elif change == 'missing-raw':
            db.execute(delete(TelemetryReading).where(TelemetryReading.id == key))
    with sessions.begin() as db:
        with pytest.raises(ValueError):
            commit_hour_refs(db, prepared)
        assert not list(db.scalars(select(DecisionTelemetryPin)))


def test_prepare_commit_preserves_concurrent_newer_pin_use(sessions):
    with sessions.begin() as db:
        key = reading(db)
        db.add(DecisionCycleContext(key='context', definition={}))
        row = decision(db, HOUR+timedelta(seconds=30), key)
        slot = row.slot
    with sessions() as db:
        prepared = prepare_hour_refs(db, HOUR, HOUR+timedelta(hours=1))
    with sessions.begin() as db:
        pin_readings(db, {key}, slot+100000)
    with sessions.begin() as db:
        commit_hour_refs(db, prepared)
        pin = db.get(DecisionTelemetryPin, key)
        assert (pin.first_slot, pin.last_slot) == (slot, slot+100000)
