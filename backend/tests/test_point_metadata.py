"""Physical SQLite checks, historical identity, backup and read-only transport."""
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from types import SimpleNamespace
import zlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from point_metadata import (Catalog, DIRECTORY, MARKER, MetadataError, copy_catalog,
                            catalog_required, validate_catalog_presence)
from app.database import Base
from app.telemetry.metadata import install_codec, pack, unpack
from app.telemetry.config import CollectorConfig, PointConfig
from app.telemetry.domain import definition_id, normalize, iso_time
from app.telemetry.models import TelemetryPoint, TelemetryReading
from app.telemetry.repository import TelemetryRepository, latest_view
from app.decision_hourly.models import DecisionCycleContext, DecisionCycleSample, DecisionCycleHour, DecisionEvidenceArchive
from app.decision_hourly.archive import archive_old_hour, archived_rows
from app.remote_readonly.server import ReadOnlyManager, BridgeSettings, TOKEN_HEADER, create_router


@pytest.fixture
def store(tmp_path):
    engine = create_engine('sqlite:///' + (tmp_path/'host.db').as_posix())
    catalog = install_codec(engine)
    Base.metadata.create_all(engine)
    yield engine, catalog
    engine.dispose()


def point(**changes):
    return PointConfig(**dict(dict(key='fixed-id', name='501 · 瞬时带煤量',
        node_id='ns=2;s=Channel.Device.501/COAL_CAP', device='Device', tag_name='501/COAL_CAP',
        item_id='Channel.Device.501/COAL_CAP', description='瞬时带煤量', unit='t/h'), **changes))


def test_fixed_metadata_absent_from_new_database_json_and_latest_still_complete(store):
    engine, catalog = store
    cfg = CollectorConfig(points=[point()], history_interval_seconds=60)
    repo = TelemetryRepository(sessionmaker(engine))
    repo.acquire(cfg, 'owner')
    repo.state(cfg, 'owner', 'connected')
    now = datetime.now(timezone.utc)
    sample = normalize(cfg.points[0], 123, 0, now, now, now)
    repo.persist(cfg, 'owner', dict(source_id=cfg.source_id, batch_id='batch-1', samples=[sample]))
    with engine.connect() as connection:
        config, latest = connection.execute(text('SELECT config,latest FROM telemetry_points')).one()
        raw = connection.scalar(text('SELECT sample FROM telemetry_readings'))
        for document in (config, latest, raw):
            assert MARKER in document
            assert 'Channel.Device' not in document
            assert '"node_id"' not in document
            assert '"item_id"' not in document
            assert '"device"' not in document
            assert '501 ·' not in document
        assert json.loads(raw)['value'] == 123
        assert json.loads(raw)['definition_id'] == definition_id(point().model_dump())
    with Session(engine) as db:
        view = latest_view(db, now=now)
        item = view['items'][0]
        assert item['device'] == 'Device'
        assert item['channel'] == 'Channel'
        assert item['item_id'] == point().item_id
        assert item['sample']['node_id'] == point().node_id
        assert db.scalar(select(TelemetryReading.sample)) == sample


def test_versions_preserve_original_identity_after_rename_and_config_change(store):
    engine, catalog = store
    original = point().model_dump()
    encoded = catalog.pack(original)
    renamed = dict(original, name='renamed', device='renamed device')
    changed = dict(original, node_id='ns=2;s=Other.Device.501/COAL_CAP')
    assert catalog.pack(renamed)[MARKER] != encoded[MARKER]
    assert catalog.pack(changed)[MARKER] != encoded[MARKER]
    assert catalog.unpack(encoded) == original
    assert definition_id(renamed) == definition_id(original)
    assert definition_id(changed) != definition_id(original)


def test_nested_decision_metadata_and_context_keys_round_trip_without_changing_fingerprint(store):
    engine, catalog = store
    item = point().item_id
    value = {'cells': {'density': {'value': 1.48, 'item_id': item,
             'raw_sample': {'name': point().name, 'node_id': point().node_id, 'value': 1.48}}},
             'point_definitions': {str(['source','point',item]): {'definition_id': 'original'}},
             'channel': 'A', 'name': 'user-supplied model name'}
    encoded = catalog.pack(value)
    assert item not in json.dumps(encoded)
    assert catalog.unpack(encoded) == value
    assert encoded['channel'] == 'A'
    assert encoded['name'] == value['name']


def test_backup_can_read_after_original_catalog_is_unavailable(store, tmp_path):
    engine, catalog = store
    with Session(engine) as db:
        db.add(TelemetryPoint(source_id='s', key='p', config=point().model_dump(), enabled=True))
        db.commit()
    dest = tmp_path/'backup'/'dense_medium.db'
    dest.parent.mkdir()
    with sqlite3.connect(engine.url.database) as source, sqlite3.connect(dest) as target:
        source.backup(target)
    copy_catalog(engine.url.database, dest)
    # Restore into a separate installation; do not rely on the original process cache.
    restored = create_engine('sqlite:///' + dest.as_posix())
    install_codec(restored)
    (tmp_path/DIRECTORY).rename(tmp_path/'original-disabled')
    with Session(restored) as db:
        assert db.get(TelemetryPoint, ('s','p')).config == point().model_dump()
    restored.dispose()


def test_missing_or_corrupt_catalog_never_falls_back_to_current_configuration(tmp_path):
    catalog = Catalog(tmp_path/DIRECTORY)
    packed = catalog.pack({'item_id': 'A.B.old', 'value': 1})
    path = tmp_path/DIRECTORY/(packed[MARKER]+'.json')
    path.write_text('{}', encoding='utf-8')
    with pytest.raises(MetadataError, match='checksum'):
        Catalog(tmp_path/DIRECTORY).unpack(packed)
    path.unlink()
    with pytest.raises(MetadataError, match='missing'):
        Catalog(tmp_path/DIRECTORY).unpack(packed)
    with pytest.raises(MetadataError):
        catalog.get('../other-file')


def test_missing_entire_catalog_rejects_backup_and_install_after_reader_rollback(store, tmp_path):
    engine, catalog = store
    assert catalog_required(engine.url.database)
    catalog.pack(point().model_dump())
    dest = tmp_path/'backup'/'dense_medium.db'
    dest.parent.mkdir()
    with sqlite3.connect(engine.url.database) as source, sqlite3.connect(dest) as target:
        source.backup(target)
    catalog.root.rename(tmp_path/'lost-catalog')
    install_codec(engine, compact_writes=False)
    with pytest.raises(MetadataError, match='directory is missing'):
        copy_catalog(engine.url.database, dest)
    with pytest.raises(MetadataError, match='directory is missing'):
        validate_catalog_presence(dest)
    with pytest.raises(MetadataError, match='directory is missing'):
        install_codec(engine)
    assert not catalog.root.exists()


def test_legacy_backup_requires_no_catalog_or_write_side_effect(tmp_path):
    database = tmp_path/'legacy.db'
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE original(value INTEGER)')
    engine = create_engine('sqlite:///' + database.as_posix())
    install_codec(engine, compact_writes=False)
    assert not catalog_required(database)
    validate_catalog_presence(database)
    copy_catalog(database, database)
    assert not (tmp_path/DIRECTORY).exists()
    engine.dispose()


def test_compact_maintenance_first_connection_has_required_pragmas(store):
    from app.storage_retention.worker import maintenance_sessions
    engine, _ = store
    maintenance, _ = maintenance_sessions(engine)
    try:
        with maintenance.connect() as connection:
            assert connection.exec_driver_sql('PRAGMA foreign_keys').scalar() == 1
            assert connection.exec_driver_sql('PRAGMA busy_timeout').scalar() == 250
    finally:
        maintenance.dispose()


def test_old_plain_database_json_is_still_readable(store):
    engine, catalog = store
    original = point().model_dump()
    with engine.begin() as connection:
        connection.execute(text('INSERT INTO telemetry_points(source_id,key,config,enabled) VALUES (:s,:k,:c,1)'),
                           dict(s='s',k='p',c=json.dumps(original)))
    with Session(engine) as db:
        assert db.get(TelemetryPoint, ('s','p')).config == original


def test_reader_first_release_can_read_compact_records_but_writes_legacy(store):
    engine, catalog = store
    original = point().model_dump()
    compact = catalog.pack(original)
    compatible = Catalog(catalog.root, compact_writes=False)
    assert compatible.unpack(compact) == original
    assert json.loads(compatible.dumps(original)) == original


def test_catalog_put_is_idempotent_and_preserves_input_objects(store):
    engine, catalog = store
    original = {'outer': [point().model_dump()]}
    before = json.dumps(original, sort_keys=True)
    first = catalog.pack(original)
    second = catalog.pack(original)
    assert first == second
    assert len(list(catalog.root.glob('*.json'))) == 1
    assert json.dumps(original, sort_keys=True) == before
    restored = catalog.unpack(first)
    restored['outer'][0]['device'] = 'changed'
    assert catalog.unpack(first) == original


def test_compressed_archive_keeps_only_references_and_restores_original(store):
    engine, catalog = store
    start = datetime(2026,10,1,tzinfo=timezone.utc)
    snapshot = {'cells': {'x': {'item_id':point().item_id,'value':1}}, 'total_ash':{'value':8.7}}
    with Session(engine) as db:
        db.add(DecisionCycleContext(key='ctx', definition={'point_definitions': {point().item_id:{'unit':'%'}}}))
        db.add(DecisionCycleSample(slot=int(start.timestamp())//5, sampled_at=iso_time(start),
            context_key='ctx', fingerprint='fingerprint', snapshot=snapshot))
        db.add(DecisionCycleHour(start=iso_time(start),end=iso_time(start+timedelta(hours=1)),
            generated_at=iso_time(start+timedelta(hours=1)),snapshot={}))
        db.commit()
        archive_old_hour(db, iso_time(start+timedelta(hours=1)))
        db.commit()
        archive = db.get(DecisionEvidenceArchive, iso_time(start))
        assert point().item_id.encode() not in zlib.decompress(archive.payload)
        assert archived_rows(db, iso_time(start))[0]['snapshot'] == snapshot


def test_authenticated_remote_catalog_restores_without_local_files(store):
    engine, catalog = store
    saved = catalog.pack(point().model_dump())
    token = 't'*40
    manager = ReadOnlyManager(engine.url.database, BridgeSettings(token, ('127.0.0.1',)))
    app = FastAPI()
    app.include_router(create_router(manager), prefix='/api/v1')
    try:
        with TestClient(app, client=('127.0.0.1',1234)) as client:
            url = '/api/v1/readonly-db/point-metadata/' + saved[MARKER]
            assert client.get(url).status_code == 403
            client.headers[TOKEN_HEADER] = token
            response = client.get(url)
            assert response.status_code == 200
            remote = Catalog(fetch=lambda key: client.get('/api/v1/readonly-db/point-metadata/'+key).json())
            assert remote.unpack(saved) == point().model_dump()
            assert remote.root is None
            assert client.get('/api/v1/readonly-db/point-metadata/'+'0'*64).status_code == 503
            assert manager._transactions == {}
    finally:
        manager.close()


def test_sql_projected_settlement_restores_metadata_before_consumption(store):
    from app.gpt_runtime.settlement_rows import load_rows
    from app.decision_hourly.ash_settlement import settlement_snapshot
    engine, catalog = store
    start = datetime(2026,10,1,tzinfo=timezone.utc)
    snapshot = {'cadence_seconds':60, 'sources':{'resolved_choices':{'x': {'item_id':point().item_id}}},
                'cells':{'belt501':{'item_id':point().item_id,'value':10,'status':'good'}},
                'total_ash':{'value':8.7},'coarse_ash':{'value':10},'heavy_amount':{'value':40}}
    with Session(engine) as db:
        db.add(DecisionCycleContext(key='ctx',definition={}))
        db.add(DecisionCycleSample(slot=int(start.timestamp())//5,sampled_at=iso_time(start),
            context_key='ctx',fingerprint='fp',snapshot=snapshot))
        db.commit()
        assert load_rows(db,start)[0].snapshot == settlement_snapshot(snapshot)


def test_one_reference_per_record_and_explicit_projection_contract(store):
    engine, catalog = store
    value = {'cells':[{'item_id':'Channel.Device.point','value':index} for index in range(30)],
             'unselected': {'item_id':'Channel.Device.other'}}
    encoded = catalog.pack(value)
    assert json.dumps(encoded).count(MARKER) == 1
    assert catalog.unpack(encoded) == value
    projected = {MARKER:encoded[MARKER], 'cells':encoded['cells']}
    assert catalog.unpack(projected, projected=True) == {'cells': value['cells']}
    with pytest.raises(MetadataError, match='position'):
        catalog.unpack(projected)
    with pytest.raises(MetadataError, match='position'):
        catalog.unpack({MARKER:encoded[MARKER], 'cells':[]}, projected=True)
    assert len(json.dumps(encoded)) < len(json.dumps(value))


def test_gpt_assay_pairing_reads_each_database_own_catalog(store):
    from contextlib import ExitStack
    from scripts.join_gpt_recent_assays import open_database, match_feature
    from pathlib import Path
    engine, catalog = store
    now = datetime.now(timezone.utc)
    original = point().model_dump()
    sample = normalize(point(),123,0,now,now,now)
    with Session(engine) as db:
        db.add(TelemetryPoint(source_id='s',key='p',config=original,enabled=True))
        db.add(TelemetryReading(source_id='s',point_key='p',batch_id='b',received_at=iso_time(now),sample=sample))
        db.commit()
    with ExitStack() as stack:
        database = open_database(stack,Path(engine.url.database),'test')
        assert database['points'][('s','p')] == original
        matched = match_feature([database],('s','p'),'coal_amount',now)
        assert matched['raw_sample'] == sample
        assert matched['value'] == 123
