"""Real WAL behavior and denied operations for the opt-in read-only transport."""
from dataclasses import replace
import sqlite3
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.remote_readonly.server import (BridgeError, BridgeLimits, BridgeSettings,
    ReadOnlyManager, TOKEN_HEADER, create_router)
from app.remote_readonly.wire import decode, encode


TOKEN = 'test-readonly-secret-' + 'x' * 40
OWNER = '127.0.0.1'


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'host.db'
    with sqlite3.connect(path) as connection:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE values_table(id INTEGER PRIMARY KEY, value TEXT, payload BLOB)')
        connection.executemany('INSERT INTO values_table(value,payload) VALUES (?,?)',
            [(str(number), bytes([number % 256])) for number in range(23)])
    return path


@pytest.fixture
def manager(database):
    current = ReadOnlyManager(database, BridgeSettings(TOKEN, (OWNER,)))
    yield current
    current.close()


@pytest.fixture
def client(manager):
    app = FastAPI()
    app.include_router(create_router(manager), prefix='/api/v1')
    with TestClient(app, client=(OWNER, 1234)) as current:
        current.headers[TOKEN_HEADER] = TOKEN
        yield current


def open_id(manager):
    return manager.open_transaction(OWNER)['transaction_id']


def test_authentication_rejects_wrong_ip_or_token_before_opening_snapshot(manager, client):
    response = client.post('/api/v1/readonly-db/transactions', headers={TOKEN_HEADER: 'wrong'})
    assert response.status_code == 403
    assert manager._transactions == {}
    with pytest.raises(BridgeError, match='readonly_auth_required'):
        manager.authenticate('127.0.0.2', TOKEN)
    response = client.post('/api/v1/readonly-db/transactions')
    assert response.status_code == 200
    assert len(manager._transactions) == 1


def test_query_authentication_precedes_json_parsing_and_errors_never_echo_body(manager, client):
    base = '/api/v1/readonly-db/transactions/unknown/queries'
    response = client.post(base, content='not JSON private SQL', headers={TOKEN_HEADER: 'wrong'})
    assert response.status_code == 403
    response = client.post(base, json={'sql': 'private SQL' * 10000, 'parameters': []})
    assert response.status_code == 413
    assert 'private' not in response.text
    response = client.post(base, json={'sql': 'SELECT 1', 'parameters': [], 'secret': 'private'})
    assert response.status_code == 400
    assert 'private' not in response.text


def test_same_transaction_is_a_snapshot_new_transaction_sees_new_value(database, manager):
    transaction = open_id(manager)
    first = manager.query(transaction, OWNER, 'SELECT value FROM values_table WHERE id=1', [])
    with sqlite3.connect(database) as writer:
        writer.execute("UPDATE values_table SET value='new' WHERE id=1")
    second = manager.query(transaction, OWNER, 'SELECT value FROM values_table WHERE id=1', [])
    assert first['rows'] == second['rows'] == [['0']]
    manager.close_transaction(transaction, OWNER)
    latest = manager.query(open_id(manager), OWNER, 'SELECT value FROM values_table WHERE id=1', [])
    assert latest['rows'] == [['new']]


@pytest.mark.parametrize('sql', [
    "UPDATE values_table SET value='bad'", 'DELETE FROM values_table',
    "INSERT INTO values_table(value) VALUES('bad')", 'DROP TABLE values_table',
    'CREATE TABLE bad(x)', "ATTACH DATABASE ':memory:' AS other", 'DETACH DATABASE main',
    'PRAGMA query_only=OFF', 'PRAGMA journal_mode=DELETE', 'VACUUM INTO \'copy.db\'',
    "SELECT load_extension('evil')", "SELECT readfile('secret.txt')",
    "WITH inputs AS (SELECT 1) DELETE FROM values_table", 'BEGIN IMMEDIATE',
    'SELECT 1; DELETE FROM values_table',
])
def test_authorizer_rejects_writes_files_attaches_pragmas_and_multiple_statements(manager, database, sql):
    with pytest.raises(BridgeError, match='readonly_query_rejected'):
        manager.query(open_id(manager), OWNER, sql, [])
    with sqlite3.connect(database) as connection:
        assert connection.execute('SELECT count(*) FROM values_table').fetchone()[0] == 23


def test_read_only_file_and_query_only_still_protect_if_authorizer_is_disabled(manager):
    transaction = open_id(manager)
    connection = manager._transactions[transaction].connection
    connection.set_authorizer(None)
    with pytest.raises(sqlite3.OperationalError):
        connection.execute("UPDATE values_table SET value='bad'")
    connection.execute('PRAGMA query_only=OFF')
    with pytest.raises(sqlite3.OperationalError):
        connection.execute("UPDATE values_table SET value='bad'")


def test_pages_include_all_rows_once_and_binary_values_roundtrip(manager):
    manager.limits = replace(manager.limits, page_rows=4)
    transaction = open_id(manager)
    page = manager.query(transaction, OWNER, 'SELECT id,value,payload FROM values_table ORDER BY id', [])
    rows = list(page['rows'])
    while not page['done']:
        assert page['columns'] == ['id', 'value', 'payload']
        page = manager.page(transaction, OWNER, page['cursor_id'])
        rows.extend(page['rows'])
    assert [row[0] for row in rows] == list(range(1, 24))
    assert decode(rows)[0] == [1, '0', b'\0']
    assert manager._transactions[transaction].cursors == {}


def test_limits_are_explicit_errors_not_silent_truncation(manager):
    manager.limits = replace(manager.limits, max_rows=3)
    transaction = open_id(manager)
    with pytest.raises(BridgeError, match='readonly_result_limit') as error:
        manager.query(transaction, OWNER, 'SELECT id FROM values_table', [])
    assert error.value.status == 413
    assert manager._transactions[transaction].cursors == {}
    manager.limits = replace(manager.limits, max_rows=100, max_result_bytes=5)
    with pytest.raises(BridgeError, match='readonly_result_limit'):
        manager.query(transaction, OWNER, 'SELECT value FROM values_table', [])
    manager.limits = replace(manager.limits, max_parameter_bytes=5)
    with pytest.raises(BridgeError, match='readonly_parameter_limit'):
        manager.query(transaction, OWNER, 'SELECT ?', ['long parameter'])


def test_oversized_scalar_blob_is_rejected_inside_sqlite_before_allocation(manager):
    manager.limits = replace(manager.limits, max_result_bytes=1024)
    transaction = open_id(manager)
    with pytest.raises(BridgeError, match='readonly_query_rejected'):
        manager.query(transaction, OWNER, 'SELECT zeroblob(1000000000)', [])


def test_transaction_and_cursor_caps_and_owner_isolation(manager):
    manager.limits = replace(manager.limits, max_transactions=1, max_cursors=1, page_rows=1)
    transaction = open_id(manager)
    with pytest.raises(BridgeError, match='readonly_transaction_limit'):
        open_id(manager)
    manager.query(transaction, OWNER, 'SELECT id FROM values_table', [])
    with pytest.raises(BridgeError, match='readonly_cursor_limit'):
        manager.query(transaction, OWNER, 'SELECT id FROM values_table', [])
    with pytest.raises(BridgeError, match='readonly_transaction_expired'):
        manager.query(transaction, '127.0.0.2', 'SELECT 1', [])


def test_expiry_releases_wal_snapshot_without_another_http_request(database):
    manager = ReadOnlyManager(database, BridgeSettings(TOKEN, (OWNER,)),
        limits=BridgeLimits(idle_seconds=.12), cleanup_interval=.02)
    try:
        transaction = open_id(manager)
        with sqlite3.connect(database) as writer:
            writer.execute("UPDATE values_table SET value='new'")
            writer.commit()
            writer.execute('PRAGMA busy_timeout=0')
            assert writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 1
            deadline = time.monotonic() + 2
            while transaction in manager._transactions and time.monotonic() < deadline:
                time.sleep(.02)
            assert transaction not in manager._transactions
            assert writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
    finally:
        manager.close()


def test_absolute_lifetime_expires_even_when_recently_used(manager):
    current = [0.0]
    manager.clock = lambda: current[0]
    manager.limits = replace(manager.limits, idle_seconds=100, lifetime_seconds=10)
    transaction = open_id(manager)
    current[0] = 9
    manager.query(transaction, OWNER, 'SELECT 1', [])
    current[0] = 11
    with pytest.raises(BridgeError, match='readonly_transaction_expired'):
        manager.query(transaction, OWNER, 'SELECT 1', [])


def test_query_timeout_and_public_error_never_echo_sql_or_parameters(manager, client):
    manager.limits = replace(manager.limits, query_seconds=.001)
    transaction = open_id(manager)
    response = client.post(f'/api/v1/readonly-db/transactions/{transaction}/queries', json={
        'sql': 'WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<10000000) SELECT sum(x) FROM n',
        'parameters': []})
    assert response.status_code == 408
    assert response.json() == {'detail': {'error_code': 'readonly_query_timeout'}}
    response = client.post(f'/api/v1/readonly-db/transactions/{transaction}/queries', json={
        'sql': 'SELECT private_column FROM private_table WHERE secret=?', 'parameters': ['private-secret']})
    assert response.status_code == 400
    assert 'private' not in response.text


def test_route_closes_cursor_and_transaction_with_204(manager, client):
    manager.limits = replace(manager.limits, page_rows=1)
    base = '/api/v1/readonly-db/transactions'
    transaction = client.post(base).json()['transaction_id']
    page = client.post(f'{base}/{transaction}/queries', json={
        'sql': 'SELECT id FROM values_table ORDER BY id', 'parameters': []}).json()
    assert client.get(f'{base}/{transaction}/cursors/{page["cursor_id"]}').json()['rows'] == [[2]]
    assert client.delete(f'{base}/{transaction}/cursors/{page["cursor_id"]}').status_code == 204
    assert client.delete(f'{base}/{transaction}').status_code == 204
    assert manager._transactions == {}


def test_wire_values_and_invalid_base64():
    value = {'rows': [(1, b'\xff\0', None, 1.2)], 'text': '汉字'}
    assert decode(encode(value)) == {'rows': [[1, b'\xff\0', None, 1.2]], 'text': '汉字'}
    with pytest.raises(ValueError, match='Invalid binary'):
        decode({'__dmcs_bytes__': 'broken!'})


def test_missing_host_config_disables_routes_without_creating_database(tmp_path, monkeypatch):
    monkeypatch.setenv('DMCS_READONLY_HOST_CONFIG', str(tmp_path / 'absent.json'))
    app = FastAPI()
    app.include_router(create_router(), prefix='/api/v1')
    with TestClient(app, client=(OWNER, 1234)) as client:
        assert client.post('/api/v1/readonly-db/transactions').status_code == 404
    assert list(tmp_path.iterdir()) == []
