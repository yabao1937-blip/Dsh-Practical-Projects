"""Cold browser startup readers share one detached, bounded host snapshot."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from types import SimpleNamespace
import threading

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from app.remote_readonly import runtime, startup


def builder_stubs(monkeypatch, failure=None):
    import app.database
    from app.gpt_coarse import repository
    from app.services import decision_sources, state

    events, data = [], {'records': [{'value': 0}], 'model': None, 'sources': {'choices': {}}}

    @contextmanager
    def budget(kind):
        assert kind == 'display'
        events.append('budget-enter')
        try:
            yield
        finally:
            events.append('budget-exit')

    class Database:
        def __enter__(self):
            events.append('session-enter')
            return self

        def __exit__(self, *_):
            events.append('session-exit')

        def execute(self, statement):
            events.append(str(statement))
            if failure == 'begin':
                raise RuntimeError('private begin')

    db = Database()

    def session():
        assert events[-1] == 'budget-enter'
        events.append('session-create')
        return db

    def read(name, result):
        def reader(database, *args, **kwargs):
            assert database is db and 'session-exit' not in events
            assert args == ()
            assert kwargs == ({'include_analysis': True} if name == 'model' else {})
            events.append(name)
            if failure == name:
                raise RuntimeError('private SQL or token')
            return result
        return reader

    monkeypatch.setattr(startup, 'read_window', budget)
    monkeypatch.setattr(app.database, 'SessionLocal', session)
    monkeypatch.setattr(state, 'load_store', read('state', data['records']))
    monkeypatch.setattr(repository, 'active', read('model', data['model']))
    monkeypatch.setattr(decision_sources, 'config_view', read('sources', data['sources']))
    return events, data


def test_startup_uses_display_budget_before_one_session_and_keeps_none_time_semantics(monkeypatch):
    events, data = builder_stubs(monkeypatch)
    result = startup._build_startup()
    assert events == ['budget-enter', 'session-create', 'session-enter', 'BEGIN',
                      'state', 'model', 'sources', 'session-exit', 'budget-exit']
    assert result == {'state': data['records'], 'model': {'model': None}, 'sources': data['sources']}
    result['state'][0]['value'] = 99
    assert data['records'][0]['value'] == 0
    assert json.loads(json.dumps(result, allow_nan=False)) == result


@pytest.mark.parametrize('failure', ['begin', 'state', 'model', 'sources'])
def test_startup_failure_closes_session_and_releases_budget(monkeypatch, failure, caplog):
    events, _ = builder_stubs(monkeypatch, failure)
    with pytest.raises(RuntimeError):
        startup._build_startup()
    assert events[-2:] == ['session-exit', 'budget-exit']
    assert events.count('session-create') == 1
    assert 'readonly_startup_failed' in caplog.text
    assert 'private' not in caplog.text


def test_startup_rejects_non_json_section_instead_of_publishing_partial_result(monkeypatch):
    events, data = builder_stubs(monkeypatch)
    data['records'][0]['value'] = float('nan')
    with pytest.raises(ValueError):
        startup._build_startup()
    assert events[-2:] == ['session-exit', 'budget-exit']


def test_three_browser_startup_routes_share_one_producer_and_waiters_never_checkout(monkeypatch):
    from app.gpt_coarse import router as models
    from app.routers import decision, state

    class UnusedDatabase:
        @property
        def bind(self):
            pytest.fail('Startup waiter attempted a database checkout')

    release, started = threading.Event(), threading.Event()
    calls = []
    payload = {'state': {'coarseCoal': []}, 'model': {'model': None}, 'sources': {'revision': 7}}

    def build():
        calls.append(1)
        started.set()
        assert release.wait(3)
        return payload

    cache = runtime.SingleFlightBundle(build, ttl_seconds=1, wait_seconds=8, failure_seconds=1)
    monkeypatch.setattr(startup, '_startup', cache)
    app = FastAPI()
    for module in (state, models, decision):
        monkeypatch.setattr(module, 'REMOTE_READONLY', {'server_url': 'http://isolated-host'})
        app.include_router(module.router, prefix='/api/v1')
        app.dependency_overrides[module.get_db] = lambda: UnusedDatabase()
    paths = ['/api/v1/state', '/api/v1/gpt-coarse/current', '/api/v1/decision/sources'] * 3
    try:
        with TestClient(app) as client, ThreadPoolExecutor(max_workers=9) as readers:
            futures = [readers.submit(client.get, path) for path in paths]
            assert started.wait(1)
            release.set()
            responses = [future.result(timeout=4) for future in futures]
        assert calls == [1]
        assert all(response.status_code == 200 for response in responses)
        assert [response.json() for response in responses[:3]] == [payload[part] for part in ('state', 'model', 'sources')]
        assert [response.json() for response in responses[:3]] == [response.json() for response in responses[3:6]]
        result = startup.startup_view('sources')
        result['revision'] = 99
        assert startup.startup_view('sources')['revision'] == 7
    finally:
        release.set()
        cache.close()


def test_failed_expired_startup_never_falls_back_to_old_json_and_recovers(monkeypatch):
    clock, enabled, calls = [0.0], [True], []

    def build():
        calls.append(1)
        if not enabled[0]:
            raise RuntimeError('private host')
        return {'state': {'revision': len(calls)}, 'model': {'model': None}, 'sources': {}}

    cache = runtime.SingleFlightBundle(build, ttl_seconds=1, wait_seconds=8, failure_seconds=1, clock=lambda: clock[0])
    monkeypatch.setattr(startup, '_startup', cache)
    try:
        assert startup.startup_view('state')['revision'] == 1
        clock[0], enabled[0] = 1.01, False
        for _ in range(3):
            with pytest.raises(HTTPException) as caught:
                startup.startup_view('model')
            assert caught.value.status_code == 503
            assert caught.value.detail['error_code'] == 'readonly_startup_busy'
            assert 'private' not in str(caught.value.detail)
        assert len(calls) == 2
        clock[0], enabled[0] = 2.02, True
        assert startup.startup_view('state')['revision'] == 3
    finally:
        cache.close()


def test_production_startup_routes_keep_original_uncached_readers(monkeypatch):
    from app.gpt_coarse import router as models
    from app.routers import decision, state

    class Database:
        bind = SimpleNamespace(dialect=SimpleNamespace(name='sqlite'))

        def __init__(self):
            self.statements = []

        def execute(self, statement):
            self.statements.append(str(statement))

    db, calls = Database(), []
    monkeypatch.setattr(startup, 'startup_view', lambda _: pytest.fail('Production entered readonly startup cache'))
    for module in (state, models, decision):
        monkeypatch.setattr(module, 'REMOTE_READONLY', None)
    monkeypatch.setattr(state, 'load_store', lambda database: calls.append('state') or {'state': database is db})
    monkeypatch.setattr(models, 'active', lambda database, *, include_analysis: calls.append('model') or None)
    monkeypatch.setattr(decision, 'config_view', lambda database: calls.append('sources') or {'sources': database is db})
    assert state.get_state(db) == {'state': True}
    assert models.current(db) == {'model': None}
    assert decision.sources(db) == {'sources': True}
    assert calls == ['state', 'model', 'sources']
    assert db.statements == ['BEGIN']
