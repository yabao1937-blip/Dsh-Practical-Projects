"""Read-only failures expose only bounded, allowlisted operational metadata."""
import io
import json
import logging
from urllib.error import HTTPError, URLError

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.exc import OperationalError, TimeoutError as PoolTimeout

from app import observability
from app.routers import health
from remote_sqlite import _RemoteError, connect


SECRET = 'private-token-and-sql-parameter'
CONFIG = {'server_url': 'http://isolated.example', 'token': SECRET}


@pytest.mark.parametrize('remote_code, expected', [
    ('readonly_transaction_limit', 'readonly_transaction_limit'),
    ('readonly_transaction_expired', 'readonly_transaction_expired'),
    ('readonly_query_timeout', 'readonly_query_timeout'),
    ('readonly_' + SECRET, 'readonly_request_failed'),
    ({'unsafe': SECRET}, 'readonly_request_failed'),
])
def test_transport_error_metadata_is_allowlisted_and_makes_no_retry(remote_code, expected):
    calls = []

    def reject(request, timeout):
        calls.append(request)
        body = json.dumps({'detail': {'error_code': remote_code, 'sql': SECRET}}).encode()
        raise HTTPError(request.full_url, 429, SECRET, {}, io.BytesIO(body))

    db = connect(CONFIG, opener=reject)
    with pytest.raises(_RemoteError) as caught:
        db.execute('SELECT 1')
    assert caught.value.error_code == expected
    assert caught.value.status == 429
    assert SECRET not in str(caught.value)
    assert len(calls) == 1
    db.close()


def test_transport_unavailable_gets_safe_code_without_leaking_url_reason():
    def unavailable(request, timeout):
        raise URLError(SECRET)

    db = connect(CONFIG, opener=unavailable)
    with pytest.raises(_RemoteError) as caught:
        db.execute('SELECT 1')
    assert caught.value.error_code == 'readonly_transport_unavailable'
    assert caught.value.status is None
    assert SECRET not in str(caught.value)
    db.close()


def wrapped_remote():
    return OperationalError('SELECT ' + SECRET, {'secret': SECRET},
        _RemoteError(SECRET, 404, error_code='readonly_transaction_expired'))


def test_wrapped_exception_groups_report_remote_code_and_local_pool_distinctly(monkeypatch):
    monkeypatch.setattr(observability, 'REMOTE_READONLY', CONFIG)
    grouped = ExceptionGroup(SECRET, [wrapped_remote(), PoolTimeout(SECRET)])
    assert observability.readonly_failure_details(grouped) == {
        'readonly_error_code': 'readonly_transaction_expired',
        'readonly_http_status': 404, 'readonly_pool_timeout': True,
    }
    monkeypatch.setattr(observability, 'REMOTE_READONLY', None)
    assert observability.readonly_failure_details(grouped) == {}


def test_request_diagnostics_are_log_only_and_production_logging_stays_minimal(monkeypatch, caplog):
    application = FastAPI()
    observability.install_observability(application)

    @application.get('/api/failing')
    def failing():
        raise wrapped_remote()

    with TestClient(application) as client:
        for mode in (CONFIG, None):
            caplog.clear()
            monkeypatch.setattr(observability, 'REMOTE_READONLY', mode)
            with caplog.at_level(logging.INFO, logger='dmcs.http'):
                response = client.get('/api/failing')
            assert response.status_code == 500
            assert response.json()['detail'] == {'error': '服务器内部错误，请使用请求编号排查'}
            assert SECRET not in response.text + caplog.text
            assert 'readonly_transaction_expired' not in response.text
            assert ('readonly_transaction_expired' in caplog.text) is bool(mode)


def test_readiness_logs_pool_timeout_but_preserves_generic_503(monkeypatch, caplog):
    class BusyDatabase:
        def connect(self):
            raise PoolTimeout(SECRET)

    monkeypatch.setattr(observability, 'REMOTE_READONLY', CONFIG)
    monkeypatch.setattr(health, 'engine', BusyDatabase())
    application = FastAPI()
    application.include_router(health.router, prefix='/api/v1')
    with TestClient(application) as client, caplog.at_level(logging.WARNING, logger='dmcs.health'):
        response = client.get('/api/v1/health')
    assert response.status_code == 503
    assert response.json()['checks']['database']['error_code'] == 'database_unavailable'
    assert 'readonly_pool_timeout' in caplog.text
    assert SECRET not in caplog.text + response.text
