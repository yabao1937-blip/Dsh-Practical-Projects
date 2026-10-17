"""The laptop executes its own ORM queries against a real remote WAL snapshot."""
from datetime import datetime
import io
import json
import sqlite3
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import DateTime, Integer, JSON, LargeBinary, String, create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import QueuePool

from app.remote_readonly.server import BridgeLimits, ReadOnlyManager, create_router
from remote_sqlite import RemoteConnection, connect


TOKEN = 'isolated-readonly-token-' + 'x' * 40
CONFIG = {'server_url': 'http://host.example:8000', 'token': TOKEN}


class Base(DeclarativeBase):
    pass


class Example(Base):
    __tablename__ = 'remote_examples'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSON)
    observed: Mapped[datetime] = mapped_column(DateTime)
    payload: Mapped[bytes] = mapped_column(LargeBinary)


class ProtocolResponse(io.BytesIO):
    def __init__(self, response):
        super().__init__(response.content)
        self.status = response.status_code


@pytest.fixture
def host(tmp_path):
    path = tmp_path/'production.db'
    engine = create_engine('sqlite:///' + path.as_posix())
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql('PRAGMA journal_mode=WAL')
        connection.execute(Example.__table__.insert(), [dict(id=i, label=f'row {i}',
            document={'value': i, 'nullable': None, 'items': [True, '煤']},
            observed=datetime(2026, 10, 16, 8, i), payload=bytes([0, i, 255])) for i in range(1, 6)])
    engine.dispose()
    manager = ReadOnlyManager(path, {'token': TOKEN, 'clients': ['127.0.0.1']},
        limits=BridgeLimits(page_rows=2))
    application = FastAPI()
    application.include_router(create_router(manager), prefix='/api/v1')
    requests = []
    with TestClient(application, client=('127.0.0.1', 12345)) as client:
        def opener(request, timeout):
            requests.append((request.method, request.full_url))
            response = client.request(request.method, urlsplit(request.full_url).path,
                content=request.data, headers=dict(request.header_items()))
            if response.status_code >= 400:
                raise HTTPError(request.full_url, response.status_code, 'request rejected',
                    response.headers, io.BytesIO(response.content))
            return ProtocolResponse(response)
        yield path, manager, requests, opener
    manager.close()


def test_sqlalchemy_orm_preserves_json_datetime_blob_count_and_pool_reuse(host):
    _, manager, requests, opener = host
    engine = create_engine('sqlite://', creator=lambda: connect(CONFIG, opener=opener),
        poolclass=QueuePool, pool_size=1, max_overflow=0)
    try:
        with Session(engine) as session:
            row = session.get(Example, 3)
            assert row.document == {'value': 3, 'nullable': None, 'items': [True, '煤']}
            assert row.observed == datetime(2026, 10, 16, 8, 3)
            assert row.payload == b'\x00\x03\xff'
            assert session.scalar(select(func.count()).select_from(Example)) == 5
            assert [r.id for r in session.scalars(select(Example).order_by(Example.id))] == [1, 2, 3, 4, 5]
            assert len(manager._transactions) == 1
        assert not manager._transactions
        with Session(engine) as session:
            assert session.scalar(select(Example.label).where(Example.id == 1)) == 'row 1'
        assert len([url for method, url in requests if method == 'POST' and url.endswith('/transactions')]) == 2
    finally:
        engine.dispose()


def test_cursor_pagination_fetch_sizes_and_early_close_release_host_cursor(host):
    _, manager, requests, opener = host
    db = connect(CONFIG, opener=opener)
    cursor = db.execute('SELECT id, label FROM remote_examples ORDER BY id')
    assert cursor.description == (('id', None, None, None, None, None, None),
                                  ('label', None, None, None, None, None, None))
    assert cursor.fetchone() == (1, 'row 1')
    assert cursor.fetchmany(2) == [(2, 'row 2'), (3, 'row 3')]
    assert cursor.fetchall() == [(4, 'row 4'), (5, 'row 5')]
    assert cursor.fetchone() is None and cursor.rowcount == -1 and cursor.lastrowid is None
    assert any(method == 'GET' and '/cursors/' in url for method, url in requests)
    cursor.close()
    cursor = db.execute('SELECT id FROM remote_examples')
    transaction = next(iter(manager._transactions.values()))
    assert len(transaction.cursors) == 1
    cursor.close()
    assert not transaction.cursors
    with pytest.raises(sqlite3.ProgrammingError, match='closed cursor'):
        cursor.fetchone()
    db.close()
    assert not manager._transactions


def test_read_transaction_keeps_snapshot_and_commit_opens_fresh_one(host):
    path, manager, _, opener = host
    db = RemoteConnection(CONFIG, opener=opener)
    assert not db.in_transaction
    db.execute('BEGIN').close()
    identifier = next(iter(manager._transactions))
    assert db.execute('SELECT label FROM remote_examples WHERE id=1').fetchone() == ('row 1',)
    with sqlite3.connect(path) as writer:
        writer.execute("UPDATE remote_examples SET label='new production sample' WHERE id=1")
    db.execute('BEGIN').close()  # Existing route BEGIN must not replace the snapshot.
    assert next(iter(manager._transactions)) == identifier
    cursor = db.execute('SELECT label FROM remote_examples WHERE id=1')
    assert cursor.fetchone() == ('row 1',)
    db.commit()
    assert not manager._transactions and not db.in_transaction
    with pytest.raises(sqlite3.ProgrammingError, match='transaction has ended'):
        cursor.fetchone()
    assert db.execute('SELECT label FROM remote_examples WHERE id=1').fetchone() == ('new production sample',)
    db.rollback()
    assert not manager._transactions
    db.close()


def test_parameters_binary_named_values_and_empty_result(host):
    _, _, _, opener = host
    db = connect(CONFIG, opener=opener)
    assert db.execute('SELECT id FROM remote_examples WHERE payload=?', (memoryview(b'\x00\x02\xff'),)).fetchall() == [(2,)]
    assert db.execute('SELECT label FROM remote_examples WHERE id=:identifier', {'identifier': 4}).fetchone() == ('row 4',)
    empty = db.execute('SELECT id FROM remote_examples WHERE id < ?', (0,))
    assert empty.description[0][0] == 'id'
    assert empty.fetchmany() == []
    db.close()


@pytest.mark.parametrize('sql', ['BEGIN IMMEDIATE', 'UPDATE remote_examples SET label=\'bad\'',
    'CREATE TABLE bad(x)', 'PRAGMA query_only=OFF', "ATTACH DATABASE ':memory:' AS other", 'COMMIT'])
def test_local_write_guard_never_opens_remote_transaction(host, sql):
    _, manager, requests, opener = host
    db = connect(CONFIG, opener=opener)
    with pytest.raises(sqlite3.ProgrammingError, match='read-only'):
        db.execute(sql)
    assert not requests and not manager._transactions
    db.close()


def test_host_guard_rejects_cte_write_and_error_does_not_leak_token(host):
    path, manager, _, opener = host
    db = connect(CONFIG, opener=opener)
    with pytest.raises(sqlite3.OperationalError, match='readonly_query_rejected') as caught:
        db.execute('WITH c AS (SELECT 1) DELETE FROM remote_examples')
    assert TOKEN not in str(caught.value)
    db.rollback()
    assert not manager._transactions
    with sqlite3.connect(path) as source:
        assert source.execute('SELECT count(*) FROM remote_examples').fetchone() == (5,)
    db.close()


def test_expired_snapshot_fails_without_implicit_reopen_or_query_retry(host):
    _, manager, requests, opener = host
    db = connect(CONFIG, opener=opener)
    db.execute('SELECT 1').close()
    manager.close_transaction(next(iter(manager._transactions)), '127.0.0.1')
    with pytest.raises(sqlite3.OperationalError, match='readonly_transaction_expired'):
        db.execute('SELECT 2')
    with pytest.raises(sqlite3.OperationalError, match='rollback is required'):
        db.execute('SELECT 3')
    assert len([url for method, url in requests if method == 'POST' and url.endswith('/transactions')]) == 1
    db.rollback()  # An already-expired transaction can still be closed idempotently.
    assert db.execute('SELECT 4').fetchone() == (4,)
    db.close()


def test_offline_host_uses_no_local_sqlite_and_makes_no_retry(monkeypatch):
    requests = []
    def unavailable(request, timeout):
        requests.append(request)
        raise URLError('failure mentioning ' + TOKEN)
    def forbidden_local_connection(*args, **kwargs):
        raise AssertionError('A local SQLite connection must never be opened')
    monkeypatch.setattr(sqlite3, 'connect', forbidden_local_connection)
    db = connect(CONFIG, opener=unavailable)
    with pytest.raises(sqlite3.OperationalError, match='no local database was used') as caught:
        db.execute('SELECT 1')
    assert TOKEN not in str(caught.value) and len(requests) == 1
    db.close()


def test_sqlite_initialization_does_not_open_snapshot_or_install_remote_code():
    db = connect(CONFIG, opener=lambda *args, **kwargs: pytest.fail('Initialization made a network request'))
    db.create_function('regexp', 2, lambda value, pattern: False, deterministic=True)
    db.create_function('floor', 1, lambda value: 0)
    assert db.execute('PRAGMA read_uncommitted').fetchone() == (0,)
    assert not db.in_transaction
    with pytest.raises(sqlite3.ProgrammingError, match='functions'):
        db.create_function('unsafe_extension', 0, lambda: None)
    db.close()


@pytest.mark.parametrize('url', ['file:///C:/data', 'http://user:secret@host',
    'http://host/path', 'http://host?token=secret'])
def test_transport_rejects_non_origin_configuration(url):
    with pytest.raises(sqlite3.ProgrammingError, match='origin'):
        connect({'server_url': url, 'token': TOKEN})
