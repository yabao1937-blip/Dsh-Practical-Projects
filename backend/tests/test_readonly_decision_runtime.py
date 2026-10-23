"""Only the laptop's read-only routes coalesce decisions in one host snapshot."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import threading

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from app.remote_readonly import runtime


def test_concurrent_current_hourly_readers_share_one_producer_and_detached_json():
    started, release = threading.Event(), threading.Event()
    producers = []

    def build():
        producers.append(threading.get_ident())
        started.set()
        assert release.wait(2)
        return {'current': {'generated_at': 'host-time'}, 'hourly': {'checked_at': 'host-time'}}

    cache = runtime.SingleFlightBundle(build)
    try:
        with ThreadPoolExecutor(max_workers=6) as readers:
            futures = [readers.submit(cache.get) for _ in range(6)]
            assert started.wait(1)
            assert len(producers) == 1
            release.set()
            results = [future.result(timeout=2) for future in futures]
        assert len(producers) == 1
        assert all(value == results[0] for value in results)
        results[0]['current']['generated_at'] = 'mutated by caller'
        assert cache.get()['current']['generated_at'] == 'host-time'
        assert len(producers) == 1
    finally:
        release.set()
        cache.close()


def test_expired_cache_failure_cools_down_without_returning_old_result():
    clock = [0.0]
    calls = []

    def build():
        calls.append(clock[0])
        if len(calls) > 1:
            raise RuntimeError('private host failure')
        return {'current': {'generated_at': 'original'}, 'hourly': {}}

    cache = runtime.SingleFlightBundle(build, clock=lambda: clock[0])
    try:
        assert cache.get()['current']['generated_at'] == 'original'
        clock[0] = 6
        with pytest.raises(runtime.ReadOnlyRuntimeUnavailable):
            cache.get()
        for _ in range(3):
            with pytest.raises(runtime.ReadOnlyRuntimeUnavailable):
                cache.get()
        assert len(calls) == 2
        clock[0] = 12
        with pytest.raises(runtime.ReadOnlyRuntimeUnavailable):
            cache.get()
        assert len(calls) == 3
    finally:
        cache.close()


def test_wait_timeout_does_not_start_a_second_producer_or_leak_private_error():
    started, release = threading.Event(), threading.Event()
    calls = []

    def build():
        calls.append(1)
        started.set()
        assert release.wait(2)
        return {'current': {}, 'hourly': {}}

    cache = runtime.SingleFlightBundle(build, wait_seconds=.03)
    try:
        with pytest.raises(runtime.ReadOnlyRuntimeUnavailable, match='still busy'):
            cache.get()
        assert started.is_set()
        with pytest.raises(runtime.ReadOnlyRuntimeUnavailable):
            cache.get()
        assert calls == [1]
    finally:
        release.set()
        cache.close()


class SessionStub:
    def __init__(self):
        self.statements = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def execute(self, sql):
        self.statements.append(str(sql))
        return self

    def scalar_one(self):
        return '2026-10-17T01:00:00.123Z'


def test_bundle_uses_one_transaction_host_clock_and_passes_current_to_hourly(monkeypatch):
    import app.database
    import app.services.decision_sources
    import app.decision_hourly.service
    from app.gpt_runtime.decision_store import load_decision_store

    db = SessionStub()
    sessions, calls = [], []

    def session_factory():
        sessions.append(db)
        return db

    def current(database, *, when, live, store_loader, realtime):
        assert database is db and live is True and not db.closed
        assert realtime is True
        assert store_loader is load_decision_store
        assert when == datetime(2026, 10, 17, 1, 0, 0, 123000, tzinfo=timezone.utc)
        calls.append(('current', when))
        return {'generated_at': when.isoformat(), 'policy': 'local-code'}

    def hourly(database, *, now, live, defer_missing, store_loader, settlement_reader):
        assert database is db and not db.closed and defer_missing is True
        assert store_loader is load_decision_store
        assert settlement_reader == runtime.settlement_replay().get
        assert live == {'generated_at': now.isoformat(), 'policy': 'local-code'}
        calls.append(('hourly', now))
        return {'checked_at': now.isoformat()}

    monkeypatch.setattr(app.database, 'SessionLocal', session_factory)
    monkeypatch.setattr(app.services.decision_sources, 'shared_snapshot', current)
    monkeypatch.setattr(app.decision_hourly.service, 'read_hour', hourly)
    result = runtime._build_bundle()
    assert len(sessions) == 1 and db.closed
    assert db.statements[0] == 'BEGIN'
    assert db.statements[1] == "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')"
    assert [name for name, _ in calls] == ['current', 'hourly']
    assert result['current']['generated_at'] == result['hourly']['checked_at']


@pytest.mark.parametrize('failure_at', ['clock', 'current', 'hourly'])
def test_bundle_always_closes_transaction_on_failure(monkeypatch, failure_at):
    import app.database
    import app.services.decision_sources
    import app.decision_hourly.service

    db = SessionStub()
    monkeypatch.setattr(app.database, 'SessionLocal', lambda: db)

    def fail(*args, **kwargs):
        raise RuntimeError('isolated failure')

    monkeypatch.setattr(runtime, 'host_now', fail if failure_at == 'clock' else lambda _: datetime.now(timezone.utc))
    monkeypatch.setattr(app.services.decision_sources, 'shared_snapshot', fail if failure_at == 'current' else lambda *a, **k: {})
    monkeypatch.setattr(app.decision_hourly.service, 'read_hour', fail if failure_at == 'hourly' else lambda *a, **k: {})
    with pytest.raises(RuntimeError, match='isolated failure'):
        runtime._build_bundle()
    assert db.closed


def test_readonly_routes_enter_cache_before_request_session_checkout(monkeypatch):
    from app.routers import decision

    class UnusedSession:
        @property
        def bind(self):
            pytest.fail('Waiting readonly HTTP request acquired a database connection')

    def dependency():
        yield UnusedSession()

    monkeypatch.setattr(decision, 'REMOTE_READONLY', {'server_url': 'http://isolated-host'})
    calls = []
    monkeypatch.setattr(runtime, 'decision_view', lambda kind: calls.append(kind) or {'kind': kind})
    application = FastAPI()
    application.include_router(decision.router, prefix='/api/v1')
    application.dependency_overrides[decision.get_db] = dependency
    with TestClient(application) as client:
        assert client.get('/api/v1/decision/current').json() == {'kind': 'current'}
        assert client.get('/api/v1/decision/hourly').json() == {'kind': 'hourly'}
    assert calls == ['current', 'hourly']


def test_production_routes_keep_original_uncached_execution(monkeypatch):
    from app.routers import decision
    import app.decision_hourly.service

    class ProductionSession:
        class bind:
            class dialect:
                name = 'sqlite'

        def __init__(self):
            self.statements = []

        def execute(self, sql):
            self.statements.append(str(sql))

    db = ProductionSession()
    monkeypatch.setattr(decision, 'REMOTE_READONLY', None)
    monkeypatch.setattr(runtime, 'decision_view', lambda _: pytest.fail('Production route used readonly cache'))
    monkeypatch.setattr(decision, 'shared_snapshot', lambda current, *, realtime: {'current': current is db, 'realtime': realtime})
    monkeypatch.setattr(app.decision_hourly.service, 'read_hour', lambda current, **kwargs: {
        'hourly': current is db, **kwargs})
    assert decision.current_decision(db) == {'current': True, 'realtime': True}
    assert decision.hourly_decision(db) == {'hourly': True, 'defer_missing': True}
    assert db.statements == ['BEGIN', 'BEGIN']


def test_public_unavailable_response_is_503_and_never_echoes_private_exception(monkeypatch):
    class Failed:
        def get(self):
            raise runtime.ReadOnlyRuntimeUnavailable('secret token and SQL')

    monkeypatch.setattr(runtime, '_decisions', Failed())
    with pytest.raises(HTTPException) as caught:
        runtime.decision_view('current')
    assert caught.value.status_code == 503
    assert caught.value.detail['error_code'] == 'readonly_decision_busy'
    assert 'secret' not in str(caught.value.detail)


@pytest.mark.parametrize('explicit_when,override,expected', [
    (True, None, False), (False, None, True), (True, True, True), (False, False, False),
])
def test_shared_snapshot_live_override_preserves_all_default_call_behaviour(monkeypatch, explicit_when, override, expected):
    from app.services import decision_sources

    observed = []

    class StopAfterInputs(Exception):
        pass

    def inputs(db, when, *, live, authoritative, realtime):
        assert realtime is False  # Stored/replayed decisions do not consume IPC implicitly.
        observed.append((live, authoritative))
        raise StopAfterInputs()

    monkeypatch.setattr(decision_sources, 'Inputs', inputs)
    now = datetime(2026, 10, 17, 1, tzinfo=timezone.utc) if explicit_when else None
    with pytest.raises(StopAfterInputs):
        decision_sources.shared_snapshot(object(), now, live=override)
    assert observed == [(expected, True)]
