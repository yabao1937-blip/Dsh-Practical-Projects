"""Verified cold reads through fixed catalog identities, never caller file paths."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path, PureWindowsPath
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

from sqlalchemy import case, create_engine, func, select, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from ..config import REMOTE_READONLY
from ..decision_hourly.references import walk
from ..telemetry.domain import iso_time, parse_time
from . import bundle
from .models import RetentionArchive, RetentionState
from .policy import storage_root

ACTIVE = ('deleting', 'retained-pins', 'online-removed')
EXPIRED = ('expiring', 'expired')
MAX_ROWS = 100_000
MAX_BYTES = 64 * 1024 * 1024
MAX_CATALOG_READS = 256


class ColdReadError(ValueError):
    """Cold evidence is unavailable, corrupt, or beyond a bounded read budget."""


class ColdDetailExpired(ColdReadError):
    """The permanent hour remains available; its one-year details have expired."""


class ColdReadLimit(ColdReadError):
    """A bounded cold read stopped; callers may label available online rows partial."""


def _hour(start):
    try:
        value = parse_time(start.isoformat() if isinstance(start, datetime) else start)
    except (ValueError, TypeError, AttributeError):
        raise ColdReadError('Cold detail start must be a timezone-aware hour.') from None
    if value != value.replace(minute=0, second=0, microsecond=0):
        raise ColdReadError('Cold detail reads require a complete natural hour.')
    return value


def _available(db):
    # A test laptop may run newer code than its production host. Never create
    # tables or issue a failing remote SQL query against that older schema.
    return bool(db.scalar(text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='retention_archives'")))


def _catalog(db, condition=None, *, history_bound=None):
    if not _available(db):
        return iter(())
    existence = select(RetentionArchive.start)
    if condition is not None:
        existence = existence.where(condition)
    if db.scalar(existence.limit(1)) is None:
        return iter(())
    query = select(RetentionArchive.start, RetentionArchive.end, RetentionArchive.manifest,
                   RetentionArchive.checksum, RetentionArchive.status,
                   RetentionArchive.raw_min_id, RetentionArchive.raw_max_id,
                   RetentionArchive.sample_count, RetentionArchive.raw_count)
    if history_bound is not None:
        query = query.add_columns(history_bound.label('history_max_id'))
    if condition is not None:
        query = query.where(condition)
    # Legacy catalogs without bounds must be read first. Known bounds then let
    # deep ID pagination skip newer bundles and stop without assuming clock order.
    ordering = ((history_bound.desc().nulls_first(), RetentionArchive.end.desc())
                if history_bound is not None else (RetentionArchive.end.desc(),))
    # Projected read probes may expose an iterable rather than SQLAlchemy Result.
    return (row._mapping if hasattr(row, '_mapping') else row
            for row in db.execute(query.order_by(*ordering)))


def _manifest(db, archive):
    relative = archive['manifest']
    if not isinstance(relative, str) or not relative or len(relative) > 512:
        raise ColdReadError('Cold archive catalog path is invalid.')
    path = Path(relative)
    win_path = PureWindowsPath(relative)
    if path.is_absolute() or win_path.drive or win_path.root or '..' in path.parts:
        raise ColdReadError('Cold archive catalog path is invalid.')
    root = storage_root(db.bind).resolve()
    current = root
    for part in path.parts:
        current /= part
        if current.is_symlink() or (current.exists() and getattr(current.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ColdReadError('Cold archive path contains a link.')
    target = current.resolve()
    if not target.is_relative_to(root) or target.name != 'manifest.json':
        raise ColdReadError('Cold archive catalog path is invalid.')
    try:
        with target.open('rb') as source:
            raw = source.read(bundle.MAX_MANIFEST_BYTES+1)
        if len(raw) > bundle.MAX_MANIFEST_BYTES or hashlib.sha256(raw).hexdigest() != archive['checksum']:
            raise ColdReadError('Cold archive manifest checksum mismatch.')
        manifest = json.loads(raw)
        meta = manifest['metadata']
        state = db.get(RetentionState, 'maintenance-v1')
        identity = state.value.get('database_id') if state else None
        if (not identity or not isinstance(meta, dict) or meta.get('database_id') != identity
                or meta.get('start') != archive['start'] or meta.get('end') != archive['end']):
            raise ColdReadError('Cold archive database or interval identity mismatch.')
        return target
    except (OSError, UnicodeError, KeyError, TypeError, AttributeError, json.JSONDecodeError):
        raise ColdReadError('Cold archive manifest is unavailable or invalid.') from None


def _decode_object(value):
    try:
        result = json.loads(value, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())) if isinstance(value, str) else deepcopy(value)
        if not isinstance(result, dict):
            raise TypeError()
        return result
    except (ValueError, TypeError, RecursionError):
        raise ColdReadError('Cold archive JSON evidence is invalid.') from None


def _bounded(rows):
    try:
        oversized = len(rows) > MAX_ROWS or len(json.dumps(rows, allow_nan=False, ensure_ascii=False).encode()) > MAX_BYTES
    except (ValueError, TypeError, RecursionError):
        raise ColdReadError('Cold detail result is invalid.') from None
    if oversized:
        raise ColdReadLimit('Cold detail read exceeds the bounded result limit.')
    return rows


def _local_cold_rows(db, start, project_snapshot=None):
    start = _hour(start)
    key, end = iso_time(start), iso_time(start+timedelta(hours=1))
    catalog = list(_catalog(db, (RetentionArchive.start < end) & (RetentionArchive.end > key)))
    if len(catalog) > MAX_CATALOG_READS:
        raise ColdReadLimit('Cold detail catalog exceeds the bounded read limit.')
    found, expired = {}, False
    deadline = time.monotonic()+15
    for archive in catalog:
        if archive['status'] in EXPIRED:
            expired = expired or archive['sample_count'] > 0
            continue
        if archive['status'] not in ACTIVE:
            continue
        path = _manifest(db, archive)
        if archive['sample_count'] == 0:
            continue
        try:
            for row in bundle.read_table(path, 'decision_resolved', budget_seconds=max(.001, deadline-time.monotonic())):
                if time.monotonic() >= deadline:
                    raise ColdReadLimit('Cold detail read exceeds its time budget.')
                if not key <= row.get('sampled_at', '') < end:
                    continue
                snapshot = _decode_object(row.get('snapshot'))
                if any('$reading' in cell or '$manual' in cell or '$cell' in cell for cell in walk(snapshot)):
                    raise ColdReadError('Cold decision evidence is not self-contained.')
                item = dict(row, snapshot=snapshot)
                slot = item.get('slot')
                if type(slot) is not int or not int(start.timestamp()//5) <= slot < int((start+timedelta(hours=1)).timestamp()//5):
                    raise ColdReadError('Cold decision identity is invalid.')
                if slot in found and found[slot] != item:
                    raise ColdReadError('Conflicting cold decision evidence.')
                found[slot] = item
                if len(found) > MAX_ROWS:
                    raise ColdReadLimit('Cold detail read exceeds the bounded result limit.')
        except bundle.BundleBudgetExceeded:
            raise ColdReadLimit('Cold detail read exceeds its time or disk budget.') from None
        except (bundle.BundleError, OSError):
            raise ColdReadError('Cold archive content failed verification.') from None
    if expired and not found:
        raise ColdDetailExpired('逐次明细已按一年保留策略过期，小时统计与人工记录仍永久保留。')
    result = _bounded(sorted(found.values(), key=lambda row: row['slot']))
    if project_snapshot is not None:
        result = [dict(row, snapshot=project_snapshot(row['snapshot'])) for row in result]
    return result


def _history_parameters(source_id, point_key, before_id, limit):
    if (not isinstance(source_id, str) or not 1 <= len(source_id) <= 64
            or not isinstance(point_key, str) or not 1 <= len(point_key) <= 64
            or type(limit) is not int or not 1 <= limit <= 501
            or before_id is not None and (type(before_id) is not int or before_id < 1)):
        raise ColdReadError('Cold history parameters are invalid.')


def _history_scope(source_id, point_key, before_id=None, floor_id=None):
    key = hashlib.sha256((source_id+'\0'+point_key).encode()).hexdigest()
    path = '$.'+json.dumps(key)
    legacy = (func.json_type(RetentionArchive.point_ranges).is_(None) |
              (func.json_type(RetentionArchive.point_ranges) == 'null'))
    exists = func.json_type(RetentionArchive.point_ranges, path).is_not(None)
    lower = case((legacy, RetentionArchive.raw_min_id),
                 else_=func.json_extract(RetentionArchive.point_ranges, path+'[0]'))
    upper = case((legacy, RetentionArchive.raw_max_id),
                 else_=func.json_extract(RetentionArchive.point_ranges, path+'[1]'))
    condition = RetentionArchive.status.in_(ACTIVE) & (legacy | exists)
    if before_id is not None:
        condition &= (lower < before_id) | lower.is_(None)
    if floor_id is not None:
        condition &= (upper >= floor_id) | upper.is_(None)
    return condition, upper


def cold_history_ceiling(db, source_id, point_key, before_id=None):
    """Conservative selected-point upper ID; zero means no catalog can match."""
    _history_parameters(source_id, point_key, before_id, 1)
    if not _available(db):
        return 0
    condition, upper = _history_scope(source_id, point_key, before_id)
    maximum, unknown = db.execute(select(func.max(upper),
        func.max(case((upper.is_(None), 1), else_=0))).where(condition)).one()
    return None if unknown else maximum or 0


def _local_cold_history(db, source_id, point_key, before_id=None, limit=100, floor_id=None):
    _history_parameters(source_id, point_key, before_id, limit)
    found = {}
    deadline = time.monotonic()+15
    condition, upper = _history_scope(source_id, point_key, before_id, floor_id)
    for index, archive in enumerate(_catalog(db, condition, history_bound=upper)):
        if len(found) >= limit and archive['history_max_id'] is not None and archive['history_max_id'] < min(found):
            break
        if index >= MAX_CATALOG_READS:
            raise ColdReadLimit('Cold history catalog exceeds the bounded read limit.')
        path = _manifest(db, archive)
        if archive['raw_count'] == 0:
            continue
        try:
            for row in bundle.read_table(path, 'telemetry_readings', budget_seconds=max(.001, deadline-time.monotonic())):
                if time.monotonic() >= deadline:
                    raise ColdReadLimit('Cold history read exceeds its time budget.')
                if row.get('source_id') != source_id or row.get('point_key') != point_key:
                    continue
                identity = row.get('id')
                if type(identity) is not int or identity < 1:
                    raise ColdReadError('Cold history identity is invalid.')
                if before_id is not None and identity >= before_id:
                    continue
                if floor_id is not None and identity < floor_id:
                    continue
                item = dict(row, sample=_decode_object(row.get('sample')))
                if identity in found and found[identity] != item:
                    raise ColdReadError('Conflicting cold history evidence.')
                found[identity] = item
            if len(found) > limit:
                found = {key: found[key] for key in sorted(found, reverse=True)[:limit]}
        except bundle.BundleBudgetExceeded:
            raise ColdReadLimit('Cold history read exceeds its time or disk budget.') from None
        except (bundle.BundleError, OSError):
            raise ColdReadError('Cold archive content failed verification.') from None
    return _bounded(sorted(found.values(), key=lambda row: row['id'], reverse=True)[:limit])


def _remote(kind, parameters):
    """Fixed, no-redirect authenticated route with a hard response-byte bound."""
    from remote_sqlite import RemoteConnection, TOKEN_HEADER
    try:
        with closing(RemoteConnection(REMOTE_READONLY, timeout=20)) as connection:
            request = Request(connection._base+'/cold/'+kind+'?'+urlencode(parameters),
                              headers={TOKEN_HEADER: connection._token}, method='GET')
            with connection._opener(request, timeout=20) as response:
                raw = response.read(MAX_BYTES+1)
            if len(raw) > MAX_BYTES:
                raise ColdReadLimit('Cold detail response exceeds the bounded result limit.')
            value = json.loads(raw)
            if not isinstance(value, dict) or not isinstance(value.get('items'), list):
                raise ColdReadError('Cold detail response is invalid.')
            return _bounded(value['items'])
    except HTTPError as error:
        status = error.code
        error.close()
        if status == 404:  # An older host has no cold archive feature.
            return []
        if status == 410:
            raise ColdDetailExpired('逐次明细已按一年保留策略过期，小时统计与人工记录仍永久保留。') from None
        if status in (413, 429):
            raise ColdReadLimit('生产主机冷归档读取达到资源限制，请缩小查询范围。') from None
        raise ColdReadError('生产主机冷归档读取失败，请检查只读连接。') from None
    except (URLError, OSError, sqlite3.Error, ValueError, TypeError) as error:
        if isinstance(error, ColdReadError):
            raise
        raise ColdReadError('生产主机冷归档暂不可用，未使用本地数据替代。') from None


def cold_rows(db, start, project_snapshot=None):
    """Read one hour of fully hydrated decisions locally or from its host."""
    if REMOTE_READONLY:
        result = _remote('decisions', {'start': iso_time(_hour(start))})
        if project_snapshot is not None:
            result = [dict(row, snapshot=project_snapshot(row['snapshot'])) for row in result]
        return result
    return _local_cold_rows(db, start, project_snapshot)


def cold_history(db, source_id, point_key, before_id=None, limit=100, *, floor_id=None):
    """Return decoded raw rows, newest IDs first; no arbitrary SQL or path input."""
    _history_parameters(source_id, point_key, before_id, limit)
    if floor_id is not None and (type(floor_id) is not int or floor_id < 1):
        raise ColdReadError('Cold history floor identity is invalid.')
    if REMOTE_READONLY:
        values = dict(source_id=source_id, point_key=point_key, limit=limit)
        if before_id is not None:
            values['before_id'] = before_id
        return _remote('telemetry', values)
    return _local_cold_history(db, source_id, point_key, before_id, limit, floor_id)


def validate_transport_request(kind, parameters):
    """Validate caller scalars before opening the fixed production database."""
    if kind == 'decisions':
        return dict(start=iso_time(_hour(parameters['start'])))
    _history_parameters(parameters['source_id'], parameters['point_key'],
                        parameters.get('before_id'), parameters.get('limit', 100))
    return parameters


def transport_read(database_path, kind, parameters):
    """Host-only fixed-path reader; creates no database, WAL writer or tables."""
    path = Path(database_path).resolve()

    def connect():
        connection = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)
        connection.execute('PRAGMA query_only=ON')
        return connection

    engine = create_engine(URL.create('sqlite', database=str(path)), creator=connect, poolclass=NullPool)
    from ..telemetry.metadata import install_codec
    install_codec(engine, compact_writes=False)
    try:
        with Session(engine, autoflush=False) as db:
            db.execute(text('BEGIN'))
            items = (_local_cold_rows(db, parameters['start']) if kind == 'decisions' else
                     _local_cold_history(db, parameters['source_id'], parameters['point_key'],
                                         parameters.get('before_id'), parameters.get('limit', 100)))
        return {'items': items}
    finally:
        engine.dispose()
