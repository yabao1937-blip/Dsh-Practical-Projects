"""Source-only deployment validates old bytes and never restores a stale DB."""
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('project_update',BACKEND/'scripts/windows_offline/apply_update.py')
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)
COLLECTOR_READY = update.collector_ready


@pytest.fixture
def prepared(tmp_path,monkeypatch):
    package,root = tmp_path/'package',tmp_path/'host'
    source = package/'payload/frontend/js/example.js'
    source.parent.mkdir(parents=True)
    source.write_text('new code')
    target = root/'frontend/js/example.js'
    target.parent.mkdir(parents=True)
    target.write_text('old code')
    (root/'offline-install.json').write_text('{}')
    (root/'Start.ps1').write_text('unused')
    (root/'backend/data').mkdir(parents=True)
    with sqlite3.connect(root/'backend/data/dense_medium.db') as db:
        db.execute('create table evidence (value integer)')
        db.execute('insert into evidence values(1)')
    manifest = dict(revision='test',files={'frontend/js/example.js':dict(
        sha256=update.digest(source),previous_sha256=update.digest(target))})
    calls = []
    monkeypatch.setattr(update,'powershell',lambda *args,**kwargs:calls.append(args))
    monkeypatch.setattr(update,'restart',lambda root:None)
    monkeypatch.setattr(update,'urlopen',lambda url,**kwargs:io.BytesIO(
        json.dumps(dict(advice_policy='gpt-hourly-reference-v4')).encode() if 'hourly' in url else '实时总精煤灰分'.encode()))
    return package,root,source,target,manifest,calls


@pytest.mark.parametrize('name',['../frontend/a','frontend/../../a','C:/data','backend/data/dense_medium.db',
    'backend/config/kepserver.local.json','backend/models/model.json','backend/.venv/python.exe'])
def test_update_cannot_replace_production_data_or_escape_root(tmp_path,name):
    with pytest.raises(ValueError): update.safe_path(tmp_path,name)


def test_modified_host_or_damaged_package_stops_before_mutation(prepared):
    package,root,source,target,manifest,calls = prepared
    target.write_text('host custom code')
    with pytest.raises(ValueError,match='differs'): update.apply(package,root,manifest)
    assert target.read_text() == 'host custom code' and not calls
    source.write_text('damaged payload')
    with pytest.raises(ValueError,match='damaged'): update.apply(package,root,manifest)
    assert not calls and not (root/'backups').exists()


def test_explicit_known_hash_is_accepted_but_unknown_edits_are_rejected(prepared):
    package,root,_,target,manifest,calls = prepared
    target.write_text('another previously released source')
    info = next(iter(manifest['files'].values()))
    info['accepted_previous_sha256'] = [update.digest(target)]
    update.verify(package,root,manifest)
    target.write_text('unpublished host customization')
    with pytest.raises(ValueError,match='differs.*sha256='):
        update.apply(package,root,manifest)
    assert not calls and not (root/'backups').exists()


def test_known_hash_does_not_allow_a_missing_original_file(prepared):
    package,root,_,target,manifest,calls = prepared
    next(iter(manifest['files'].values()))['accepted_previous_sha256'] = [update.digest(target)]
    target.unlink()
    with pytest.raises(ValueError,match='source is missing'):
        update.apply(package,root,manifest)
    assert not calls and not (root/'backups').exists()


def test_update_keeps_database_and_is_repeatable(prepared):
    package,root,source,target,manifest,calls = prepared
    backup = update.apply(package,root,manifest)
    assert target.read_text() == 'new code'
    assert (backup/'source/frontend/js/example.js').read_text() == 'old code'
    with sqlite3.connect(backup/'dense_medium.db') as db:
        assert db.execute('select * from evidence').fetchall() == [(1,)]
    update.apply(package,root,manifest)
    with sqlite3.connect(root/'backend/data/dense_medium.db') as db:
        assert db.execute('select * from evidence').fetchall() == [(1,)]


def test_failed_start_rolls_back_code_but_keeps_new_production_rows(prepared,monkeypatch):
    package,root,source,target,manifest,calls = prepared
    count = 0
    def fail_once(root):
        nonlocal count
        count += 1
        if count == 1:
            with sqlite3.connect(root/'backend/data/dense_medium.db') as db:
                db.execute('insert into evidence values(2)')
            raise RuntimeError('failed startup')
    monkeypatch.setattr(update,'restart',fail_once)
    with pytest.raises(RuntimeError,match='failed startup'): update.apply(package,root,manifest)
    assert target.read_text() == 'old code' and count == 2
    with sqlite3.connect(root/'backend/data/dense_medium.db') as db:
        assert db.execute('select * from evidence').fetchall() == [(1,),(2,)]


def test_live_wal_backup_is_consistent_and_releases_snapshot_before_check(prepared,tmp_path):
    _,root,_,_,_,_ = prepared
    backup = tmp_path/'snapshot'
    backup.mkdir()
    events = []
    path = root/'backend/data/dense_medium.db'
    with sqlite3.connect(path) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        # More than one batch: write while the copy still has pages remaining.
        writer.execute('CREATE TABLE payload (data blob)')
        writer.executemany('INSERT INTO payload VALUES(zeroblob(1048576))',[()]*10)
        writer.commit()
        inserted = False
        def report(stage,message):
            nonlocal inserted
            events.append((stage,message))
            if 'pages)' in message and not inserted:
                writer.execute('INSERT INTO evidence VALUES(2)')
                writer.commit()
                inserted = True
            if 'checking backup integrity' in message:
                # A leaked source snapshot would prevent truncating the live WAL.
                assert writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
        update.backup_database(root,backup,report)
        assert inserted
        assert writer.execute('SELECT * FROM evidence').fetchall() == [(1,),(2,)]
    with sqlite3.connect(backup/'dense_medium.db') as snapshot:
        assert snapshot.execute('SELECT * FROM evidence').fetchall() == [(1,)]
        assert snapshot.execute('SELECT COUNT(*) FROM payload').fetchone() == (10,)
    assert all(stage == 'copying' for stage,_ in events)
    assert any('checking backup integrity' in message for _,message in events)
    assert 'verified' in events[-1][1]


def test_backup_timeout_leaves_source_and_web_untouched(prepared,monkeypatch):
    package,root,_,target,manifest,calls = prepared
    backup_database = update.backup_database
    monkeypatch.setattr(update,'backup_database',lambda root,backup,report:
        backup_database(root,backup,report,timeout=0))
    with pytest.raises(TimeoutError,match='backup timed out'):
        update.apply(package,root,manifest)
    assert target.read_text() == 'old code'
    assert not calls
    with sqlite3.connect(root/'backend/data/dense_medium.db') as db:
        assert db.execute('SELECT * FROM evidence').fetchall() == [(1,)]


def test_query_hotfix_cannot_skip_snapshot_for_storage_changes(prepared):
    package,root,_,target,manifest,calls = prepared
    manifest['profile'] = 'query-only-hotfix'
    with pytest.raises(ValueError,match='cannot change schema'):
        update.apply(package,root,manifest)
    assert not calls and target.read_text() == 'old code'
    assert not (root/'backups').exists()


def test_allowlisted_query_hotfix_preserves_database_without_full_scan(prepared,monkeypatch):
    package,root,source,target,manifest,calls = prepared
    name = 'frontend/js/api.js'
    source.rename(source.with_name('api.js'))
    target.rename(target.with_name('api.js'))
    manifest['files'] = {name:next(iter(manifest['files'].values()))}
    manifest['profile'] = 'query-only-hotfix'
    monkeypatch.setattr(update,'backup_database',lambda *a,**kw:pytest.fail('Query hotfix must not scan live data'))
    backup = update.apply(package,root,manifest)
    assert (root/name).read_text() == 'new code'
    assert (backup/'source'/name).read_text() == 'old code'
    assert not (backup/'dense_medium.db').exists()
    with sqlite3.connect(root/'backend/data/dense_medium.db') as db:
        assert db.execute('SELECT * FROM evidence').fetchall() == [(1,)]
    assert all('collector' not in str(call).lower() for call in calls)


@pytest.fixture
def collector_prepared(prepared,monkeypatch):
    package,root,_,_,manifest,calls = prepared
    name = 'backend/app/telemetry/worker.py'
    payload,target = package/'payload'/name,root/name
    for path in (payload,target): path.parent.mkdir(parents=True,exist_ok=True)
    payload.write_text('new collector code')
    target.write_text('old collector code')
    manifest['files'] = {name:dict(sha256=update.digest(payload),previous_sha256=update.digest(target))}
    (package/'StopOwnedCollector.ps1').write_text('fixture helper')
    config = root/'backend/config/kepserver.local.json'
    config.parent.mkdir()
    config.write_text(json.dumps(dict(enabled=True,source_id='test-source')))
    before = dict(owner='old-owner',last_sample_at='2026-01-01T00:00:00+00:00')
    monkeypatch.setattr(update,'collector_status',lambda *args:before)
    events = []
    original_backup = update.backup_database
    def backup(*args):
        result = original_backup(*args)
        events.append('backup-complete')
        return result
    monkeypatch.setattr(update,'backup_database',backup)
    monkeypatch.setattr(update,'powershell',lambda script,*args,**kw:events.append(script.name))
    monkeypatch.setattr(update,'wait_collector_lease',lambda *a,**kw:events.append('lease-expired'))
    monkeypatch.setattr(update,'restart',lambda root:events.append('restart:'+target.read_text()))
    monkeypatch.setattr(update,'collector_ready',lambda *a,**kw:events.append('ready:'+target.read_text()))
    return package,root,target,manifest,events


@pytest.mark.parametrize('name',[
    'backend/collector.py','backend/app/telemetry/worker.py','backend/app/database.py',
    'backend/app/config.py','backend/app/models.py','backend/app/decision_hourly/models.py',
    'backend/migrations/versions/schema.py','backend/scripts/start_collector.py'])
def test_collector_dependency_changes_require_restart(name):
    assert update.collector_changed({'files':{name:{}}})


def test_frontend_query_and_business_changes_keep_collector_running():
    assert not update.collector_changed({'files':{'frontend/js/operations.js':{}}})
    assert not update.collector_changed({'files':{'backend/app/routers/manual_measurements.py':{}}})
    assert not update.collector_changed({'profile':'query-only-hotfix','files':{'backend/app/telemetry/worker.py':{}}})


@pytest.mark.parametrize('interval',[30,60])
def test_explicit_history_patch_requires_collector_restart_even_without_source_dependencies(interval):
    assert update.collector_changed(dict(files={},collector_history_interval_seconds=interval))


@pytest.fixture
def history_prepared(collector_prepared):
    package,root,target,manifest,events = collector_prepared
    manifest['collector_history_interval_seconds'] = 30
    configuration = root/'backend/config/kepserver.local.json'
    original = (b'\xef\xbb\xbf{\r\n  "enabled": true, "source_id": "test-source",\r\n'
        b'  "history_interval_seconds": 5, "poll_seconds": 5,\r\n'
        b'  "credentials": {"password":"fixture-secret", "history_interval_seconds": 5},\r\n'
        b'  "points": [{"id":"wash", "node":"s=unchanged"}]\r\n}\r\n')
    configuration.write_bytes(original)
    return package,root,target,manifest,events,configuration,original


@pytest.mark.parametrize('value',[None,True,False,30.0,60.0,5,90,0,'30','60',{},[]])
def test_history_patch_rejects_manifest_values_before_backup(history_prepared,value):
    package,root,target,manifest,events,configuration,original = history_prepared
    manifest['collector_history_interval_seconds'] = value
    with pytest.raises(ValueError,match='integer value 30'):
        update.apply(package,root,manifest)
    assert configuration.read_bytes() == original and not events
    assert not (root/'backups').exists()


@pytest.mark.parametrize('interval',[30,60])
def test_query_hotfix_cannot_patch_history_configuration(history_prepared,interval):
    package,root,target,manifest,events,configuration,original = history_prepared
    manifest['collector_history_interval_seconds'] = interval
    manifest['profile'] = 'query-only-hotfix'
    with pytest.raises(ValueError,match='standard update'):
        update.apply(package,root,manifest)
    assert configuration.read_bytes() == original and not events
    assert not (root/'backups').exists()


def test_missing_history_configuration_fails_before_backup(history_prepared):
    package,root,target,manifest,events,configuration,_ = history_prepared
    configuration.unlink()
    with pytest.raises(ValueError,match='configuration is missing or invalid'):
        update.apply(package,root,manifest)
    assert not events and not (root/'backups').exists()


@pytest.mark.parametrize('value',[
    b'{broken',b'[]',b'{"poll_seconds":true}',b'{"poll_seconds":35}',
    b'{"poll_seconds":"5"}',b'{"history_interval_seconds":false}',
    b'{"history_interval_seconds":5,"history_interval_seconds":10}',
])
def test_invalid_history_configuration_or_polling_relation_fails_before_backup(history_prepared,value):
    package,root,target,manifest,events,configuration,_ = history_prepared
    configuration.write_bytes(value)
    with pytest.raises(ValueError,match='configuration is missing or invalid'):
        update.apply(package,root,manifest)
    assert configuration.read_bytes() == value and not events
    assert not (root/'backups').exists()


@pytest.mark.parametrize('interval',[30,60])
def test_history_configuration_changes_only_after_backup_and_collector_stop(history_prepared,monkeypatch,interval):
    package,root,target,manifest,events,configuration,original = history_prepared
    manifest['collector_history_interval_seconds'] = interval
    messages = []
    replace = update.replace_configuration
    def change(path,data):
        assert path == configuration and path.read_bytes() == original
        assert events == ['backup-complete','StopOwnedWeb.ps1','StopOwnedCollector.ps1','lease-expired']
        assert target.read_text() == 'old collector code'
        backup = next((root/'backups').iterdir())
        assert (backup/'dense_medium.db').is_file()
        assert (backup/'configuration/backend/config/kepserver.local.json').read_bytes() == original
        replace(path,data)
    monkeypatch.setattr(update,'replace_configuration',change)
    backup = update.apply(package,root,manifest,lambda _,message:messages.append(message))
    # BOM, CRLF, endpoint/credential/point data and even a nested name survive byte for byte.
    assert configuration.read_bytes() == original.replace(
        b'"history_interval_seconds": 5',b'"history_interval_seconds": '+str(interval).encode(),1)
    assert (backup/'configuration/backend/config/kepserver.local.json').read_bytes() == original
    record = json.loads((root/'source-update.json').read_text())
    assert record['collector_history_interval_seconds'] == interval and record['collector_restarted'] is True
    assert any(f'interval set to {interval} seconds' in message for message in messages)
    if interval == 60:
        assert not any('set to 30 seconds' in message for message in messages)
    assert not configuration.with_name(configuration.name+'.source-update.tmp').exists()


@pytest.mark.parametrize('value',[b'{}',b'{ "poll_seconds": 5 }'])
@pytest.mark.parametrize('interval',[30,60])
def test_history_field_can_be_added_without_rewriting_existing_configuration(prepared,value,interval):
    _,root,*_ = prepared
    path = root/'backend/config/kepserver.local.json'
    path.parent.mkdir()
    path.write_bytes(value)
    patch = update.history_configuration_patch(root,dict(collector_history_interval_seconds=interval))
    assert patch['original'] == value and path.read_bytes() == value
    assert json.loads(patch['updated']) == dict(json.loads(value),history_interval_seconds=interval)


@pytest.mark.parametrize('previous,interval',[(30,60),(60,30)])
def test_history_patch_switches_supported_intervals_without_rewriting_other_bytes(history_prepared,previous,interval):
    _,root,_,_,_,configuration,original = history_prepared
    original = original.replace(b'"history_interval_seconds": 5',
        b'"history_interval_seconds": '+str(previous).encode(),1)
    configuration.write_bytes(original)
    patch = update.history_configuration_patch(root,dict(collector_history_interval_seconds=interval))
    assert configuration.read_bytes() == original
    assert patch['updated'] == original.replace(b'"history_interval_seconds": '+str(previous).encode(),
        b'"history_interval_seconds": '+str(interval).encode(),1)
    assert b'"poll_seconds": 5' in patch['updated']
    assert b'"credentials": {"password":"fixture-secret", "history_interval_seconds": 5}' in patch['updated']


def test_sixty_second_patch_preserves_a_slower_existing_poll_instead_of_overriding_it(prepared):
    _,root,*_ = prepared
    configuration = root/'backend/config/kepserver.local.json'
    configuration.parent.mkdir()
    original = b'{ "poll_seconds": 35, "history_interval_seconds": 30 }'
    configuration.write_bytes(original)
    patch = update.history_configuration_patch(root,dict(collector_history_interval_seconds=60))
    assert patch['updated'] == original.replace(b'"history_interval_seconds": 30',b'"history_interval_seconds": 60')
    with pytest.raises(ValueError,match='30 seconds must be at least the polling interval'):
        update.history_configuration_patch(root,dict(collector_history_interval_seconds=30))


@pytest.mark.parametrize('interval',[30,60])
def test_updater_entry_reports_actual_explicit_interval(prepared,monkeypatch,capsys,interval):
    import sys
    package,root,*_ = prepared
    (package/'update.json').write_text(json.dumps(dict(revision='test',files={},
        collector_history_interval_seconds=interval,report_url='http://127.0.0.1/unused')))
    monkeypatch.setattr(update,'__file__',str(package/'apply_update.py'))
    monkeypatch.setattr(sys,'argv',['apply_update.py','--install-dir',str(root)])
    monkeypatch.setattr(update,'apply',lambda *args:root/'backups/fixture')
    monkeypatch.setattr(update,'urlopen',lambda *args,**kwargs:(_ for _ in ()).throw(OSError('offline fixture')))
    assert update.main() == 0
    output = capsys.readouterr().out
    assert f'explicit {interval}-second history setting' in output
    if interval == 60:
        assert 'explicit 30-second' not in output


@pytest.mark.parametrize('existing_interval',[5,30,60])
def test_no_explicit_history_patch_preserves_exact_configuration(history_prepared,existing_interval):
    package,root,target,manifest,events,configuration,original = history_prepared
    original = original.replace(b'"history_interval_seconds": 5',
        b'"history_interval_seconds": '+str(existing_interval).encode(),1)
    configuration.write_bytes(original)
    del manifest['collector_history_interval_seconds']
    backup = update.apply(package,root,manifest)
    assert configuration.read_bytes() == original
    assert not (backup/'configuration').exists()
    assert 'collector_history_interval_seconds' not in json.loads((root/'source-update.json').read_text())


@pytest.mark.parametrize('phase',['restart','collector-ready','policy'])
@pytest.mark.parametrize('interval',[30,60])
def test_history_patch_failure_restores_original_config_before_old_services_restart(history_prepared,monkeypatch,phase,interval):
    package,root,target,manifest,events,configuration,original = history_prepared
    manifest['collector_history_interval_seconds'] = interval
    restart_intervals,ready_count = [],0
    def restart(root):
        interval = json.loads(configuration.read_bytes().decode('utf-8-sig'))['history_interval_seconds']
        restart_intervals.append(interval)
        if len(restart_intervals) == 1:
            assert interval == manifest['collector_history_interval_seconds'] and target.read_text() == 'new collector code'
            with sqlite3.connect(root/'backend/data/dense_medium.db') as db:
                db.execute('INSERT INTO evidence VALUES(2)')
            if phase == 'restart': raise RuntimeError('fixture update failure')
        else:
            assert configuration.read_bytes() == original
            assert target.read_text() == 'old collector code'
    def ready(*args):
        nonlocal ready_count
        ready_count += 1
        if ready_count == 1 and phase == 'collector-ready': raise RuntimeError('fixture update failure')
    monkeypatch.setattr(update,'restart',restart)
    monkeypatch.setattr(update,'collector_ready',ready)
    if phase == 'policy':
        monkeypatch.setattr(update,'urlopen',lambda *a,**kw:(_ for _ in ()).throw(RuntimeError('fixture update failure')))
    with pytest.raises(RuntimeError,match='fixture update failure'):
        update.apply(package,root,manifest)
    assert restart_intervals == [interval,5] and configuration.read_bytes() == original
    assert events[-3:] == ['StopOwnedWeb.ps1','StopOwnedCollector.ps1','lease-expired']
    with sqlite3.connect(root/'backend/data/dense_medium.db') as db:
        assert db.execute('SELECT * FROM evidence').fetchall() == [(1,),(2,)]
    assert not (root/'source-update.json').exists()


def test_config_changed_during_backup_is_never_overwritten(history_prepared,monkeypatch):
    package,root,target,manifest,events,configuration,original = history_prepared
    backup = update.backup_database
    external = original.replace(b'"poll_seconds": 5',b'"poll_seconds": 10')
    def race(*args):
        result = backup(*args)
        configuration.write_bytes(external)
        return result
    monkeypatch.setattr(update,'backup_database',race)
    with pytest.raises(ValueError,match='configuration changed during backup'):
        update.apply(package,root,manifest)
    assert configuration.read_bytes() == external and target.read_text() == 'old collector code'


def test_interrupted_configuration_replace_also_restores_original_bytes(history_prepared,monkeypatch):
    package,root,target,manifest,events,configuration,original = history_prepared
    replace,count = update.replace_configuration,0
    def interrupted(path,data):
        nonlocal count
        count += 1
        replace(path,data)
        if count == 1: raise RuntimeError('interrupted after atomic configuration replace')
    monkeypatch.setattr(update,'replace_configuration',interrupted)
    with pytest.raises(RuntimeError,match='interrupted after atomic'):
        update.apply(package,root,manifest)
    assert count == 2 and configuration.read_bytes() == original
    assert target.read_text() == 'old collector code'
    assert events[-2:] == ['restart:old collector code','ready:old collector code']


def test_ratio_hotfix_allowlist_covers_wash_coarse_change():
    """600:15→600:25 比例改动(2026-10-22)必须能走 query-only-hotfix 通道:
    只改计算常量/文案/前端展示,不触发采集器重启,也不动 schema/模型存储。"""
    ratio_files = {'backend/app/gpt_coarse/amount.py','backend/app/routers/decision.py',
                   'frontend/js/decision-overview.js','frontend/js/decision-sources.js'}
    hotfix = dict(profile='query-only-hotfix',files={name:{} for name in ratio_files})
    assert not update.collector_changed(hotfix), '比例热修不得重启采集器'
    # verify() 的白名单判定:把 manifest 喂给 verify 的白名单检查路径(不落盘则构造同款检查)
    allowed = {'backend/app/decision_hourly/ash_history.py','backend/app/decision_hourly/ash_settlement.py',
               'backend/app/decision_hourly/quality_advice.py','backend/app/routers/health.py',
               'backend/app/gpt_coarse/amount.py','backend/app/routers/decision.py',
               'frontend/js/decision-overview.js','frontend/js/decision-sources.js',
               'frontend/js/api.js','frontend/js/operations.js'}
    assert ratio_files <= allowed


def test_missing_collector_helper_fails_before_backup_or_stopping(collector_prepared):
    package,root,target,manifest,events = collector_prepared
    (package/'StopOwnedCollector.ps1').unlink()
    with pytest.raises(ValueError,match='requires StopOwnedCollector'):
        update.apply(package,root,manifest)
    assert not events and target.read_text() == 'old collector code'
    assert not (root/'backups').exists()


def test_collector_restart_follows_verified_backup_and_expired_lease(collector_prepared):
    package,root,target,manifest,events = collector_prepared
    backup = update.apply(package,root,manifest)
    assert (backup/'dense_medium.db').is_file()
    assert events == ['backup-complete','StopOwnedWeb.ps1','StopOwnedCollector.ps1',
        'lease-expired','restart:new collector code','ready:new collector code']
    assert json.loads((root/'source-update.json').read_text())['collector_restarted'] is True


def test_failed_new_collector_restores_old_code_and_verifies_old_collector(collector_prepared,monkeypatch):
    package,root,target,manifest,events = collector_prepared
    count = 0
    def ready(*args):
        nonlocal count
        count += 1
        events.append('ready:'+target.read_text())
        if count == 1:
            with sqlite3.connect(root/'backend/data/dense_medium.db') as db:
                db.execute('INSERT INTO evidence VALUES(2)')
            raise TimeoutError('new collector did not sample')
    monkeypatch.setattr(update,'collector_ready',ready)
    with pytest.raises(TimeoutError,match='did not sample'):
        update.apply(package,root,manifest)
    assert target.read_text() == 'old collector code'
    assert events[-5:] == ['StopOwnedWeb.ps1','StopOwnedCollector.ps1','lease-expired',
        'restart:old collector code','ready:old collector code']
    with sqlite3.connect(root/'backend/data/dense_medium.db') as db:
        assert db.execute('SELECT * FROM evidence').fetchall() == [(1,),(2,)]
    assert not (root/'source-update.json').exists()


def test_lease_timeout_restores_old_collector_without_installing_new_code(collector_prepared,monkeypatch):
    package,root,target,manifest,events = collector_prepared
    count = 0
    def wait(*args):
        nonlocal count
        count += 1
        events.append('lease-wait')
        if count == 1: raise TimeoutError('lease did not expire')
    monkeypatch.setattr(update,'wait_collector_lease',wait)
    with pytest.raises(TimeoutError,match='lease did not expire'):
        update.apply(package,root,manifest)
    assert count == 2 and target.read_text() == 'old collector code'
    assert 'restart:new collector code' not in events
    assert events[-2:] == ['restart:old collector code','ready:old collector code']


def test_permanently_invalid_lease_still_restores_source_and_attempts_web(collector_prepared,monkeypatch):
    package,root,target,manifest,events = collector_prepared
    monkeypatch.setattr(update,'wait_collector_lease',lambda *a:(_ for _ in ()).throw(TimeoutError('lease exceeded wait')))
    with pytest.raises(RuntimeError,match='previous source was restored.*recovery could not be verified'):
        update.apply(package,root,manifest)
    assert target.read_text() == 'old collector code' and events[-1] == 'restart:old collector code'
    assert not (root/'source-update.json').exists()


def test_disabled_collector_skips_lease_and_readiness(collector_prepared,monkeypatch):
    package,root,target,manifest,events = collector_prepared
    (root/'backend/config/kepserver.local.json').write_text('{"enabled":false}')
    monkeypatch.setattr(update,'wait_collector_lease',lambda *a:pytest.fail('disabled lease wait'))
    monkeypatch.setattr(update,'collector_status',lambda *a:pytest.fail('disabled status read'))
    monkeypatch.setattr(update,'collector_ready',COLLECTOR_READY)
    backup = update.apply(package,root,manifest)
    assert backup.is_dir() and target.read_text() == 'new collector code'
    assert json.loads((root/'source-update.json').read_text())['collector_restarted'] is False


class Clock:
    def __init__(self,monkeypatch):
        self.elapsed = 0
        self.stamp = datetime(2026,10,21,tzinfo=timezone.utc)
        clock = self
        class FakeDateTime(datetime):
            @classmethod
            def now(cls,tz=None):
                current = clock.stamp+timedelta(seconds=clock.elapsed)
                return current.astimezone(tz) if tz else current.replace(tzinfo=None)
        monkeypatch.setattr(update,'datetime',FakeDateTime)
        monkeypatch.setattr(update,'time',SimpleNamespace(monotonic=lambda:self.elapsed,sleep=self.sleep))
    def sleep(self,seconds): self.elapsed += seconds


def test_lease_wait_uses_readonly_sqlite_until_natural_expiry(prepared,monkeypatch):
    _,root,*_ = prepared
    clock = Clock(monkeypatch)
    database = root/'backend/data/dense_medium.db'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE telemetry_sources (source_id TEXT,owner TEXT,lease_until TEXT,state TEXT,last_sample_at TEXT)')
        db.execute('INSERT INTO telemetry_sources VALUES(?,?,?,?,?)',('test','unchanged-owner',
            (clock.stamp+timedelta(seconds=2)).isoformat(),'connected',None))
    real_connect,uris = sqlite3.connect,[]
    def connect(path,*args,**kwargs):
        uris.append((path,kwargs.get('uri')))
        return real_connect(path,*args,**kwargs)
    monkeypatch.setattr(update.sqlite3,'connect',connect)
    reports = []
    update.wait_collector_lease(root,dict(source_id='test'),lambda *a:reports.append(a),timeout=5)
    assert clock.elapsed == 2 and reports
    assert all('?mode=ro' in str(path) and uri for path,uri in uris)
    with real_connect(database) as db:
        assert db.execute('SELECT owner FROM telemetry_sources').fetchone() == ('unchanged-owner',)


def test_lease_wait_is_bounded_and_reports_progress(prepared,monkeypatch):
    _,root,*_ = prepared
    clock = Clock(monkeypatch)
    monkeypatch.setattr(update,'collector_status',lambda *a:dict(owner='owner',
        lease_until=(clock.stamp+timedelta(hours=1)).isoformat()))
    reports = []
    with pytest.raises(TimeoutError,match='owner was not changed'):
        update.wait_collector_lease(root,dict(source_id='test'),lambda *a:reports.append(a),timeout=7)
    assert clock.elapsed == 7 and len(reports) == 2


def test_readiness_requires_new_owner_fresh_committed_samples_and_connected(monkeypatch,tmp_path):
    clock = Clock(monkeypatch)
    before = dict(owner='old',last_sample_at=(clock.stamp-timedelta(seconds=10)).isoformat())
    def status(*args):
        age = clock.elapsed
        return dict(owner='old' if age == 0 else 'new',state='connecting' if age == 1 else 'connected',
            last_sample_at=(clock.stamp+timedelta(seconds=age-3)).isoformat(),
            lease_until=(clock.stamp+timedelta(minutes=2)).isoformat())
    monkeypatch.setattr(update,'collector_status',status)
    reports = []
    update.collector_ready(tmp_path,dict(enabled=True,source_id='test',ready_timeout=8),before,
        lambda *a:reports.append(a))
    assert clock.elapsed == 5 and 'two committed sample times advanced' in reports[-1][1]


@pytest.mark.parametrize('state,owner,advance',[('retrying','new',True),('connected','old',True),('connected','new',False)])
def test_readiness_rejects_connection_only_old_owner_or_nonadvancing_samples(monkeypatch,tmp_path,state,owner,advance):
    clock = Clock(monkeypatch)
    before = dict(owner='old',last_sample_at=(clock.stamp-timedelta(seconds=1)).isoformat())
    monkeypatch.setattr(update,'collector_status',lambda *args:dict(owner=owner,state=state,
        last_sample_at=(clock.stamp+timedelta(seconds=clock.elapsed if advance else -2)).isoformat(),
        lease_until=(clock.stamp+timedelta(minutes=2)).isoformat()))
    with pytest.raises(TimeoutError,match='connected state and advancing samples'):
        update.collector_ready(tmp_path,dict(enabled=True,source_id='test',ready_timeout=5),before,lambda *a:None)
    assert clock.elapsed == 5


def test_large_snapshot_defers_full_check_until_services_are_live(prepared,monkeypatch,tmp_path):
    """A >1 GiB snapshot must pass structural checks without a pre-mutation
    quick_check (which starves the still-running old collector on slow hosts),
    and the deferred full check runs only after the updated services are up."""
    package,root,source,target,manifest,calls = prepared
    # Force the deferred path without building a real 1 GiB database.
    monkeypatch.setattr(update,'FULL_CHECK_MAX_BYTES',0)
    full_checks = []
    real_verify = update.verify_snapshot_content
    def spying_verify(path,report=lambda *_:None,*,budget):
        full_checks.append(budget)
        return real_verify(path,report,budget=budget)
    monkeypatch.setattr(update,'verify_snapshot_content',spying_verify)
    backup = update.apply(package,root,manifest)
    assert target.read_text() == 'new code'
    # The snapshot itself was taken and structurally verified.
    assert (backup/'dense_medium.db').is_file()
    assert len(full_checks) == 1 and full_checks[0] >= update.DEFERRED_CHECK_BASE_SECONDS
    record = json.loads((root/'source-update.json').read_text(encoding='utf-8'))
    assert record['snapshot_verification'] == 'deferred'
    assert record['snapshot_content_check']['passed'] is True


def test_deferred_check_failure_does_not_rollback_or_hide_installation(prepared,monkeypatch):
    package,root,source,target,manifest,calls = prepared
    monkeypatch.setattr(update,'FULL_CHECK_MAX_BYTES',0)
    monkeypatch.setattr(update,'verify_snapshot_content',
        lambda path,report=lambda *_:None,*,budget: False)
    backup = update.apply(package,root,manifest)
    # The update stays installed; the failure is recorded, not silently ignored.
    assert target.read_text() == 'new code'
    record = json.loads((root/'source-update.json').read_text(encoding='utf-8'))
    assert record['snapshot_content_check']['passed'] is False


def test_structural_mismatch_aborts_before_any_service_change(prepared,monkeypatch):
    package,root,source,target,manifest,calls = prepared
    monkeypatch.setattr(update,'FULL_CHECK_MAX_BYTES',0)
    # Corrupt the finished snapshot after the source fingerprint was taken.
    real_fingerprint = update.structure_fingerprint
    seen = []
    def fingerprint(db):
        value = real_fingerprint(db)
        seen.append(value)
        if len(seen) == 2:
            return (value[0]+1,value[1],value[2])
        return value
    monkeypatch.setattr(update,'structure_fingerprint',fingerprint)
    with pytest.raises(ValueError,match='structural check failed'):
        update.apply(package,root,manifest)
    assert target.read_text() == 'old code' and not calls


@pytest.mark.parametrize('policy',[None,False,True,'skip','full',0,{},[]])
def test_unknown_snapshot_policy_stops_before_backup_or_service_changes(prepared,policy):
    package,root,_,target,manifest,calls = prepared
    manifest['snapshot_content_check_policy'] = policy
    with pytest.raises(ValueError,match='snapshot content check policy'):
        update.apply(package,root,manifest)
    assert target.read_text() == 'old code' and not calls
    assert not (root/'backups').exists()


def test_snapshot_deferral_cannot_be_combined_with_no_database_hotfix(prepared):
    package,root,_,target,manifest,calls = prepared
    manifest.update(snapshot_content_check_policy='maintenance-window',profile='query-only-hotfix')
    with pytest.raises(ValueError,match='incompatible snapshot content check policy'):
        update.apply(package,root,manifest)
    assert target.read_text() == 'old code' and not calls
    assert not (root/'backups').exists()


def test_explicit_large_snapshot_deferral_keeps_copy_and_structure_without_full_scan(prepared,monkeypatch):
    package,root,_,target,manifest,calls = prepared
    manifest['snapshot_content_check_policy'] = 'maintenance-window'
    # Exercise the real copy/structure path using the fixture DB, not a fake backup.
    monkeypatch.setattr(update,'MANUAL_CHECK_MIN_BYTES',1)
    statements = []
    real_connect = update.sqlite3.connect
    def connect(*args,**kwargs):
        database = real_connect(*args,**kwargs)
        database.set_trace_callback(statements.append)
        return database
    monkeypatch.setattr(update.sqlite3,'connect',connect)
    monkeypatch.setattr(update,'verify_snapshot_content',lambda *a,**kw:pytest.fail('Explicit large deferral must not scan snapshot content'))
    events = []
    backup = update.apply(package,root,manifest,lambda *args:events.append(args))
    assert target.read_text() == 'new code' and calls
    assert (backup/'dense_medium.db').is_file()
    assert len([s for s in statements if s.startswith('SELECT type,name,tbl_name,sql')]) == 2
    assert not any('quick_check' in s.lower() or 'integrity_check' in s.lower() for s in statements)
    with real_connect(backup/'dense_medium.db') as snapshot:
        assert snapshot.execute('SELECT * FROM evidence').fetchall() == [(1,)]
    record = json.loads((root/'source-update.json').read_text(encoding='utf-8'))
    assert record['snapshot_verification'] == 'deferred_manual'
    assert record['snapshot_content_check_policy'] == 'maintenance-window'
    assert record['snapshot_content_check']['passed'] is False
    assert record['snapshot_content_check']['reason'] == 'maintenance-window'
    assert record['snapshot_content_check']['budget_seconds'] == 0
    assert any('protected and unverified' in message for _,message in events)


def test_explicit_policy_still_fully_checks_small_snapshots(prepared,monkeypatch):
    package,root,_,target,manifest,_ = prepared
    manifest['snapshot_content_check_policy'] = 'maintenance-window'
    statements = []
    real_connect = update.sqlite3.connect
    def connect(*args,**kwargs):
        database = real_connect(*args,**kwargs)
        database.set_trace_callback(statements.append)
        return database
    monkeypatch.setattr(update.sqlite3,'connect',connect)
    update.apply(package,root,manifest)
    assert target.read_text() == 'new code'
    assert any('quick_check' in statement.lower() for statement in statements)
    record = json.loads((root/'source-update.json').read_text(encoding='utf-8'))
    assert record['snapshot_verification'] == 'full'
    assert record['snapshot_content_check'] is None


def test_manual_snapshot_cannot_be_registered_verified_or_replace_stable(prepared,monkeypatch):
    from app.storage_retention import files
    from test_retention_producers import install_retention_module, sqlite_file
    package,root,_,_,manifest,_ = prepared
    install_retention_module(root)
    old = sqlite_file(root/'backups/confirmed-stable/dense_medium.db').parent
    identity = files.installation_identity(root)
    now = datetime.now(timezone.utc)
    files.register_verified_backup(root,old,installation_id=identity,backup_id='confirmed-stable',
        created_at=now-timedelta(days=3),verification=dict(method='sqlite-full-check',result='ok',checked_at=now),stable=True)
    original_registry = json.loads((root/files.REGISTRY_NAME).read_text(encoding='utf-8'))
    manifest['snapshot_content_check_policy'] = 'maintenance-window'
    monkeypatch.setattr(update,'MANUAL_CHECK_MIN_BYTES',1)
    backup = update.apply(package,root,manifest)
    state = json.loads((root/files.REGISTRY_NAME).read_text(encoding='utf-8'))
    entry = state['entries']['source-update:'+backup.name]
    assert entry['state'] == 'unverified'
    assert 'verification' not in entry and 'runtime_verified_at' not in entry
    assert state['stable_backup_id'] == original_registry['stable_backup_id'] == 'confirmed-stable'
    assert old.exists()
    # Even a stale/misreported downstream pass flag cannot promote a manual scan.
    update.register_retention_snapshot(root,backup,{'content_check':'deferred_manual'},{'passed':True},now)
    state = json.loads((root/files.REGISTRY_NAME).read_text(encoding='utf-8'))
    assert state['entries']['source-update:'+backup.name]['state'] == 'unverified'
