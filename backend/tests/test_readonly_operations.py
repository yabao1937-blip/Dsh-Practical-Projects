"""Operational polling shares short host snapshots only on the laptop test server."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import threading

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.remote_readonly import operations, runtime


NOW = datetime(2026, 10, 17, 1, tzinfo=timezone.utc)


class SessionStub:
    def __init__(self):
        self.statements = []
        self.closed = False
        self.rollbacks = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def execute(self, statement):
        self.statements.append(str(statement))

    def connection(self):
        return self

    def rollback(self):
        self.rollbacks += 1


def configured_builder(monkeypatch, *, schema_ready=True):
    import app.database
    from app.routers import health
    from app.services import manual_measurements
    from app.telemetry import repository

    db = SessionStub()
    sessions, calls = [], []
    monkeypatch.setattr(app.database, 'SessionLocal', lambda: sessions.append(db) or db)
    monkeypatch.setattr(operations, 'host_now', lambda _: NOW)
    monkeypatch.setattr(operations, 'inspect', lambda _: SimpleNamespace(
        get_table_names=lambda: list(health.REQUIRED_TABLES) if schema_ready else []))

    def reader(name):
        def read(database, *, now):
            assert database is db and not db.closed
            calls.append((name, now))
            return {'items': [{'value': 0}], 'checked_at': now.isoformat(timespec='milliseconds')}
        return read

    monkeypatch.setattr(repository, 'latest_view', reader('telemetry'))
    monkeypatch.setattr(manual_measurements, 'latest_points', reader('manual_measurements'))
    return db, sessions, calls


def test_builder_uses_one_snapshot_and_host_time_for_all_sections(monkeypatch):
    db, sessions, calls = configured_builder(monkeypatch)
    result = operations._build_operations()
    assert sessions == [db] and db.closed and db.rollbacks == 0
    assert db.statements == ['BEGIN', 'SELECT 1']
    assert calls == [('telemetry', NOW), ('manual_measurements', NOW)]
    assert result['schema_version'] == 1 and result['health']['ok'] is True
    assert result['errors'] == {}
    assert result['generated_at'] == result['health']['checked_at']
    assert datetime.fromisoformat(result['generated_at'].replace('Z', '+00:00')) == NOW
    assert json.loads(json.dumps(result, allow_nan=False)) == result


@pytest.mark.parametrize('failure', ['conversion', 'query'])
def test_bad_telemetry_keeps_healthy_database_and_reads_manual_in_new_snapshot(monkeypatch, failure):
    from app.services import manual_measurements
    from app.telemetry import repository

    db, _, _ = configured_builder(monkeypatch)
    clocks = iter((NOW, NOW + timedelta(seconds=1)))
    monkeypatch.setattr(operations, 'host_now', lambda _: next(clocks))
    reads = []

    def telemetry(database, *, now):
        reads.append('telemetry')
        if failure == 'query':
            raise RuntimeError('private SQL or token')
        return {'items': [{'value': float('nan')}]}

    def manual(database, *, now):
        reads.append('manual')
        assert db.rollbacks == 1
        assert now == NOW + timedelta(seconds=1)
        return {'checked_at': now.isoformat(), 'items': []}

    monkeypatch.setattr(repository, 'latest_view', telemetry)
    monkeypatch.setattr(manual_measurements, 'latest_points', manual)
    result = operations._build_operations()
    assert reads == ['telemetry', 'manual']
    assert db.closed and db.statements == ['BEGIN', 'SELECT 1', 'BEGIN']
    assert result['health']['ok'] is True and result['telemetry'] is None
    assert result['errors'] == {'telemetry': {'error_code': 'readonly_telemetry_unavailable'}}
    assert 'private' not in json.dumps(result)
    assert result['generated_at'] != result['manual_measurements']['checked_at']


def test_health_schema_failure_preserves_other_sections(monkeypatch):
    db, _, calls = configured_builder(monkeypatch, schema_ready=False)
    result = operations._build_operations()
    assert result['health']['ok'] is False
    assert result['health']['checks']['database']['error_code'] == 'schema_not_ready'
    assert len(calls) == 2 and db.closed


def test_health_query_failure_releases_failed_snapshot_and_reads_other_sections(monkeypatch):
    db, _, calls = configured_builder(monkeypatch)
    monkeypatch.setattr(operations, 'inspect', lambda _: (_ for _ in ()).throw(RuntimeError('private')))
    result = operations._build_operations()
    assert result['health']['ok'] is False
    assert result['health']['checks']['database']['error_code'] == 'database_unavailable'
    assert result['errors']['health']['error_code'] == 'readonly_health_unavailable'
    assert len(calls) == 2 and db.rollbacks == 1 and db.closed


def test_clock_failure_closes_session_and_never_attempts_sections(monkeypatch, caplog):
    db, _, calls = configured_builder(monkeypatch)
    monkeypatch.setattr(operations, 'host_now', lambda _: (_ for _ in ()).throw(RuntimeError('clock failed')))
    with pytest.raises(RuntimeError, match='clock failed'):
        operations._build_operations()
    assert db.closed and calls == []
    assert 'section=snapshot' in caplog.text
    assert 'clock failed' not in caplog.text


def test_manual_cutoff_uses_supplied_host_time_and_default_stays_unchanged(monkeypatch):
    from app.models import ManualMeasurement
    from app.services import manual_measurements

    engine = create_engine('sqlite://')
    ManualMeasurement.__table__.create(engine)
    earlier = NOW - timedelta(seconds=1)
    later = NOW + timedelta(seconds=1)
    with Session(engine) as db:
        for value, stamp in ((8.1, earlier), (9.9, later)):
            db.add(ManualMeasurement(metric='heavy_clean_coal_ash', name='重介精煤灰分', unit='%',
                                    value=value, observed_at=stamp.isoformat(timespec='milliseconds'),
                                    created_at=stamp.isoformat(timespec='milliseconds'), time_basis='production-time-v1',
                                    request_hash=str(value)))
        db.commit()
        monkeypatch.setattr(manual_measurements, 'timestamp_now', lambda: later.isoformat(timespec='milliseconds'))
        host = manual_measurements.latest_points(db, now=NOW)
        normal = manual_measurements.latest_points(db)
    assert host['items'][0]['sample']['value'] == 8.1
    assert normal['items'][0]['sample']['value'] == 9.9
    assert host['checked_at'] == NOW.isoformat(timespec='milliseconds')
    engine.dispose()


def test_three_browsers_and_compatibility_routes_share_one_producer_without_request_checkout(monkeypatch):
    from app.routers import health, telemetry, manual_measurements

    class UnusedSession:
        @property
        def bind(self):
            pytest.fail('A cache waiter checked out its request database session')

    def unused():
        yield UnusedSession()

    started, release = threading.Event(), threading.Event()
    producers = []

    def build():
        producers.append(1)
        started.set()
        assert release.wait(3)
        return {'schema_version': 1, 'generated_at': 'host-time', 'health': {'ok': True, 'status': 'ok'},
                'telemetry': {'items': [{'value': 0}]}, 'manual_measurements': {'items': []}, 'errors': {}}

    cache = runtime.SingleFlightBundle(build, ttl_seconds=1, wait_seconds=4, failure_seconds=1)
    monkeypatch.setattr(operations, '_operations', cache)
    for module in (operations, health, telemetry, manual_measurements):
        monkeypatch.setattr(module, 'REMOTE_READONLY', {'server_url': 'http://isolated'})
    app = FastAPI()
    for module in (operations, health, telemetry, manual_measurements):
        app.include_router(module.router, prefix='/api/v1')
    app.dependency_overrides[telemetry.get_db] = unused
    app.dependency_overrides[manual_measurements.get_db] = unused
    paths = ['/api/v1/readonly-runtime/operations', '/api/v1/health',
             '/api/v1/telemetry/latest', '/api/v1/manual-measurements/latest'] * 3
    try:
        with TestClient(app) as client, ThreadPoolExecutor(max_workers=12) as readers:
            futures = [readers.submit(client.get, path) for path in paths]
            assert started.wait(1)
            release.set()
            responses = [future.result(timeout=4) for future in futures]
        assert len(producers) == 1
        assert all(response.status_code == 200 for response in responses)
        first = responses[0].json()
        first['telemetry']['items'][0]['value'] = 999
        assert operations.operations_view('telemetry')['items'][0]['value'] == 0
    finally:
        release.set()
        cache.close()


def test_expired_operations_failure_returns_503_without_old_success_and_recovers(monkeypatch):
    clock, available, calls = [0.0], [True], []

    def build():
        calls.append(1)
        if not available[0]:
            raise RuntimeError('private token')
        return {'generated_at': len(calls), 'health': {'ok': True}}

    cache = runtime.SingleFlightBundle(build, ttl_seconds=1, failure_seconds=1, clock=lambda: clock[0])
    monkeypatch.setattr(operations, '_operations', cache)
    try:
        assert operations.operations_view()['generated_at'] == 1
        clock[0], available[0] = 1.01, False
        for _ in range(2):
            with pytest.raises(HTTPException) as caught:
                operations.operations_view()
            assert caught.value.status_code == 503
            assert caught.value.detail['error_code'] == 'readonly_operations_busy'
            assert 'private' not in str(caught.value.detail)
        assert len(calls) == 2
        clock[0], available[0] = 2.02, True
        assert operations.operations_view()['generated_at'] == 3
    finally:
        cache.close()


def test_partial_projection_is_503_but_health_route_keeps_independent_status(monkeypatch):
    from app.routers import health

    data = {'health': {'ok': True, 'status': 'ok'}, 'telemetry': None,
            'errors': {'telemetry': {'error_code': 'readonly_telemetry_unavailable'}}}
    monkeypatch.setattr(operations, '_operations', SimpleNamespace(get=lambda: data))
    monkeypatch.setattr(health, 'REMOTE_READONLY', {'server_url': 'http://isolated'})
    with pytest.raises(HTTPException) as caught:
        operations.operations_view('telemetry')
    assert caught.value.status_code == 503
    assert health.health().status_code == 200
    data['health'] = {'ok': False, 'status': 'unavailable'}
    assert health.health().status_code == 503


def test_initial_failure_preserves_compatibility_health_schema_without_retry(monkeypatch):
    from app.routers import health

    producers = []

    def unavailable():
        producers.append(1)
        raise RuntimeError('private host failure')

    cache = runtime.SingleFlightBundle(unavailable, ttl_seconds=1, failure_seconds=1, clock=lambda: 0)
    monkeypatch.setattr(operations, '_operations', cache)
    monkeypatch.setattr(health, 'REMOTE_READONLY', {'server_url': 'http://isolated'})
    monkeypatch.setattr(health, 'engine', SimpleNamespace(connect=lambda: pytest.fail('Health retried database read')))
    app = FastAPI()
    app.include_router(health.router, prefix='/api/v1')
    try:
        with TestClient(app) as client:
            for path in ('/api/v1/health', '/api/v1/health/ready'):
                response = client.get(path)
                assert response.status_code == 503
                payload = response.json()
                assert set(payload) == {'status', 'modules', 'version', 'checked_at', 'uptime_seconds', 'checks'}
                assert payload['status'] == 'unavailable'
                assert payload['checks']['database'] == {'status': 'unavailable', 'error_code': 'database_unavailable'}
                assert payload['modules'] == ['coal-records', 'telemetry']
                assert datetime.fromisoformat(payload['checked_at'].replace('Z', '+00:00')).tzinfo is not None
                assert 'private' not in response.text
        assert producers == [1]
    finally:
        cache.close()


def test_production_and_explicit_source_keep_original_paths(monkeypatch):
    from app.routers import telemetry, manual_measurements

    class Database:
        bind = SimpleNamespace(dialect=SimpleNamespace(name='sqlite'))

        def __init__(self):
            self.sql = []

        def execute(self, statement):
            self.sql.append(str(statement))

    db = Database()
    monkeypatch.setattr(operations, 'operations_view', lambda *_: pytest.fail('Unexpected readonly cache'))
    monkeypatch.setattr(telemetry, 'REMOTE_READONLY', None)
    monkeypatch.setattr(manual_measurements, 'REMOTE_READONLY', None)
    monkeypatch.setattr(telemetry, 'latest_view', lambda database, source_id=None, **kw: {'source_id': source_id, **kw})
    monkeypatch.setattr(manual_measurements, 'latest_points', lambda database: {'normal': database is db})
    assert telemetry.latest(None, db) == {'source_id': None}
    assert manual_measurements.latest_measurements(db) == {'normal': True}
    monkeypatch.setattr(telemetry, 'REMOTE_READONLY', {'server_url': 'http://isolated'})
    monkeypatch.setattr(runtime, 'host_now', lambda _: NOW)
    assert telemetry.latest('selected', db) == {'source_id': 'selected', 'now': NOW}
    assert db.sql == ['BEGIN', 'BEGIN', 'BEGIN']
    monkeypatch.setattr(operations, 'REMOTE_READONLY', None)
    with pytest.raises(HTTPException) as caught:
        operations.operations()
    assert caught.value.status_code == 404
