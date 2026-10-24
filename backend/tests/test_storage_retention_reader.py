"""Cold history preserves original evidence and crosses only fixed read routes."""
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import threading
from urllib.error import HTTPError
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.decision_hourly.archive import archived_rows, read_sample
from app.gpt_runtime.settlement_kernel import load_kernel_rows
from app.remote_readonly.server import BridgeSettings, ReadOnlyManager, TOKEN_HEADER, create_router
from app.storage_retention import bundle, reader
from app.storage_retention.models import RetentionArchive, RetentionState
from app.storage_retention.policy import storage_root
from app.telemetry.domain import iso_time
from test_hourly_brief import sessions, HOUR

TOKEN = 'cold-reader-test-'+40*'x'
OWNER = '127.0.0.1'


@pytest.fixture(autouse=True)
def local_reader(monkeypatch):
    monkeypatch.setattr(reader, 'REMOTE_READONLY', None)


def seed_archive(sessions, status='online-removed', snapshot=None, *, when=HOUR, archive_id='hour', ids=(1, 2, 3)):
    engine = sessions.kw['bind']
    root = storage_root(engine)
    if snapshot is None:
        snapshot = {'cells': {'density': {'value': 1.48, 'status': 'good'}},
                    'total_ash': {'value': 8.7}, 'cadence_seconds': 30}
    decision = dict(slot=int(when.timestamp()//5)+6, sampled_at=iso_time(when+timedelta(seconds=30)),
                    context_key='original-context', fingerprint='original-fingerprint', snapshot=snapshot)
    raws = [dict(id=i, source_id='kepserver-main', point_key='level', batch_id='batch'+str(i),
                 received_at=iso_time(when+timedelta(seconds=i*30)), sample={'value': 60+i}) for i in ids]
    metadata = dict(database_id='original-db', start=iso_time(when), end=iso_time(when+timedelta(hours=1)))
    with bundle.ArchiveBundleWriter(root/'archives'/'2026-10', archive_id, metadata) as writer:
        writer.add_rows('decision_resolved', list(decision), [dict(decision, snapshot=json.dumps(snapshot))])
        writer.add_rows('telemetry_readings', list(raws[0]), [dict(row, sample=json.dumps(row['sample'])) for row in raws])
        path = writer.finish()
    with sessions.begin() as db:
        if not db.get(RetentionState, 'maintenance-v1'):
            db.add(RetentionState(key='maintenance-v1', value={'database_id': 'original-db'}))
        db.add(RetentionArchive(start=metadata['start'], end=metadata['end'], archive_id=archive_id,
            manifest=str(path.relative_to(root)), checksum=hashlib.sha256(path.read_bytes()).hexdigest(),
            created_at=metadata['end'], status=status, sample_count=1, raw_count=3,
            raw_min_id=min(ids), raw_max_id=max(ids),
            point_ranges={hashlib.sha256(b'kepserver-main\0level').hexdigest(): [min(ids), max(ids)]}))
    return path, decision, raws


@pytest.mark.parametrize('status', ['online-removed', 'deleting', 'retained-pins'])
def test_verified_cold_detail_is_lossless_for_active_archive_states(sessions, status):
    _path, original, _raws = seed_archive(sessions, status)
    with sessions() as db:
        assert reader.cold_rows(db, HOUR) == [original]
        assert archived_rows(db, iso_time(HOUR)) == [original]
        assert read_sample(db, original['slot']) == original
        projected = reader.cold_rows(db, HOUR, project_snapshot=lambda snapshot: snapshot['total_ash'])
        assert projected[0]['snapshot'] == {'value': 8.7}


def test_cold_raw_history_keeps_original_ids_source_filters_and_cursor(sessions):
    _path, _original, rows = seed_archive(sessions)
    with sessions() as db:
        assert reader.cold_history(db, 'kepserver-main', 'level', limit=2) == list(reversed(rows[1:]))
        assert reader.cold_history(db, 'kepserver-main', 'level', before_id=3) == list(reversed(rows[:2]))
        assert reader.cold_history(db, 'another-source', 'level') == []
        assert reader.cold_history(db, 'kepserver-main', 'another-point') == []


def test_cold_id_bounds_support_deep_pagination_without_assuming_clock_order(sessions, monkeypatch):
    seed_archive(sessions, when=HOUR-timedelta(hours=1), archive_id='older-clock', ids=(100, 101, 102))
    seed_archive(sessions)
    touched = []
    original = reader._manifest
    monkeypatch.setattr(reader, '_manifest', lambda db, row: touched.append(row['raw_max_id']) or original(db, row))
    with sessions() as db:
        assert [row['id'] for row in reader.cold_history(db, 'kepserver-main', 'level', limit=2)] == [102, 101]
        assert touched == [102]
        touched.clear()
        assert [row['id'] for row in reader.cold_history(db, 'kepserver-main', 'level', before_id=4, limit=2)] == [3, 2]
        assert touched == [3]


def test_new_point_without_cold_scope_does_not_open_any_archive_file(sessions, monkeypatch):
    seed_archive(sessions)
    monkeypatch.setattr(reader, '_manifest', lambda *args: (_ for _ in ()).throw(AssertionError('unrelated archive scan')))
    with sessions() as db:
        assert reader.cold_history_ceiling(db, 'new-source', 'new-point') == 0
        assert reader.cold_history(db, 'new-source', 'new-point') == []


def test_point_scope_ceiling_and_floor_bound_avoid_irrelevant_archive_reads(sessions, monkeypatch):
    seed_archive(sessions)
    with sessions() as db:
        assert reader.cold_history_ceiling(db, 'kepserver-main', 'level') == 3
    monkeypatch.setattr(reader, '_manifest', lambda *args: (_ for _ in ()).throw(AssertionError('older archive scan')))
    with sessions() as db:
        assert reader.cold_history(db, 'kepserver-main', 'level', floor_id=4) == []


@pytest.mark.parametrize('kind', ['checksum', 'database', 'escape', 'unresolved'])
def test_cold_corruption_never_returns_partial_or_current_replacement(sessions, kind):
    snapshot = {'cells': {'x': {'$reading': 999}}} if kind == 'unresolved' else None
    path, _original, _rows = seed_archive(sessions, snapshot=snapshot)
    with sessions.begin() as db:
        saved = db.get(RetentionArchive, iso_time(HOUR))
        if kind == 'checksum':
            saved.checksum = '0'*64
        elif kind == 'database':
            state = db.get(RetentionState, 'maintenance-v1')
            state.value = {'database_id': 'another-db'}
        elif kind == 'escape':
            saved.manifest = '../outside/manifest.json'
    with sessions() as db, pytest.raises(reader.ColdReadError):
        reader.cold_rows(db, HOUR)


@pytest.mark.parametrize('status', ['expiring', 'expired'])
def test_expired_detail_is_explicit_even_when_old_bundle_still_exists(sessions, status):
    seed_archive(sessions, status)
    with sessions() as db, pytest.raises(reader.ColdDetailExpired, match='一年'):
        reader.cold_rows(db, HOUR)


def test_older_local_schema_is_empty_cold_history_without_writes(sessions):
    engine = sessions.kw['bind']
    with engine.begin() as db:
        db.exec_driver_sql('DROP TABLE retention_archives')
    with sessions() as db:
        assert reader.cold_rows(db, HOUR) == []
        assert reader.cold_history(db, 'kepserver-main', 'level') == []


def test_kernel_uses_permanent_basis_before_expired_cold_reader(sessions, monkeypatch):
    evidence = [object()]
    monkeypatch.setattr('app.storage_retention.basis.load_basis_rows', lambda db, start: evidence)
    monkeypatch.setattr(reader, 'cold_rows', lambda *a, **k: (_ for _ in ()).throw(AssertionError('expired cold read')))
    with sessions() as db:
        assert load_kernel_rows(db, HOUR) is evidence


def test_new_cadence_cold_kernel_falls_back_without_losing_thirty_second_metadata(sessions):
    seed_archive(sessions)
    with sessions() as db:
        rows = load_kernel_rows(db, HOUR)
        assert len(rows) == 1 and rows[0].snapshot['cadence_seconds'] == 30


@pytest.fixture
def cold_client(sessions):
    manager = ReadOnlyManager(sessions.kw['bind'].url.database, BridgeSettings(TOKEN, (OWNER,)))
    app = FastAPI()
    app.include_router(create_router(manager), prefix='/api/v1')
    try:
        with TestClient(app, client=(OWNER, 1234)) as client:
            client.headers[TOKEN_HEADER] = TOKEN
            yield client
    finally:
        manager.close()


def test_cold_transport_authenticates_before_rejecting_arbitrary_paths_or_sql(cold_client, monkeypatch):
    touched = []
    monkeypatch.setattr(reader, 'transport_read', lambda *args: touched.append(args) or {'items': []})
    url = '/api/v1/readonly-db/cold/decisions'
    unauthorized = cold_client.get(url, params={'path': 'private-secret', 'sql': 'private-query'}, headers={TOKEN_HEADER: 'wrong'})
    assert unauthorized.status_code == 403 and touched == []
    rejected = cold_client.get(url, params={'start': iso_time(HOUR), 'path': 'private-secret'})
    assert rejected.status_code == 400 and 'private-secret' not in rejected.text and touched == []
    repeated = cold_client.get(url, params=[('start', iso_time(HOUR)), ('start', iso_time(HOUR))])
    assert repeated.status_code == 400 and touched == []


@pytest.mark.parametrize('params', [{'source_id': 's', 'point_key': 'p', 'limit': '502'},
                                    {'source_id': 's', 'point_key': 'p', 'before_id': '0'},
                                    {'source_id': 's', 'point_key': 'p', 'limit': 'text'},
                                    {'source_id': '', 'point_key': 'p'}])
def test_cold_transport_rejects_unbounded_or_invalid_history_before_file_read(cold_client, monkeypatch, params):
    monkeypatch.setattr(reader, 'transport_read', lambda *a: (_ for _ in ()).throw(AssertionError('file read')))
    assert cold_client.get('/api/v1/readonly-db/cold/telemetry', params=params).status_code == 400


def test_fixed_cold_transport_reads_actual_archive_in_readonly_session(sessions, cold_client):
    _path, original, _raws = seed_archive(sessions)
    result = cold_client.get('/api/v1/readonly-db/cold/decisions', params={'start': iso_time(HOUR)})
    assert result.status_code == 200 and result.json() == {'items': [original]}
    raw = cold_client.get('/api/v1/readonly-db/cold/telemetry', params={'source_id': 'kepserver-main', 'point_key': 'level', 'limit': 2})
    assert raw.status_code == 200 and [row['id'] for row in raw.json()['items']] == [3, 2]


def test_cold_transport_expiry_is_410_and_corruption_is_sanitized_503(sessions, cold_client):
    seed_archive(sessions, 'expired')
    url = '/api/v1/readonly-db/cold/decisions'
    assert cold_client.get(url, params={'start': iso_time(HOUR)}).status_code == 410
    with sessions.begin() as db:
        record = db.get(RetentionArchive, iso_time(HOUR))
        record.status, record.manifest = 'online-removed', '../private-path/manifest.json'
    result = cold_client.get(url, params={'start': iso_time(HOUR)})
    assert result.status_code == 503 and 'private-path' not in result.text


def test_concurrent_cold_reads_are_rejected_without_consuming_live_sql_capacity(cold_client, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def held(*_args):
        entered.set()
        assert release.wait(5)
        return {'items': []}

    monkeypatch.setattr(reader, 'transport_read', held)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(cold_client.get, '/api/v1/readonly-db/cold/decisions', params={'start': iso_time(HOUR)})
        try:
            assert entered.wait(3)
            busy = cold_client.get('/api/v1/readonly-db/cold/telemetry', params={'source_id': 's', 'point_key': 'p'})
            assert busy.status_code == 429 and busy.headers['Retry-After'] == '1'
            live = cold_client.post('/api/v1/readonly-db/transactions')
            assert live.status_code == 200
            identifier = live.json()['transaction_id']
            assert cold_client.delete('/api/v1/readonly-db/transactions/'+identifier).status_code == 204
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 200
    assert cold_client.get('/api/v1/readonly-db/cold/decisions', params={'start': iso_time(HOUR)}).status_code == 200


def test_cold_read_failure_releases_admission_slot(cold_client, monkeypatch):
    monkeypatch.setattr(reader, 'transport_read', lambda *_: (_ for _ in ()).throw(reader.ColdReadError('invalid archive')))
    url, params = '/api/v1/readonly-db/cold/decisions', {'start': iso_time(HOUR)}
    assert cold_client.get(url, params=params).status_code == 503
    monkeypatch.setattr(reader, 'transport_read', lambda *_: {'items': []})
    assert cold_client.get(url, params=params).status_code == 200


@pytest.mark.parametrize('status', [404, 403, 503, 410])
def test_remote_provider_has_no_local_fallback_and_supports_older_host_404(sessions, monkeypatch, status):
    calls = []

    class Connection:
        _base, _token = 'http://host.invalid/api/v1/readonly-db', TOKEN

        def __init__(self, *args, **kwargs):
            pass

        def _opener(self, request, **kwargs):
            calls.append(request)
            raise HTTPError(request.full_url, status, 'error', {}, io.BytesIO(b'private'))

        def close(self):
            pass

    monkeypatch.setattr(reader, 'REMOTE_READONLY', {'server_url': 'http://host.invalid', 'token': TOKEN})
    monkeypatch.setattr('remote_sqlite.RemoteConnection', Connection)
    monkeypatch.setattr(reader, '_local_cold_rows', lambda *a, **k: (_ for _ in ()).throw(AssertionError('local fallback')))
    with sessions() as db:
        if status == 404:
            assert reader.cold_rows(db, HOUR) == []
        else:
            expected = reader.ColdDetailExpired if status == 410 else reader.ColdReadError
            with pytest.raises(expected) as error:
                reader.cold_rows(db, HOUR)
            assert TOKEN not in str(error.value) and 'private' not in str(error.value)
    assert urlsplit(calls[0].full_url).path == '/api/v1/readonly-db/cold/decisions'
    assert {key.lower(): value for key, value in calls[0].header_items()}[TOKEN_HEADER.lower()] == TOKEN
