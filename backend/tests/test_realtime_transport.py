"""The test server uses host IPC without obtaining a second writable database."""
import json
import sqlite3
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.remote_readonly import live_cache
from app.remote_readonly.server import BridgeSettings, ReadOnlyManager, TOKEN_HEADER, create_router


def test_live_transport_authenticates_before_reading_cache(tmp_path, monkeypatch):
    path = tmp_path / 'host.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE marker(value INTEGER)')
    token = 'isolated-runtime-token-' + 'x' * 40
    manager = ReadOnlyManager(path, BridgeSettings(token, ('127.0.0.1',)))
    calls = []
    monkeypatch.setattr(live_cache, 'transport_view', lambda database: calls.append(database) or {'sources': {}})
    app = FastAPI()
    app.include_router(create_router(manager), prefix='/api/v1')
    try:
        with TestClient(app, client=('127.0.0.1', 1234)) as client:
            assert client.get('/api/v1/readonly-db/live-cache').status_code == 403
            assert calls == []
            response = client.get('/api/v1/readonly-db/live-cache', headers={TOKEN_HEADER: token})
            assert response.status_code == 200 and response.json() == {'sources': {}}
            assert calls == [path.resolve()]
            assert manager._transactions == {}  # No SQL snapshot is opened for IPC.
    finally:
        manager.close()


def test_attach_uses_remote_cache_and_drops_old_documents_on_failure(monkeypatch):
    import remote_sqlite
    calls = []
    response = [{'sources': {'kepserver-main': {'version': 1, 'owner': 'isolated'}}}]

    class Connection:
        def __init__(self, config, *, timeout):
            assert timeout == 3 and config['server_url'] == 'http://isolated-host'

        def _request(self, method, suffix):
            calls.append((method, suffix))
            value = response[0]
            if isinstance(value, Exception):
                raise value
            return value

        def close(self):
            calls.append('closed')

    monkeypatch.setattr(remote_sqlite, 'RemoteConnection', Connection)
    db = SimpleNamespace(info={})
    config = {'server_url': 'http://isolated-host', 'token': 'isolated'}
    assert live_cache.attach_runtime(db, config)
    assert db.info['telemetry_runtime']['kepserver-main']['owner'] == 'isolated'
    response[0] = sqlite3.OperationalError('isolated network failure')
    assert not live_cache.attach_runtime(db, config)
    assert db.info['telemetry_runtime'] == {}
    assert calls == [('GET', '/live-cache'), 'closed', ('GET', '/live-cache'), 'closed']


def test_oversized_or_invalid_transport_does_not_enter_session(monkeypatch):
    import remote_sqlite

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def _request(self, *args):
            return {'sources': {'too-large': 'x' * 200}}

        def close(self):
            pass

    monkeypatch.setattr(remote_sqlite, 'RemoteConnection', Connection)
    monkeypatch.setattr(live_cache, 'MAX_TRANSPORT_BYTES', 100)
    db = SimpleNamespace(info={'telemetry_runtime': {'old': {}}})
    assert not live_cache.attach_runtime(db, {'server_url': 'http://isolated-host'})
    assert db.info['telemetry_runtime'] == {}


def test_configuration_keeps_live_poll_and_extends_lease_for_sparse_commits():
    from app.telemetry.config import CollectorConfig
    cfg = CollectorConfig()
    assert cfg.poll_seconds == 5 and cfg.history_interval_seconds == 30
    assert cfg.lease_seconds >= 2 * cfg.history_interval_seconds + cfg.timeout_seconds
