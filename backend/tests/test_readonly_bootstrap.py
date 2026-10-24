"""The decision workspace boots without loading historical /state or models."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import json
import threading

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, select

from app.gpt_runtime.bootstrap import bootstrap_inputs
from app.models import AutoState, DecisionSourceRevision, Setting
from app.remote_readonly import runtime, startup
from app.services.decision_sources import config_view, identity
from app.services.state import SETTING_TO_STORE
from app.telemetry.domain import iso_time
from app.telemetry.models import TelemetryPoint
from test_decision_sources import seeded
from test_hourly_brief import HOUR, sessions
from test_readonly_runtime import execute_runtime, remote_runtime


def test_finite_settings_preserve_values_and_sources_without_history_queries(sessions, seeded):
    values = [0, None, 'heavy', False, True, {'maxStep': 0, 'deadband': False}, False, 0.0, 'all']
    with sessions.begin() as db:
        for key, value in zip(SETTING_TO_STORE, values):
            db.add(Setting(key=key, value=value))
        db.add(Setting(key='unknown-training-data', value={'rows': ['unneeded']*1000}))
        db.add(AutoState(key='densityDecisionLog', value=['unneeded-history']*1000))
    with sessions() as db:
        expected = config_view(db, when=HOUR)
        statements = []
        def record(_c, _u, statement, _p, _x, _m):
            statements.append(statement)
        event.listen(db.bind, 'before_cursor_execute', record)
        try:
            result = bootstrap_inputs(db, HOUR)
        finally:
            event.remove(db.bind, 'before_cursor_execute', record)
        assert result == {'settings': dict(zip(SETTING_TO_STORE.values(), values)), 'sources': expected}
        assert len(result['settings']) == 9 and len(statements) == 6
        sql = '\n'.join(statements).lower()
        assert all(name not in sql for name in ('auto_state', 'coal_records', 'calc_logs', 'heavy_samples',
            'coarse_models', 'gpt_coarse_revisions', 'telemetry_readings', 'telemetry_sources'))
        point_query = next(statement for statement in statements if 'telemetry_points' in statement)
        # Fixed ItemIDs are now file-catalog references: read only the small config
        # column so its JSON decoder can hydrate the original source identity.
        assert 'telemetry_points.latest' not in point_query and 'telemetry_points.config' in point_query
        assert not db.new and not db.dirty and not db.deleted
        assert 'unneeded' not in json.dumps(result)


@pytest.mark.parametrize('case', ['default', 'explicit', 'average', 'future', 'duplicate', 'disabled'])
def test_source_adapter_matches_complete_original_view_at_same_host_time(sessions, seeded, case):
    cfg, _, _ = seeded
    with sessions.begin() as db:
        if case in ('explicit', 'average', 'future'):
            key = 'a303_a304_density_average' if case == 'average' else 'point3'
            db.add(DecisionSourceRevision(effective_at=iso_time(HOUR-timedelta(seconds=1)),
                config=dict(choices={'density-current': identity(cfg.source_id, key),
                    'heavy-ash': identity('manual-measurements', 'heavy_clean_coal_ash')}, target_min=7.5, target_max=8)))
        if case == 'future':
            db.add(DecisionSourceRevision(effective_at=iso_time(HOUR+timedelta(seconds=1)),
                config=dict(choices={}, target_min=10, target_max=11)))
        if case == 'duplicate':
            point = db.get(TelemetryPoint, (cfg.source_id, 'point4'))
            db.add(TelemetryPoint(source_id='other', key='same-density', config=deepcopy(point.config), enabled=True))
        if case == 'disabled':
            db.get(TelemetryPoint, (cfg.source_id, 'point4')).enabled = False
    with sessions() as db:
        assert bootstrap_inputs(db, HOUR)['sources'] == config_view(db, when=HOUR)


@pytest.mark.parametrize('offset', [timedelta(minutes=-4), timedelta(minutes=-2), timedelta(seconds=1)])
def test_manual_default_choices_keep_original_observation_and_entry_cutoffs(sessions, seeded, offset):
    with sessions() as db:
        observed = HOUR+offset
        assert bootstrap_inputs(db, observed)['sources'] == config_view(db, when=observed)


def builder(monkeypatch, failure=None):
    import app.database
    from app.gpt_runtime import bootstrap as inputs
    events = []
    data = {'settings': {'densityAutoOn': False}, 'sources': {'revision': 0, 'choices': {}}}
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
                raise RuntimeError('private SQL or token')
    db = Database()
    def session():
        assert events[-1] == 'budget-enter'
        events.append('session-create')
        return db
    def clock(database):
        assert database is db
        events.append('host-clock')
        if failure == 'clock':
            raise RuntimeError('private host clock')
        return HOUR
    def read(database, observed):
        assert database is db and observed == HOUR
        events.append('inputs')
        if failure == 'inputs':
            raise RuntimeError('private inputs')
        return data
    monkeypatch.setattr(startup, 'read_window', budget)
    monkeypatch.setattr(app.database, 'SessionLocal', session)
    monkeypatch.setattr(startup, 'host_now', clock)
    monkeypatch.setattr(inputs, 'bootstrap_inputs', read)
    return events, data


def test_bootstrap_one_display_snapshot_uses_host_clock_and_detaches_json(monkeypatch):
    events, data = builder(monkeypatch)
    result = startup._build_bootstrap()
    assert events == ['budget-enter', 'session-create', 'session-enter', 'BEGIN',
        'host-clock', 'inputs', 'session-exit', 'budget-exit']
    assert result['schema_version'] == 1 and result['mode'] == 'readonly-test'
    assert result['generated_at'] == iso_time(HOUR) and isinstance(result['app_version'], str)
    assert set(result) == {'schema_version', 'mode', 'generated_at', 'app_version', 'settings', 'sources'}
    result['settings']['densityAutoOn'] = True
    assert data['settings']['densityAutoOn'] is False


@pytest.mark.parametrize('failure', ['begin', 'clock', 'inputs', 'json'])
def test_bootstrap_failures_release_session_budget_and_do_not_log_secrets(monkeypatch, failure, caplog):
    events, data = builder(monkeypatch, failure)
    if failure == 'json':
        data['settings']['ashTarget'] = float('nan')
    with pytest.raises((RuntimeError, ValueError)):
        startup._build_bootstrap()
    assert events[-2:] == ['session-exit', 'budget-exit']
    assert events.count('session-create') == 1
    assert 'readonly_bootstrap_failed' in caplog.text and 'private' not in caplog.text


def test_three_browser_bootstrap_requests_share_one_producer_without_state(monkeypatch):
    started, release, calls = threading.Event(), threading.Event(), []
    payload = {'schema_version': 1, 'mode': 'readonly-test', 'settings': {}, 'sources': {}}
    def build():
        calls.append(1)
        started.set()
        assert release.wait(3)
        return payload
    cache = runtime.SingleFlightBundle(build, ttl_seconds=1, wait_seconds=8, failure_seconds=1)
    monkeypatch.setattr(startup, '_bootstrap', cache)
    monkeypatch.setattr(startup, 'startup_view', lambda *_: pytest.fail('Core boot entered full state reader'))
    app = FastAPI()
    app.include_router(startup.router, prefix='/api/v1')
    try:
        with TestClient(app) as client, ThreadPoolExecutor(max_workers=3) as readers:
            futures = [readers.submit(client.get, '/api/v1/readonly-runtime/bootstrap') for _ in range(3)]
            assert started.wait(1)
            release.set()
            responses = [future.result(timeout=4) for future in futures]
        assert calls == [1]
        assert all(response.status_code == 200 and response.json() == payload for response in responses)
        first = startup.bootstrap_view()
        first['settings']['changed'] = True
        assert startup.bootstrap_view()['settings'] == {}
    finally:
        release.set()
        cache.close()


def test_expired_bootstrap_failure_is_typed_and_never_returns_old_settings(monkeypatch):
    clock, enabled, calls = [0.0], [True], []
    def build():
        calls.append(1)
        if not enabled[0]:
            raise RuntimeError('private host')
        return {'settings': {'version': len(calls)}, 'sources': {}}
    cache = runtime.SingleFlightBundle(build, ttl_seconds=1, wait_seconds=8, failure_seconds=1, clock=lambda: clock[0])
    monkeypatch.setattr(startup, '_bootstrap', cache)
    try:
        assert startup.bootstrap_view()['settings']['version'] == 1
        clock[0], enabled[0] = 1.01, False
        for _ in range(3):
            with pytest.raises(HTTPException) as caught:
                startup.bootstrap_view()
            assert caught.value.status_code == 503
            assert caught.value.detail['error_code'] == 'readonly_bootstrap_busy'
            assert 'private' not in str(caught.value.detail)
        assert len(calls) == 2
        clock[0], enabled[0] = 2.02, True
        assert startup.bootstrap_view()['settings']['version'] == 3
    finally:
        cache.close()


def test_bootstrap_is_absent_from_production_app():
    from app.main import app
    assert TestClient(app).get('/api/v1/readonly-runtime/bootstrap').status_code == 404


def test_bootstrap_real_readonly_bridge_preserves_host_and_never_creates_local_db(remote_runtime):
    execute_runtime(remote_runtime, """
        with TestClient(app, client=('127.0.0.1', 12345)) as client:
            response = client.get('/api/v1/readonly-runtime/bootstrap')
            assert response.status_code == 200, response.text
            result = response.json()
            assert result['schema_version'] == 1 and result['mode'] == 'readonly-test'
            assert set(result) == {'schema_version','mode','app_version','generated_at','settings','sources'}
            assert result['settings'] == {}
            assert result['sources']['revision'] == 0 and 'resolved_choices' in result['sources']
            before = len(requests)
            assert client.post('/api/v1/readonly-runtime/bootstrap', json={}).status_code == 403
            assert client.put('/api/v1/state?force=true', json={}).status_code == 403
            assert len(requests) == before
        cleanup()
    """)
