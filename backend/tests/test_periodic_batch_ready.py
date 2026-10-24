"""Durable minute gates must read a time range, never a source's full history."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, insert
from sqlalchemy.orm import Session

from app.decision_hourly.service import periodic_batch_ready
from app.telemetry.domain import iso_time
from app.telemetry.models import TelemetryReading


NOW = datetime(2026, 10, 24, 1, 25, 20, tzinfo=timezone.utc)


@pytest.fixture
def db():
    engine = create_engine('sqlite://')
    TelemetryReading.__table__.create(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def add_reading(db, source='one', *, at=None, cadence=60, kind='periodic', **sample):
    row = TelemetryReading(source_id=source, point_key='point',
        batch_id=f'{source}-{db.info.get("batch", 0)}', received_at=iso_time(at or NOW),
        sample=dict(storage_kind=kind, cadence_seconds=cadence, **sample))
    db.info['batch'] = db.info.get('batch', 0) + 1
    db.add(row)
    db.flush()


@pytest.mark.parametrize('cadence', [30, 60])
def test_every_required_source_needs_a_durable_batch_in_this_bucket(db, cadence):
    add_reading(db, 'one', cadence=cadence)
    add_reading(db, 'unrelated', cadence=cadence)
    assert not periodic_batch_ready(db, NOW, {'one', 'two'}, cadence_seconds=cadence)
    assert not periodic_batch_ready(db, NOW, {'missing'}, cadence_seconds=cadence)
    assert periodic_batch_ready(db, NOW, cadence_seconds=cadence)
    assert periodic_batch_ready(db, NOW, set(), cadence_seconds=cadence)

    # Bad-quality periodic batches must still be archived, so their invalid
    # cells remain visible to coverage accounting instead of disappearing.
    add_reading(db, 'two', cadence=cadence, quality='bad', value=None, status_code=0x80000000)
    assert periodic_batch_ready(db, NOW, {'one', 'two'}, cadence_seconds=cadence)


@pytest.mark.parametrize('kwargs', [
    dict(kind='quality-event'),
    dict(kind='alignment-evidence', cadence=5),
    dict(runtime_only=True),
    dict(cadence=30),
    dict(at=NOW.replace(second=0) - timedelta(milliseconds=1)),
    dict(at=NOW + timedelta(milliseconds=1)),
])
def test_events_runtime_samples_wrong_cadence_and_other_times_cannot_unlock_bucket(db, kwargs):
    add_reading(db, 'required', **kwargs)
    add_reading(db, 'unrelated')
    assert not periodic_batch_ready(db, NOW, {'required'}, cadence_seconds=60)


def test_durable_bucket_includes_start_and_now_boundaries(db):
    add_reading(db, 'start', at=NOW.replace(second=0))
    add_reading(db, 'now', at=NOW)
    assert periodic_batch_ready(db, NOW, {'start', 'now'}, cadence_seconds=60)


def test_source_gate_uses_received_at_range_without_scanning_old_rows(db):
    # The old source_id IN predicate picked ix_telemetry_point_time(source_id=?)
    # and visited all of these rows, despite its received_at WHERE conditions.
    old = iso_time(NOW - timedelta(days=2))
    db.execute(insert(TelemetryReading), [dict(source_id='required',
        point_key=f'old-{i}', batch_id=f'old-{i}', received_at=old,
        sample=dict(storage_kind='periodic', cadence_seconds=30)) for i in range(6000)])
    add_reading(db, 'required')
    db.commit()

    statements = []
    def capture(_conn, _cursor, statement, parameters, _context, _many):
        if statement.lstrip().upper().startswith('SELECT'):
            statements.append((statement, parameters))

    engine = db.get_bind()
    raw = db.connection().connection.driver_connection
    steps = 0
    def bounded_work():
        nonlocal steps
        steps += 100
        return int(steps > 2000)

    event.listen(engine, 'before_cursor_execute', capture)
    raw.set_progress_handler(bounded_work, 100)
    try:
        assert periodic_batch_ready(db, NOW, {'required'}, cadence_seconds=60)
    finally:
        raw.set_progress_handler(None, 0)
        event.remove(engine, 'before_cursor_execute', capture)

    assert len(statements) == 1
    statement, parameters = statements[0]
    plan = db.connection().exec_driver_sql('EXPLAIN QUERY PLAN ' + statement, parameters).all()
    details = ' '.join(row[3] for row in plan)
    assert 'ix_telemetry_retention' in details, details
    assert 'received_at>?' in details and 'received_at<?' in details, details
    assert steps <= 2000
