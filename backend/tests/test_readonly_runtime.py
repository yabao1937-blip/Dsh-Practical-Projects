"""Local application code reads a temporary host WAL database without writers.

Every application import happens in a new process with an explicit temporary
database URL.  These tests never open the workstation or production database.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


BACKEND = Path(__file__).resolve().parents[1]
TOKEN = "isolated-runtime-read-token-" + "x" * 40


def isolated_environment(database):
    environment = os.environ.copy()
    for key in tuple(environment):
        if key.startswith("DMCS_"):
            environment.pop(key)
    environment.update(
        DMCS_DATABASE_URL="sqlite:///" + database.as_posix(),
        DMCS_ISOLATED_TEST_DB="1",
        DMCS_WRITE_TOKEN="off",
        PYTHONUTF8="1",
    )
    return environment


def run_isolated(code, environment, *, timeout=45):
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", textwrap.dedent(code)],
        cwd=BACKEND,
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return completed.stdout


@pytest.fixture
def remote_runtime(tmp_path):
    host_database = tmp_path / "temporary-production.db"
    run_isolated(
        """
        from app.main import app
        from app.database import engine
        with engine.begin() as connection:
            connection.exec_driver_sql('CREATE TABLE runtime_probe(value TEXT)')
            connection.exec_driver_sql("INSERT INTO runtime_probe VALUES ('host data')")
        engine.dispose()
        """,
        isolated_environment(host_database),
    )
    config_path = tmp_path / "readonly.json"
    config_path.write_text(json.dumps({
        "mode": "readonly-test",
        "server_url": "http://isolated-host.example:8000/",
        "token": TOKEN,
    }), encoding="utf-8")
    local_database = tmp_path / "forbidden-local-production.db"
    environment = isolated_environment(local_database)
    environment["DMCS_REMOTE_READONLY_CONFIG"] = str(config_path)
    environment["TEST_RUNTIME_HOST_DATABASE"] = str(host_database)
    environment["TEST_RUNTIME_ROOT"] = str(tmp_path)
    # Even a localhost write exemption or explicit write token must not enable
    # test-server writes or leak credentials into the HTML.
    environment["DMCS_ALLOW_LOCALHOST_WRITES"] = "1"
    environment["DMCS_WRITE_TOKEN"] = "write-token-must-not-be-used"
    return environment, local_database


RUNTIME_SETUP = r'''
import io
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
import sqlite3
from urllib.error import HTTPError
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient

host_database = Path(os.environ['TEST_RUNTIME_HOST_DATABASE']).resolve()
root = Path(os.environ['TEST_RUNTIME_ROOT'])
real_connect = sqlite3.connect
writer = real_connect(host_database)
writer.execute('PRAGMA journal_mode=WAL')
writer.commit()
before_changes = writer.total_changes

def checked_connect(database, *args, **kwargs):
    candidate = str(database)
    if candidate.startswith('file:'):
        from urllib.request import url2pathname
        candidate = url2pathname(urlsplit(candidate).path)
    assert Path(candidate).resolve() == host_database, 'Local SQLite fallback attempted: ' + candidate
    return real_connect(database, *args, **kwargs)

sqlite3.connect = checked_connect
sqlite3.dbapi2.connect = checked_connect

from app.remote_readonly.server import ReadOnlyManager, create_router
manager = ReadOnlyManager(host_database, {
    'token': json.loads(Path(os.environ['DMCS_REMOTE_READONLY_CONFIG']).read_text())['token'],
    'clients': ['127.0.0.1'],
})
transport_app = FastAPI()
transport_app.include_router(create_router(manager), prefix='/api/v1')
host_client = TestClient(transport_app, client=('127.0.0.1', 12345))
host_client.__enter__()
requests = []

class ResponseBytes(io.BytesIO):
    def __init__(self, response):
        super().__init__(response.content)
        self.status = response.status_code

def opener(request, timeout):
    requests.append((request.method, request.full_url))
    response = host_client.request(request.method, urlsplit(request.full_url).path,
        content=request.data, headers=dict(request.header_items()))
    if response.status_code >= 400:
        raise HTTPError(request.full_url, response.status_code, 'isolated response',
            response.headers, io.BytesIO(response.content))
    return ResponseBytes(response)

import remote_sqlite
real_remote_connect = remote_sqlite.connect
remote_sqlite.connect = lambda configuration: real_remote_connect(configuration, opener=opener)

def forbidden_writer(*args, **kwargs):
    raise AssertionError('Read-only app attempted schema initialization or a background writer')

from sqlalchemy.sql.schema import MetaData
MetaData.create_all = forbidden_writer
import app.schema_upgrade
import app.telemetry.views
import app.hourly_brief.worker
app.schema_upgrade.upgrade_schema = forbidden_writer
app.telemetry.views.ensure_sqlite_views = forbidden_writer

@asynccontextmanager
async def forbidden_lifespan(app):
    forbidden_writer()
    yield

app.hourly_brief.worker.lifespan = forbidden_lifespan

real_write_text = Path.write_text
def checked_write_text(path, *args, **kwargs):
    assert path.name != 'write_token.txt', 'Read-only auth generated a production write token'
    return real_write_text(path, *args, **kwargs)
Path.write_text = checked_write_text

import app.config
frontend = root / 'frontend'
frontend.mkdir()
(frontend/'index.html').write_text('<html><head></head><body>LOCAL FRONTEND CODE</body></html>')
app.config.FRONTEND_DIR = frontend
from app.main import app
from app.database import engine

def cleanup():
    engine.dispose()
    host_client.__exit__(None, None, None)
    manager.close()
    assert not manager._transactions
    assert writer.total_changes == before_changes
    assert writer.execute('SELECT value FROM runtime_probe').fetchall() == [('host data',)]
    writer.close()
'''


def execute_runtime(remote_runtime, assertions):
    environment, local_database = remote_runtime
    run_isolated(RUNTIME_SETUP + "\n" + textwrap.dedent(assertions), environment)
    assert not local_database.exists(), "Read-only service created a local production database"


def test_readonly_startup_skips_schema_workers_and_auth_token(remote_runtime):
    execute_runtime(remote_runtime, """
        from sqlalchemy import text
        from app import auth
        assert auth.WRITE_TOKEN is None
        assert auth.auth_enabled() is False
        with TestClient(app, client=('127.0.0.1', 12345)) as client:
            assert client.get('/api/v1/health/live').status_code == 200
            assert not requests, 'Liveness or startup unexpectedly opened a remote transaction'
            with engine.connect() as connection:
                assert connection.execute(text('SELECT value FROM runtime_probe')).scalar() == 'host data'
        assert any(method == 'POST' and url.endswith('/transactions') for method, url in requests)
        cleanup()
    """)


def test_runtime_mode_and_banner_identify_local_code_and_readonly_host(remote_runtime):
    execute_runtime(remote_runtime, """
        with TestClient(app, client=('127.0.0.1', 12345)) as client:
            response = client.get('/api/v1/runtime-mode')
            assert response.status_code == 200
            assert response.json() == {'mode': 'readonly-test', 'data_server': 'http://isolated-host.example:8000'}
            page = client.get('/')
            assert page.status_code == 200
            assert 'LOCAL FRONTEND CODE' in page.text
            assert '测试服' in page.text and '本地前后端代码' in page.text and '主机数据只读' in page.text
            assert 'http://isolated-host.example:8000' in page.text
            assert 'dmcs-token' not in page.text
            assert 'write-token-must-not-be-used' not in page.text
            assert json.loads(Path(os.environ['DMCS_REMOTE_READONLY_CONFIG']).read_text())['token'] not in page.text
            assert not requests
        cleanup()
    """)


def test_localhost_writes_are_blocked_but_read_calculations_work(remote_runtime):
    execute_runtime(remote_runtime, """
        with TestClient(app, client=('127.0.0.1', 12345)) as client:
            for method, path in [
                ('PUT', '/api/v1/state?force=true'),
                ('POST', '/api/v1/manual-measurements'),
                ('PATCH', '/api/v1/decision/sources'),
                ('POST', '/api/v1/telemetry/collector/start'),
                ('POST', '/api/v1/import/apply'),
                ('DELETE', '/api/v1/records/1'),
            ]:
                response = client.request(method, path, json={})
                assert response.status_code == 403, (path, response.status_code, response.text)
                assert response.json()['detail']['error_code'] == 'readonly_test_server'
            assert not requests, 'Rejected writes should not acquire any host database transaction'
            ready = client.get('/api/v1/health/ready')
            assert ready.status_code == 200, ready.text
            state = client.get('/api/v1/state')
            assert state.status_code == 200, state.text
            assert state.json()['coarseCoal'] == []
            snapshot = client.post('/api/v1/decision/snapshots', json={
                'cells': {}, 'target_min': 8.5, 'target_max': 9.0})
            assert snapshot.status_code == 200, snapshot.text
            current = client.get('/api/v1/decision/current')
            assert current.status_code == 200, current.text
        cleanup()
    """)


def test_remote_disconnect_reports_unavailable_without_local_fallback(remote_runtime):
    execute_runtime(remote_runtime, """
        from urllib.error import URLError
        def unavailable(request, timeout):
            requests.append((request.method, request.full_url))
            raise URLError('temporary transport unavailable')
        remote_sqlite.connect = lambda configuration: real_remote_connect(configuration, opener=unavailable)
        import importlib
        importlib.import_module('app.database').remote_connect = remote_sqlite.connect
        with TestClient(app, client=('127.0.0.1', 12345)) as client:
            assert client.get('/api/v1/health/live').status_code == 200
            response = client.get('/api/v1/health/ready')
            assert response.status_code == 503
            assert response.json()['checks']['database']['error_code'] == 'database_unavailable'
            assert len(requests) == 1, 'Offline reads must not repeatedly retry or fall back'
        cleanup()
    """)


def test_telemetry_freshness_uses_the_production_clock_in_readonly_mode(remote_runtime):
    execute_runtime(remote_runtime, """
        from datetime import datetime, timezone
        from app.telemetry import repository
        from app.remote_readonly import operations
        expected = datetime(2026, 10, 17, 1, 0, tzinfo=timezone.utc)
        operations.host_now = lambda db: expected
        repository.latest_view = lambda db, source_id=None, now=None: {'checked_at': now.isoformat()}
        with TestClient(app, client=('127.0.0.1', 12345)) as client:
            response = client.get('/api/v1/telemetry/latest')
            assert response.status_code == 200, response.text
            assert response.json()['checked_at'] == expected.isoformat()
        cleanup()
    """)


def test_published_hour_windows_use_the_production_clock_in_readonly_mode(remote_runtime):
    execute_runtime(remote_runtime, """
        from datetime import datetime, timezone
        from app.decision_hourly import ash_settlement
        from app.remote_readonly import runtime
        expected = datetime(2026, 10, 17, 0, 59, 50, tzinfo=timezone.utc)
        runtime.host_now = lambda db: expected
        def settled(db, start=None, end=None, now=None):
            assert now == expected
            return {'checked_at': now.isoformat()}
        ash_settlement.read_settled_hours = settled
        ash_settlement.read_settled_interval = settled
        with TestClient(app, client=('127.0.0.1', 12345)) as client:
            for path in ('/api/v1/decision/total-ash-hours', '/api/v1/decision/total-ash-interval'):
                response = client.get(path)
                assert response.status_code == 200, response.text
                assert response.json()['checked_at'] == expected.isoformat()
        cleanup()
    """)


def test_pool_checkin_closes_remote_snapshot_and_reuse_opens_a_fresh_one(remote_runtime):
    execute_runtime(remote_runtime, """
        from sqlalchemy import text
        with engine.connect() as connection:
            assert connection.execute(text('SELECT value FROM runtime_probe')).scalar() == 'host data'
            assert len(manager._transactions) == 1
            first_snapshot = next(iter(manager._transactions))
        assert not manager._transactions, 'Pool checkin kept a production WAL snapshot open'
        with engine.connect() as connection:
            assert connection.execute(text('SELECT value FROM runtime_probe')).scalar() == 'host data'
            assert len(manager._transactions) == 1
            assert next(iter(manager._transactions)) != first_snapshot
        assert not manager._transactions
        cleanup()
    """)


def test_local_pool_limits_concurrent_host_snapshots_and_waits_for_a_slot(remote_runtime):
    execute_runtime(remote_runtime, """
        from concurrent.futures import ThreadPoolExecutor
        import threading
        from sqlalchemy import text
        holders = []
        for _ in range(3):
            connection = engine.connect()
            assert connection.execute(text('SELECT value FROM runtime_probe')).scalar() == 'host data'
            holders.append(connection)
        assert len(manager._transactions) == 3
        started = threading.Event()
        def fourth_read():
            started.set()
            with engine.connect() as connection:
                return connection.execute(text('SELECT value FROM runtime_probe')).scalar()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(fourth_read)
            assert started.wait(1)
            import time
            time.sleep(.15)
            assert not future.done(), 'Fourth local request opened a host snapshot instead of waiting'
            assert len(manager._transactions) == 3
            holders.pop().close()
            assert future.result(timeout=2) == 'host data'
        assert len(manager._transactions) == 2
        for connection in holders:
            connection.close()
        assert not manager._transactions
        cleanup()
    """)


@pytest.mark.parametrize('entrypoint', ['collector.py', 'scripts/start_collector.py'])
def test_both_collector_entrypoints_refuse_start_in_readonly_mode(remote_runtime, entrypoint):
    environment, local_database = remote_runtime
    environment['TEST_COLLECTOR_ENTRYPOINT'] = str(BACKEND / entrypoint)
    run_isolated("""
        import os
        import runpy
        import sqlite3
        import sys
        def no_sqlite(*args, **kwargs):
            raise AssertionError('Collector reached a local SQLite database')
        sqlite3.connect = sqlite3.dbapi2.connect = no_sqlite
        module = runpy.run_path(os.environ['TEST_COLLECTOR_ENTRYPOINT'])
        assert module['main']() == 0
        assert 'app.database' not in sys.modules
        assert 'app.telemetry.control' not in sys.modules
        assert 'app.telemetry.worker' not in sys.modules
    """, environment)
    assert not local_database.exists()


@pytest.mark.parametrize('configuration', [
    None,
    {'mode': 'readonly-test', 'server_url': 'http://isolated-host.example:8000'},
    {'mode': 'readonly-test', 'server_url': 'http://isolated-host.example:8000', 'token': 'short'},
    {'mode': 'readonly-test', 'server_url': 'http://isolated-host.example:8000', 'token': ' ' + TOKEN},
    {'mode': 'readonly-test', 'server_url': 'http://isolated-host.example:8000', 'token': TOKEN + '\n'},
    {'mode': 'readonly-test', 'server_url': 'file:///production.db', 'token': TOKEN},
    {'mode': 'production', 'server_url': 'http://isolated-host.example:8000', 'token': TOKEN},
])
def test_explicit_missing_or_invalid_remote_configuration_cannot_open_local_database(tmp_path, configuration):
    local_database = tmp_path/'never-open-local.db'
    environment = isolated_environment(local_database)
    configuration_path = tmp_path/'explicit-remote.json'
    if configuration is not None:
        configuration_path.write_text(json.dumps(configuration), encoding='utf-8')
    environment['DMCS_REMOTE_READONLY_CONFIG'] = str(configuration_path)
    run_isolated("""
        import sqlite3
        import sys
        def no_local_database(*args, **kwargs):
            raise AssertionError('Invalid remote config fell back to a local database')
        sqlite3.connect = sqlite3.dbapi2.connect = no_local_database
        try:
            import app.config
        except (ValueError, OSError):
            pass
        else:
            raise AssertionError('Explicit invalid or missing remote config was silently ignored')
        assert 'app.database' not in sys.modules
    """, environment)
    assert not local_database.exists()


def test_existing_isolated_database_url_does_not_inherit_workstation_remote_marker(tmp_path):
    environment = isolated_environment(tmp_path/'isolated.db')
    run_isolated("""
        import remote_mode
        assert remote_mode.settings() is None
        from app.config import DATABASE_URL, REMOTE_READONLY
        import os
        assert DATABASE_URL == os.environ['DMCS_DATABASE_URL']
        assert REMOTE_READONLY is None
    """, environment)


def test_database_url_alone_cannot_disable_a_default_readonly_configuration(tmp_path):
    marker = tmp_path/'data'/'remote-readonly.json'
    marker.parent.mkdir()
    marker.write_text(json.dumps({
        'mode': 'readonly-test', 'server_url': 'http://isolated-host.example:8000/', 'token': TOKEN,
    }), encoding='utf-8')
    environment = isolated_environment(tmp_path/'never-open-local.db')
    environment.pop('DMCS_ISOLATED_TEST_DB')
    environment['TEST_FAKE_CONFIG_MODULE'] = str(tmp_path/'remote_mode.py')
    run_isolated("""
        import os
        import remote_mode
        remote_mode.__file__ = os.environ['TEST_FAKE_CONFIG_MODULE']
        configuration = remote_mode.settings()
        assert configuration['server_url'] == 'http://isolated-host.example:8000'
        assert len(configuration['token']) >= 32
    """, environment)
    assert not (tmp_path/'never-open-local.db').exists()


def test_explicit_readonly_configuration_takes_priority_over_isolation_flag(remote_runtime):
    environment, local_database = remote_runtime
    run_isolated("""
        import os
        import remote_mode
        assert os.environ['DMCS_ISOLATED_TEST_DB'] == '1'
        assert remote_mode.settings()['server_url'] == 'http://isolated-host.example:8000'
        from app.config import REMOTE_READONLY
        assert REMOTE_READONLY is not None
    """, environment)
    assert not local_database.exists()
