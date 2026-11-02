"""Temporary-install tests for lossless history-only spool rollback/replay."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from test_project_update import collector_prepared, prepared, update


def point_batch(root, *, batch_id='a'*32, policy='point-history-v1'):
    path = root/'backend/data/telemetry-spool/test-source'/('batch-20261102001030000000-'+batch_id+'.json')
    path.parent.mkdir(parents=True,exist_ok=True)
    data = json.dumps(dict(source_id='test-source',batch_id=batch_id,storage_policy=policy,
                           samples=[dict(key='level',received_at='2026-11-02T00:10:30+00:00',value=43)])).encode()
    path.write_bytes(data)
    return path,data


@pytest.fixture
def point_install(collector_prepared):
    package,root,target,manifest,events = collector_prepared
    for name in update.POINT_HISTORY_FILES:
        if name.endswith('/worker.py'): continue
        payload,previous = package/'payload'/name,root/name
        payload.parent.mkdir(parents=True,exist_ok=True)
        previous.parent.mkdir(parents=True,exist_ok=True)
        payload.write_text('new '+name)
        previous.write_text('old '+name)
        manifest['files'][name] = dict(sha256=update.digest(payload),previous_sha256=update.digest(previous))
    manifest['collector_spool_compatibility'] = 'point-history-v1'
    return package,root,target,manifest,events


def test_retain_only_new_envelopes_then_release_idempotently(tmp_path):
    root = tmp_path/'host'
    point,data = point_batch(root)
    ordinary,ordinary_data = point_batch(root,batch_id='b'*32,policy='periodic-v1')
    partial = point.parent/'batch-interrupted.tmp'
    partial.write_bytes(b'partial original')
    cfg = {'source_id':'test-source'}
    assert update.point_history_spool(root,cfg) == 1
    held = point.parent/update.POINT_HISTORY_RETAINED/'batches'/point.name
    record_path = point.parent/update.POINT_HISTORY_RETAINED/'records'/point.name
    assert json.loads(record_path.read_bytes()) == dict(format=1,relative_path='test-source/'+point.name,
        sha256=update.digest(held),source_id='test-source',batch_id='a'*32,released=False)
    assert not point.exists() and held.read_bytes() == data
    assert ordinary.read_bytes() == ordinary_data and partial.read_bytes() == b'partial original'
    assert update.point_history_spool(root,cfg) == 0
    assert update.point_history_spool(root,cfg,restore=True) == 1
    assert point.read_bytes() == held.read_bytes() == data
    assert json.loads(record_path.read_bytes())['released'] is True
    assert update.point_history_spool(root,cfg,restore=True) == 0
    point.unlink()  # New collector committed and acknowledged the active copy.
    assert update.point_history_spool(root,cfg,restore=True) == 0 and held.read_bytes() == data


def test_intent_survives_interrupted_atomic_move(tmp_path,monkeypatch):
    root = tmp_path/'host'
    point,data = point_batch(root)
    transfer = update._point_spool_transfer
    monkeypatch.setattr(update,'_point_spool_transfer',lambda *a,**kw:(_ for _ in ()).throw(OSError('interrupted move')))
    with pytest.raises(OSError,match='interrupted move'):
        update.point_history_spool(root,{'source_id':'test-source'})
    assert point.read_bytes() == data
    record = point.parent/update.POINT_HISTORY_RETAINED/'records'/point.name
    assert json.loads(record.read_bytes())['released'] is False
    monkeypatch.setattr(update,'_point_spool_transfer',transfer)
    assert update.point_history_spool(root,{'source_id':'test-source'}) == 1
    assert update.point_history_spool(root,{'source_id':'test-source'},restore=True) == 1
    assert point.read_bytes() == data


def test_intent_fsync_failure_never_moves_the_original(tmp_path,monkeypatch):
    root = tmp_path/'host'
    point,data = point_batch(root)
    fsync = update.os.fsync
    monkeypatch.setattr(update.os,'fsync',lambda *_:(_ for _ in ()).throw(OSError('cannot flush intent')))
    with pytest.raises(OSError,match='flush intent'):
        update.point_history_spool(root,{'source_id':'test-source'})
    held = point.parent/update.POINT_HISTORY_RETAINED/'batches'/point.name
    assert point.read_bytes() == data and not held.exists()
    monkeypatch.setattr(update.os,'fsync',fsync)
    assert update.point_history_spool(root,{'source_id':'test-source'}) == 1
    assert held.read_bytes() == data


@pytest.mark.parametrize('acked_before_retry',[False,True])
def test_release_keeps_original_across_record_write_interruption(tmp_path,monkeypatch,acked_before_retry):
    root = tmp_path/'host'
    point,data = point_batch(root)
    cfg = {'source_id':'test-source'}
    update.point_history_spool(root,cfg)
    write = update._write_point_spool_record
    def interrupted(path,record):
        if record['released']: raise OSError('interrupted release record')
        return write(path,record)
    monkeypatch.setattr(update,'_write_point_spool_record',interrupted)
    with pytest.raises(OSError,match='release record'):
        update.point_history_spool(root,cfg,restore=True)
    held = point.parent/update.POINT_HISTORY_RETAINED/'batches'/point.name
    assert point.read_bytes() == held.read_bytes() == data
    if acked_before_retry: point.unlink()
    monkeypatch.setattr(update,'_write_point_spool_record',write)
    assert update.point_history_spool(root,cfg,restore=True) == 1
    assert point.read_bytes() == held.read_bytes() == data
    assert update.point_history_spool(root,cfg) == 1
    assert not point.exists() and held.read_bytes() == data


@pytest.mark.parametrize('corruption',['active','held','record','orphan'])
def test_conflicts_never_overwrite_original_bytes(tmp_path,corruption):
    root = tmp_path/'host'
    point,data = point_batch(root)
    cfg = {'source_id':'test-source'}
    update.point_history_spool(root,cfg)
    retained = point.parent/update.POINT_HISTORY_RETAINED
    held,record = retained/'batches'/point.name,retained/'records'/point.name
    if corruption == 'active': point.write_bytes(data+b' ')
    if corruption == 'held': held.write_bytes(data+b' ')
    if corruption == 'record':
        item = json.loads(record.read_bytes())
        item['relative_path'] = '../escape/'+point.name
        record.write_text(json.dumps(item))
    if corruption == 'orphan': record.unlink()
    before = {p:p.read_bytes() for p in (point,held,record) if p.exists()}
    with pytest.raises(ValueError): update.point_history_spool(root,cfg,restore=True)
    assert all(p.read_bytes() == content for p,content in before.items())


def test_resolves_installation_data_junction(tmp_path):
    root,storage = tmp_path/'host',tmp_path/'mapped-D-data'
    data = root/'backend/data'
    data.parent.mkdir(parents=True)
    storage.mkdir()
    if os.name == 'nt':
        result = subprocess.run(['cmd.exe','/c','mklink','/J',str(data),str(storage)],capture_output=True)
        if result.returncode: pytest.skip('Temporary directory junction creation is unavailable')
    else: data.symlink_to(storage,target_is_directory=True)
    point,original = point_batch(root)
    cfg = {'source_id':'test-source'}
    assert update.point_history_spool(root,cfg) == 1
    held = storage/'telemetry-spool/test-source'/update.POINT_HISTORY_RETAINED/'batches'/point.name
    assert held.read_bytes() == original
    assert update.point_history_spool(root,cfg,restore=True) == 1
    assert point.read_bytes() == original


@pytest.mark.parametrize('destination_exists',[False,True])
def test_rejects_cross_volume_transfer_without_moving_bytes(tmp_path,monkeypatch,destination_exists):
    source = tmp_path/'source.json'
    source.write_bytes(b'original')
    destination = tmp_path/'another-volume'/'target.json'
    destination.parent.mkdir()
    if destination_exists: destination.write_bytes(b'original')
    stat = Path.stat
    def different_volume(path,*args,**kwargs):
        result = stat(path,*args,**kwargs)
        if path == destination.parent:
            values = list(result)
            values[2] += 1
            return os.stat_result(values)
        return result
    monkeypatch.setattr(Path,'stat',different_volume)
    with pytest.raises(ValueError,match='same volume'): update._point_spool_transfer(source,destination)
    assert source.read_bytes() == b'original' and destination.exists() == destination_exists


def test_install_releases_only_after_all_new_sources_are_installed(point_install,monkeypatch):
    package,root,target,manifest,events = point_install
    point,data = point_batch(root)
    update.point_history_spool(root,{'source_id':'test-source'})
    def restart(path):
        assert point.read_bytes() == data
        assert all(update.digest(root/name) == info['sha256'] for name,info in manifest['files'].items())
        events.append('restart:'+target.read_text())
    monkeypatch.setattr(update,'restart',restart)
    update.apply(package,root,manifest)
    assert json.loads((root/'source-update.json').read_bytes())['collector_spool_compatibility'] == 'point-history-v1'
    assert point.read_bytes() == data


def test_failed_new_collector_retains_before_old_source_or_restart(point_install,monkeypatch):
    package,root,target,manifest,events = point_install
    ordinary,old_data = point_batch(root,batch_id='b'*32,policy='periodic-v1')
    observations = []
    def ready(*args,**kwargs):
        if target.read_text() == 'new collector code':
            observations.append(point_batch(root))
            raise TimeoutError('new collector failed after enqueue')
    def restart(path):
        if target.read_text() == 'old collector code':
            assert not observations[0][0].exists() and ordinary.read_bytes() == old_data
        events.append('restart:'+target.read_text())
    monkeypatch.setattr(update,'collector_ready',ready)
    monkeypatch.setattr(update,'restart',restart)
    with pytest.raises(TimeoutError,match='after enqueue'): update.apply(package,root,manifest)
    point,data = observations[0]
    held = point.parent/update.POINT_HISTORY_RETAINED/'batches'/point.name
    assert held.read_bytes() == data and not point.exists()
    assert all(update.digest(root/name) == info['previous_sha256'] for name,info in manifest['files'].items())
    monkeypatch.setattr(update,'collector_ready',lambda *args:None)
    update.apply(package,root,manifest)
    assert point.read_bytes() == held.read_bytes() == data


@pytest.mark.parametrize('failure',['retention','stop'])
def test_retention_failure_or_unconfirmed_stop_blocks_old_restart(point_install,monkeypatch,failure):
    package,root,target,manifest,events = point_install
    point,data = point_batch(root)
    real_action = update.point_history_spool
    def spool_action(*args,**kwargs):
        if not kwargs.get('restore'): raise OSError('cannot retain point spool')
        return real_action(*args,**kwargs)
    if failure == 'retention': monkeypatch.setattr(update,'point_history_spool',spool_action)
    stopped = 0
    def powershell(script,*args,**kwargs):
        nonlocal stopped
        events.append(script.name)
        if script.name == 'StopOwnedCollector.ps1':
            stopped += 1
            if stopped == 2 and failure == 'stop': raise OSError('cannot confirm stop')
    monkeypatch.setattr(update,'powershell',powershell)
    monkeypatch.setattr(update,'collector_ready',lambda *args:(_ for _ in ()).throw(TimeoutError('new failed')))
    with pytest.raises(RuntimeError,match='restart blocked'): update.apply(package,root,manifest)
    assert target.read_text() == 'new collector code' and 'restart:old collector code' not in events
    assert point.read_bytes() == data


@pytest.mark.parametrize('invalid',['tag','missing_source','profile'])
def test_capability_rejects_unknown_or_broader_updates_before_backup(point_install,invalid):
    package,root,_,manifest,events = point_install
    if invalid == 'tag': manifest['collector_spool_compatibility'] = 'unknown'
    if invalid == 'missing_source': manifest['files'].pop('backend/app/telemetry/config.py')
    if invalid == 'profile': manifest['profile'] = 'query-only-hotfix'
    with pytest.raises(ValueError,match='reviewed three-file'): update.apply(package,root,manifest)
    assert events == []


def test_baseline_rechecked_after_backup_before_services_stop(point_install,monkeypatch):
    package,root,target,manifest,events = point_install
    backup = update.backup_database
    def changed(*args,**kwargs):
        result = backup(*args,**kwargs)
        target.write_text('unknown edit during backup')
        return result
    monkeypatch.setattr(update,'backup_database',changed)
    with pytest.raises(ValueError,match='differs from update baseline'): update.apply(package,root,manifest)
    assert target.read_text() == 'unknown edit during backup' and events == ['backup-complete']
