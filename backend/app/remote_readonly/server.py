"""Opt-in, authenticated read-only SQLite transport for the local test server.

Each transaction owns a real WAL read snapshot on the production computer.
There is no database download, write operation, or client-supplied file path.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from .wire import decode, encode


TOKEN_HEADER = 'X-DMCS-Read-Token'


@dataclass(frozen=True)
class BridgeSettings:
    token: str
    clients: tuple[str, ...]

    def __post_init__(self):
        if (not isinstance(self.token, str) or len(self.token) < 32
                or self.token.strip() != self.token or '\r' in self.token or '\n' in self.token):
            raise ValueError('Read-only token must contain at least 32 characters')
        if not self.clients:
            raise ValueError('At least one exact client IP is required')
        for client in self.clients:
            if str(ipaddress.ip_address(client)) != client:
                raise ValueError('Clients must be canonical exact IP addresses')


@dataclass(frozen=True)
class BridgeLimits:
    idle_seconds: float = 60
    lifetime_seconds: float = 300
    max_transactions: int = 4
    max_cursors: int = 16
    page_rows: int = 1000
    max_rows: int = 100_000
    max_result_bytes: int = 32 * 1024 * 1024
    max_sql_bytes: int = 32 * 1024
    max_parameter_bytes: int = 1024 * 1024
    query_seconds: float = 15


class BridgeError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


_FUNCTIONS = frozenset('''abs avg char coalesce concat concat_ws count date datetime
    floor ceil ceiling round format printf glob group_concat hex ifnull iif instr
    json json_array json_array_length json_error_position json_extract json_insert
    json_object json_patch json_quote json_remove json_replace json_set json_type
    json_valid julianday last_insert_rowid length like likelihood likely lower ltrim
    max min mod nullif octet_length pow power quote replace rtrim sign soundex sqrt
    sqlite_version strftime string_agg substr substring sum time timediff total
    total_changes trim typeof unicode unixepoch unlikely upper zeroblob'''.split())


def _authorize(action, first, second, _database, _trigger):
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
                  getattr(sqlite3, 'SQLITE_RECURSIVE', 33)):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION and (second or first or '').lower() in _FUNCTIONS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


@dataclass
class _Cursor:
    cursor: object
    columns: list[str]
    pending: object = None
    rows: int = 0
    size: int = 0
    spent: float = 0


@dataclass
class _Transaction:
    connection: object
    owner: str
    created: float
    touched: float
    lock: object = field(default_factory=threading.RLock)
    cursors: dict = field(default_factory=dict)
    closed: bool = False


class ReadOnlyManager:
    """Inject a temporary local database and settings in isolated tests.

    Public operations: authenticate, open_transaction, query, page,
    close_cursor, close_transaction, reap, close. Call close at shutdown.
    A daemon reaper also releases abandoned snapshots without new HTTP traffic.
    """

    def __init__(self, database_path, settings, *, limits=None, clock=time.monotonic,
                 cleanup_interval=1.0):
        self.path = Path(database_path).resolve()
        if not self.path.is_file():
            raise ValueError('Read-only database must already exist')
        self.settings = settings if isinstance(settings, BridgeSettings) else BridgeSettings(
            token=settings['token'], clients=tuple(settings['clients']))
        self.limits = limits or BridgeLimits()
        self.clock = clock
        self._lock = threading.RLock()
        self._transactions = {}
        self._stop = threading.Event()
        self._cleanup_interval = max(.01, cleanup_interval)
        self._thread = threading.Thread(target=self._cleanup, name='dmcs-readonly-reaper', daemon=True)
        self._thread.start()

    def authenticate(self, client, token):
        if client not in self.settings.clients or not hmac.compare_digest(
                token or '', self.settings.token):
            raise BridgeError('readonly_auth_required', 403)
        return client

    def _expired(self, transaction):
        now = self.clock()
        return (now - transaction.touched >= self.limits.idle_seconds or
                now - transaction.created >= self.limits.lifetime_seconds)

    def _cleanup(self):
        while not self._stop.wait(self._cleanup_interval):
            self.reap()

    def reap(self):
        with self._lock:
            transactions = list(self._transactions.items())
        for identifier, transaction in transactions:
            if self._expired(transaction):
                if transaction.lock.acquire(blocking=False):
                    try:
                        if self._expired(transaction):
                            self._dispose(identifier, transaction)
                    finally:
                        transaction.lock.release()
                elif self.clock() - transaction.created >= self.limits.lifetime_seconds:
                    try:
                        transaction.connection.interrupt()
                    except sqlite3.Error:  # A simultaneous explicit close already released it.
                        pass

    def _dispose(self, identifier, transaction):
        with self._lock:
            self._transactions.pop(identifier, None)
        if not transaction.closed:
            transaction.closed = True
            for cursor in transaction.cursors.values():
                cursor.cursor.close()
            transaction.cursors.clear()
            transaction.connection.set_authorizer(None)
            try:
                transaction.connection.rollback()
            finally:
                transaction.connection.close()

    def close(self):
        self._stop.set()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        with self._lock:
            transactions = list(self._transactions.items())
        for identifier, transaction in transactions:
            try:
                transaction.connection.interrupt()
            except sqlite3.Error:
                continue
            with transaction.lock:
                self._dispose(identifier, transaction)

    def open_transaction(self, owner):
        self.reap()
        with self._lock:
            if self._stop.is_set():
                raise BridgeError('readonly_unavailable', 503)
            if len(self._transactions) >= self.limits.max_transactions:
                raise BridgeError('readonly_transaction_limit', 429)
            connection = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True,
                                         check_same_thread=False, isolation_level=None, timeout=5)
            try:
                # A single scalar function must not allocate an unbounded blob
                # before Python can enforce the serialized result budget.
                connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, self.limits.max_result_bytes)
                connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, self.limits.max_sql_bytes)
                connection.enable_load_extension(False)
                connection.execute('PRAGMA query_only=ON')
                connection.execute('BEGIN')
                # BEGIN itself is lazy: access the catalog now to pin the snapshot.
                connection.execute('SELECT rootpage FROM sqlite_master LIMIT 1').fetchone()
                connection.set_authorizer(_authorize)
            except Exception:
                connection.close()
                raise BridgeError('readonly_database_unavailable', 503) from None
            now = self.clock()
            identifier = secrets.token_urlsafe(32)
            self._transactions[identifier] = _Transaction(connection, owner, now, now)
            return {'transaction_id': identifier}

    def _transaction(self, identifier, owner):
        with self._lock:
            transaction = self._transactions.get(identifier)
        if not transaction or transaction.owner != owner:
            raise BridgeError('readonly_transaction_expired', 404)
        return transaction

    def _check(self, identifier, transaction):
        if transaction.closed or self._expired(transaction):
            self._dispose(identifier, transaction)
            raise BridgeError('readonly_transaction_expired', 404)
        transaction.touched = self.clock()

    def _run(self, transaction, state, operation):
        remaining = self.limits.query_seconds - state.spent
        if remaining <= 0:
            raise BridgeError('readonly_query_timeout', 408)
        started = self.clock()
        deadline = min(started + remaining, transaction.created + self.limits.lifetime_seconds)
        transaction.connection.set_progress_handler(lambda: int(self.clock() >= deadline), 1000)
        try:
            return operation()
        except sqlite3.Error as error:
            code = 'readonly_query_timeout' if ('interrupted' in str(error).lower() or
                self.clock() >= deadline) else 'readonly_query_rejected'
            raise BridgeError(code, 408 if code.endswith('timeout') else 400) from None
        finally:
            state.spent += max(0, self.clock() - started)
            transaction.connection.set_progress_handler(None, 0)

    def _next(self, transaction, state):
        row = self._run(transaction, state, state.cursor.fetchone)
        if row is None:
            return None
        encoded = encode(row)
        try:
            size = len(json.dumps(encoded, ensure_ascii=False, allow_nan=False,
                                  separators=(',', ':')).encode('utf-8'))
        except (ValueError, TypeError):
            raise BridgeError('readonly_result_type_rejected') from None
        state.rows += 1
        state.size += size
        if state.rows > self.limits.max_rows or state.size > self.limits.max_result_bytes:
            raise BridgeError('readonly_result_limit', 413)
        return encoded

    def _page(self, transaction, cursor_id, state):
        rows = []
        if state.pending is not None:
            rows.append(state.pending)
            state.pending = None
        done = False
        while len(rows) < self.limits.page_rows:
            row = self._next(transaction, state)
            if row is None:
                done = True
                break
            rows.append(row)
        if not done:
            state.pending = self._next(transaction, state)
            done = state.pending is None
        result = {'cursor_id': cursor_id, 'columns': state.columns, 'rows': rows, 'done': done}
        if done:
            state.cursor.close()
            transaction.cursors.pop(cursor_id, None)
        return result

    def query(self, identifier, owner, sql, parameters):
        if not isinstance(sql, str) or not sql.strip() or len(sql.encode('utf-8')) > self.limits.max_sql_bytes:
            raise BridgeError('readonly_sql_limit', 413)
        try:
            parameter_size = len(json.dumps(parameters, allow_nan=False).encode('utf-8'))
            parameters = decode(parameters)
            if not isinstance(parameters, (list, tuple, dict)):
                raise ValueError()
        except (ValueError, TypeError):
            raise BridgeError('readonly_parameters_rejected') from None
        if parameter_size > self.limits.max_parameter_bytes:
            raise BridgeError('readonly_parameter_limit', 413)
        transaction = self._transaction(identifier, owner)
        with transaction.lock:
            self._check(identifier, transaction)
            if len(transaction.cursors) >= self.limits.max_cursors:
                raise BridgeError('readonly_cursor_limit', 429)
            state = _Cursor(transaction.connection.cursor(), [])
            cursor_id = secrets.token_urlsafe(24)
            try:
                self._run(transaction, state, lambda: state.cursor.execute(sql, parameters))
                if state.cursor.description is None:
                    raise BridgeError('readonly_query_rejected')
                state.columns = [column[0] for column in state.cursor.description]
                transaction.cursors[cursor_id] = state
                return self._page(transaction, cursor_id, state)
            except Exception:
                state.cursor.close()
                transaction.cursors.pop(cursor_id, None)
                raise

    def page(self, identifier, owner, cursor_id):
        transaction = self._transaction(identifier, owner)
        with transaction.lock:
            self._check(identifier, transaction)
            state = transaction.cursors.get(cursor_id)
            if state is None:
                raise BridgeError('readonly_cursor_expired', 404)
            try:
                return self._page(transaction, cursor_id, state)
            except Exception:
                state.cursor.close()
                transaction.cursors.pop(cursor_id, None)
                raise

    def close_cursor(self, identifier, owner, cursor_id):
        transaction = self._transaction(identifier, owner)
        with transaction.lock:
            self._check(identifier, transaction)
            state = transaction.cursors.pop(cursor_id, None)
            if state:
                state.cursor.close()

    def close_transaction(self, identifier, owner):
        transaction = self._transaction(identifier, owner)
        with transaction.lock:
            self._dispose(identifier, transaction)


_default_lock = threading.RLock()
_default = None
_default_signature = None


def close_default_manager():
    global _default, _default_signature
    with _default_lock:
        if _default:
            _default.close()
        _default = _default_signature = None


def default_manager():
    global _default, _default_signature
    from ..config import BASE_DIR, DATABASE_URL
    from sqlalchemy.engine import make_url
    path = Path(os.environ.get('DMCS_READONLY_HOST_CONFIG') or BASE_DIR / 'config/readonly-access.local.json')
    try:
        stat = path.stat()
        signature = (str(path.resolve()), stat.st_mtime_ns, stat.st_size, DATABASE_URL)
        with _default_lock:
            if _default and _default_signature == signature:
                return _default
            settings = json.loads(path.read_text(encoding='utf-8-sig'))
            url = make_url(DATABASE_URL)
            if (url.get_backend_name() != 'sqlite' or url.host or not url.database
                    or url.database == ':memory:' or url.query
                    or url.database.startswith(('\\\\', '//', 'file:'))):
                raise ValueError('Only a local installed SQLite database is supported')
            manager = ReadOnlyManager(url.database, settings)
            if _default:
                _default.close()
            _default, _default_signature = manager, signature
            return manager
    except (OSError, ValueError, TypeError, KeyError):
        close_default_manager()
        raise BridgeError('readonly_disabled', 404) from None


def create_router(manager=None):
    router = APIRouter(prefix='/readonly-db', tags=['只读测试数据连接'])

    def authorized(request):
        current = manager if manager is not None else default_manager()
        owner = current.authenticate(request.client.host if request.client else '',
                                     request.headers.get(TOKEN_HEADER))
        return current, owner

    def invoke(request, action):
        try:
            current, owner = authorized(request)
            return action(current, owner)
        except BridgeError as error:
            raise HTTPException(error.status, detail={'error_code': error.code}) from None

    @router.post('/transactions')
    def begin(request: Request):
        return invoke(request, lambda current, owner: current.open_transaction(owner))

    @router.post('/transactions/{identifier}/queries')
    async def query(identifier: str, request: Request):
        # Authenticate before reading SQL/parameters. Avoid framework validation
        # errors, which otherwise echo the invalid SQL as an "input" field.
        try:
            current, owner = authorized(request)
            limit = current.limits.max_parameter_bytes + current.limits.max_sql_bytes * 6 + 4096
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > limit:
                    raise BridgeError('readonly_request_limit', 413)
                body.extend(chunk)
            try:
                payload = json.loads(body)
                if not isinstance(payload, dict) or set(payload) - {'sql', 'parameters'}:
                    raise ValueError()
            except (ValueError, TypeError, RecursionError):
                raise BridgeError('readonly_request_rejected') from None
            return await run_in_threadpool(current.query, identifier, owner,
                payload.get('sql'), payload.get('parameters', []))
        except BridgeError as error:
            raise HTTPException(error.status, detail={'error_code': error.code}) from None

    @router.get('/transactions/{identifier}/cursors/{cursor_id}')
    def page(identifier: str, cursor_id: str, request: Request):
        return invoke(request, lambda current, owner: current.page(identifier, owner, cursor_id))

    @router.delete('/transactions/{identifier}/cursors/{cursor_id}', status_code=204)
    def close_cursor(identifier: str, cursor_id: str, request: Request):
        invoke(request, lambda current, owner: current.close_cursor(identifier, owner, cursor_id))
        return Response(status_code=204)

    @router.delete('/transactions/{identifier}', status_code=204)
    def close_transaction(identifier: str, request: Request):
        invoke(request, lambda current, owner: current.close_transaction(identifier, owner))
        return Response(status_code=204)

    return router


router = create_router()
