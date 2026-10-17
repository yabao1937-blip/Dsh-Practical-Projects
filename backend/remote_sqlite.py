"""SQLite DB-API facade over the host's authenticated read-only transport.

Only query results live in client memory. There is no local SQLite file, database
download, automatic retry, or fallback when the production host is unavailable.
"""
from __future__ import annotations

import base64
import binascii
from collections import deque
import json
import re
import sqlite3
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


TOKEN_HEADER = 'X-DMCS-Read-Token'
_PREFIX = '/api/v1/readonly-db'
_LEADING = re.compile(r'\A(?:\s+|--[^\n]*(?:\n|$)|/\*[\s\S]*?\*/)*')
_READONLY = 'Remote database is read-only; production data was not changed.'


def _encode(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {'__dmcs_bytes__': base64.b64encode(bytes(value)).decode('ascii')}
    if isinstance(value, (tuple, list)):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    return value


def _decode(value):
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {'__dmcs_bytes__'}:
            try:
                return base64.b64decode(value['__dmcs_bytes__'], validate=True)
            except (TypeError, ValueError, binascii.Error):
                raise sqlite3.OperationalError('Invalid binary response from read-only host.') from None
        return {key: _decode(item) for key, item in value.items()}
    return value


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect must never forward the dedicated database token elsewhere.
        return None


class _RemoteError(sqlite3.OperationalError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class RemoteConnection:
    """Use with ``create_engine('sqlite://', creator=lambda: connect(config))``.

    A remote transaction opens lazily and lasts until DB-API rollback/commit.
    Both operations only close a read snapshot; neither can write on the host.
    ``opener`` is injectable for isolated protocol/SQLAlchemy tests.
    """

    def __init__(self, config, *, timeout=30, opener=None):
        origin = config.get('server_url', '')
        parsed = urlsplit(origin)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
                or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise sqlite3.ProgrammingError('Read-only server must be an HTTP(S) origin.')
        token = config.get('token')
        if not isinstance(token, str) or not token.strip() or '\r' in token or '\n' in token:
            raise sqlite3.ProgrammingError('A dedicated read-only database token is required.')
        self._base = origin.rstrip('/') + _PREFIX
        self._token = token
        self._timeout = timeout
        self._opener = opener or build_opener(ProxyHandler({}), _NoRedirect()).open
        self._transaction_id = None
        self._cursors = set()
        self._closed = False
        self._failed = False
        self.isolation_level = ''
        self.total_changes = 0

    @property
    def in_transaction(self):
        return self._transaction_id is not None

    def _check(self):
        if self._closed:
            raise sqlite3.ProgrammingError('Cannot operate on a closed database.')

    def _request(self, method, suffix, body=None):
        self._check()
        try:
            data = None if body is None else json.dumps(_encode(body), allow_nan=False,
                ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        except (TypeError, ValueError):
            raise sqlite3.ProgrammingError('Unsupported remote SQLite parameter type.') from None
        request = Request(self._base + suffix, data=data, method=method,
            headers={TOKEN_HEADER: self._token, 'Content-Type': 'application/json'})
        try:
            with self._opener(request, timeout=self._timeout) as response:
                if response.status == 204:
                    return None
                result = json.load(response)
                if not isinstance(result, dict):
                    raise ValueError()
                return result
        except HTTPError as error:
            code = None
            try:
                detail = json.loads(error.read(16_384)).get('detail', {})
                code = detail.get('error_code') if isinstance(detail, dict) else None
            except (ValueError, TypeError, AttributeError):
                pass
            finally:
                error.close()
            self._failed = True
            # Only known identifier-shaped codes are surfaced, never SQL, tokens,
            # query parameters, response HTML, or the remote authentication URL.
            code = code if isinstance(code, str) and re.fullmatch(r'readonly_[a-z_]+', code) else 'readonly_request_failed'
            raise _RemoteError(f'Read-only host rejected the request: {code} (HTTP {error.code}).', error.code) from None
        except (URLError, OSError, ValueError) as error:
            self._failed = True
            if isinstance(error, ValueError):
                raise _RemoteError('Invalid response from read-only host; no local database was used.') from None
            raise _RemoteError('Read-only host is unavailable; no local database was used.') from None

    def _begin(self):
        self._check()
        if self._failed:
            raise sqlite3.OperationalError('Remote read transaction failed; rollback is required before another query.')
        if self._transaction_id is None:
            result = self._request('POST', '/transactions')
            identifier = result.get('transaction_id')
            if not isinstance(identifier, str) or not identifier:
                self._failed = True
                raise sqlite3.OperationalError('Read-only host returned an invalid transaction identifier.')
            self._transaction_id = identifier
        return self._transaction_id

    def _end(self):
        self._check()
        identifier, self._transaction_id = self._transaction_id, None
        for cursor in list(self._cursors):
            cursor._invalidate()
        try:
            if identifier is not None:
                try:
                    self._request('DELETE', '/transactions/' + quote(identifier, safe=''))
                except _RemoteError as error:
                    if error.status != 404:
                        raise
        finally:
            self._failed = False

    def rollback(self):
        self._end()

    def commit(self):
        self._end()

    def close(self):
        if self._closed:
            return
        try:
            self._end()
        finally:
            self._closed = True

    def cursor(self, *args, **kwargs):
        self._check()
        if args or kwargs:
            raise sqlite3.ProgrammingError('Custom remote cursor factories are not supported.')
        cursor = RemoteCursor(self)
        self._cursors.add(cursor)
        return cursor

    def execute(self, sql, parameters=()):
        cursor = self.cursor()
        try:
            return cursor.execute(sql, parameters)
        except BaseException:
            cursor.close()
            raise

    def executemany(self, *args, **kwargs):
        raise sqlite3.ProgrammingError(_READONLY)

    def executescript(self, *args, **kwargs):
        raise sqlite3.ProgrammingError(_READONLY)

    def create_function(self, name, num_params, func, **kwargs):
        self._check()
        if (name.lower(), num_params) not in {('regexp', 2), ('floor', 1)}:
            raise sqlite3.ProgrammingError('Registering remote SQLite functions is not supported.')
        # SQLiteDialect installs these during connect. No Python callable or
        # extension is transmitted; the host controls its own SQL functions.

    def __enter__(self):
        self._check()
        return self

    def __exit__(self, kind, value, traceback):
        self.rollback() if kind else self.commit()


class RemoteCursor:
    arraysize = 1
    rowcount = -1
    lastrowid = None

    def __init__(self, connection):
        self.connection = connection
        self.description = None
        self._rows = deque()
        self._remote_id = None
        self._owner = None
        self._closed = False
        self._ended = False

    def _check(self):
        self.connection._check()
        if self._closed:
            raise sqlite3.ProgrammingError('Cannot operate on a closed cursor.')

    def _invalidate(self):
        self._rows.clear()
        self._remote_id = None
        self._ended = True

    def _release(self):
        identifier, self._remote_id = self._remote_id, None
        if identifier is not None and self._owner == self.connection._transaction_id:
            try:
                self.connection._request('DELETE', '/transactions/' + quote(self._owner, safe='')
                    + '/cursors/' + quote(identifier, safe=''))
            except _RemoteError as error:
                if error.status != 404:
                    raise

    def _page(self, page):
        try:
            columns, rows, done = page['columns'], page['rows'], page['done']
            identifier = page['cursor_id']
            if (not isinstance(columns, list) or any(not isinstance(name, str) for name in columns)
                    or not isinstance(rows, list) or not isinstance(done, bool)
                    or not isinstance(identifier, str) or not identifier
                    or any(not isinstance(row, list) or len(row) != len(columns) for row in rows)
                    or (not rows and not done)):
                raise ValueError()
            description = tuple((name, None, None, None, None, None, None) for name in columns)
            if self.description is not None and self.description != description:
                raise ValueError()
            self.description = description
            self._rows.extend(tuple(_decode(value) for value in row) for row in rows)
            self._remote_id = None if done else identifier
        except (KeyError, TypeError, ValueError):
            self.connection._failed = True
            raise sqlite3.OperationalError('Invalid query page from read-only host.') from None

    def execute(self, sql, parameters=()):
        self._check()
        if not isinstance(sql, str):
            raise sqlite3.ProgrammingError('SQL must be a string.')
        self._release()
        self._rows.clear()
        self.description = None
        self._ended = False
        cleaned = _LEADING.sub('', sql).strip().rstrip(';').strip()
        if re.fullmatch(r'PRAGMA\s+(?:main\.)?read_uncommitted', cleaned, re.IGNORECASE):
            # SQLiteDialect only reads this flag to discover isolation. The host
            # always uses a real read transaction and rejects mutable PRAGMAs.
            self.description = (('read_uncommitted', None, None, None, None, None, None),)
            self._rows.append((0,))
            self._owner = None
            return self
        if re.fullmatch(r'BEGIN(?:\s+TRANSACTION)?', cleaned, re.IGNORECASE):
            self._owner = self.connection._begin()
            return self
        if not re.match(r'(?:SELECT|WITH|EXPLAIN)\b', cleaned, re.IGNORECASE):
            raise sqlite3.ProgrammingError(_READONLY)
        self._owner = self.connection._begin()
        page = self.connection._request('POST', '/transactions/' + quote(self._owner, safe='')
            + '/queries', {'sql': sql, 'parameters': parameters})
        self._page(page)
        return self

    def fetchone(self):
        self._check()
        if self._ended:
            raise sqlite3.ProgrammingError('Remote cursor transaction has ended.')
        if self.description is None:
            return None
        if not self._rows and self._remote_id is not None:
            self._page(self.connection._request('GET', '/transactions/' + quote(self._owner, safe='')
                + '/cursors/' + quote(self._remote_id, safe='')))
        return self._rows.popleft() if self._rows else None

    def fetchmany(self, size=None):
        size = self.arraysize if size is None else size
        if not isinstance(size, int):
            raise sqlite3.ProgrammingError('Fetch size must be an integer.')
        if size < 0:
            return self.fetchall()
        result = []
        for _ in range(size):
            row = self.fetchone()
            if row is None:
                break
            result.append(row)
        return result

    def fetchall(self):
        rows = []
        while True:
            row = self.fetchone()
            if row is None:
                return rows
            rows.append(row)

    def close(self):
        if self._closed:
            return
        try:
            if not self.connection._closed:
                self._release()
        finally:
            self._invalidate()
            self._closed = True
            self.connection._cursors.discard(self)

    def setinputsizes(self, *args):
        pass

    def setoutputsize(self, *args):
        pass

    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row


def connect(config, **kwargs):
    return RemoteConnection(config, **kwargs)
